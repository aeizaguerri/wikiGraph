# Public launch security boundary

The browser may submit and observe Crawl runs, but it never receives Supabase
credentials or Wikimedia identity configuration. `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, and `WIKIGRAPH_USER_AGENT` are server-only
settings; production startup fails if any is absent. Every Wikimedia request
uses the configured descriptive User-Agent.

Launch admission is bounded independently of upstream request accounting: the
API applies per-IP and deployment-wide rolling-minute limits plus a per-IP
daily quota. These limits govern accepted Crawl creation, not individual
MediaWiki requests, and use a stable JSON error contract.

The service does not use CORS, Cloudflare origin hiding, or any origin
indirection as authentication. The public API must be protected by its
deployment and launch limits; an origin is not an identity boundary.
