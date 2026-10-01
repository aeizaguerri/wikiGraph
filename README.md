# wikiGraph

wikiGraph crawls links between Wikipedia articles and presents the result as an interactive, directed graph. It supports Spanish and English editions, bounded breadth-first runs, live progress, and shareable run URLs. This README is the canonical current guide to implemented behavior, architecture, security, local development, and deployment. Source and tests are linked inline so behavior can be checked without treating historical reports as current verification.

## Quick start

Requirements: Python 3.11+, `uv`, Node.js/npm for frontend work. Install the Python project and run the API locally:

```sh
uv sync
uv run uvicorn wikigraph.app:app --reload
```

The local default uses in-memory run storage and is for development; its runs do not survive a process restart. Open the API-served UI at <http://127.0.0.1:8000>. Run the Python suite with `uv run pytest`; build the frontend with `npm --prefix frontend ci && npm --prefix frontend run build`. These are commands, not claims that checks have been run for this documentation change.

## Product behavior

### Launch and input

A run accepts a Wikipedia article title or an HTTP(S) URL on a Wikipedia subdomain. URL edition and the selected `es`/`en` edition must agree. The seed is normalized and resolved through MediaWiki; missing pages and non-article namespaces are rejected. Launch input is `seed`, `language` (`es` or `en`, default `es`), `depth` (1–3, API default 1), and `nodeCap` (1–5000, API default 500). The UI currently initializes depth 2 and cap 500. References: [`seed.py`](src/wikigraph/seed.py), [`app.py`](src/wikigraph/app.py), [`launcher.js`](frontend/src/launcher.js); tests: [`test_ticket02_normalization.py`](tests/test_ticket02_normalization.py), [`test_ticket05_input_surface.py`](tests/test_ticket05_input_surface.py), [`test_ticket27_launch_protection.py`](tests/test_ticket27_launch_protection.py).

### Crawl and graph semantics

The crawler traverses breadth-first, one depth level at a time, and crawls each canonical article once. It follows links in the chosen edition, resolves redirects, and excludes missing pages and non-article namespaces. The graph is directed: an article linking to another creates a source-to-target edge; cycles and reciprocal links are retained. Nodes are unique by canonical title and edges are deduplicated. The node cap bounds nodes; when discovery would exceed it, the graph is marked truncated and edges to absent nodes are omitted. Thus the returned graph has no dangling edges and `discovered` equals its node count. `crawled` counts source articles actually fetched, which may be lower than discovered. References: [`crawler.py`](src/wikigraph/crawler.py), [`mediawiki.py`](src/wikigraph/mediawiki.py); tests: [`test_ticket04_deep_crawl.py`](tests/test_ticket04_deep_crawl.py), [`test_ticket29_node_cap_stops_frontier.py`](tests/test_ticket29_node_cap_stops_frontier.py).

A completed run is partitioned server-side with NetworkX Louvain on the directed graph, fixed seed 0 and resolution 1.0. IDs are normalized by descending community size, then lexicographically by smallest member title; ID 0 is the largest group. Graph payloads include `communityCount` and `modularity`. References: [`communities.py`](src/wikigraph/communities.py), [`runs.py`](src/wikigraph/runs.py); tests: [`test_v2_ticket06_community_detection.py`](tests/test_v2_ticket06_community_detection.py).

### User experience

The launcher accepts text or a pasted Wikipedia URL, edition and depth controls, and a node cap. It locks the form during launch, reports inline errors/progress, and displays crawled versus discovered counts. The graph view is a directed Sigma/Graphology canvas: node size reflects degree; color can encode community or crawl level; hovering previews an article, clicking pins it, and the backdrop clears selection. The initial layout settles, becomes static, and reheats briefly on actual node dragging. Article details link to the selected Wikipedia edition. See [`launcher.js`](frontend/src/launcher.js), [`experience.js`](frontend/src/experience.js), and UI tests [`test_v2_ticket05_view.py`](tests/test_v2_ticket05_view.py) through [`test_v2_ticket12_launcher_completeness.py`](tests/test_v2_ticket12_launcher_completeness.py).

## API and run lifecycle

The API is implemented in [`app.py`](src/wikigraph/app.py); request/response models use camelCase JSON.

| Route | Behavior |
|---|---|
| `POST /api/runs` | Validate seed and admission limits, create a run, return `runId` (201). |
| `GET /api/runs/{runId}` | Read status and progress. |
| `GET /api/runs/{runId}/events` | Server-Sent Events for progress and terminal status. |
| `GET /api/runs/{runId}/preview` | Return checkpoint preview where available, marked incomplete; preview community values are placeholders. |
| `GET /api/runs/{runId}/graph` | Return completed graph; non-completed, recoverable, overload-waiting, failed, or expired runs return appropriate errors. |
| `POST /api/runs/{runId}/retry` | Resume eligible recoverable/overload-waiting run from its checkpoint (202). |
| `POST /api/operator/runs/{runId}/cancel` | Optional operator-only durable cancellation; requires its separate configured bearer token. |
| `GET /healthz`, `GET /readyz` | Process liveness and persistence readiness, respectively. |

The durable production store records progress/checkpoints and uses ownership leases so a retry can resume without publishing partial work as a completed Graph. Completed graphs expire after seven days; non-completed run records have a 24-hour expiry. Cleanup runs at startup and every 15 minutes by default (`WIKIGRAPH_RETENTION_INTERVAL_SECONDS` can override with a positive value). Expiration removes reconstructive graph/checkpoint data while retaining an expired tombstone so a known expired URL is distinguishable from an unknown run. These are source/configured semantics, not evidence of elapsed production retention. References: [`runs.py`](src/wikigraph/runs.py), migrations [`00000`](supabase/migrations/20260922000000_crawl_runs.sql) and [`00004`](supabase/migrations/20260922000004_run_retention.sql); tests: [`test_ticket20_persisted_runs.py`](tests/test_ticket20_persisted_runs.py), [`test_ticket21_frontier_checkpoints.py`](tests/test_ticket21_frontier_checkpoints.py), [`test_ticket26_retention.py`](tests/test_ticket26_retention.py).

## Architecture decisions

These four decisions were consolidated from the former ADR documents; rationale and constraints are retained here.

1. **Graph, not tree.** Wikipedia links contain cycles and shared targets. A tree duplicates the same article beneath multiple parents and obscures cycles; the graph represents each canonical article once and each discovered directed relation as an edge. See [`crawler.py`](src/wikigraph/crawler.py) and [`test_ticket04_deep_crawl.py`](tests/test_ticket04_deep_crawl.py).
2. **Truncation preserves referential integrity.** At the node cap, omit links to nodes that could not be admitted. Keeping dangling edges would let renderers fabricate phantom nodes and make counts misleading. See [`crawler.py`](src/wikigraph/crawler.py) and [`test_ticket29_node_cap_stops_frontier.py`](tests/test_ticket29_node_cap_stops_frontier.py).
3. **Community detection uses directed modularity.** Pass the directed graph unchanged to NetworkX rather than inventing an undirected projection/deduplication convention or adding a heavier alternate dependency. Fixed seed/resolution and normalized IDs make results deterministic for a pinned library version. See [`communities.py`](src/wikigraph/communities.py), [`pyproject.toml`](pyproject.toml), and [`test_v2_ticket06_community_detection.py`](tests/test_v2_ticket06_community_detection.py).
4. **Wikimedia overload handling honors provider backpressure.** HTTP 429/503 and `maxlag` trigger retries with `Retry-After`/lag when supplied, capped at 45 seconds; otherwise bounded jittered exponential delay is used, up to ten attempts. The shared governor applies cooldown to later requests rather than letting each crawl loop bypass a common budget. Exhaustion surfaces an overload-waiting run that can be retried. See [`mediawiki.py`](src/wikigraph/mediawiki.py), [`governor.py`](src/wikigraph/governor.py), and [`test_ticket23_overload_recovery.py`](tests/test_ticket23_overload_recovery.py).

## Security and privacy

- Production startup requires server-only `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `WIKIGRAPH_USER_AGENT`, and `WIKIGRAPH_IP_HASH_SECRET`; startup validates canonical persistence and required runtime schema. Do not place these in browser or Cloudflare configuration. See [`app.py`](src/wikigraph/app.py), [`mediawiki.py`](src/wikigraph/mediawiki.py), and [`render.yaml`](render.yaml).
- Launch abuse limits are enforced by shared atomic Supabase admission in production: defaults are 5 per IP/minute, 30 per deployment/minute, 50 per IP/day, and 10,000 IP keys. The IP identity is HMAC-hashed using the server secret; the app uses the request's client host and does not trust client-supplied forwarding identity headers. Local-only fallback limiting is process-local and is not a distributed production control.
- Wikimedia requests use a configured descriptive User-Agent. A deployment-wide durable governor limits upstream work to at most one in flight, two starts/second and 120 attempts/minute; each new run snapshots a positive attempt quota (default 120). Cache hits do not consume upstream attempts. Raising the quota is an explicit service configuration change, not a reason to weaken the global provider limits. See [`governor.py`](src/wikigraph/governor.py), [`mediawiki.py`](src/wikigraph/mediawiki.py), and migrations [`00001`](supabase/migrations/20260922000001_wikimedia_governor.sql) and [`00008`](supabase/migrations/20260926000008_run_attempt_quota.sql).
- The Cloudflare Worker forwards `/api` and `/api/*` to Render and streams responses/SSE; it is routing, not authentication or origin hiding. There is no CORS-based or origin-based security guarantee. See [`worker.js`](cloudflare/worker.js) and [`worker.test.js`](cloudflare/worker.test.js).
- Operator cancellation is separate from ordinary API access. Configure `WIKIGRAPH_OPERATOR_CANCEL_TOKEN` as a dedicated random secret at least 32 characters, distinct from the IP-hash and Supabase secrets. Send only from a trusted operator environment; never expose it in URLs, browser requests, or frontend config. Without valid configuration the route is unavailable. Cancellation fences later admissions; an already-granted upstream attempt can finish and remains charged. See [`app.py`](src/wikigraph/app.py), [`runs.py`](src/wikigraph/runs.py), and migration [`00010`](supabase/migrations/20260928000010_run_cancellation.sql).
- Runtime migrations revoke browser-role table/RPC access and enable RLS policies; keep those protections when adding migrations. The service-role credential remains a high-privilege server secret. See [`20260922000005_runtime_security.sql`](supabase/migrations/20260922000005_runtime_security.sql).

## Local development and checks

```sh
uv sync
uv run pytest
uv run mypy src
npm --prefix frontend ci
npm --prefix frontend run build
node --test cloudflare/worker.test.js
```

The two tests specific to retired benchmarks have been removed. Product regression tests share `DeterministicClock` from [`tests/helpers.py`](tests/helpers.py); they do not depend on the benchmark package.

Python tests use pytest/pytest-asyncio (configured in [`pyproject.toml`](pyproject.toml)); the package has no configured frontend Node test script, but the Cloudflare Worker has a direct Node test file. Some Supabase/PostgREST integration tests need the repository's local database harness and environment; see the test setup in [`tests/conftest.py`](tests/conftest.py) and [`tests/test_ticket28_postgrest_roles.py`](tests/test_ticket28_postgrest_roles.py). Do not infer that an unrun command passed, or that an isolated unit/integration test proves a hosted deployment property.

## Deployment and operations

### Runtime and database

Render's [`render.yaml`](render.yaml) defines the Python web service (`uv` install/sync, Uvicorn, `/readyz` health check) and default launch/run quotas. Configure each `sync: false` variable in the Render service environment; no secrets belong in the repository. Production uses Supabase as canonical run storage and also for shared launch admission, Wikimedia governor, response cache, checkpointing, retention, and cancellation RPCs.

Before runtime deployment, an authorized operator must apply every migration in [`supabase/migrations/`](supabase/migrations/) in order, including runtime security `20260922000005` and cancellation `20260928000010` if the deployed runtime enables that endpoint. Verify `/readyz` against the intended Supabase project. It checks canonical database readiness; it does **not** prove the optional cancellation RPC works. Run authorized local PostgREST tests for optional RPC capability. Failed migration, readiness, or smoke checks reject cutover; there is no automatic fallback to an in-memory store.

Migration `20260924000006_bound_cache_identity_index.sql` changes the oversized cache uniqueness index. Its recorded migration procedure takes an `ACCESS EXCLUSIVE` table lock while changing the constraint/index. Plan and authorize this operational impact; apply the migration before a runtime relying on it. A production cache-write failure was historically traced to PostgreSQL index row-size limits; do not repeat heavy runs against a schema lacking the fix. See migration [`00006`](supabase/migrations/20260924000006_bound_cache_identity_index.sql) and tests [`test_ticket29_cache_index.py`](tests/test_ticket29_cache_index.py).

Each newly created run snapshots `WIKIGRAPH_RUN_ATTEMPT_QUOTA` (default 120, must be positive). Raising the service setting affects new runs only; an existing retry retains the original snapshot and quota already spent. Do not launch concurrent production runs by default; the governor remains shared and provider-limited.

If enabling operator cancellation, separately generate/store a strong token in Render and the authorized operator secret store. Example trusted-shell request:

```sh
curl --fail-with-body -X POST \
  "https://wikigraph.onrender.com/api/operator/runs/${RUN_ID}/cancel" \
  -H "Authorization: Bearer ${WIKIGRAPH_OPERATOR_CANCEL_TOKEN}"
```

### Frontend

[`wrangler.jsonc`](wrangler.jsonc) deploys the Vite output at `src/wikigraph/static` through `cloudflare/worker.js`; API and SSE requests are proxied to `https://wikigraph.onrender.com`. Build/deploy from repository root:

```sh
npm --prefix frontend ci
npm --prefix frontend run build
npx wrangler whoami
npx wrangler deploy --config wrangler.jsonc
```

Before deployment, verify the selected Cloudflare account and its plan/billing/resource view; `wrangler whoami` alone confirms neither plan nor cost. Do not enable paid features, configure a custom domain, or deploy if account/plan selection is unclear. API/SSE bypass static asset routing/cache. Rollback is a Cloudflare Worker deployment rollback, for example `npx wrangler deployments list --config wrangler.jsonc` followed by an authorized `npx wrangler rollback --config wrangler.jsonc`; this does not roll back Render or Supabase. There is no migration of old in-memory runs or URLs. Never point the new runtime at an unmigrated database or restore old direct database grants as an automatic rollback. Revoke-first ownership migration requires adapter-level launch/progress/completion verification and a cutover/drain plan; equivalent raw REST/RPC tests alone did not establish the old/new application adapters' behavior.

## Verification status and production gaps

**This documentation change has not run behavior tests, a build, a database migration, a hosted smoke test, or a deployment.** Source and test references above identify implemented/tested contracts, not checks executed in this change. Historical ticket-29 reports recorded successful bounded local tests and selected hosted smokes, but also recorded that the automated gate exited 1 and that production acceptance remained incomplete. A past result is not current deployment evidence.

The historical ticket-29 disposition explicitly accepted issue closure while all nine original production criteria remained unproven/incomplete; closure was an owner risk waiver, not nine passes. The historical states and outstanding proof are:

| Production criterion | Historical evidence status; still missing |
|---|---|
| Spanish and English deployed runs | Bounded small-run/browser smoke recorded; full 2,500-article acceptance journeys pending. |
| Reopen and seven-day retention | Immediate URL reopen recorded; seven elapsed days of production retention pending. |
| Render restart/checkpoint recovery | Pending operator-controlled restart and same-run retry receipt. |
| Representative 2,500-article benchmark | Warm and mixed-cache samples recorded; representative, repeatable fully cold hosted run pending. Virtual-clock estimates are not wall-clock measurements. |
| Concurrent global budget and fairness | Local governor evidence recorded; deployed cross-process fairness evidence pending. |
| Injected 429/503/maxlag recovery | Hosted recovery proof pending; any fault exercise must use an isolated approved harness, never live Wikimedia. |
| Supabase failure fail-closed and retention | Readiness probe evidence recorded; controlled availability failure and seven-day retention boundary pending. |
| Higher budget disabled | Pending: historical personal TEST service used a 1,500 attempt override; original provider eligibility, monitor thresholds, and automatic fallback were unproven. |
| Failed check rejects cutover | Complete deployed cutover-gate receipt pending. |

The historical record also lacked required deployed measurements (attempts, continuations, cache hits, retry delays, fairness, persistence effects, CPU and memory) and operator-controlled restart authority. Ownership migration adapter-level end-to-end proof, no-active-run cutover evidence, and a safe rollback procedure were missing. Do not apply a revoke-first migration based only on equivalent raw REST/RPC tests.

Personal TEST receipts, bounded canaries, historical deployments, prior run IDs, and a local benchmark do not close these gaps or authorize a new production run/deployment. The historical report referenced `benchmarks.ticket19_representative` and `benchmarks.ticket29_production_gate`; those benchmark scripts are absent in this working tree, and the two tests specific to those retired benchmarks have been removed. Product regression tests use the shared `DeterministicClock` in [`tests/helpers.py`](tests/helpers.py). See the historical Git version of `docs/ticket-29-production-gate.md` and local issue history for detailed receipts and caveats; these were consolidated here so active operational guidance has one current home, not to erase history.

## Documentation map

This README is the single current project/product reference. `docs/agents/*.md` and `.agents/skills/` remain agent workflow/instruction assets, not competing product documentation. The former ADR, deployment, security and ticket-29 report content is reconciled into the sections above; those seven documents have been retired in favor of this reference. Local `.scratch/` issue history remains unchanged and historical Git references are retained deliberately.
