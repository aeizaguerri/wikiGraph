-- Successful upstream facts are reusable across isolated Crawl runs.  The
-- complete request identity is explicit so editions, schemas, and API
-- continuation pages cannot collide.
create table if not exists public.upstream_response_cache (
  cache_key text primary key,
  kind text not null check (kind in ('article-links', 'redirects')),
  edition text not null check (length(edition) > 0),
  normalized_request text not null check (length(normalized_request) > 0),
  continuation text not null,
  schema_version text not null check (length(schema_version) > 0),
  response jsonb not null,
  fetched_at timestamptz not null default now(),
  expires_at timestamptz not null,
  last_accessed_at timestamptz not null default now(),
  unique (kind, edition, normalized_request, continuation, schema_version),
  check (expires_at <= fetched_at + interval '7 days')
);

create index if not exists upstream_response_cache_lru_idx
  on public.upstream_response_cache (last_accessed_at);
create index if not exists upstream_response_cache_expiry_idx
  on public.upstream_response_cache (expires_at);

create or replace function public.prune_upstream_response_cache()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  delete from public.upstream_response_cache
  where cache_key in (
    select cache_key
    from public.upstream_response_cache
    where expires_at <= now()
       or cache_key not in (
         select cache_key from public.upstream_response_cache
         order by last_accessed_at desc
         limit 10000
       )
  );
  return new;
end;
$$;

drop trigger if exists upstream_response_cache_prune on public.upstream_response_cache;
create trigger upstream_response_cache_prune
after insert or update on public.upstream_response_cache
for each statement execute function public.prune_upstream_response_cache();
