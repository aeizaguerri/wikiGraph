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

-- Launch admission is canonical because the public API may be served by more
-- than one Render process. IPs are supplied as server-side HMACs, never raw
-- client input. Rows are deliberately short-lived to bound storage growth.
create table if not exists public.crawl_launch_admission (
  bucket_start timestamptz not null,
  ip_hash text not null,
  launch_count integer not null default 0 check (launch_count >= 0),
  quota_day date not null,
  quota_count integer not null default 0 check (quota_count >= 0),
  primary key (bucket_start, ip_hash)
);

create table if not exists public.crawl_launch_admission_total (
  singleton boolean primary key default true check (singleton),
  bucket_start timestamptz not null,
  launch_count integer not null default 0 check (launch_count >= 0)
);
insert into public.crawl_launch_admission_total (singleton, bucket_start)
values (true, date_trunc('minute', now()))
on conflict (singleton) do nothing;

create index if not exists crawl_launch_admission_retention_idx
  on public.crawl_launch_admission (bucket_start);

create or replace function public.admit_crawl_launch(
  p_ip_hash text,
  p_per_ip_per_minute integer,
  p_deployment_per_minute integer,
  p_per_ip_per_day integer,
  p_max_ip_keys integer default 10000
)
returns table (admitted boolean, code text)
language plpgsql
security definer
set search_path = public
as $$
declare
  current_minute timestamptz := date_trunc('minute', now());
  current_day date := current_date;
  total_row public.crawl_launch_admission_total%rowtype;
  ip_row public.crawl_launch_admission%rowtype;
begin
  if p_ip_hash is null or length(p_ip_hash) < 32 then
    return query select false, 'invalid_client_identity';
    return;
  end if;
  if p_per_ip_per_minute < 1 or p_deployment_per_minute < 1
     or p_per_ip_per_day < 1 or p_max_ip_keys < 1 then
    return query select false, 'invalid_limits';
    return;
  end if;

  delete from public.crawl_launch_admission
  where bucket_start < current_minute - interval '2 days';

  select * into total_row
  from public.crawl_launch_admission_total
  where singleton
  for update;
  if total_row.bucket_start <> current_minute then
    update public.crawl_launch_admission_total
    set bucket_start = current_minute, launch_count = 0
    where singleton;
    total_row.bucket_start := current_minute;
    total_row.launch_count := 0;
  end if;
  if total_row.launch_count >= p_deployment_per_minute then
    return query select false, 'launch_rate_limited';
    return;
  end if;

  select * into ip_row
  from public.crawl_launch_admission
  where bucket_start = current_minute and ip_hash = p_ip_hash
  for update;
  if not found then
    if (select count(*) from public.crawl_launch_admission
        where quota_day = current_day) >= p_max_ip_keys then
      return query select false, 'storage_quota_exceeded';
      return;
    end if;
    insert into public.crawl_launch_admission
      (bucket_start, ip_hash, quota_day, launch_count, quota_count)
    values (current_minute, p_ip_hash, current_day, 0, 0)
    returning * into ip_row;
  end if;
  if ip_row.launch_count >= p_per_ip_per_minute then
    return query select false, 'launch_rate_limited';
    return;
  end if;
  if (select coalesce(sum(quota_count), 0)
      from public.crawl_launch_admission
      where ip_hash = p_ip_hash and quota_day = current_day) >= p_per_ip_per_day then
    return query select false, 'launch_quota_exceeded';
    return;
  end if;

  update public.crawl_launch_admission_total
  set launch_count = launch_count + 1
  where singleton;
  update public.crawl_launch_admission
  set launch_count = launch_count + 1,
      quota_day = current_day,
      quota_count = case when quota_day = current_day then quota_count + 1 else 1 end
  where bucket_start = current_minute and ip_hash = p_ip_hash;
  return query select true, 'admitted';
end;
$$;

revoke all on function public.admit_crawl_launch(text, integer, integer, integer, integer)
from public, anon;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.admit_crawl_launch(text, integer, integer, integer, integer)
      to service_role;
  end if;
end;
$$;
