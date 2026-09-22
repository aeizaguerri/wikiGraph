# Production deployment

The production process is a Render web service running both the FastAPI API and
its asyncio crawl workers. `render.yaml` is the deployment contract. It does
not contain credentials; set the `sync: false` values in Render's environment
configuration.

Before deploying, apply every migration under `supabase/migrations/` in order,
including `20260922000005_runtime_security.sql`, and verify `/readyz` against
the target project. The final migration explicitly grants the Render
`service_role` only the runtime tables and RPCs, revokes those tables/RPCs from
`anon` and `authenticated`, and enables RLS with deny policies for browser
roles. Render uses `/readyz` as its health check, so an unavailable canonical
database or missing runtime RPC prevents the service from being considered
healthy.

Retention cleanup runs immediately at production startup and periodically
every 15 minutes (override with the positive
`WIKIGRAPH_RETENTION_INTERVAL_SECONDS` setting). A failed cleanup is logged and
the next readiness check still probes the canonical database; cleanup never
falls back to process-local state.

The frontend is served by the API by default. It may instead be copied to a
static Cloudflare Pages site, provided its API origin remains the Render URL.
No Supabase service-role value may be present in that static site. Keep-warm
requests are optional latency optimization only; they do not own run state or
recovery.

There is no migration of pre-cutover in-memory runs or old URLs. Rollback means
stop routing traffic to the new Render service and restore the previous
deployment; do not point the new process at an un-migrated database or re-enable
an in-memory run store.
