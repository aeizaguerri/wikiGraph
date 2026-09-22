-- Deployment-wide Wikimedia arbitration shared by every API/worker process.
create table if not exists public.wikimedia_governor_state (
  singleton boolean primary key default true check (singleton),
  in_flight boolean not null default false,
  current_request uuid,
  current_owner text,
  lease_until timestamptz,
  last_owner text,
  starts timestamptz[] not null default '{}',
  attempts timestamptz[] not null default '{}'
);

create table if not exists public.wikimedia_governor_queue (
  request_id uuid primary key,
  owner text not null,
  enqueued_at timestamptz not null default clock_timestamp()
);

insert into public.wikimedia_governor_state (singleton)
values (true)
on conflict (singleton) do nothing;

create or replace function public.acquire_wikimedia_attempt(
  p_request_id uuid,
  p_owner text
)
returns table (granted boolean, retry_after_seconds double precision)
language plpgsql
security definer
set search_path = public
as $$
declare
  state_row public.wikimedia_governor_state%rowtype;
  candidate uuid;
  now_at timestamptz := clock_timestamp();
  second_wait double precision := 0;
  minute_wait double precision := 0;
begin
  if p_request_id is null or p_owner is null or length(p_owner) = 0 then
    return query select false, 1.0;
    return;
  end if;
  insert into public.wikimedia_governor_queue(request_id, owner)
  values (p_request_id, p_owner)
  on conflict (request_id) do nothing;
  select * into state_row from public.wikimedia_governor_state
  where singleton for update;
  if state_row.in_flight and state_row.lease_until <= now_at then
    update public.wikimedia_governor_state
    set in_flight = false, current_request = null, current_owner = null,
        lease_until = null where singleton;
    state_row.in_flight := false;
  end if;
  if state_row.in_flight then
    return query select false, 0.05::double precision;
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
    return query select false, greatest(second_wait, minute_wait);
    return;
  end if;
  select request_id into candidate from public.wikimedia_governor_queue q
  where q.owner <> coalesce(state_row.last_owner, '')
     or not exists (select 1 from public.wikimedia_governor_queue other
                    where other.owner <> coalesce(state_row.last_owner, ''))
  order by q.enqueued_at, q.request_id limit 1;
  if candidate is null or candidate <> p_request_id then
    return query select false, 0.05::double precision;
    return;
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

create or replace function public.release_wikimedia_attempt(p_request_id uuid)
returns void language plpgsql security definer set search_path = public as $$
begin
  update public.wikimedia_governor_state
  set in_flight = false, current_request = null, current_owner = null, lease_until = null
  where singleton and current_request = p_request_id;
end;
$$;

do $$
begin
  revoke all on function public.acquire_wikimedia_attempt(uuid, text) from public;
  revoke all on function public.release_wikimedia_attempt(uuid) from public;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.acquire_wikimedia_attempt(uuid, text) from anon;
    revoke all on function public.release_wikimedia_attempt(uuid) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.acquire_wikimedia_attempt(uuid, text) to service_role;
    grant execute on function public.release_wikimedia_attempt(uuid) to service_role;
  end if;
end;
$$;
