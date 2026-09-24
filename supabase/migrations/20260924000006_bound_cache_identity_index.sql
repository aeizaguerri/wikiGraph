-- The raw identity columns remain the authoritative metadata for cache rows,
-- but their combined btree key can exceed PostgreSQL's per-entry limit for
-- legitimate batches of fifty high-entropy titles.  cache_key is the bounded
-- SHA-256 identity key already supplied by the runtime and remains the
-- conflict target used by PostgREST.
alter table public.upstream_response_cache
  drop constraint if exists upstream_response_cache_kind_edition_normalized_request_con_key;

-- Keep equality uniqueness for the five raw identity fields without placing
-- their unbounded text values in one btree entry.  Length framing makes the
-- serialized identity unambiguous; an MD5 collision can only reject a write,
-- never make two different facts compare equal or overwrite one another.
create unique index upstream_response_cache_identity_idx
  on public.upstream_response_cache (
    md5(
      length(kind)::text || ':' || kind ||
      length(edition)::text || ':' || edition ||
      length(normalized_request)::text || ':' || normalized_request ||
      length(continuation)::text || ':' || continuation ||
      length(schema_version)::text || ':' || schema_version
    )
  );

-- A SHA-256 collision must never silently replace a fact belonging to another
-- raw identity.  This guard runs for the UPDATE half of an upsert whose
-- cache_key already exists and rejects an identity mismatch.
create or replace function public.reject_cache_key_identity_collision()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_op = 'UPDATE' and (
    old.kind is distinct from new.kind
    or old.edition is distinct from new.edition
    or old.normalized_request is distinct from new.normalized_request
    or old.continuation is distinct from new.continuation
    or old.schema_version is distinct from new.schema_version
  ) then
    raise exception 'cache_key identity mismatch' using errcode = '23514';
  end if;
  return new;
end;
$$;

drop trigger if exists upstream_response_cache_identity_guard
  on public.upstream_response_cache;
create trigger upstream_response_cache_identity_guard
before insert or update on public.upstream_response_cache
for each row execute function public.reject_cache_key_identity_collision();
