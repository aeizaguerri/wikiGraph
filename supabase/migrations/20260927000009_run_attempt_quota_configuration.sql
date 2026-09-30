begin;

-- Replace the owned-create RPC without changing its ownership or lease rules.
-- The defaults preserve direct callers that do not yet send a run quota.
drop function public.create_owned_crawl_run(text,text,text,smallint,integer,jsonb,uuid,integer);

create function public.create_owned_crawl_run(
  p_run_id text, p_seed text, p_language text, p_depth smallint,
  p_node_cap integer, p_checkpoint jsonb, p_owner_token uuid,
  p_lease_seconds integer default 90,
  p_attempt_quota integer default 120
)
returns table (run_id text, owner_version bigint, lease_until timestamptz)
language plpgsql security definer set search_path = public, pg_temp as $$
declare expires timestamptz;
begin
  if p_owner_token is null or p_lease_seconds not between 5 and 3600
     or p_attempt_quota is null or p_attempt_quota < 1 then
    raise exception 'invalid owner, lease duration, or attempt quota';
  end if;
  expires := clock_timestamp() + make_interval(secs => p_lease_seconds);
  insert into public.crawl_runs
    (run_id, seed, language, depth, node_cap, status, progress, checkpoint,
     owner_token, owner_version, lease_until, attempt_quota)
  values (p_run_id, p_seed, p_language, p_depth, p_node_cap, 'running',
          '{"crawled":0,"discovered":1,"depth":0,"recent":[]}'::jsonb,
          p_checkpoint, p_owner_token, 1, expires, p_attempt_quota)
  returning crawl_runs.run_id, crawl_runs.owner_version, crawl_runs.lease_until
    into run_id, owner_version, lease_until;
  return next;
end $$;

revoke all on function public.create_owned_crawl_run(text,text,text,smallint,integer,jsonb,uuid,integer,integer)
  from public, anon, authenticated;
grant execute on function public.create_owned_crawl_run(text,text,text,smallint,integer,jsonb,uuid,integer,integer)
  to service_role;

commit;
