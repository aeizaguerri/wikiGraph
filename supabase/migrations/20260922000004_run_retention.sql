-- Retention cleanup keeps a small tombstone so an expired share URL is
-- distinguishable from an unknown run, while removing all reconstructive data.
alter table public.crawl_runs
  alter column progress drop not null,
  alter column checkpoint drop not null;

alter table public.crawl_runs
  drop constraint if exists crawl_runs_status_check,
  drop constraint if exists completed_run_has_graph;

alter table public.crawl_runs
  add constraint crawl_runs_status_check
    check (status in ('running', 'overload_waiting', 'recoverable', 'completed', 'failed', 'expired')),
  add constraint completed_run_has_graph check (
    (status = 'completed' and graph is not null and completed_at is not null)
    or (status <> 'completed' and graph is null and completed_at is null)
  );

alter table public.crawl_runs
  add column if not exists expired_at timestamptz;

create table if not exists public.crawl_run_metrics (
  metric_day date not null,
  language text not null check (language in ('es', 'en')),
  outcome text not null check (outcome in ('expired')),
  run_count integer not null default 0 check (run_count >= 0),
  primary key (metric_day, language, outcome)
);

create index if not exists crawl_run_metrics_day_idx
  on public.crawl_run_metrics (metric_day);

create or replace function public.prevent_completed_crawl_run_mutation()
returns trigger
language plpgsql
as $$
begin
  if old.status = 'completed'
     and coalesce(current_setting('wikigraph.expiry_cleanup', true), 'off') <> 'on'
     and (
    new.status <> old.status or new.graph is distinct from old.graph
  ) then
    raise exception 'completed Crawl run is immutable';
  end if;
  return new;
end;
$$;

create or replace function public.expire_crawl_runs(p_now timestamptz default now())
returns table (expired_count integer)
language plpgsql
security definer
set search_path = public
as $$
declare
  expired_row record;
  total integer := 0;
begin
  perform set_config('wikigraph.expiry_cleanup', 'on', true);
  for expired_row in
    select run_id, language
    from public.crawl_runs
    where status <> 'expired' and expires_at <= p_now
    for update
  loop
    update public.crawl_runs
    set status = 'expired',
        progress = null,
        checkpoint = null,
        graph = null,
        error = null,
        retry_state = null,
        completed_at = null,
        expired_at = p_now
    where run_id = expired_row.run_id;

    insert into public.crawl_run_metrics (metric_day, language, outcome, run_count)
    values (p_now::date, expired_row.language, 'expired', 1)
    on conflict (metric_day, language, outcome)
    do update set run_count = crawl_run_metrics.run_count + 1;
    total := total + 1;
  end loop;
  return query select total;
end;
$$;

revoke all on function public.expire_crawl_runs(timestamptz) from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.expire_crawl_runs(timestamptz) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.expire_crawl_runs(timestamptz) to service_role;
  end if;
end;
$$;
