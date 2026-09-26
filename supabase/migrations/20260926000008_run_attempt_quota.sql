begin;

alter table public.crawl_runs
  add column if not exists attempt_quota integer not null default 120
    check (attempt_quota > 0),
  add column if not exists attempts_used integer not null default 0
    check (attempts_used >= 0 and attempts_used <= attempt_quota),
  add column if not exists admitted_request_ids uuid[] not null default '{}';

create or replace function public.clear_expired_run_admission_ids()
returns trigger language plpgsql set search_path = public as $$
begin
  if new.status = 'expired' and old.status is distinct from 'expired' then
    new.admitted_request_ids := '{}';
  end if;
  return new;
end;
$$;

drop trigger if exists crawl_runs_clear_expired_admission_ids on public.crawl_runs;
create trigger crawl_runs_clear_expired_admission_ids
before update on public.crawl_runs
for each row execute function public.clear_expired_run_admission_ids();

drop function public.acquire_wikimedia_attempt(uuid, text);

create function public.acquire_wikimedia_attempt(
  p_request_id uuid,
  p_owner text
)
returns table (granted boolean, retry_after_seconds double precision, denial_code text)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  state_row public.wikimedia_governor_state%rowtype;
  run_row public.crawl_runs%rowtype;
  candidate uuid;
  now_at timestamptz := clock_timestamp();
  second_wait double precision := 0;
  minute_wait double precision := 0;
  cooldown_wait double precision := 0;
  is_run_owner boolean := false;
begin
  if p_request_id is null or p_owner is null or length(p_owner) = 0 then
    return query select false, 1.0::double precision, 'invalid_request'::text;
    return;
  end if;

  -- UUID owners are durable run IDs; legacy non-UUID owners stay global-only.
  if p_owner ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    select * into run_row from public.crawl_runs where run_id = p_owner;
    if not found then
      return query select false, 1.0::double precision, 'run_quota_unavailable'::text;
      return;
    end if;
    now_at := clock_timestamp();
    if run_row.status <> 'running' then
      return query select false, 1.0::double precision, 'run_not_running'::text;
      return;
    end if;
    if run_row.expires_at <= now_at then
      return query select false, 1.0::double precision, 'run_expired'::text;
      return;
    end if;
    if run_row.owner_token is null or run_row.lease_until is null or run_row.lease_until <= now_at then
      return query select false, 1.0::double precision, 'run_lease_expired'::text;
      return;
    end if;
    is_run_owner := true;
  end if;

  -- Replay identity is scoped to the durable run and checked on its locked row.
  insert into public.wikimedia_governor_queue(request_id, owner)
  values (p_request_id, p_owner)
  on conflict (request_id) do nothing;

  select * into state_row from public.wikimedia_governor_state
    where singleton for update;
  now_at := clock_timestamp();

  -- Lock and revalidate the run after waiting for the global state lock. This
  -- gives every acquisition the same lock order and prevents stale snapshots.
  if is_run_owner then
    select * into run_row from public.crawl_runs where run_id = p_owner for update;
    now_at := clock_timestamp();
    if not found then
      delete from public.wikimedia_governor_queue where request_id = p_request_id;
      return query select false, 1.0::double precision, 'run_quota_unavailable'::text;
      return;
    elsif run_row.status <> 'running' then
      delete from public.wikimedia_governor_queue where request_id = p_request_id;
      return query select false, 1.0::double precision, 'run_not_running'::text;
      return;
    elsif run_row.expires_at <= now_at then
      delete from public.wikimedia_governor_queue where request_id = p_request_id;
      return query select false, 1.0::double precision, 'run_expired'::text;
      return;
    elsif run_row.owner_token is null or run_row.lease_until is null or run_row.lease_until <= now_at then
      delete from public.wikimedia_governor_queue where request_id = p_request_id;
      return query select false, 1.0::double precision, 'run_lease_expired'::text;
      return;
    end if;
    if p_request_id = any(coalesce(run_row.admitted_request_ids, '{}')) then
      delete from public.wikimedia_governor_queue where request_id = p_request_id;
      return query select false, 1.0::double precision, 'request_already_admitted'::text;
      return;
    end if;
  end if;

  if state_row.in_flight and state_row.lease_until <= now_at then
    update public.wikimedia_governor_state
    set in_flight = false, current_request = null, current_owner = null,
        lease_until = null where singleton;
    state_row.in_flight := false;
  end if;
  if state_row.in_flight then
    return query select false, 0.05::double precision, null::text;
    return;
  end if;
  cooldown_wait := extract(epoch from (state_row.cooldown_until - now_at));
  if cooldown_wait > 0 then
    return query select false, cooldown_wait, null::text;
    return;
  end if;
  update public.wikimedia_governor_state
  set starts = coalesce((select array_agg(value order by value) from unnest(starts) value
                         where value > now_at - interval '1 second'), '{}'),
      attempts = coalesce((select array_agg(value order by value) from unnest(attempts) value
                           where value > now_at - interval '60 seconds'), '{}')
  where singleton returning * into state_row;
  if coalesce(cardinality(state_row.starts), 0) >= 2 then
    second_wait := extract(epoch from (state_row.starts[1] + interval '1 second' - now_at));
  end if;
  if coalesce(cardinality(state_row.attempts), 0) >= 120 then
    minute_wait := extract(epoch from (state_row.attempts[1] + interval '60 seconds' - now_at));
  end if;
  if greatest(second_wait, minute_wait) > 0 then
    return query select false, greatest(second_wait, minute_wait)::double precision, null::text;
    return;
  end if;
  select request_id into candidate from public.wikimedia_governor_queue q
  where q.owner <> coalesce(state_row.last_owner, '')
     or not exists (select 1 from public.wikimedia_governor_queue other
                    where other.owner <> coalesce(state_row.last_owner, ''))
  order by q.enqueued_at, q.request_id limit 1;
  if candidate is null or candidate <> p_request_id then
    return query select false, 0.05::double precision, null::text;
    return;
  end if;

  if is_run_owner and run_row.attempts_used >= run_row.attempt_quota then
    delete from public.wikimedia_governor_queue where request_id = p_request_id;
    return query select false, 1.0::double precision, 'run_quota_exhausted'::text;
    return;
  end if;

  delete from public.wikimedia_governor_queue where request_id = candidate;
  if is_run_owner then
    update public.crawl_runs
    set attempts_used = attempts_used + 1,
        admitted_request_ids = array_append(admitted_request_ids, p_request_id)
    where run_id = p_owner;
  end if;
  update public.wikimedia_governor_state
  set in_flight = true, current_request = p_request_id, current_owner = p_owner,
      lease_until = now_at + interval '60 seconds', last_owner = p_owner,
      starts = array_append(starts, now_at), attempts = array_append(attempts, now_at)
  where singleton;
  return query select true, 0.0::double precision, null::text;
end;
$$;

do $$
begin
  revoke all on function public.acquire_wikimedia_attempt(uuid, text) from public;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.acquire_wikimedia_attempt(uuid, text) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.acquire_wikimedia_attempt(uuid, text) to service_role;
  end if;
end;
$$;

commit;
