"""Real PostgREST regressions for logical expiry of owned crawl runs."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import time
import uuid

import httpx
import pytest

from tests.support.postgres import load_postgres_harness, run_postgres_admin_sql, start_postgres_admin_sql

HARNESS = load_postgres_harness()
URL = HARNESS.url if HARNESS else None
JWT = HARNESS.service_role_jwt if HARNESS else None
pytestmark = pytest.mark.skipif(
    not URL or not JWT,
    reason="ticket28 disposable loopback PostgREST credentials are required",
)


def _headers() -> dict[str, str]:
    assert JWT is not None
    return {"apikey": JWT, "Authorization": f"Bearer {JWT}"}


def _rpc(name: str, args: dict[str, object]) -> httpx.Response:
    assert URL is not None
    return httpx.post(f"{URL}/rpc/{name}", headers=_headers(), json=args, timeout=10)


def _sql(query: str) -> str:
    return run_postgres_admin_sql(query).stdout.strip()


def _new_run() -> tuple[str, str, int]:
    run_id, token = "t30-" + uuid.uuid4().hex, str(uuid.uuid4())
    response = _rpc("create_owned_crawl_run", {
        "p_run_id": run_id,
        "p_seed": "Expiration guard test",
        "p_language": "en",
        "p_depth": 1,
        "p_node_cap": 10,
        "p_checkpoint": {"nodes": ["kept"], "edges": []},
        "p_owner_token": token,
        "p_lease_seconds": 90,
    })
    assert response.status_code == 200, response.text
    assert response.json()[0]["owner_version"] == 1
    return run_id, token, 1


def _args(run_id: str, token: str, version: int) -> dict[str, object]:
    return {"p_run_id": run_id, "p_owner_token": token, "p_owner_version": version}


def _operation(name: str, run_id: str, token: str, version: int) -> httpx.Response:
    if name == "claim_crawl_run":
        args: dict[str, object] = {
            "p_run_id": run_id,
            "p_owner_token": token,
            "p_expected_version": version,
            "p_lease_seconds": 90,
        }
    else:
        args = _args(run_id, token, version)
    if name == "heartbeat_crawl_run":
        args["p_lease_seconds"] = 90
    elif name == "fenced_update_crawl_run":
        args.update({
            "p_operation": "progress",
            "p_patch": {"progress": {"crawled": 7}},
        })
    return _rpc(name, args)


@pytest.mark.parametrize("rpc", ["claim_crawl_run", "heartbeat_crawl_run", "fenced_update_crawl_run"])
def test_expired_run_rejects_owner_mutations_while_healthy_counterpart_works(rpc: str) -> None:
    expired_id, expired_token, version = _new_run()
    healthy_id, healthy_token, _ = _new_run()
    _sql(
        f"update public.crawl_runs set expires_at=clock_timestamp()-interval '1 second' "
        f"where run_id='{expired_id}'"
    )
    if rpc == "claim_crawl_run":
        _sql(
            "update public.crawl_runs set lease_until=clock_timestamp()-interval '1 second' "
            f"where run_id in ('{expired_id}','{healthy_id}')"
        )

    expired = _operation(rpc, expired_id, expired_token, version)
    healthy = _operation(rpc, healthy_id, healthy_token, version)

    assert expired.status_code == 200, expired.text
    assert healthy.status_code == 200, healthy.text
    if rpc == "claim_crawl_run":
        assert expired.json() == []
        assert len(healthy.json()) == 1
    elif rpc == "heartbeat_crawl_run":
        assert expired.json() == []
        assert healthy.json() and isinstance(healthy.json()[0]["lease_until"], str)
    else:
        assert expired.json() is False
        assert healthy.json() is True

    assert _sql(
        f"select status='running' and owner_token='{expired_token}'::uuid "
        f"and owner_version=1 and checkpoint->'nodes'='[\"kept\"]'::jsonb "
        f"from public.crawl_runs where run_id='{expired_id}'"
    ) == "t"


@pytest.mark.parametrize("rpc", ["claim_crawl_run", "heartbeat_crawl_run", "fenced_update_crawl_run"])
def test_expiry_is_rechecked_after_waiting_for_run_row_lock(rpc: str) -> None:
    run_id, token, version = _new_run()
    _sql(
        f"update public.crawl_runs set expires_at=clock_timestamp()+interval '0.7 seconds', "
        f"lease_until=clock_timestamp()-interval '1 second' where run_id='{run_id}'"
    )
    lock_sql = (
        "begin; select run_id from public.crawl_runs "
        f"where run_id='{run_id}' for update; select pg_sleep(2); commit;"
    )
    locker = start_postgres_admin_sql(lock_sql)
    assert locker.stdout is not None
    assert locker.stdout.readline().strip() == run_id
    time.sleep(0.9)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(_operation, rpc, run_id, token, version).result(timeout=5)
    assert locker.wait(timeout=5) == 0
    assert response.status_code == 200, response.text
    if rpc == "claim_crawl_run":
        assert response.json() == []
    elif rpc == "heartbeat_crawl_run":
        assert response.json() == []
    else:
        assert response.json() is False
    assert _sql(
        f"select status='running' and owner_token='{token}'::uuid and owner_version=1 "
        f"from public.crawl_runs where run_id='{run_id}'"
    ) == "t"
