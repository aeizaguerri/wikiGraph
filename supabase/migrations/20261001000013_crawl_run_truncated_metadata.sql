begin;

-- Keep the SSE completion bit available to narrow state reads without loading
-- the completed Graph. PostgreSQL backfills this STORED column on migration.
drop function public.read_crawl_run_snapshot(text, text);

alter table public.crawl_runs
  add column truncated boolean generated always as (
    case when jsonb_typeof(graph -> 'truncated') = 'boolean'
      then (graph ->> 'truncated')::boolean
      else null::boolean
    end
  ) stored;

create function public.read_crawl_run_snapshot(
  p_run_id text, p_view text
)
returns table (
  run_id text,
  seed text,
  language text,
  depth smallint,
  node_cap integer,
  status text,
  progress jsonb,
  error text,
  retry_state jsonb,
  expires_at timestamptz,
  owner_token uuid,
  owner_version bigint,
  lease_until timestamptz,
  checkpoint jsonb,
  graph jsonb,
  truncated boolean
)
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  current_status text;
  current_expiry timestamptz;
  current_lease timestamptz;
  current_version bigint;
  current_owner uuid;
  now_at timestamptz;
begin
  if p_view is null or p_view not in ('state', 'preview', 'graph') then
    raise exception 'invalid run snapshot view' using errcode = '22023';
  end if;

  -- This narrow MVCC probe never locks an ordinary healthy row and never
  -- fetches either reconstructive JSONB column.
  select c.status, c.expires_at, c.lease_until
    into current_status, current_expiry, current_lease
    from public.crawl_runs c where c.run_id = p_run_id;
  if not found then return; end if;
  now_at := clock_timestamp();

  -- Only a potentially stale target reaches FOR UPDATE. Re-read current
  -- ownership after waiting, then fence the recovery against renewal, claim,
  -- completion, or expiry. No global reconciliation is performed here.
  if current_expiry > now_at and current_status = 'running'
     and current_lease is not null and current_lease <= now_at then
    select c.status, c.expires_at, c.lease_until, c.owner_version, c.owner_token
      into current_status, current_expiry, current_lease, current_version, current_owner
      from public.crawl_runs c where c.run_id = p_run_id for update;
    if not found then return; end if;
    now_at := clock_timestamp();
    if current_expiry > now_at and current_status = 'running'
       and current_lease is not null and current_lease <= now_at then
      update public.crawl_runs c
        set status = 'recoverable', owner_token = null,
            owner_version = c.owner_version + 1, lease_until = null,
            error = coalesce(c.error, 'Run ownership lease expired')
        where c.run_id = p_run_id and c.status = 'running'
          and c.owner_token is not distinct from current_owner
          and c.owner_version = current_version
          and c.lease_until is not distinct from current_lease
          and c.lease_until <= clock_timestamp()
          and c.expires_at > clock_timestamp();
    end if;
  end if;

  -- Each projection evaluates expiry against one fresh PostgreSQL clock
  -- value so the status and payload cannot disagree at the boundary.
  now_at := clock_timestamp();

  -- State never reads checkpoint or graph from the table; its scalar comes
  -- from the persisted generated column and is hidden outside live completion.
  if p_view = 'state' then
    return query
      select c.run_id, c.seed, c.language, c.depth, c.node_cap,
        case when c.expires_at <= now_at then 'expired' else c.status end,
        c.progress, c.error, c.retry_state, c.expires_at,
        c.owner_token, c.owner_version, c.lease_until,
        null::jsonb, null::jsonb,
        case when c.expires_at > now_at and c.status = 'completed'
          then c.truncated else null::boolean end
      from public.crawl_runs c where c.run_id = p_run_id;
  elsif p_view = 'preview' then
    return query
      select c.run_id, c.seed, c.language, c.depth, c.node_cap,
        case when c.expires_at <= now_at then 'expired' else c.status end,
        c.progress, c.error, c.retry_state, c.expires_at,
        c.owner_token, c.owner_version, c.lease_until,
        case when c.expires_at > now_at and c.status <> 'completed'
          then c.checkpoint else null::jsonb end,
        case when c.expires_at > now_at and c.status = 'completed'
          then c.graph else null::jsonb end,
        case when c.expires_at > now_at and c.status = 'completed'
          then c.truncated else null::boolean end
      from public.crawl_runs c where c.run_id = p_run_id;
    return;
  else
    return query
      select c.run_id, c.seed, c.language, c.depth, c.node_cap,
        case when c.expires_at <= now_at then 'expired' else c.status end,
        c.progress, c.error, c.retry_state, c.expires_at,
        c.owner_token, c.owner_version, c.lease_until,
        null::jsonb,
        case when c.expires_at > now_at and c.status = 'completed'
          then c.graph else null::jsonb end,
        case when c.expires_at > now_at and c.status = 'completed'
          then c.truncated else null::boolean end
      from public.crawl_runs c where c.run_id = p_run_id;
    return;
  end if;
end $$;

revoke all on function public.read_crawl_run_snapshot(text, text)
  from public, anon, authenticated;
grant execute on function public.read_crawl_run_snapshot(text, text)
  to service_role;

notify pgrst, 'reload schema';

commit;
