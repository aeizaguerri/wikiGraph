-- Versioned canonical store for Crawl runs.  The service-role API is the only
-- writer; the Graph is JSONB so its immutable payload is committed atomically
-- with the terminal lifecycle transition.
create table if not exists public.crawl_runs (
  run_id text primary key,
  seed text not null check (length(seed) > 0),
  language text not null check (language in ('es', 'en')),
  depth smallint not null check (depth between 1 and 3),
  node_cap integer not null check (node_cap between 1 and 5000),
  status text not null check (status in ('running', 'recoverable', 'completed', 'failed')),
  progress jsonb not null default '{"crawled":0,"discovered":1,"depth":0,"recent":[]}'::jsonb,
  checkpoint jsonb not null,
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
