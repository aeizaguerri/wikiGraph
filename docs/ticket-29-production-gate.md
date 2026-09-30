# Ticket 29 production reliability gate

This gate is fail-closed. The collector never launches a production Crawl and
never sends synthetic `429`, `503`, or `maxlag` responses to Wikimedia. It keeps
deployed evidence, prior acceptance evidence, and deterministic local evidence
separate.

## Repeatable commands

```bash
uv run pytest tests/test_ticket29_production_gate.py -q
uv run python -m benchmarks.ticket29_production_gate \
  --render-url https://wikigraph.onrender.com
```

The second command performs only the bounded ticket 19 deterministic fixture and
GET requests to `/healthz` and `/readyz`. It does not launch a run, mutate
Supabase, inject provider faults, or measure Render CPU/memory. It exits `1`
until every production criterion is explicitly `proven`; it still writes the
report so missing evidence is reviewable.

## Evidence captured on 2026-09-23

| Command | Result | Origin |
| --- | --- | --- |
| `uv run pytest tests/test_ticket29_production_gate.py -q` | 4 passed | local test |
| `uv run python -m benchmarks.ticket19_representative` | 2,500 nodes, 7,579 edges, 1,521 crawled, 32 initial link batches, 1,136 continuation requests, 136 redirect requests, max batch 50, max in-flight 1, max starts/second 2, max attempts/minute 120 | local deterministic transport |
| `GET https://wikigraph.onrender.com/healthz` | HTTP 200, `{"status":"ok"}` | deployed public endpoint |
| `GET https://wikigraph.onrender.com/readyz` | HTTP 200, `{"status":"ready","persistence":"supabase"}` | deployed public endpoint |
| `npm --prefix frontend ci && npm --prefix frontend run build` | dependencies installed; Vite build passed (`vite v8.3.0`) | local frontend build |
| `node --test` from `frontend/` | 0 tests discovered; no Node proxy test suite is configured in this branch | local tooling |
| `scripts/ticket28-postgrest.sh start` plus all ticket env aliases and `uv run pytest -q` | 128 passed, 0 skipped, 4:21 | local Podman/Postgres/PostgREST harness |
| `uv run mypy src` | success, 11 source files | local typecheck |
| `uv run python -m benchmarks.ticket29_production_gate --render-url https://wikigraph.onrender.com` | exit 1, 0 launches, 0 fault injections; all incomplete criteria listed in report | fail-closed collector |
| `node --test cloudflare/worker.test.js` | 2 passed, 0 skipped | local Cloudflare proxy tests |
| `uv run pytest -q` after ticket-29 observability changes | 128 passed, 0 skipped, 4:21 | local Podman/Postgres/PostgREST harness |
| `uv run pytest tests/test_ticket29_cache_index.py -q` | 1 passed | local role-shaped PostgREST harness with migration `20260924000006` |
| `uv run pytest -q` after the cache-index migration and logger handler fix | 129 passed, 0 skipped, 4:21 | local Podman/Postgres/PostgREST harness |
| `uv run mypy src` after the cache-index migration and logger handler fix | success, 12 source files | local typecheck |
| Uvicorn probe invoking `emit("cache_write_probe", status_code=500)` | HTTP 200; JSON INFO event emitted to stderr | local production-shaped ASGI server |

The local benchmark's `simulated_time` is 654.5 seconds. It is not a wall-clock
completion measurement. It has no Render CPU, Render memory, cache-hit,
retry-delay, or Supabase persistence telemetry.

## Performance objective and bounded evidence

The accepted objective is approximately two minutes for a representative
2,500-node run with a warm cache. One bounded warm-cache sample reached a
2,500-node cap, with 5,033 edges and 51 source Articles crawled, in 99.223
seconds; it met the approximate 120-second objective in that sample. This does
not establish repeatability or generalize to representative workloads. Cold
runs may take longer, but their duration must be measured and communicated
explicitly rather than inferred from a warm-cache result. The current
deterministic cold fixture requires 1,304 upstream attempts, implying at least
652 seconds at the unchanged two-starts-per-second limit, before network and
persistence overhead; this is a theoretical lower bound, not a wall-clock or
deployed cold-run proof. The sample also provides no cold-performance or
deployed fairness proof.

Every warm and cold run remains subject to the global Wikimedia governor:
maximum 1 in-flight request, 2 request starts per second, and 120 attempts per
minute. The warm-cache objective does not authorize relaxing any of these
limits.

## Bounded hosted cache-write canary — 2026-09-26

At 2026-09-26 21:07:15Z, the deployed Render revision returned `/readyz`
HTTP 200; a separate Supabase migration listing confirmed versions `00000`–
`00007`. A read-only run/cache inventory found no active run and no fresh
`en` article-links cache entry for Aric Hagberg. One direct POST launched
a depth-1, node-cap-2 English run (`<run-id>`),
returning HTTP 201. SSE reported `progress` and `completed`; Graph returned
HTTP 200 with 2 nodes, 1 edge, 1 crawled, truncated true, and 0 dangling edges.
The application completion event recorded 2.798s. The bounded 31-entry log
sample contained 3 upstream attempts and an article-links `hit:false`. One new
777-byte article-links row
was fetched at 21:07:17.897773Z and expires October 3. There was no repeat or
cleanup. This establishes one deployed miss/write path only; it is not a
2,500-node benchmark, deployed fairness, retention, or recovery proof.

## Personal hosted TEST receipts — bounded, not acceptance

These personal TEST receipts used service-local
`WIKIGRAPH_RUN_ATTEMPT_QUOTA=1500` on a pinned same-SHA Render deployment
(`<deployment-id>`) at `64fd1fe`. The override is service-local: the
repository `render.yaml`, app, and SQL default remain 120; the global Wikimedia
governor remains 1 in-flight request, 2 starts/second, and 120 attempts/minute.
These bounded runs do not close any of the nine criteria below.

| Receipt | Observed hosted result |
| --- | --- |
| EN warm, Argentina, depth 3, cap 2,500 (`<run-id>`) | Completed in 93.067488s; durable quota 1,500, `attempts_used=1`; Graph had 2,500 unique nodes, 5,033 unique directed edges, and zero dangling edges; 381 cache lookups = 380 hits / 1 miss. The delegated warm-Graph verifier mistakenly made two read-only GETs despite a one-GET budget, after first trying nonexistent `node.id`. Disclose this process deviation; the observed Graph was structurally valid. |
| ES mixed-cache, Argentina, depth 3, cap 2,500 (`<run-id>`) | Preflight found zero active runs; Live and `/readyz` HTTP 200. Completed in 142.348903s; durable quota 1,500, `attempts_used=160`, 160 admitted IDs; 160 misses + 52 hits; 160 HTTP 200 Wikimedia attempts, including 18 continuations, with no observed overload or retry delay. Graph had 2,500 unique nodes, 4,881 unique directed edges, no dangling edges, and was truncated at the node cap (`truncated=true`); exactly one schema-specific Graph GET returned HTTP 200. Process CPU: user 5.299996→8.176975s, system 0.733313→1.114141s; max RSS 102,281,216 bytes. |
| Sacrificial TEST run (`<run-id>`) | Deliberately aged through guarded atomic SQL, then expired once: Graph and checkpoint removed, EN expired metric 0→1, state and Graph GET returned 410, and all three canaries remained protected. This proves simulated cleanup only, not seven real days. |

The local deterministic fixture's 1,304 virtual-clock attempts / 654.5 simulated
seconds for 2,500 nodes is not a hosted cold run. There is no representative
fully cold benchmark, deployed fairness, restart, injected 429/503/maxlag,
outage, or cutover proof. The TEST override also means original provider
eligibility, monitor thresholds, and automatic fallback remain unproven.

## Ticket 29 closure disposition — owner waiver

The owner explicitly closed the local ticket by accepting the risk that all nine
original production criteria remain unproven. The issue's resolved status records
that decision; it is not nine PASS results. The personal TEST quota of 1,500
remains enabled and service-local. Original provider eligibility and automatic
fallback are unproven; there is no fully cold hosted run or seven elapsed days.
The 1,304-attempt local fixture uses a simulated clock. The ticket-29 automated
gate remains fail-closed and should still exit 1. The committed TEST receipt is
in feature merge commit `7f7f1c6`; this separate closure note records the later
owner waiver. That feature merge does not authorize main, release, or cutover.

## Criterion state

| Criterion | State | Evidence origin / remaining proof |
| --- | --- | --- |
| Spanish and English deployed runs | **bounded fresh-browser/SSE smoke proven; full criterion pending** | Cloudflare browser launches (run IDs omitted), both depth 1/node cap 20; each received `/events` HTTP 200, reached Ready with 1 crawled/20 discovered, Graph HTTP 200, and reopening the same run URL returned Ready. This is not the required 2,500 acceptance run. |
| Reopen and seven-day retention | **bounded browser reopen proven; retention pending** | Both browser run URLs reopened immediately through Cloudflare; their concrete URLs and run IDs are omitted. The sacrificial TEST cleanup receipt above proves simulated expiry/removal only; seven-day elapsed/controlled retention evidence is still absent. |
| Render restart checkpoint recovery | **pending** | Requires an operator-coordinated restart and same-run manual retry receipt. |
| Representative 2,500-Article benchmark | **incomplete** | The personal hosted TEST receipts above include a warm EN run at 93.067488s and mixed-cache ES run at 142.348903s; neither is a representative fully cold benchmark or proves repeatability/generalization. The 1,304-attempt local virtual-clock fixture gives 654.5 simulated seconds, not hosted cold performance. Deployed fairness, restart, fault recovery, outage, and cutover proof remain absent. |
| Concurrent global budget and fairness | **local-only** | The deterministic local governor tests prove 1 in-flight, 2 starts/second, 120 attempts/minute and alternating owners. Ticket-branch commit `8c1ed5f` also passed a real two-client PostgREST governor test; the full role-shaped suite passed 171 tests, 0 skipped. These are local proofs, not deployed fairness evidence; cross-process deployed evidence is pending. |
| Injected 429/503/maxlag recovery | **pending** | Must use an isolated harness or approved seam, never real Wikimedia traffic. |
| Supabase failure fail-closed and retention | **live-probe-only** | `/readyz` was healthy; controlled availability failure and seven-day boundary evidence are pending. |
| Higher budget disabled | **pending** | A service-local TEST override of 1,500 was active for the hosted receipts above; repository `render.yaml`, app, and SQL defaults remain 120. Original provider eligibility, monitor thresholds, and automatic fallback are not proven. |
| Failed check rejects cutover | **pending** | Requires the complete deployed cutover gate receipt. |

## Explicit blockers and rollback posture

## Migration 00007 overlap assessment

**Decision: STOP; the revoke-first sequence is not yet accepted as a safe
cutover.** The local PostgREST ownership integration suite passed against the
isolated ticket28 harness with migration `20260925000007_crawl_run_ownership`
already applied:

```text
uv run pytest tests/test_ticket29_ownership_integration.py -q
12 passed in 2.08s
```

That suite proves raw service-role `POST`/`PATCH` to `crawl_runs` are denied,
and proves the new ownership RPCs can create, heartbeat, and transition runs.
Source inspection of the deployed commit `831644e9b2de0e618ab12b82387f595e395574ff`
shows its store starts via direct table `POST` and persists progress,
checkpoints, and terminal state via direct table `PATCH`; therefore those old
write attempts should fail after the revoke. Its `expire_crawl_runs` RPC is a
pre-existing `SECURITY DEFINER` function, but it only expires rows whose
`expires_at <= p_now`; the old runtime supplies its current clock and this is
not a general write path for active runs. Admission and Wikimedia governor RPCs
do not write `crawl_runs`.

This is **not a PASS**: the integration suite exercises equivalent raw REST
requests and RPCs, not the old binary's real HTTP launch/worker path or the new
binary's actual adapter end-to-end. It consequently does not establish that an
old launch cannot return HTTP 201, how that handler reports its later write
failure, or that new binary start/progress/complete/heartbeat all work through
the application adapter. Nor does it establish that no runs are active at
cutover. Do not apply the migration to production on this evidence alone.

If these adapter-level local proofs pass, revoking direct grants before
deploying the new SHA can be a data-integrity fence without proving old Render
processes have drained: the legacy worker's direct writes are denied, while the
new worker's allowlisted owner RPCs remain available. This does not make the
transition zero-downtime: the old launch handler may accept a request before
its asynchronous direct insert fails, and the new app may be unavailable until
deployment completes. Block user ingress for UX if practical, but direct
Render-origin access remains possible unless separately restricted. Do not
restore old grants as an automatic rollback: first drain the new workers and
resolve any owner-token rows. No production database, Render deployment, or
hosted migration was touched for this assessment.

The public Render API and read-only Render CLI are reachable for bounded smoke
runs, but required deployment metrics and restart authority are unavailable. The
precise missing access is:

1. the deployed Render service revision/runtime identifier and permission to
   inspect its worker logs/metrics;
2. production request-attempt, continuation, cache-hit, retry-delay, fairness,
   CPU, memory, and persistence-effect counters for a controlled 2,500-Article
   run; and
3. an operator-controlled Render restart during a run, followed by manual retry
   of the same run ID.

Render CLI verification at `2026-09-23T21:42:10Z` identified the service
`<service-id>` at the public URL `https://wikigraph.onrender.com`, one Free-plan
Oregon web instance, and live deployment `<deployment-id>` at commit
`76a9aa2286a28912e36840bfbefaeceadaa920b1`. `render logs` returned only
Uvicorn access records for the smoke window: it contains HTTP paths/statuses but
no upstream-attempt, continuation, cache-hit, retry-delay, fairness,
persistence, CPU, or memory measurements. `render services`/`render logs` have
no metrics command; Render's documented CPU/memory metrics are Dashboard/API
observability, not exposed by this CLI invocation.

To close that instrumentation gap, this branch adds secret-free structured
events for Wikimedia attempts, cache lookups, completion timing/process usage,
and persistence writes in commit
`ec7dc3d517bb87a1f76da6a360bc6e8096840d3d`. The deployed revision did not
emit them because its logger level remained above INFO. Commit
`534edc7f72574e4ed7416113531aa150070e2a95` fixes that and also emits a
secret-free cache-write failure event. Neither commit has been deployed by this
agent.
The deployed service is still the earlier commit above, so this is local code
evidence only. It requires an explicitly authorized Render deployment of that
commit (and no deployment was performed here) before a production benchmark can
consume the new events. Render CPU/memory still requires the service Metrics
view/API after deployment.

## Actual controlled benchmark attempt

At `2026-09-24T18:55:13.661279Z`, after confirming no active `/api/runs` work in
the Render log window, one cold English run was launched directly against the
deployed Render API:

```text
POST /api/runs {seed: Argentina, language: en, depth: 3, node_cap: 2500}
run_id: <run-id>
SSE: HTTP 200
terminal: 2026-09-24T19:07:33.412806Z, duration 739.751527 seconds
progress: crawled 1/discovered 1 -> 1/1594 -> 51/1594 -> 51/2500 -> 101/2500
terminal state: failed, crawled 101, discovered 2500, current depth 2
Graph: HTTP 409, no completed Graph
failure: POST /rest/v1/upstream_response_cache?on_conflict=cache_key returned HTTP 500
```

This is an actual failed production attempt, not a benchmark pass. No request,
continuation, cache-hit, retry-delay, fairness, process CPU/RSS, Render CPU/
memory, or persistence-effect metrics were recorded because the deployed
structured logger emitted no events. The user-provided hosted Supabase log export
confirmed the exact cause:

```text
2026-09-24T19:07:30.594Z SQLSTATE 54000
index row size 2824 exceeds btree version 4 maximum 2704 for index
upstream_response_cache_kind_edition_normalized_request_con_key
```

The corresponding edge record was the cache POST at `19:07:30.504Z`. A local
real-Postgres/PostgREST role-shaped reproduction with 50 high-entropy legitimate
titles produced the same HTTP 500 and PostgreSQL `54000`; after migration
`20260924000006_bound_cache_identity_index.sql`, the same upsert, repeat upsert,
different-continuation read, and collision guard passed. The raw identity columns
remain stored for inspection and freshness/access constraints remain unchanged.
The bounded `cache_key` primary key is the PostgREST conflict target; its
before-write guard rejects a digest identity mismatch rather than silently
overwriting a fact if a SHA-256 collision were ever presented.

The migration drops only the oversized compound unique constraint and replaces
it with a length-framed, bounded MD5 identity index; it does not delete cached
facts. A digest collision can reject a write but cannot silently merge distinct
facts. It takes PostgreSQL's `ACCESS EXCLUSIVE` table lock while the constraint
and index are changed; the migration is transactional and rolls back on failure.
Deploy the migration before deploying the runtime that relies on it:

```text
supabase db push --project-ref <project-ref>   # authorized operator only
then deploy the runtime commit and verify INFO cache telemetry
```

Do not repeat another heavy run until that migration and the logger/telemetry fix
are deployed and the resulting cache-write events are observed.

No blind production 2,500-Article load, restart, fault injection, or claim of
Render CPU/memory was made. A maintainer with those exact permissions must
complete the pending rows and attach timestamps, run IDs, runtime version, and
rollback decision in the authorized operational record; do not copy personal
hosted run identifiers into this shared gate document. A failed schema,
readiness, smoke, edition, reopen, or restart check rejects cutover; there is no
automatic fallback to the old backend.
