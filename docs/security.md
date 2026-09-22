# Public launch security boundary

The browser may submit and observe Crawl runs, but it never receives Supabase
credentials or Wikimedia identity configuration. `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `WIKIGRAPH_USER_AGENT`, and
`WIKIGRAPH_IP_HASH_SECRET` are server-only settings; production startup fails
if any is absent. Every Wikimedia request uses the configured descriptive
User-Agent.

Launch admission is bounded independently of upstream request accounting by an
atomic Supabase/Postgres admission function shared by every API process: the
API applies per-IP and deployment-wide rolling-minute limits, a per-IP daily
quota, and a maximum admission-row storage quota. These limits govern accepted
Crawl creation, not individual MediaWiki requests, and use a stable JSON error
contract. The API hashes `Request.client.host` with the server-only HMAC secret
and never trusts `X-Forwarded-For` or another client-supplied identity header.

The service does not use CORS, Cloudflare origin hiding, or any origin
indirection as authentication. The public API must be protected by its
deployment and launch limits; an origin is not an identity boundary.
