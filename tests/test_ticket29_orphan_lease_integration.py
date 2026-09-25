"""Prove lease recovery after killing a separate worker process.

Requires the isolated ticket28 PostgREST harness with migration 00007 applied.
The admin SQL connection is used only to expire this test's lease; ownership
transitions themselves go through the production RPCs.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wikigraph.app import create_app
from wikigraph.runs import PersistenceError, SupabaseCrawlRunStore

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
KEY = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
pytestmark = pytest.mark.skipif(not URL or not KEY, reason="local PostgREST harness is not configured")


def _headers() -> dict[str, str]:
    assert KEY
    return {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def _store(*, heartbeat_interval_seconds: float = 30.0) -> SupabaseCrawlRunStore:
    assert URL and KEY
    return SupabaseCrawlRunStore(
        URL, KEY, rest_path="", heartbeat_interval_seconds=heartbeat_interval_seconds
    )


def _row(run_id: str) -> dict[str, object]:
    assert URL
    response = httpx.get(
        f"{URL}/crawl_runs", params={"run_id": f"eq.{run_id}"},
        headers=_headers(), timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    assert len(rows) == 1
    return rows[0]


def _sql(query: str) -> str:
    result = subprocess.run(
        ["podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-A", "-t",
         "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph"],
        input=query, text=True, capture_output=True, check=True, timeout=15,
    )
    return result.stdout.strip()


async def _eventually(read, predicate, *, timeout: float = 12) -> object:
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = await asyncio.to_thread(read)
        if predicate(value):
            return value
        await asyncio.sleep(0.025)
    raise AssertionError(f"condition was not reached before the bounded deadline; last value: {value!r}")


async def test_sigkill_recovery_keeps_checkpoint_and_fences_old_owner(tmp_path: Path) -> None:
    assert URL and KEY
    marker = tmp_path / "worker.json"
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(marker)],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    observer = _store()
    run_id = None
    try:
        await _eventually(marker.exists, bool)
        worker = json.loads(marker.read_text())
        run_id = worker["run_id"]
        row = await _eventually(
            lambda: _row(run_id),
            lambda value: value["status"] == "running"
            and value["checkpoint"].get("crawled", 0) >= 1
            and "Leaf" in [node["title"] for node in value["checkpoint"]["nodes"]],
        )
        assert process.poll() is None, "worker should be blocked in the next upstream operation"
        assert row["owner_token"] and row["lease_until"]
        checkpoint = row["checkpoint"]
        old_token = row["owner_token"]
        old_version = row["owner_version"]

        # A second process can observe, but cannot steal, an unexpired owner's run.
        live = observer.get(run_id)
        assert live is not None and live.status.value == "running"
        with pytest.raises(PersistenceError, match="Only recoverable"):
            observer.retry_run(run_id, None)
        assert _row(run_id)["owner_token"] == old_token
        # The worker is still blocked upstream, but its independent heartbeat
        # extends the database lease before the first lease can expire.
        renewed = await _eventually(
            lambda: _row(run_id),
            lambda value: value["lease_until"] != row["lease_until"],
            timeout=8,
        )
        assert renewed["owner_token"] == old_token

        # A persistence outage is fail-closed; it cannot manufacture a local run.
        unavailable = SupabaseCrawlRunStore("http://127.0.0.1:1", KEY, rest_path="")
        try:
            with pytest.raises(PersistenceError):
                unavailable.get(run_id)
        finally:
            await unavailable.shutdown()

        # This is an actual process death: no task cancellation/finally path runs.
        process.send_signal(signal.SIGKILL)
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert _row(run_id)["status"] == "running"
        assert _row(run_id)["checkpoint"] == checkpoint

        # Expire only our isolated fixture with its trusted local DB role.
        _sql(
            "update public.crawl_runs set lease_until=clock_timestamp()-interval '1 second' "
            f"where run_id='{run_id}'"
        )
        await observer.shutdown()
        observer = _store()
        recovered = observer.get(run_id)
        assert recovered is not None and recovered.status.value == "recoverable"
        assert recovered._checkpoint is not None
        assert recovered._checkpoint.crawled == checkpoint["crawled"]

        app_store = _store()
        app = create_app(run_store=app_store)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                events = await client.get(f"/api/runs/{run_id}/events", timeout=5)
                assert events.status_code == 200
                assert "event: recoverable" in events.text
                graph = await client.get(f"/api/runs/{run_id}/graph", timeout=5)
                assert graph.status_code == 409
                assert graph.json()["error"]["code"] == "run_recoverable"

        stale_payloads = [
            ("progress", {"progress": {"crawled": 999}}),
            ("checkpoint", {"progress": {}, "checkpoint": {"crawled": 999}}),
            ("completed", {"graph": {"nodes": [], "edges": []}}),
            ("failed", {"error": "stale"}),
            ("recoverable", {"error": "stale"}),
        ]
        for operation, patch in stale_payloads:
            response = httpx.post(
                f"{URL}/rpc/fenced_update_crawl_run", headers=_headers(), timeout=10,
                json={"p_run_id": run_id, "p_owner_token": old_token,
                      "p_owner_version": old_version, "p_operation": operation,
                      "p_patch": patch},
            )
            assert response.status_code == 200 and response.json() is False

        raw_patch = httpx.patch(
            f"{URL}/crawl_runs", headers=_headers(), params={"run_id": f"eq.{run_id}"},
            json={"status": "completed"}, timeout=10,
        )
        assert raw_patch.status_code in {401, 403}

        # Retrying the identical run resumes the durable frontier and emits one
        # copy of every node/edge. The fake upstream is defined by the child mode.
        from tests.stub import FakeMediaWiki
        stub = FakeMediaWiki()
        stub.add_page(worker["seed"], links=[(0, "Leaf")])
        stub.add_page("Leaf")
        retried = observer.retry_run(run_id, stub.transport)
        assert retried.id == run_id
        await asyncio.wait_for(retried.wait_done(), timeout=20)
        assert retried.status.value == "completed"
        assert [node.title for node in retried.result.nodes].count("Leaf") == 1
        assert len(retried.result.edges) == 1
        final = _row(run_id)
        assert final["status"] == "completed"
        assert [node["title"] for node in final["graph"]["nodes"]].count("Leaf") == 1
        assert len(final["graph"]["edges"]) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        await observer.shutdown()
        if run_id is not None:
            _sql(f"delete from public.crawl_runs where run_id='{run_id}'")


async def _run_worker(marker: Path) -> None:
    from tests.stub import FakeMediaWiki
    from wikigraph.crawler import CrawlRequest

    stub = FakeMediaWiki()
    seed = "Ticket29" + uuid.uuid4().hex[:8]
    stub.add_page(seed, links=[(0, "Leaf")])
    stub.add_page("Leaf")
    stub.gate("Leaf")
    store = _store(heartbeat_interval_seconds=5)
    run = store.start_run(CrawlRequest(seed=seed, depth=2, language="en", node_cap=10), stub.transport)
    temporary_marker = marker.with_suffix(".tmp")
    temporary_marker.write_text(json.dumps({"run_id": run.id, "seed": seed}))
    temporary_marker.replace(marker)
    await asyncio.Event().wait()


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    asyncio.run(_run_worker(Path(sys.argv[2])))
