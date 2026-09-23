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

The local benchmark's `simulated_time` is 654.5 seconds. It is not a wall-clock
completion measurement. It has no Render CPU, Render memory, cache-hit,
retry-delay, or Supabase persistence telemetry, so the 2,500-Article benchmark
criterion remains incomplete rather than being promoted to production proof.

## Criterion state

| Criterion | State | Evidence origin / remaining proof |
| --- | --- | --- |
| Spanish and English deployed runs | **bounded fresh-browser/SSE smoke proven; full criterion pending** | Cloudflare browser launch ES `NKDKCoZB-w8tbAE_5hLy45MCVgjg2vdl` and EN `x2RzjHZVZ0FRKG4swOK3SC6D9oY38s7P`, both depth 1/node cap 20; each received `/events` HTTP 200, reached Ready with 1 crawled/20 discovered, Graph HTTP 200, and same-URL reload reopened Ready. This is not the required 2,500 acceptance run. |
| Reopen and seven-day retention | **bounded browser reopen proven; retention pending** | The two browser run URLs reopened immediately through Cloudflare; seven-day elapsed/controlled retention evidence is still absent. |
| Render restart checkpoint recovery | **pending** | Requires an operator-coordinated restart and same-run manual retry receipt. |
| Representative 2,500-Article benchmark | **incomplete** | Local shape and governor limits are proven; deployed timing/upstream/continuation/cache/retry/fairness/CPU/memory/persistence metrics are absent. |
| Concurrent global budget and fairness | **local-only** | Deterministic local governor proves 1 in-flight, 2 starts/second, 120 attempts/minute and alternating owners; cross-process deployed evidence is pending. |
| Injected 429/503/maxlag recovery | **pending** | Must use an isolated harness or approved seam, never real Wikimedia traffic. |
| Supabase failure fail-closed and retention | **live-probe-only** | `/readyz` was healthy; controlled availability failure and seven-day boundary evidence are pending. |
| Higher budget disabled | **pending** | Must record deployed configuration/eligibility evidence. No higher budget was enabled by this ticket. |
| Failed check rejects cutover | **pending** | Requires the complete deployed cutover gate receipt. |

## Explicit blockers and rollback posture

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

Render CLI verification at `2026-09-23T21:42:10Z` identified service
`srv-dapg020ae00c73d03pbg`, URL `https://wikigraph.onrender.com`, one Free-plan
Oregon web instance, and live deployment `dep-dapg02gae00c73d03r8g` at commit
`76a9aa2286a28912e36840bfbefaeceadaa920b1`. `render logs` returned only
Uvicorn access records for the smoke window: it contains HTTP paths/statuses but
no upstream-attempt, continuation, cache-hit, retry-delay, fairness,
persistence, CPU, or memory measurements. `render services`/`render logs` have
no metrics command; Render's documented CPU/memory metrics are Dashboard/API
observability, not exposed by this CLI invocation.

To close that instrumentation gap, this branch adds secret-free structured
events for Wikimedia attempts, cache lookups, completion timing/process usage,
and persistence writes in commit
`ec7dc3d517bb87a1f76da6a360bc6e8096840d3d`. The
The deployed service is still the earlier commit above, so this is local code
evidence only. It requires an explicitly authorized Render deployment of that
commit (and no deployment was performed here) before a production benchmark can
consume the new events. Render CPU/memory still requires the service Metrics
view/API after deployment.

No blind production 2,500-Article load, restart, fault injection, or claim of
Render CPU/memory was made. A maintainer with those exact permissions must
complete the pending rows and attach timestamps, run IDs, runtime version, and
rollback decision. A failed schema, readiness, smoke, edition, reopen, or
restart check rejects cutover; there is no automatic fallback to the old
backend.
