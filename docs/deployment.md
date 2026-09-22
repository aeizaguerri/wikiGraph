# Production deployment

The production process is a Render web service running both the FastAPI API and
its asyncio crawl workers. `render.yaml` is the deployment contract. It does
not contain credentials; set the `sync: false` values in Render's environment
configuration.

Before deploying, apply every migration under `supabase/migrations/` in order
and verify `/readyz` against the target project. Render uses `/readyz` as its
health check, so an unavailable canonical database prevents the service from
being considered healthy.

The frontend is served by the API by default. It may instead be copied to a
static Cloudflare Pages site, provided its API origin remains the Render URL.
No Supabase service-role value may be present in that static site. Keep-warm
requests are optional latency optimization only; they do not own run state or
recovery.

There is no migration of pre-cutover in-memory runs or old URLs. Rollback means
stop routing traffic to the new Render service and restore the previous
deployment; do not point the new process at an un-migrated database or re-enable
an in-memory run store.
