begin;

-- PostgreSQL expiry is authoritative even before the retention job scrubs a
-- row. Lock each run before checking its expiry so a transaction that waited
-- on a row lock cannot act on a stale transaction timestamp.
create or replace function public.claim_crawl_run(
  p_run_id text, p_owner_token uuid, p_expected_version bigint,
  p_lease_seconds integer default 90
)
returns table (owner_version bigint, lease_until timestamptz)
language plpgsql security definer set search_path = public, pg_temp as $$
declare r public.crawl_runs%rowtype; now_at timestamptz;
begin
  select * into r from public.crawl_runs
    where crawl_runs.run_id = p_run_id for update;
  now_at := clock_timestamp();
  if not found or r.expires_at <= now_at
     or r.status not in ('running','recoverable','overload_waiting')
     or r.owner_version <> p_expected_version
     or (r.status = 'running' and (r.owner_token is null or r.lease_until is null))
     or (r.status = 'running' and r.lease_until is not null and r.lease_until > now_at)
     or p_owner_token is null or p_lease_seconds not between 5 and 3600 then
    return;
  end if;
  update public.crawl_runs set status='running', owner_token=p_owner_token,
      owner_version=r.owner_version+1,
      lease_until=clock_timestamp()+make_interval(secs=>p_lease_seconds),
      error=null, retry_state=null
    where crawl_runs.run_id=p_run_id
      and crawl_runs.expires_at > clock_timestamp()
    returning crawl_runs.owner_version, crawl_runs.lease_until into owner_version, lease_until;
  if not found then return; end if;
  return next;
end $$;

create or replace function public.heartbeat_crawl_run(
  p_run_id text, p_owner_token uuid, p_owner_version bigint,
  p_lease_seconds integer default 90
)
returns table (lease_until timestamptz)
language plpgsql security definer set search_path = public, pg_temp as $$
declare r public.crawl_runs%rowtype; now_at timestamptz;
begin
  select * into r from public.crawl_runs
    where crawl_runs.run_id = p_run_id for update;
  now_at := clock_timestamp();
  if not found or r.expires_at <= now_at or r.status <> 'running'
     or r.owner_token is distinct from p_owner_token
     or r.owner_version <> p_owner_version or r.lease_until <= now_at
     or p_lease_seconds not between 5 and 3600 then
    return;
  end if;
  return query update public.crawl_runs target
    set lease_until=clock_timestamp()+make_interval(secs=>p_lease_seconds)
    where target.run_id=p_run_id and target.status='running'
      and target.owner_token=p_owner_token and target.owner_version=p_owner_version
      and target.lease_until > clock_timestamp()
      and target.expires_at > clock_timestamp()
    returning target.lease_until;
end $$;

create or replace function public.fenced_update_crawl_run(
  p_run_id text, p_owner_token uuid, p_owner_version bigint,
  p_operation text, p_patch jsonb
)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare r public.crawl_runs%rowtype; now_at timestamptz;
begin
  select * into r from public.crawl_runs where run_id=p_run_id for update;
  now_at:=clock_timestamp();
  if not found or r.expires_at <= now_at
     or r.status not in ('running','overload_waiting')
     or r.owner_token is distinct from p_owner_token
     or r.owner_version<>p_owner_version or r.lease_until<=now_at then
    return false;
  end if;
  if p_operation='progress' and p_patch ?& array['progress']
     and (p_patch - 'progress')='{}'::jsonb and jsonb_typeof(p_patch->'progress')='object' then
    update public.crawl_runs set progress=p_patch->'progress'
      where run_id=p_run_id and expires_at > clock_timestamp();
  elsif p_operation='checkpoint' and p_patch ?& array['checkpoint','progress']
     and (p_patch - array['checkpoint','progress'])='{}'::jsonb
     and jsonb_typeof(p_patch->'checkpoint')='object' and jsonb_typeof(p_patch->'progress')='object' then
    update public.crawl_runs set checkpoint=p_patch->'checkpoint',progress=p_patch->'progress'
      where run_id=p_run_id and expires_at > clock_timestamp();
  elsif p_operation='completed' and p_patch ?& array['graph']
     and (p_patch - 'graph')='{}'::jsonb and jsonb_typeof(p_patch->'graph')='object' then
    update public.crawl_runs set status='completed',graph=p_patch->'graph',completed_at=clock_timestamp(),
      owner_token=null,owner_version=owner_version+1,lease_until=null
      where run_id=p_run_id and expires_at > clock_timestamp();
  elsif p_operation in ('failed','recoverable','overload_waiting') and p_patch ?& array['error']
     and (p_patch - array['error','retry_state'])='{}'::jsonb
     and jsonb_typeof(p_patch->'error') in ('string','null') then
    update public.crawl_runs set status=p_operation,error=p_patch->>'error',
      retry_state=case when p_patch ? 'retry_state' then p_patch->'retry_state' else retry_state end,
      owner_token=null,owner_version=owner_version+1,lease_until=null
      where run_id=p_run_id and expires_at > clock_timestamp();
  else return false;
  end if;
  if not found then return false; end if;
  return true;
end $$;

revoke all on function public.claim_crawl_run(text,uuid,bigint,integer) from public, anon, authenticated;
revoke all on function public.heartbeat_crawl_run(text,uuid,bigint,integer) from public, anon, authenticated;
revoke all on function public.fenced_update_crawl_run(text,uuid,bigint,text,jsonb) from public, anon, authenticated;
grant execute on function public.claim_crawl_run(text,uuid,bigint,integer) to service_role;
grant execute on function public.heartbeat_crawl_run(text,uuid,bigint,integer) to service_role;
grant execute on function public.fenced_update_crawl_run(text,uuid,bigint,text,jsonb) to service_role;

commit;
