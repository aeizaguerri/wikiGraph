from __future__ import annotations

import asyncio
import os
import subprocess
import uuid

import pytest

from wikigraph.crawler import CrawlRequest
from wikigraph.runs import RunStatus, SupabaseCrawlRunStore


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL") != "http://127.0.0.1:33028"
    or not os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
    or os.environ.get("WIKIGRAPH_TICKET28_LOCAL_PSQL_CLEANUP") != "1",
    reason="ticket28 loopback credentials and explicit local psql cleanup opt-in are required",
)
async def test_local_postgrest_snapshot_adapter_smoke() -> None:
    url = os.environ["WIKIGRAPH_TICKET28_POSTGREST_URL"]
    assert url == "http://127.0.0.1:33028", "refusing non-loopback fixture writes"
    key = os.environ["WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT"]
    assert os.environ.get("WIKIGRAPH_TICKET28_LOCAL_PSQL_CLEANUP") == "1"
    store = SupabaseCrawlRunStore(url, key, rest_path="")
    run_id = "u2b-adapter-" + uuid.uuid4().hex
    token = str(uuid.uuid4())
    created = False
    try:
        response = await store._async_client.post(
            f"{url}/rpc/create_owned_crawl_run",
            headers=store._headers(),
            json={
                "p_run_id": run_id, "p_seed": "Adapter smoke", "p_language": "en",
                "p_depth": 1, "p_node_cap": 19,
                "p_checkpoint": {"version": 1, "nodes": [], "edges": []},
                "p_owner_token": token, "p_attempt_quota": 120,
            },
        )
        response.raise_for_status()
        created = True

        snapshot = await store.get_state(run_id)
        assert snapshot is not None
        assert snapshot.id == run_id
        assert snapshot.request == CrawlRequest("Adapter smoke", 1, "en", node_cap=19)
        assert snapshot.status is RunStatus.RUNNING
        assert snapshot.owner_token == token
    finally:
        try:
            deleted = await asyncio.to_thread(_delete_local_smoke_fixture, run_id)
            assert deleted == ([run_id] if created else [])
        finally:
            await store.shutdown()


def _delete_local_smoke_fixture(run_id: str) -> list[str]:
    """Delete only this smoke fixture through the opt-in local admin harness."""
    assert os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL") == "http://127.0.0.1:33028"
    assert os.environ.get("WIKIGRAPH_TICKET28_LOCAL_PSQL_CLEANUP") == "1"
    container = subprocess.run(
        ["podman", "inspect", "--format", "{{.Name}}", "ticket28-postgres"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert container.stdout.strip() == "ticket28-postgres"
    result = subprocess.run(
        [
            "podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-A", "-t", "-q",
            "-v", "ON_ERROR_STOP=1", "-v", f"run_id={run_id}",
            "-U", "postgres", "-d", "wikigraph",
        ],
        input="DELETE FROM public.crawl_runs WHERE run_id = :'run_id' RETURNING run_id;\n",
        capture_output=True,
        check=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]
