-- Run this migration only after draining every application binary that writes
-- crawl_runs directly. The table UPDATE revoke below is the fencing boundary.
alter table public.crawl_runs
  add column if not exists owner_token uuid,
  add column if not exists owner_version bigint not null default 0,
  add column if not exists lease_until timestamptz;

create index if not exists crawl_runs_expired_owner_lease_idx
  on public.crawl_runs (lease_until)
  where status = 'running' and lease_until is not null;

alter table public.crawl_runs
  add constraint crawl_runs_running_requires_owner_check
  check (status <> 'running' or
         (owner_token is not null and owner_version > 0 and lease_until is not null))
  not valid;

create table if not exists public.crawl_run_ownership_repairs (
  repair_id bigint generated always as identity primary key,
  run_id text not null,
  expected_updated_at timestamptz not null,
  previous_owner_token uuid,
  previous_owner_version bigint not null,
  repaired_at timestamptz not null default clock_timestamp(),
  operator_note text not null check (length(trim(operator_note)) > 0)
);
revoke all on public.crawl_run_ownership_repairs from public, anon, authenticated, service_role;

create or replace function public.create_owned_crawl_run(
  p_run_id text, p_seed text, p_language text, p_depth smallint,
  p_node_cap integer, p_checkpoint jsonb, p_owner_token uuid,
  p_lease_seconds integer default 90
)
returns table (run_id text, owner_version bigint, lease_until timestamptz)
language plpgsql security definer set search_path = public, pg_temp as $$
declare expires timestamptz;
begin
  if p_owner_token is null or p_lease_seconds not between 5 and 3600 then
    raise exception 'invalid owner or lease duration';
  end if;
  expires := clock_timestamp() + make_interval(secs => p_lease_seconds);
  insert into public.crawl_runs
    (run_id, seed, language, depth, node_cap, status, progress, checkpoint,
     owner_token, owner_version, lease_until)
  values (p_run_id, p_seed, p_language, p_depth, p_node_cap, 'running',
          '{"crawled":0,"discovered":1,"depth":0,"recent":[]}'::jsonb,
          p_checkpoint, p_owner_token, 1, expires)
  returning crawl_runs.run_id, crawl_runs.owner_version, crawl_runs.lease_until
    into run_id, owner_version, lease_until;
  return next;
end $$;

create or replace function public.claim_crawl_run(
  p_run_id text, p_owner_token uuid, p_expected_version bigint,
  p_lease_seconds integer default 90
)
returns table (owner_version bigint, lease_until timestamptz)
language plpgsql security definer set search_path = public, pg_temp as $$
declare r public.crawl_runs%rowtype; now_at timestamptz;
begin
  select * into r from public.crawl_runs where crawl_runs.run_id = p_run_id for update;
  now_at := clock_timestamp();
  if not found or r.status not in ('running','recoverable','overload_waiting')
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
    returning crawl_runs.owner_version, crawl_runs.lease_until into owner_version, lease_until;
  return next;
end $$;

create or replace function public.heartbeat_crawl_run(
  p_run_id text, p_owner_token uuid, p_owner_version bigint,
  p_lease_seconds integer default 90
)
returns table (lease_until timestamptz)
language plpgsql security definer set search_path = public, pg_temp as $$
begin
  return query update public.crawl_runs r
    set lease_until=clock_timestamp()+make_interval(secs=>p_lease_seconds)
    where r.run_id=p_run_id and r.status='running'
      and r.owner_token=p_owner_token and r.owner_version=p_owner_version
      and r.lease_until > clock_timestamp() and p_lease_seconds between 5 and 3600
    returning r.lease_until;
end $$;

create or replace function public.reconcile_expired_crawl_runs()
returns table (reconciled_count integer)
language plpgsql security definer set search_path = public, pg_temp as $$
declare r record; n integer:=0; now_at timestamptz;
begin
  for r in select run_id from public.crawl_runs
    where status='running' and owner_token is not null and lease_until <= clock_timestamp()
    order by lease_until for update skip locked
  loop
    now_at := clock_timestamp();
    update public.crawl_runs set status='recoverable', owner_token=null,
      owner_version=owner_version+1, lease_until=null,
      error=coalesce(error,'Run ownership lease expired')
      where run_id=r.run_id and status='running' and lease_until <= now_at;
    if found then n:=n+1; end if;
  end loop;
  return query select n;
end $$;

-- One narrow allowlist, with state-specific payload validation. No caller can
-- modify identity, owner, lease, timestamps, expiry, or unrelated metadata.
create or replace function public.fenced_update_crawl_run(
  p_run_id text, p_owner_token uuid, p_owner_version bigint,
  p_operation text, p_patch jsonb
)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare r public.crawl_runs%rowtype; now_at timestamptz;
begin
  select * into r from public.crawl_runs where run_id=p_run_id for update;
  now_at:=clock_timestamp();
  if not found or r.status not in ('running','overload_waiting') or r.owner_token is distinct from p_owner_token
     or r.owner_version<>p_owner_version or r.lease_until<=now_at then return false; end if;
  if p_operation='progress' and p_patch ?& array['progress']
     and (p_patch - 'progress')='{}'::jsonb and jsonb_typeof(p_patch->'progress')='object' then
    update public.crawl_runs set progress=p_patch->'progress' where run_id=p_run_id;
  elsif p_operation='checkpoint' and p_patch ?& array['checkpoint','progress']
     and (p_patch - array['checkpoint','progress'])='{}'::jsonb
     and jsonb_typeof(p_patch->'checkpoint')='object' and jsonb_typeof(p_patch->'progress')='object' then
    update public.crawl_runs set checkpoint=p_patch->'checkpoint',progress=p_patch->'progress' where run_id=p_run_id;
  elsif p_operation='completed' and p_patch ?& array['graph']
     and (p_patch - 'graph')='{}'::jsonb and jsonb_typeof(p_patch->'graph')='object' then
    update public.crawl_runs set status='completed',graph=p_patch->'graph',completed_at=clock_timestamp(),
      owner_token=null,owner_version=owner_version+1,lease_until=null where run_id=p_run_id;
  elsif p_operation in ('failed','recoverable','overload_waiting') and p_patch ?& array['error']
     and (p_patch - array['error','retry_state'])='{}'::jsonb
     and jsonb_typeof(p_patch->'error') in ('string','null') then
    update public.crawl_runs set status=p_operation,error=p_patch->>'error',
      retry_state=case when p_patch ? 'retry_state' then p_patch->'retry_state' else retry_state end,
      owner_token=null,owner_version=owner_version+1,lease_until=null where run_id=p_run_id;
  else return false;
  end if;
  return true;
end $$;

-- Deliberately not executable by service_role: this is a post-drain operator
-- procedure with optimistic concurrency and an immutable audit receipt.
create or replace function public.repair_legacy_crawl_run(
  p_run_id text, p_expected_updated_at timestamptz, p_operator_note text
)
returns void language plpgsql security definer set search_path = public, pg_temp as $$
declare r public.crawl_runs%rowtype;
begin
  select * into r from public.crawl_runs where run_id=p_run_id for update;
  if not found or r.status<>'running' or r.owner_token is not null
     or r.owner_version<>0 or r.updated_at<>p_expected_updated_at
     or p_operator_note is null or length(trim(p_operator_note))=0 then
    raise exception 'legacy run repair precondition failed';
  end if;
  insert into public.crawl_run_ownership_repairs(run_id,expected_updated_at,previous_owner_token,previous_owner_version,operator_note)
    values(p_run_id,p_expected_updated_at,r.owner_token,r.owner_version,p_operator_note);
  update public.crawl_runs set status='recoverable', owner_token=null,
    owner_version=owner_version+1, lease_until=null,
    error=coalesce(error,'Legacy run repaired after application binaries were drained')
    where run_id=p_run_id;
end $$;

revoke all on function public.create_owned_crawl_run(text,text,text,smallint,integer,jsonb,uuid,integer) from public, anon, authenticated;
revoke all on function public.claim_crawl_run(text,uuid,bigint,integer) from public, anon, authenticated;
revoke all on function public.heartbeat_crawl_run(text,uuid,bigint,integer) from public, anon, authenticated;
revoke all on function public.reconcile_expired_crawl_runs() from public, anon, authenticated;
revoke all on function public.fenced_update_crawl_run(text,uuid,bigint,text,jsonb) from public, anon, authenticated;
revoke all on function public.repair_legacy_crawl_run(text,timestamptz,text) from public, anon, authenticated, service_role;
grant execute on function public.create_owned_crawl_run(text,text,text,smallint,integer,jsonb,uuid,integer) to service_role;
grant execute on function public.claim_crawl_run(text,uuid,bigint,integer) to service_role;
grant execute on function public.heartbeat_crawl_run(text,uuid,bigint,integer) to service_role;
grant execute on function public.reconcile_expired_crawl_runs() to service_role;
grant execute on function public.fenced_update_crawl_run(text,uuid,bigint,text,jsonb) to service_role;

-- Remove the direct-write path but preserve SELECT for existing readers. Legacy
-- inserts are blocked too: new rows must enter through create_owned_crawl_run.
revoke insert, update, delete on table public.crawl_runs from service_role;
grant select on table public.crawl_runs to service_role;
