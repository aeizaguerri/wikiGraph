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

## Cloudflare frontend

The production frontend is a Cloudflare Workers static-assets deployment. The
committed `wrangler.jsonc` binds the Vite output at
`src/wikigraph/static` and runs `cloudflare/worker.js` first. Requests to
`/api` and `/api/*` are forwarded to `https://wikigraph.onrender.com` with the
original method, query, body, request headers, response status, and streaming
body. This same-origin route means the browser does not need CORS, and the
Worker contains no Supabase or other application secret.

Build and publish from the repository root:

```sh
npm --prefix frontend ci
npm --prefix frontend run build
npx wrangler deploy --config wrangler.jsonc
```

Vite's generated hashed files are immutable for one year via `frontend/public/_headers`;
`index.html` is explicitly `no-cache`, so a fresh launch or shared `?run=...`
URL gets the current shell. Wrangler's `single-page-application` fallback
serves that shell for direct route reloads. API and SSE responses are never
served from the asset cache.

Before publishing, run `npx wrangler whoami` and verify that the selected
account is the intended free Workers account. Record the account/project and
the dashboard billing/resource view before and after deployment. Do not enable
paid features, add a custom domain, or deploy if authentication or account
selection is unavailable. This checkout currently has no authenticated
Wrangler session, so no production URL or free-tier publication is claimed.

Rollback is a Workers deployment rollback, for example:

```sh
npx wrangler deployments list --config wrangler.jsonc
npx wrangler rollback --config wrangler.jsonc
```

Rollback changes only the frontend Worker version and asset routing. It does
not touch Supabase run data, the Render runtime, or in-flight runs. If the
previous version is not available, restore the previous deployment from the
Cloudflare dashboard before changing any Render or database setting.

There is no migration of pre-cutover in-memory runs or old URLs. Rollback means
stop routing traffic to the new Render service and restore the previous
deployment; do not point the new process at an un-migrated database or re-enable
an in-memory run store.
