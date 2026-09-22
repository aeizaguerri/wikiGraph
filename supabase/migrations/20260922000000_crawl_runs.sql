-- Versioned canonical store for Crawl runs.  The service-role API is the only
-- writer; the Graph is JSONB so its immutable payload is committed atomically
-- with the terminal lifecycle transition.
create table if not exists public.crawl_runs (
  run_id text primary key,
  seed text not null check (length(seed) > 0),
  language text not null check (language in ('es', 'en')),
  depth smallint not null check (depth between 1 and 3),
  node_cap integer not null check (node_cap between 1 and 5000),
  status text not null check (status in ('running', 'completed', 'failed')),
  progress jsonb not null default '{"crawled":0,"discovered":1,"depth":0,"recent":[]}'::jsonb,
  graph jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  expires_at timestamptz not null default (now() + interval '24 hours'),
  constraint completed_run_has_graph check (
    (status = 'completed' and graph is not null and completed_at is not null)
    or (status <> 'completed' and graph is null and completed_at is null)
  )
);

create index if not exists crawl_runs_status_updated_idx
  on public.crawl_runs (status, updated_at);
create index if not exists crawl_runs_expiry_idx
  on public.crawl_runs (expires_at);

create or replace function public.touch_crawl_run_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists crawl_runs_touch_updated_at on public.crawl_runs;
create trigger crawl_runs_touch_updated_at
before update on public.crawl_runs
for each row execute function public.touch_crawl_run_updated_at();

-- Completed Graphs retain the public URL for seven days; unfinished and failed
-- records retain recovery diagnostics for 24 hours.
create or replace function public.set_crawl_run_expiry()
returns trigger
language plpgsql
as $$
begin
  if new.status = 'completed' and old.status <> 'completed' then
    new.completed_at = coalesce(new.completed_at, now());
    new.expires_at = new.completed_at + interval '7 days';
  elsif new.status <> 'completed' then
    new.expires_at = coalesce(new.expires_at, now() + interval '24 hours');
  end if;
  return new;
end;
$$;

drop trigger if exists crawl_runs_set_expiry on public.crawl_runs;
create trigger crawl_runs_set_expiry
before update on public.crawl_runs
for each row execute function public.set_crawl_run_expiry();

create or replace function public.prevent_completed_crawl_run_mutation()
returns trigger
language plpgsql
as $$
begin
  if old.status = 'completed' and (
    new.status <> old.status or new.graph is distinct from old.graph
  ) then
    raise exception 'completed Crawl run is immutable';
  end if;
  return new;
end;
$$;

drop trigger if exists crawl_runs_immutable_completion on public.crawl_runs;
create trigger crawl_runs_immutable_completion
before update on public.crawl_runs
for each row execute function public.prevent_completed_crawl_run_mutation();

-- Wikimedia arbitration is stored in Supabase because Render may run more
-- than one API or worker process. The RPC owns the lease, rolling windows,
-- and FIFO-by-owner selection; a module-local governor is not authoritative.
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

  select * into state_row
  from public.wikimedia_governor_state
  where singleton
  for update;

  if state_row.in_flight and state_row.lease_until <= now_at then
    update public.wikimedia_governor_state
    set in_flight = false, current_request = null, current_owner = null,
        lease_until = null
    where singleton;
    state_row.in_flight := false;
  end if;
  if state_row.in_flight then
    return query select false, 0.05::double precision;
    return;
  end if;

  update public.wikimedia_governor_state
  set starts = coalesce((select array_agg(value order by value)
                         from unnest(starts) value
                         where value > now_at - interval '1 second'), '{}'),
      attempts = coalesce((select array_agg(value order by value)
                           from unnest(attempts) value
                           where value > now_at - interval '60 seconds'), '{}')
  where singleton
  returning * into state_row;

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

  select request_id into candidate
  from public.wikimedia_governor_queue q
  where q.owner <> coalesce(state_row.last_owner, '')
     or not exists (
       select 1 from public.wikimedia_governor_queue other
       where other.owner <> coalesce(state_row.last_owner, '')
     )
  order by q.enqueued_at, q.request_id
  limit 1;
  if candidate is null or candidate <> p_request_id then
    return query select false, 0.05::double precision;
    return;
  end if;

  delete from public.wikimedia_governor_queue where request_id = candidate;
  update public.wikimedia_governor_state
  set in_flight = true,
      current_request = p_request_id,
      current_owner = p_owner,
      lease_until = now_at + interval '60 seconds',
      last_owner = p_owner,
      starts = array_append(starts, now_at),
      attempts = array_append(attempts, now_at)
  where singleton;
  return query select true, 0.0::double precision;
end;
$$;

create or replace function public.release_wikimedia_attempt(p_request_id uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.wikimedia_governor_state
  set in_flight = false, current_request = null, current_owner = null,
      lease_until = null
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
