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
| `scripts/ticket28-postgrest.sh start` plus all ticket env aliases and `uv run pytest -q` | 126 passed, 0 skipped, 4:21 | local Podman/Postgres/PostgREST harness |
| `uv run mypy src` | success, 11 source files | local typecheck |
| `uv run python -m benchmarks.ticket29_production_gate --render-url https://wikigraph.onrender.com` | exit 1, 0 launches, 0 fault injections; all incomplete criteria listed in report | fail-closed collector |

The local benchmark's `simulated_time` is 654.5 seconds. It is not a wall-clock
completion measurement. It has no Render CPU, Render memory, cache-hit,
retry-delay, or Supabase persistence telemetry, so the 2,500-Article benchmark
criterion remains incomplete rather than being promoted to production proof.

## Criterion state

| Criterion | State | Evidence origin / remaining proof |
| --- | --- | --- |
| Spanish and English deployed runs | **bounded smoke proven; full criterion pending** | Direct Render API launches completed: ES `ROdJ3gu4chYcGvqrKBf2oQ9JIOxG00EY`, EN `T6lmeVzpx_2pkQy8qv7DEK9gc2Ma9NMS`; each depth 1/node cap 20, Graph HTTP 200 with 20 nodes, same-ID state/Graph reopens HTTP 200. This is not the required fresh-browser/SSE acceptance receipt. |
| Reopen and seven-day retention | **bounded reopen proven; retention pending** | The two run IDs above reopened immediately through the public API; seven-day elapsed/controlled retention evidence is still absent. |
| Render restart checkpoint recovery | **pending** | Requires an operator-coordinated restart and same-run manual retry receipt. |
| Representative 2,500-Article benchmark | **incomplete** | Local shape and governor limits are proven; deployed timing/upstream/continuation/cache/retry/fairness/CPU/memory/persistence metrics are absent. |
| Concurrent global budget and fairness | **local-only** | Deterministic local governor proves 1 in-flight, 2 starts/second, 120 attempts/minute and alternating owners; cross-process deployed evidence is pending. |
| Injected 429/503/maxlag recovery | **pending** | Must use an isolated harness or approved seam, never real Wikimedia traffic. |
| Supabase failure fail-closed and retention | **live-probe-only** | `/readyz` was healthy; controlled availability failure and seven-day boundary evidence are pending. |
| Higher budget disabled | **pending** | Must record deployed configuration/eligibility evidence. No higher budget was enabled by this ticket. |
| Failed check rejects cutover | **pending** | Requires the complete deployed cutover gate receipt. |

## Explicit blockers and rollback posture

The public Render API is reachable for bounded smoke runs, but Render API/CLI
deployment access and telemetry are unavailable. The precise missing access is:

1. the deployed Render service revision/runtime identifier and permission to
   inspect its worker logs/metrics;
2. production request-attempt, continuation, cache-hit, retry-delay, fairness,
   CPU, memory, and persistence-effect counters for a controlled 2,500-Article
   run; and
3. an operator-controlled Render restart during a run, followed by manual retry
   of the same run ID.

No blind production 2,500-Article load, restart, fault injection, or claim of
Render CPU/memory was made. A maintainer with those exact permissions must
complete the pending rows and attach timestamps, run IDs, runtime version, and
rollback decision. A failed schema, readiness, smoke, edition, reopen, or
restart check rejects cutover; there is no automatic fallback to the old
backend.
