"""Real PostgREST checks for the targeted lightweight run-read RPC."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import time
import uuid

import httpx
import pytest

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
JWT = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
pytestmark = pytest.mark.skipif(
    URL != "http://127.0.0.1:33028" or not JWT,
    reason="ticket31 disposable loopback PostgREST credentials are required",
)


def _headers(token: str | None = None) -> dict[str, str]:
    credential = JWT if token is None else token
    assert credential is not None
    return {"apikey": credential, "Authorization": f"Bearer {credential}"}


def _rpc(name: str, args: dict[str, object], token: str | None = None) -> httpx.Response:
    assert URL is not None
    return httpx.post(f"{URL}/rpc/{name}", headers=_headers(token), json=args, timeout=10)


def _new_run() -> str:
    run_id = "t31-" + uuid.uuid4().hex
    response = _rpc("create_owned_crawl_run", {
        "p_run_id": run_id,
        "p_seed": "Lightweight read test",
        "p_language": "en",
        "p_depth": 2,
        "p_node_cap": 37,
        "p_checkpoint": {"nodes": ["seed"], "edges": []},
        "p_owner_token": str(uuid.uuid4()),
        "p_lease_seconds": 90,
    })
    assert response.status_code == 200, response.text
    return run_id


def _read(run_id: str, view: str = "state") -> httpx.Response:
    return _rpc("read_crawl_run_snapshot", {"p_run_id": run_id, "p_view": view})


def _sql(query: str) -> str:
    result = subprocess.run(
        ["podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-A", "-t",
         "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph"],
        input=query, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def _complete(run_id: str, token: str, graph: dict[str, object]) -> None:
    response = _rpc("fenced_update_crawl_run", {
        "p_run_id": run_id, "p_owner_token": token, "p_owner_version": 1,
        "p_operation": "completed", "p_patch": {"graph": graph},
    })
    assert response.status_code == 200 and response.json() is True


def _direct_complete(run_id: str, graph: object) -> None:
    graph_json = json.dumps(graph, separators=(",", ":")).replace("'", "''")
    _sql(
        "update public.crawl_runs set status='completed', "
        f"graph='{graph_json}'::jsonb, completed_at=clock_timestamp(), "
        f"owner_token=null, lease_until=null where run_id='{run_id}'"
    )


def test_state_rpc_returns_one_narrow_snapshot_for_a_known_run() -> None:
    run_id = _new_run()

    response = _read(run_id)

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "run_id", "seed", "language", "depth", "node_cap", "status", "progress",
        "error", "retry_state", "expires_at", "owner_token", "owner_version",
        "lease_until", "checkpoint", "graph", "truncated",
    }
    assert row == {
        "run_id": run_id,
        "seed": "Lightweight read test",
        "language": "en",
        "depth": 2,
        "node_cap": 37,
        "status": "running",
        "progress": {"crawled": 0, "discovered": 1, "depth": 0, "recent": []},
        "error": None,
        "retry_state": None,
        "expires_at": row["expires_at"],
        "owner_token": row["owner_token"],
        "owner_version": 1,
        "lease_until": row["lease_until"],
        "checkpoint": None,
        "graph": None,
        "truncated": None,
    }
    assert all(isinstance(row[field], str) for field in ("expires_at", "owner_token", "lease_until"))


@pytest.mark.parametrize("truncated", [True, False])
def test_fenced_completion_persists_truncated_in_state_without_graph(truncated: bool) -> None:
    run_id = _new_run()
    token = _sql(f"select owner_token::text from public.crawl_runs where run_id='{run_id}'")

    _complete(run_id, token, {"nodes": [], "edges": [], "truncated": truncated})
    response = _read(run_id)

    assert response.status_code == 200
    row = response.json()[0]
    assert row["status"] == "completed" and row["truncated"] is truncated
    assert row["graph"] is row["checkpoint"] is None


@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        ({"truncated": True}, True),
        ({"truncated": False}, False),
        ({"truncated": "true"}, None),
        ({}, None),
    ],
)
def test_direct_graph_update_computes_only_json_boolean_metadata(
    graph: object, expected: bool | None
) -> None:
    run_id = _new_run()

    _direct_complete(run_id, graph)
    response = _read(run_id)

    assert response.status_code == 200
    assert response.json()[0]["truncated"] is expected
    assert response.json()[0]["graph"] is None


def test_expired_completion_hides_truncated_without_physical_cleanup() -> None:
    run_id = _new_run()
    token = _sql(f"select owner_token::text from public.crawl_runs where run_id='{run_id}'")
    _complete(run_id, token, {"nodes": [], "edges": [], "truncated": True})
    _sql(
        "update public.crawl_runs set expires_at=clock_timestamp()-interval '1 second' "
        f"where run_id='{run_id}'"
    )

    response = _read(run_id)

    assert response.status_code == 200
    assert response.json()[0]["status"] == "expired"
    assert response.json()[0]["truncated"] is None
    assert _sql(
        f"select (graph is not null)::text||':'||truncated::text "
        f"from public.crawl_runs where run_id='{run_id}'"
    ) == "true:true"


def test_retention_graph_scrub_clears_generated_truncated_metadata() -> None:
    run_id = _new_run()
    token = _sql(f"select owner_token::text from public.crawl_runs where run_id='{run_id}'")
    _complete(run_id, token, {"nodes": [], "edges": [], "truncated": True})
    _sql(
        "begin; set local wikigraph.expiry_cleanup='on'; "
        "update public.crawl_runs set status='expired', graph=null, checkpoint=null, "
        f"progress=null, completed_at=null, expired_at=clock_timestamp() where run_id='{run_id}'; commit"
    )

    assert _sql(
        f"select (graph is null)::text||':'||(truncated is null)::text "
        f"from public.crawl_runs where run_id='{run_id}'"
    ) == "true:true"
    response = _read(run_id)
    assert response.status_code == 200
    assert response.json()[0]["status"] == "expired"
    assert response.json()[0]["truncated"] is None


@pytest.mark.parametrize("view", ["state", "preview", "graph"])
def test_read_views_select_only_their_intended_payload(view: str) -> None:
    run_id = _new_run()
    token = _sql(f"select owner_token::text from public.crawl_runs where run_id='{run_id}'")
    graph = {
        "nodes": [{"title": "seed", "communityId": 0}],
        "edges": [],
        "community_ids": [["seed", 0]],
        "community_count": 1,
        "modularity": 0.0,
    }
    if view != "state":
        _complete(run_id, token, graph)

    response = _read(run_id, view)

    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["run_id"] == run_id
    if view == "state":
        assert row["checkpoint"] is row["graph"] is None
    elif view == "preview":
        assert row["checkpoint"] is None
        assert row["graph"] == graph
    else:
        assert row["checkpoint"] is None
        assert row["graph"] == graph
    assert row["truncated"] is None


def test_preview_returns_checkpoint_and_graph_view_hides_incomplete_payload() -> None:
    run_id = _new_run()

    preview = _read(run_id, "preview")
    graph = _read(run_id, "graph")

    assert preview.status_code == graph.status_code == 200
    assert preview.json()[0]["checkpoint"] == {"nodes": ["seed"], "edges": []}
    assert preview.json()[0]["graph"] is None
    assert graph.json()[0]["checkpoint"] is None
    assert graph.json()[0]["graph"] is None


def test_unknown_and_invalid_views_have_coherent_rpc_results() -> None:
    missing = _read("t31-missing-" + uuid.uuid4().hex)
    invalid = _read("unused", "worker")
    null_view = _rpc("read_crawl_run_snapshot", {"p_run_id": "unused", "p_view": None})

    assert missing.status_code == 200 and missing.json() == []
    assert invalid.status_code == null_view.status_code == 400


def test_expired_snapshot_hides_payload_without_scrubbing_or_reconciling() -> None:
    run_id = _new_run()
    _sql(
        f"update public.crawl_runs set expires_at=clock_timestamp()-interval '1 second', "
        f"lease_until=clock_timestamp()-interval '1 second' where run_id='{run_id}'"
    )
    before = _sql(
        f"select status||':'||owner_version||':'||checkpoint::text||':'||graph::text "
        f"from public.crawl_runs where run_id='{run_id}'"
    )

    response = _read(run_id, "preview")
    row = response.json()[0]

    assert response.status_code == 200
    assert row["status"] == "expired"
    assert row["checkpoint"] is row["graph"] is None
    assert _sql(
        f"select status||':'||owner_version||':'||checkpoint::text||':'||graph::text "
        f"from public.crawl_runs where run_id='{run_id}'"
    ) == before


def test_stale_target_is_reconciled_and_version_fenced_without_global_cleanup() -> None:
    run_id = _new_run()
    unrelated_id = _new_run()
    _sql(
        "update public.crawl_runs set lease_until=clock_timestamp()-interval '1 second' "
        f"where run_id in ('{run_id}','{unrelated_id}')"
    )

    response = _read(run_id)

    assert response.status_code == 200
    row = response.json()[0]
    assert row["status"] == "recoverable"
    assert row["owner_token"] is None and row["lease_until"] is None
    assert row["owner_version"] == 2
    assert _sql(f"select status||':'||owner_version from public.crawl_runs where run_id='{unrelated_id}'") == "running:1"


def _locked_read_after_update(run_id: str, change: str) -> httpx.Response:
    lock_sql = (
        "begin; select run_id from public.crawl_runs "
        f"where run_id='{run_id}' for update; {change}; "
        "select 'locked'; select pg_sleep(2); commit;"
    )
    locker = subprocess.Popen(
        ["podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-q", "-A", "-t",
         "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert locker.stdin is not None and locker.stdout is not None
    locker.stdin.write(lock_sql)
    locker.stdin.close()
    assert locker.stdout.readline().strip() == run_id
    assert locker.stdout.readline().strip() == "locked"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(_read, run_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waiting = _sql(
                "select count(*) from pg_stat_activity where wait_event_type='Lock' "
                "and query ilike '%read_crawl_run_snapshot%'"
            )
            if int(waiting) > 0:
                break
            if result.done():
                pytest.fail("stale read returned without waiting for the locked current row")
            time.sleep(0.02)
        else:
            pytest.fail("stale read did not enter a PostgreSQL row-lock wait")
        response = result.result(timeout=5)
    assert locker.wait(timeout=5) == 0, locker.stderr.read() if locker.stderr else "locker failed"
    return response


@pytest.mark.parametrize("change", ["heartbeat", "completion", "new_owner", "expiry"])
def test_stale_reconciliation_rechecks_row_after_concurrent_change(change: str) -> None:
    run_id = _new_run()
    token = _sql(f"select owner_token::text from public.crawl_runs where run_id='{run_id}'")
    _sql(f"update public.crawl_runs set lease_until=clock_timestamp()-interval '1 second' where run_id='{run_id}'")
    if change == "heartbeat":
        concurrent_change = (
            f"update public.crawl_runs set lease_until=clock_timestamp()+interval '2 minutes' "
            f"where run_id='{run_id}'"
        )
    elif change == "completion":
        concurrent_change = (
            "update public.crawl_runs set status='completed', graph='{}'::jsonb, "
            "completed_at=clock_timestamp(), owner_token=null, "
            "owner_version=owner_version+1, lease_until=null "
            f"where run_id='{run_id}'"
        )
    elif change == "new_owner":
        concurrent_change = (
            f"do $$ begin perform public.claim_crawl_run('{run_id}','{uuid.uuid4()}'::uuid,1,90); end $$;"
        )
    else:
        concurrent_change = (
            f"update public.crawl_runs set expires_at=clock_timestamp()-interval '1 second' "
            f"where run_id='{run_id}'"
        )

    response = _locked_read_after_update(run_id, concurrent_change)

    assert response.status_code == 200, response.text
    row = response.json()[0]
    if change == "heartbeat":
        assert row["status"] == "running" and row["owner_version"] == 1
        assert row["lease_until"] is not None
    elif change == "completion":
        assert row["status"] == "completed" and row["owner_version"] == 2
    elif change == "new_owner":
        assert row["status"] == "running" and row["owner_version"] == 2
        assert row["owner_token"] != token
    else:
        assert row["status"] == "expired" and row["owner_version"] == 1
    assert _sql(f"select owner_version from public.crawl_runs where run_id='{run_id}'") == str(row["owner_version"])


def test_healthy_read_does_not_wait_for_held_row_lock() -> None:
    run_id = _new_run()
    lock_sql = (
        "begin; select run_id from public.crawl_runs "
        f"where run_id='{run_id}' for update; select 'locked'; select pg_sleep(2); commit;"
    )
    locker = subprocess.Popen(
        ["podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-q", "-A", "-t",
         "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert locker.stdin is not None and locker.stdout is not None
    locker.stdin.write(lock_sql)
    locker.stdin.close()
    assert locker.stdout.readline().strip() == run_id
    assert locker.stdout.readline().strip() == "locked"
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(_read, run_id).result(timeout=1)
    assert response.status_code == 200 and response.json()[0]["status"] == "running"
    assert locker.wait(timeout=5) == 0
