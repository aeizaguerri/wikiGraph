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
Supabase, inject provider faults, or measure Render CPU/memory.

## Evidence captured on 2026-09-23

| Command | Result | Origin |
| --- | --- | --- |
| `uv run pytest tests/test_ticket29_production_gate.py -q` | 3 passed | local test |
| `uv run python -m benchmarks.ticket19_representative` | 2,500 nodes, 7,579 edges, 1,521 crawled, 32 initial link batches, 1,136 continuation requests, 136 redirect requests, max batch 50, max in-flight 1, max starts/second 2, max attempts/minute 120 | local deterministic transport |
| `GET https://wikigraph.onrender.com/healthz` | HTTP 200, `{"status":"ok"}` | deployed public endpoint |
| `GET https://wikigraph.onrender.com/readyz` | HTTP 200, `{"status":"ready","persistence":"supabase"}` | deployed public endpoint |

The local benchmark's `simulated_time` is 654.5 seconds. It is not a wall-clock
completion measurement. It has no Render CPU, Render memory, cache-hit,
retry-delay, or Supabase persistence telemetry, so the 2,500-Article benchmark
criterion remains incomplete rather than being promoted to production proof.

## Criterion state

| Criterion | State | Evidence origin / remaining proof |
| --- | --- | --- |
| Spanish and English deployed runs | **pending** | Requires fresh-browser launch, SSE, completion, and run IDs. Prior ticket 28/30 runs are historical acceptance evidence only. |
| Reopen and seven-day retention | **pending** | Requires both completed run IDs and controlled retention evidence; health does not prove retention. |
| Render restart checkpoint recovery | **pending** | Requires an operator-coordinated restart and same-run manual retry receipt. |
| Representative 2,500-Article benchmark | **incomplete** | Local shape and governor limits are proven; deployed timing/upstream/continuation/cache/retry/fairness/CPU/memory/persistence metrics are absent. |
| Concurrent global budget and fairness | **local-only** | Deterministic local governor proves 1 in-flight, 2 starts/second, 120 attempts/minute and alternating owners; cross-process deployed evidence is pending. |
| Injected 429/503/maxlag recovery | **pending** | Must use an isolated harness or approved seam, never real Wikimedia traffic. |
| Supabase failure fail-closed and retention | **live-probe-only** | `/readyz` was healthy; controlled availability failure and seven-day boundary evidence are pending. |
| Higher budget disabled | **pending** | Must record deployed configuration/eligibility evidence. No higher budget was enabled by this ticket. |
| Failed check rejects cutover | **pending** | Requires the complete deployed cutover gate receipt. |

## Explicit blockers and rollback posture

The public Render health endpoints are reachable, but Render API/CLI access and
the deployed revision/telemetry are unavailable to this ticket. Therefore no
blind production 2,500-Article load, restart, fault injection, or claim of
Render CPU/memory was made. A maintainer with Render access must complete the
pending rows and attach timestamps, run IDs, request/cache/retry/fairness
metrics, persistence effects, runtime version, and rollback decision. A failed
schema, readiness, smoke, edition, reopen, or restart check rejects cutover;
there is no automatic fallback to the old backend.
