-- Durable provider backpressure shared by every application process.
alter table public.crawl_runs
  add column if not exists retry_state jsonb;

alter table public.crawl_runs drop constraint if exists crawl_runs_status_check;
alter table public.crawl_runs add constraint crawl_runs_status_check
  check (status in ('running', 'overload_waiting', 'recoverable', 'completed', 'failed'));

alter table public.wikimedia_governor_state
  add column if not exists cooldown_until timestamptz;

create or replace function public.set_wikimedia_cooldown(
  p_delay_seconds double precision
)
returns void language plpgsql security definer set search_path = public as $$
begin
  update public.wikimedia_governor_state
  set cooldown_until = greatest(
    coalesce(cooldown_until, clock_timestamp()),
    clock_timestamp() + make_interval(secs => greatest(p_delay_seconds, 0))
  )
  where singleton;
end;
$$;

create or replace function public.acquire_wikimedia_attempt(
  p_request_id uuid,
  p_owner text
)
returns table (granted boolean, retry_after_seconds double precision)
language plpgsql security definer set search_path = public as $$
declare
  state_row public.wikimedia_governor_state%rowtype;
  candidate uuid;
  now_at timestamptz := clock_timestamp();
  second_wait double precision := 0;
  minute_wait double precision := 0;
  cooldown_wait double precision := 0;
begin
  insert into public.wikimedia_governor_queue(request_id, owner)
  values (p_request_id, p_owner) on conflict (request_id) do nothing;
  select * into state_row from public.wikimedia_governor_state
    where singleton for update;
  if state_row.in_flight and state_row.lease_until <= now_at then
    update public.wikimedia_governor_state
      set in_flight = false, current_request = null, current_owner = null,
          lease_until = null where singleton;
    state_row.in_flight := false;
  end if;
  if state_row.in_flight then
    return query select false, 0.05::double precision; return;
  end if;
  cooldown_wait := extract(epoch from (state_row.cooldown_until - now_at));
  if cooldown_wait > 0 then
    return query select false, cooldown_wait; return;
  end if;
  update public.wikimedia_governor_state
    set starts = coalesce((select array_agg(value order by value) from unnest(starts) value
                           where value > now_at - interval '1 second'), '{}'),
        attempts = coalesce((select array_agg(value order by value) from unnest(attempts) value
                             where value > now_at - interval '60 seconds'), '{}')
    where singleton returning * into state_row;
  if cardinality(state_row.starts) >= 2 then
    second_wait := extract(epoch from (state_row.starts[1] + interval '1 second' - now_at));
  end if;
  if cardinality(state_row.attempts) >= 120 then
    minute_wait := extract(epoch from (state_row.attempts[1] + interval '60 seconds' - now_at));
  end if;
  if greatest(second_wait, minute_wait) > 0 then
    return query select false, greatest(second_wait, minute_wait); return;
  end if;
  select request_id into candidate from public.wikimedia_governor_queue q
    where q.owner <> coalesce(state_row.last_owner, '')
       or not exists (select 1 from public.wikimedia_governor_queue other
                      where other.owner <> coalesce(state_row.last_owner, ''))
    order by q.enqueued_at, q.request_id limit 1;
  if candidate is null or candidate <> p_request_id then
    return query select false, 0.05::double precision; return;
  end if;
  delete from public.wikimedia_governor_queue where request_id = candidate;
  update public.wikimedia_governor_state
    set in_flight = true, current_request = p_request_id, current_owner = p_owner,
        lease_until = now_at + interval '60 seconds', last_owner = p_owner,
        starts = array_append(starts, now_at), attempts = array_append(attempts, now_at)
    where singleton;
  return query select true, 0.0::double precision;
end;
$$;

revoke all on function public.set_wikimedia_cooldown(double precision) from public;
revoke all on function public.acquire_wikimedia_attempt(uuid, text) from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.set_wikimedia_cooldown(double precision) to service_role;
    grant execute on function public.acquire_wikimedia_attempt(uuid, text) to service_role;
  end if;
end;
$$;
