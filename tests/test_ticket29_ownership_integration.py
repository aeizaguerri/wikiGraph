"""Real local PostgREST/PostgreSQL checks for ticket 29 run ownership.

Run against the isolated ticket28 harness after applying migration 00007:
source /tmp/opencode/wikigraph-ticket29-lease.env
uv run pytest tests/test_ticket29_ownership_integration.py -q
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import uuid

import httpx
import pytest

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
JWT = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
pytestmark = pytest.mark.skipif(not URL or not JWT, reason="ticket29 local database credentials are not configured")


def _headers() -> dict[str, str]:
    assert JWT is not None
    return {"apikey": JWT, "Authorization": f"Bearer {JWT}"}


def _rpc(name: str, args: dict[str, object]) -> httpx.Response:
    assert URL is not None
    return httpx.post(f"{URL}/rpc/{name}", headers=_headers(), json=args, timeout=10)


def _sql(query: str) -> str:
    result = subprocess.run(
        ["podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-A", "-t",
         "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph"],
        input=query, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def _new_run() -> tuple[str, str, int]:
    run_id, token = "t29-" + uuid.uuid4().hex, str(uuid.uuid4())
    response = _rpc("create_owned_crawl_run", {
        "p_run_id": run_id, "p_seed": "Ownership test", "p_language": "en",
        "p_depth": 1, "p_node_cap": 10, "p_checkpoint": {"nodes": [], "edges": []},
        "p_owner_token": token, "p_lease_seconds": 90,
    })
    assert response.status_code == 200, response.text
    assert response.json()[0]["owner_version"] == 1
    return run_id, token, 1


def test_owned_run_claim_race_heartbeat_fencing_and_direct_patch_denial() -> None:
    run_id, _, version = _new_run()
    _sql(f"update public.crawl_runs set lease_until=clock_timestamp()-interval '1 second' where run_id='{run_id}'")
    contenders = [(str(uuid.uuid4()), version) for _ in range(2)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda pair: _rpc("claim_crawl_run", {
                "p_run_id": run_id, "p_owner_token": pair[0],
                "p_expected_version": pair[1], "p_lease_seconds": 90,
            }), contenders,
        ))
    assert all(response.status_code == 200 for response in responses)
    claims = [response.json() for response in responses]
    winners = [(pair[0], claim[0]) for pair, claim in zip(contenders, claims) if claim]
    assert len(winners) == 1
    token, claim = winners[0]
    version = claim["owner_version"]
    heartbeat = _rpc("heartbeat_crawl_run", {
        "p_run_id": run_id, "p_owner_token": token,
        "p_owner_version": version, "p_lease_seconds": 120,
    })
    assert heartbeat.status_code == 200 and heartbeat.json()
    assert _sql(f"select lease_until > clock_timestamp() from public.crawl_runs where run_id='{run_id}'") == "t"

    # Only this isolated test row is made expired using the trusted local psql role.
    _sql(f"update public.crawl_runs set lease_until=clock_timestamp()-interval '1 second' where run_id='{run_id}'")
    live_id, _, _ = _new_run()
    reconcile = _rpc("reconcile_expired_crawl_runs", {})
    assert reconcile.status_code == 200 and reconcile.json()[0]["reconciled_count"] >= 1
    assert _sql(f"select status from public.crawl_runs where run_id='{live_id}'") == "running"
    stale = _rpc("fenced_update_crawl_run", {
        "p_run_id": run_id, "p_owner_token": token, "p_owner_version": version,
        "p_operation": "progress", "p_patch": {"progress": {"crawled": 1}},
    })
    assert stale.status_code == 200 and stale.json() is False
    assert _sql(f"select status from public.crawl_runs where run_id='{run_id}'") == "recoverable"

    raw = httpx.patch(f"{URL}/crawl_runs", headers=_headers(), params={"run_id": f"eq.{run_id}"}, json={"status": "completed"}, timeout=10)
    assert raw.status_code in {401, 403}
    raw_insert = httpx.post(f"{URL}/crawl_runs", headers=_headers(), json={
        "run_id": "t29-raw-" + uuid.uuid4().hex, "seed": "forged",
        "language": "en", "depth": 1, "node_cap": 10, "status": "running",
        "progress": {}, "checkpoint": {},
    }, timeout=10)
    assert raw_insert.status_code in {401, 403}


@pytest.mark.parametrize("operation,patch", [
    ("completed", {"graph": {"nodes": [], "edges": []}}),
    ("failed", {"error": "forced test failure"}),
    ("recoverable", {"error": "forced recovery"}),
])
def test_stale_owner_cannot_complete_fail_or_recover(operation: str, patch: dict[str, object]) -> None:
    run_id, token, version = _new_run()
    denied = _rpc("fenced_update_crawl_run", {
        "p_run_id": run_id, "p_owner_token": str(uuid.uuid4()),
        "p_owner_version": version, "p_operation": operation, "p_patch": patch,
    })
    assert denied.status_code == 200 and denied.json() is False
    assert _sql(f"select status from public.crawl_runs where run_id='{run_id}'") == "running"


@pytest.mark.parametrize(("operation", "patch", "expected_status"), [
    ("progress", {"progress": {"crawled": 2}}, "running"),
    ("checkpoint", {"checkpoint": {"nodes": ["A"]}, "progress": {"crawled": 1}}, "running"),
    ("completed", {"graph": {"nodes": [], "edges": []}}, "completed"),
    ("failed", {"error": "controlled failure"}, "failed"),
    ("recoverable", {"error": "controlled recovery", "retry_state": {"attempt": 1}}, "recoverable"),
])
def test_current_fence_can_apply_only_the_selected_transition(
    operation: str, patch: dict[str, object], expected_status: str,
) -> None:
    run_id, token, version = _new_run()
    updated = _rpc("fenced_update_crawl_run", {
        "p_run_id": run_id, "p_owner_token": token,
        "p_owner_version": version, "p_operation": operation, "p_patch": patch,
    })
    assert updated.status_code == 200 and updated.json() is True
    assert _sql(f"select status from public.crawl_runs where run_id='{run_id}'") == expected_status
    if operation in {"completed", "failed", "recoverable"}:
        assert _sql(f"select owner_token is null and lease_until is null and owner_version=2 from public.crawl_runs where run_id='{run_id}'") == "t"


def test_legacy_null_owner_is_not_claimed_and_operator_repair_is_guarded_and_audited() -> None:
    run_id = "t29-legacy-" + uuid.uuid4().hex
    # Model the pre-migration row permitted by NOT VALID: it is installed before
    # the unvalidated check is restored, as existing rows are in a real upgrade.
    _sql(f"""alter table public.crawl_runs drop constraint crawl_runs_running_requires_owner_check;
      insert into public.crawl_runs
      (run_id,seed,language,depth,node_cap,status,progress,checkpoint)
      values ('{run_id}','legacy','en',1,10,'running','{{}}','{{}}');
      alter table public.crawl_runs add constraint crawl_runs_running_requires_owner_check
        check (status <> 'running' or (owner_token is not null and owner_version > 0 and lease_until is not null)) not valid""")
    updated = _sql(f"select updated_at::text from public.crawl_runs where run_id='{run_id}'")
    claim = _rpc("claim_crawl_run", {
        "p_run_id": run_id, "p_owner_token": str(uuid.uuid4()),
        "p_expected_version": 0, "p_lease_seconds": 90,
    })
    assert claim.status_code == 200 and claim.json() == []
    assert _sql(f"select owner_token is null and owner_version=0 from public.crawl_runs where run_id='{run_id}'") == "t"
    assert _sql(f"select has_function_privilege('service_role','public.repair_legacy_crawl_run(text,timestamptz,text)','execute')") == "f"
    token = _sql(f"select public.repair_legacy_crawl_run('{run_id}','{updated}'::timestamptz,'post-drain ticket29 local test')")
    assert token
    assert _sql(f"select owner_token is not null and owner_version=1 from public.crawl_runs where run_id='{run_id}'") == "t"
    assert _sql(f"select count(*) from public.crawl_run_ownership_repairs where run_id='{run_id}'") == "1"
    with pytest.raises(subprocess.CalledProcessError):
        _sql(f"select public.repair_legacy_crawl_run('{run_id}','{updated}'::timestamptz,'stale second repair')")


def test_missing_postgrest_is_a_connection_error_not_a_success() -> None:
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://127.0.0.1:1/crawl_runs", timeout=1)
