-- Backend-only access is explicit rather than inherited from platform defaults.
-- PostgREST receives only the service_role JWT from the Render process.
do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'crawl_runs',
    'crawl_launch_admission',
    'crawl_launch_admission_total',
    'wikimedia_governor_state',
    'wikimedia_governor_queue',
    'upstream_response_cache',
    'crawl_run_metrics'
  ] loop
    execute format('revoke all on table public.%I from public', table_name);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on table public.%I from anon', table_name);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on table public.%I from authenticated', table_name);
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant select, insert, update, delete on table public.%I to service_role', table_name);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant usage on schema public to service_role;
  end if;
end;
$$;

-- RLS is enabled for platform configurations that do not make service_role
-- bypass it. The policies are deliberately explicit for every runtime table.
alter table public.crawl_runs enable row level security;
alter table public.crawl_launch_admission enable row level security;
alter table public.crawl_launch_admission_total enable row level security;
alter table public.wikimedia_governor_state enable row level security;
alter table public.wikimedia_governor_queue enable row level security;
alter table public.upstream_response_cache enable row level security;
alter table public.crawl_run_metrics enable row level security;

do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'crawl_runs',
    'crawl_launch_admission',
    'crawl_launch_admission_total',
    'wikimedia_governor_state',
    'wikimedia_governor_queue',
    'upstream_response_cache',
    'crawl_run_metrics'
  ] loop
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('drop policy if exists wikigraph_service_role_all on public.%I', table_name);
      execute format('create policy wikigraph_service_role_all on public.%I for all to service_role using (true) with check (true)', table_name);
    end if;
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('drop policy if exists wikigraph_anon_denied on public.%I', table_name);
      execute format('create policy wikigraph_anon_denied on public.%I for all to anon using (false) with check (false)', table_name);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('drop policy if exists wikigraph_authenticated_denied on public.%I', table_name);
      execute format('create policy wikigraph_authenticated_denied on public.%I for all to authenticated using (false) with check (false)', table_name);
    end if;
  end loop;
end;
$$;

do $$
begin
  revoke all on function public.acquire_wikimedia_attempt(uuid, text) from public;
  revoke all on function public.release_wikimedia_attempt(uuid) from public;
  revoke all on function public.set_wikimedia_cooldown(double precision) from public;
  revoke all on function public.admit_crawl_launch(text, integer, integer, integer, integer) from public;
  revoke all on function public.expire_crawl_runs(timestamptz) from public;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.acquire_wikimedia_attempt(uuid, text) from anon;
    revoke all on function public.release_wikimedia_attempt(uuid) from anon;
    revoke all on function public.set_wikimedia_cooldown(double precision) from anon;
    revoke all on function public.admit_crawl_launch(text, integer, integer, integer, integer) from anon;
    revoke all on function public.expire_crawl_runs(timestamptz) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on function public.acquire_wikimedia_attempt(uuid, text) from authenticated;
    revoke all on function public.release_wikimedia_attempt(uuid) from authenticated;
    revoke all on function public.set_wikimedia_cooldown(double precision) from authenticated;
    revoke all on function public.admit_crawl_launch(text, integer, integer, integer, integer) from authenticated;
    revoke all on function public.expire_crawl_runs(timestamptz) from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.acquire_wikimedia_attempt(uuid, text) to service_role;
    grant execute on function public.release_wikimedia_attempt(uuid) to service_role;
    grant execute on function public.set_wikimedia_cooldown(double precision) to service_role;
    grant execute on function public.admit_crawl_launch(text, integer, integer, integer, integer) to service_role;
    grant execute on function public.expire_crawl_runs(timestamptz) to service_role;
  end if;
end;
$$;
