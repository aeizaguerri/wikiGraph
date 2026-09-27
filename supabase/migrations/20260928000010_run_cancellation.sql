begin;

create function public.cancel_crawl_run(p_run_id text)
returns table (result text)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  run_row public.crawl_runs%rowtype;
  now_at timestamptz;
begin
  select * into run_row
  from public.crawl_runs
  where crawl_runs.run_id = p_run_id
  for update;

  if not found then
    return query select 'not_found'::text;
    return;
  end if;
  if run_row.status <> 'running' then
    return query select 'not_running'::text;
    return;
  end if;

  now_at := clock_timestamp();
  if run_row.expires_at <= now_at then
    return query select 'expired'::text;
    return;
  end if;

  update public.crawl_runs
  set status = 'recoverable',
      owner_token = null,
      owner_version = run_row.owner_version + 1,
      lease_until = null,
      error = 'Run cancelled by operator'
  where crawl_runs.run_id = p_run_id;

  -- Pending admissions are not grants: remove them without touching the shared
  -- governor lease, rate history, or already-admitted request accounting.
  -- An admission can hold its newly inserted queue tuple while waiting for
  -- governor state, then wait for this run row. Skip such tuples to avoid a
  -- run->queue / governor->run deadlock; that admission rechecks status and
  -- removes its own row when it resumes.
  delete from public.wikimedia_governor_queue q
  where q.ctid in (
    select pending.ctid
    from public.wikimedia_governor_queue pending
    where pending.owner = p_run_id
    for update skip locked
  );

  return query select 'cancelled'::text;
end;
$$;

revoke all on function public.cancel_crawl_run(text) from public, anon, authenticated;
grant execute on function public.cancel_crawl_run(text) to service_role;

commit;
