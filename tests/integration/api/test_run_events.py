from __future__ import annotations

import asyncio

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.crawler import CrawlRequest
from wikigraph.runs import (
    CrawlRun,
    InMemoryCrawlRunStore,
    RunEvent,
    RunProgressSnapshot,
    RunStateSnapshot,
    RunStatus,
)


def _supabase_state_row(*, owner_token: str, status: str) -> dict[str, object]:
    return {
        "run_id": "snapshot-id", "seed": "Argentina", "language": "en", "depth": 2,
        "node_cap": 37, "status": status,
        "progress": {"crawled": 1, "discovered": 2, "depth": 1, "recent": ["Argentina"]},
        "error": "canonical failure" if status == "failed" else None,
        "retry_state": None, "expires_at": None, "owner_token": owner_token,
        "owner_version": 5, "lease_until": "2030-01-02T03:00:00+00:00",
        "checkpoint": None, "graph": None, "truncated": None,
    }


class StateOnlyStore(InMemoryCrawlRunStore):
    def get(self, run_id: str):
        raise AssertionError("SSE must not hydrate through get")

    async def get_graph(self, run_id: str):
        raise AssertionError("SSE must not fetch the completed graph")

    def cleanup_expired(self):
        raise AssertionError("SSE must not run global maintenance")


async def test_events_resolves_unknown_run_from_canonical_state_only():
    app = create_app(run_store=StateOnlyStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/runs/missing/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_run"


class RemoteStateStore(StateOnlyStore):
    _remote_poll_seconds = 0.001

    def __init__(self, truncated: bool):
        super().__init__()
        self.states = [
            RunStateSnapshot(
                id="remote-run",
                request=CrawlRequest("Synthetic", 1, "en"),
                status=RunStatus.RUNNING,
                progress=RunProgressSnapshot(1, 2, 1, ("Synthetic",)),
                error=None,
                retry_state=None,
                expires_at=None,
                truncated=None,
            ),
            RunStateSnapshot(
                id="remote-run",
                request=CrawlRequest("Synthetic", 1, "en"),
                status=RunStatus.COMPLETED,
                progress=RunProgressSnapshot(1, 2, 1, ("Synthetic",)),
                error=None,
                retry_state=None,
                expires_at=None,
                truncated=truncated,
            ),
        ]

    async def get_state(self, run_id: str):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


@pytest.mark.parametrize("truncated", [True, False])
async def test_events_delivers_remote_initial_progress_then_canonical_terminal(truncated: bool):
    app = create_app(run_store=RemoteStateStore(truncated))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await asyncio.wait_for(
            client.get("/api/runs/remote-run/events"), timeout=1
        )

    assert response.status_code == 200
    assert "event: progress" in response.text
    assert '"crawled": 1' in response.text
    assert "event: completed\ndata: {\"truncated\": " + str(truncated).lower() + "}" in response.text
    assert "event: graph" not in response.text


@pytest.mark.asyncio
async def test_events_heartbeats_unchanged_remote_progress_until_terminal(monkeypatch):
    monkeypatch.setattr("wikigraph.app.SSE_HEARTBEAT_SECONDS", 0)
    store = RemoteStateStore(truncated=False)
    app = create_app(run_store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await asyncio.wait_for(
            client.get("/api/runs/remote-run/events"), timeout=1
        )

    assert response.status_code == 200
    assert response.text.count("event: progress") == 1
    assert response.text.count(": ping\n\n") >= 1
    assert "event: completed" in response.text
    assert "heartbeat_crawl_run" not in response.text


async def test_events_request_cancellation_while_connected_finishes_promptly(monkeypatch):
    monkeypatch.setattr("wikigraph.app.SSE_HEARTBEAT_SECONDS", 0)

    class OngoingStateStore(StateOnlyStore):
        _remote_poll_seconds = 0.001

        def __init__(self):
            super().__init__()
            self.read_started = asyncio.Event()

        async def get_state(self, run_id: str):
            self.read_started.set()
            return RunStateSnapshot(
                run_id, CrawlRequest("Synthetic", 1, "en"), RunStatus.RUNNING,
                RunProgressSnapshot(1, 2, 1, ("Synthetic",)), None, None, None,
            )

    store = OngoingStateStore()
    app = create_app(run_store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.get("/api/runs/ongoing/events"))
        await asyncio.wait_for(store.read_started.wait(), timeout=1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=1)


async def test_owner_change_cancels_obsolete_worker_and_suppresses_its_terminal_event():
    from wikigraph.runs import CrawlRun, SupabaseCrawlRunStore

    old_owner = "11111111-1111-1111-1111-111111111111"
    new_owner = "22222222-2222-2222-2222-222222222222"
    rows = [
        _supabase_state_row(owner_token=new_owner, status="running"),
        _supabase_state_row(owner_token=new_owner, status="failed"),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[rows.pop(0) if len(rows) > 1 else rows[0]])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as database:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=database)
        worker = CrawlRun(
            "snapshot-id", CrawlRequest("Argentina", 2, "en"), owner_token=old_owner,
            owner_version=4,
        )
        worker.task = asyncio.create_task(asyncio.sleep(30))
        worker.publish(RunEvent("failed", {"error": "obsolete local terminal"}))
        store._runs[worker.id] = worker
        app = create_app(run_store=store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await asyncio.wait_for(
                client.get("/api/runs/snapshot-id/events"), timeout=1
            )
        assert response.status_code == 200
        assert worker.task.cancelled()
        assert "event: failed" in response.text
        assert "obsolete local terminal" not in response.text
        await store.shutdown()


async def test_local_completed_event_replay_keeps_canonical_truncated(stub):
    stub.add_page("Synthetic")
    store = StateOnlyStore()
    run = store.start_run(CrawlRequest("Synthetic", 0, "en"), stub.transport)
    await run.wait_done()
    app = create_app(run_store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/runs/{run.id}/events")

    assert response.status_code == 200
    assert f'event: completed\ndata: {{"truncated": {str(run.result.truncated).lower()}}}' in response.text
    await store.shutdown()


async def test_canonical_expiry_suppresses_queued_local_terminal_event():
    store = InMemoryCrawlRunStore()
    run = CrawlRun("local-run", CrawlRequest("Synthetic", 1, "en"))
    run.publish(RunEvent("failed", {"error": "stale local failure"}))
    run._done.set()
    store._runs[run.id] = run
    progress = RunProgressSnapshot(0, 1, 0, ())
    store.states = [
        RunStateSnapshot(run.id, run.request, RunStatus.RUNNING, progress, None, None, None),
        RunStateSnapshot(run.id, run.request, RunStatus.EXPIRED, progress, None, None, None),
    ]

    async def get_state(run_id: str):
        return store.states.pop(0) if len(store.states) > 1 else store.states[0]

    store.get_state = get_state
    app = create_app(run_store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await asyncio.wait_for(
            client.get("/api/runs/local-run/events"), timeout=1
        )

    assert response.status_code == 200
    assert "event: expired" in response.text
    assert "stale local failure" not in response.text
