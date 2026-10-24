from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

import httpx
import pytest

from wikigraph.crawler import CrawlRequest
from wikigraph.runs import (
    PersistenceError,
    RunStatus,
    SupabaseCrawlRunStore,
)


def _row(
    *, status: str = "running", checkpoint: object = None, graph: object = None,
    truncated: object = None,
    owner_token: str = "11111111-1111-1111-1111-111111111111",
) -> dict[str, object]:
    return {
        "run_id": "snapshot-id",
        "seed": "Argentina",
        "language": "en",
        "depth": 2,
        "node_cap": 37,
        "status": status,
        "progress": {"crawled": 3, "discovered": 8, "depth": 1, "recent": ["A", "B"]},
        "error": None,
        "retry_state": {"attempts": 2, "retry_after": 1.5},
        "expires_at": "2030-01-02T03:04:05+00:00",
        "owner_token": owner_token,
        "owner_version": 4,
        "lease_until": "2030-01-02T03:00:00+00:00",
        "checkpoint": checkpoint,
        "graph": graph,
        "truncated": truncated,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("view,method", [
    ("state", "get_state"), ("preview", "get_preview"), ("graph", "get_graph")
])
async def test_async_read_uses_one_purpose_rpc_and_detached_snapshot(view: str, method: str) -> None:
    requests: list[httpx.Request] = []
    checkpoint = {
        "version": 1, "level": 1, "frontier": ["Argentina"], "batch_start": 0,
        "next_frontier": [], "current_batch": [], "continuation": None,
        "nodes": [{"title": "Argentina", "level": 0, "is_seed": True}],
        "edges": [], "truncated": False, "crawled": 0, "recent": [],
    }
    graph = {
        "nodes": [{"title": "Argentina", "level": 0, "is_seed": True}],
        "edges": [], "truncated": False, "crawled": 3, "discovered": 8,
        "community_ids": {"Argentina": 0}, "community_count": 1, "modularity": 0.0,
    }
    payload = _row(
        status="completed" if view == "graph" else "running",
        checkpoint=checkpoint if view == "preview" else None,
        graph=graph if view == "graph" else None,
        truncated=False if view == "graph" else None,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[payload])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = SupabaseCrawlRunStore("https://db.example", "test-key", async_client=client)
    try:
        snapshot = await getattr(store, method)("snapshot-id")
    finally:
        await client.aclose()

    assert snapshot is not None
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://db.example/rest/v1/rpc/read_crawl_run_snapshot"
    assert request.content == (
        b'{"p_run_id":"snapshot-id","p_view":"' + view.encode() + b'"}'
    )
    assert json.loads(request.content) == {"p_run_id": "snapshot-id", "p_view": view}
    assert request.headers["Authorization"] == "Bearer test-key"
    state = snapshot if view == "state" else snapshot.state
    assert state.id == "snapshot-id"
    assert state.request == CrawlRequest("Argentina", 2, "en", node_cap=37)
    assert state.progress.recent == ("A", "B")
    assert state.truncated is (False if view == "graph" else None)
    assert state.retry_state is not None
    assert state.retry_state.attempts == 2
    assert state.expires_at == datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    if view == "state":
        assert not hasattr(snapshot, "checkpoint")
        assert not hasattr(snapshot, "graph")
    elif view == "preview":
        assert snapshot.checkpoint is not None
        assert snapshot.checkpoint.frontier == ("Argentina",)
        assert snapshot.graph is snapshot.communities is None
    else:
        assert snapshot.graph is not None
        assert snapshot.graph.nodes[0].title == "Argentina"
        assert snapshot.communities is not None
        assert snapshot.communities.ids == (("Argentina", 0),)


@pytest.mark.asyncio
async def test_purpose_reads_never_call_cleanup_reconciliation_or_legacy_hydration(monkeypatch) -> None:
    requests: list[httpx.Request] = []
    checkpoint = {
        "nodes": [], "edges": [], "frontier": ["Argentina"], "next_frontier": [],
        "current_batch": [], "recent": [], "continuation": None,
        "version": 1, "level": 1, "batch_start": 0, "truncated": False,
        "crawled": 0,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        view = json.loads(request.content)["p_view"]
        payload = _row(checkpoint=checkpoint if view == "preview" else ["must not hydrate"])
        payload["graph"] = {"unrelated": "must not hydrate"}
        return httpx.Response(200, json=[payload])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        def forbidden(*args, **kwargs):
            raise AssertionError("read path entered global cleanup or legacy hydration")
        monkeypatch.setattr(store, "cleanup_expired", forbidden)
        monkeypatch.setattr(store, "_rpc_request", forbidden)
        monkeypatch.setattr(store, "_request", forbidden)
        assert await store.get_state("snapshot-id") is not None
        assert await store.get_preview("snapshot-id") is not None
        assert await store.get_graph("snapshot-id") is not None

    assert [json.loads(request.content) for request in requests] == [
        {"p_run_id": "snapshot-id", "p_view": view}
        for view in ("state", "preview", "graph")
    ]
    assert all(request.url.path.endswith("/rpc/read_crawl_run_snapshot") for request in requests)


@pytest.mark.asyncio
async def test_state_survives_concurrent_lease_renewal_without_fencing_worker() -> None:
    entered = asyncio.Event()
    renewed = asyncio.Event()
    requests: list[httpx.Request] = []
    row = _row()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        await renewed.wait()
        return httpx.Response(200, json=[row.copy()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        from wikigraph.runs import CrawlRun

        worker = CrawlRun(
            "snapshot-id", CrawlRequest("Argentina", 2, "en"),
            owner_token=row["owner_token"], owner_version=4,
        )
        worker.task = asyncio.create_task(asyncio.sleep(30))
        store._runs[worker.id] = worker
        read = asyncio.create_task(store.get_state(worker.id))
        await entered.wait()
        row["lease_until"] = "2030-01-02T03:30:00+00:00"
        renewed.set()
        state = await read
        assert state is not None and state.status is RunStatus.RUNNING
        assert state.owner_version == 4
        assert row["lease_until"] == "2030-01-02T03:30:00+00:00"
        assert not worker._ownership_lost
        assert not worker.task.cancelled()
        assert len(requests) == 1
        assert requests[0].url.path.endswith("/rpc/read_crawl_run_snapshot")
        assert "heartbeat_crawl_run" not in str(requests[0].url)
        worker.task.cancel()
        await asyncio.gather(worker.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_invalid_snapshot_schema_fails_loudly() -> None:
    invalid = _row()
    del invalid["node_cap"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[invalid])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        with pytest.raises(PersistenceError, match="invalid run snapshot"):
            await store.get_state("snapshot-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_completed_snapshot_preserves_persisted_truncated_boolean(value: bool) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row(status="completed", truncated=value)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        state = await store.get_state("snapshot-id")
    assert state is not None and state.truncated is value


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "true", 1, 0])
async def test_completed_snapshot_requires_real_persisted_truncated_boolean(value: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row(status="completed", truncated=value)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        with pytest.raises(PersistenceError, match="invalid run snapshot"):
            await store.get_state("snapshot-id")


@pytest.mark.asyncio
async def test_completed_snapshot_requires_truncated_column() -> None:
    payload = _row(status="completed", truncated=True)
    del payload["truncated"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[payload])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        with pytest.raises(PersistenceError, match="invalid run snapshot"):
            await store.get_state("snapshot-id")


@pytest.mark.asyncio
async def test_state_ignores_malformed_reconstructive_payloads() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row(checkpoint=["invalid"], graph="invalid")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        assert (await store.get_state("snapshot-id")).status is RunStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["state", "preview", "graph"])
async def test_unknown_and_expired_reads_preserve_empty_and_payload_suppression(view: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["p_run_id"] == "unknown":
            return httpx.Response(200, json=[])
        expired = _row(
            status="expired", checkpoint={"secret": "checkpoint"}, graph={"secret": "graph"}
        )
        expired["progress"] = None
        return httpx.Response(200, json=[expired])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        assert await getattr(store, f"get_{view}")("unknown") is None
        expired = await getattr(store, f"get_{view}")("expired")
    assert expired is not None
    state = expired if view == "state" else expired.state
    assert state.status is RunStatus.EXPIRED
    if view == "preview":
        assert expired.checkpoint is expired.graph is expired.communities is None
    if view == "graph":
        assert expired.graph is expired.communities is None


@pytest.mark.asyncio
async def test_snapshot_transport_failure_is_not_replayed() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("ambiguous response", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        with pytest.raises(PersistenceError, match="persistence operation failed"):
            await store.get_state("snapshot-id")
    assert requests == 1


@pytest.mark.asyncio
async def test_canonical_expiry_stops_local_worker_without_returning_it() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row(status="expired")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        from wikigraph.runs import CrawlRun

        local = CrawlRun(
            "snapshot-id", CrawlRequest("Argentina", 2, "en"),
            owner_token="11111111-1111-1111-1111-111111111111", owner_version=4,
        )
        local.task = asyncio.create_task(asyncio.sleep(30))
        store._runs[local.id] = local
        snapshot = await store.get_state(local.id)
        assert snapshot is not None and snapshot.status is RunStatus.EXPIRED
        assert snapshot is not local
        assert local._ownership_lost
        await asyncio.gather(local.task, return_exceptions=True)
        assert local.task.cancelled()


@pytest.mark.asyncio
async def test_stale_snapshot_does_not_cancel_replacement_local_worker() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_row(owner_token="different-owner")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        from wikigraph.runs import CrawlRun

        old = CrawlRun(
            "snapshot-id", CrawlRequest("Argentina", 2, "en"),
            owner_token="11111111-1111-1111-1111-111111111111", owner_version=4,
        )
        replacement = CrawlRun(
            "snapshot-id", CrawlRequest("Argentina", 2, "en"),
            owner_token="different-owner", owner_version=4,
        )
        old.task = asyncio.create_task(asyncio.sleep(30))
        replacement.task = asyncio.create_task(asyncio.sleep(30))
        store._runs[old.id] = replacement
        snapshot = await store.get_state(old.id)
        assert snapshot is not None
        assert not old.task.cancelled()
        assert not replacement.task.cancelled()
        old.task.cancel()
        replacement.task.cancel()
        await asyncio.gather(old.task, replacement.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_snapshot_http_does_not_block_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.to_thread(release.wait, 2)
        return httpx.Response(200, json=[_row()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseCrawlRunStore("https://db.example", "key", async_client=client)
        read = asyncio.create_task(store.get_state("snapshot-id"))
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        release.set()
        assert await read is not None


def test_runtime_schema_validation_probes_snapshot_rpc_and_truncated_column() -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    class Client:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            requests.append((method, url, dict(kwargs)))
            return httpx.Response(200, request=httpx.Request(method, url), json=[])

    store = SupabaseCrawlRunStore("https://db.example", "key", client=Client())
    store.validate_runtime_schema()
    assert any(
        method == "GET" and kwargs.get("params") == {"select": "run_id,truncated", "limit": "0"}
        for method, _, kwargs in requests
    )
    assert any(
        method == "POST" and url.endswith("/rpc/read_crawl_run_snapshot")
        for method, url, _ in requests
    )
