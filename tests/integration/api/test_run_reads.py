from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.runs import SupabaseCrawlRunStore


def snapshot_row(*, status: str = "completed", graph: object = None, checkpoint: object = None) -> dict[str, object]:
    return {
        "run_id": "http-run",
        "seed": "Argentina",
        "language": "en",
        "depth": 2,
        "node_cap": 37,
        "status": status,
        "progress": {"crawled": 3, "discovered": 8, "depth": 1, "recent": ["Argentina"]},
        "error": None,
        "retry_state": {"attempts": 2, "retry_after": 1.5},
        "expires_at": "2030-01-02T03:04:05+00:00",
        "owner_token": "internal-owner-secret",
        "owner_version": 9,
        "lease_until": "2030-01-02T03:00:00+00:00",
        "checkpoint": checkpoint,
        "graph": graph,
        "truncated": False if status == "completed" else None,
    }


GRAPH = {
    "nodes": [{"title": "Argentina", "level": 0, "is_seed": True}],
    "edges": [],
    "truncated": False,
    "crawled": 3,
    "discovered": 8,
    "community_ids": {"Argentina": 4},
    "community_count": 5,
    "modularity": 0.25,
}
CHECKPOINT = {
    "version": 1,
    "level": 1,
    "frontier": ["Argentina"],
    "batch_start": 0,
    "next_frontier": [],
    "current_batch": [],
    "continuation": None,
    "nodes": [{"title": "Argentina", "level": 0, "is_seed": True}],
    "edges": [],
    "truncated": False,
    "crawled": 0,
    "recent": [],
}


@pytest.mark.asyncio
async def test_http_reads_use_purpose_rpc_and_preserve_public_shapes() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        view = json.loads(request.content)["p_view"]
        if view == "state":
            row = snapshot_row()
        elif view == "preview":
            row = snapshot_row(graph=GRAPH)
        else:
            row = snapshot_row(graph=GRAPH)
        return httpx.Response(200, json=[row])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as db:
        store = SupabaseCrawlRunStore("https://db.example", "test-key", async_client=db)
        app = create_app(run_store=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            state = await client.get("/api/runs/http-run")
            preview = await client.get("/api/runs/http-run/preview")
            graph = await client.get("/api/runs/http-run/graph")

    assert state.status_code == 200
    assert state.json() == {
        "runId": "http-run", "seed": "Argentina", "language": "en", "depth": 2,
        "nodeCap": 37, "status": "completed", "crawled": 3, "discovered": 8,
        "currentDepth": 1, "recent": ["Argentina"], "error": None, "retryAfter": 1.5,
    }
    assert preview.status_code == graph.status_code == 200
    assert preview.json()["complete"] is True
    assert preview.json()["communityCount"] == 5
    assert graph.json()["nodes"][0]["communityId"] == 4
    assert [json.loads(request.content) for request in requests] == [
        {"p_run_id": "http-run", "p_view": "state"},
        {"p_run_id": "http-run", "p_view": "preview"},
        {"p_run_id": "http-run", "p_view": "state"},
        {"p_run_id": "http-run", "p_view": "graph"},
    ]
    assert all(request.method == "POST" for request in requests)
    assert all(b"internal-owner-secret" not in request.content for request in requests)
    assert all("internal-owner-secret" not in response.text for response in (state, preview, graph))


@pytest.mark.asyncio
async def test_incomplete_preview_uses_checkpoint_snapshot() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert json.loads(request.content) == {"p_run_id": "http-run", "p_view": "preview"}
        return httpx.Response(200, json=[snapshot_row(status="running", checkpoint=CHECKPOINT)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as db:
        store = SupabaseCrawlRunStore("https://db.example", "test-key", async_client=db)
        app = create_app(run_store=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/runs/http-run/preview")

    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert response.json()["nodes"] == [
        {"title": "Argentina", "level": 0, "isSeed": True, "communityId": 0}
    ]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_http_reads_keep_unknown_and_expired_contracts_without_cleanup() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        run_id = json.loads(request.content)["p_run_id"]
        if run_id == "unknown":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[snapshot_row(status="expired", graph={"secret": "hidden"}, checkpoint={"secret": "hidden"})])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as db:
        store = SupabaseCrawlRunStore("https://db.example", "test-key", async_client=db)
        app = create_app(run_store=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for suffix in ("", "/preview", "/graph"):
                unknown = await client.get(f"/api/runs/unknown{suffix}")
                expired = await client.get(f"/api/runs/http-run{suffix}")
                assert unknown.status_code == 404
                assert unknown.json()["error"]["code"] == "unknown_run"
                assert expired.status_code == 410
                assert expired.json()["error"]["code"] == "run_expired"
    assert [json.loads(request.content)["p_view"] for request in requests] == [
        "state", "state", "preview", "preview", "state", "state"
    ]


@pytest.mark.asyncio
async def test_graph_wait_polls_state_then_reads_completed_graph_once() -> None:
    requests: list[httpx.Request] = []
    states = iter(("running", "completed"))

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        view = json.loads(request.content)["p_view"]
        if view == "state":
            return httpx.Response(200, json=[snapshot_row(status=next(states))])
        assert view == "graph"
        return httpx.Response(200, json=[snapshot_row(graph=GRAPH)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as db:
        store = SupabaseCrawlRunStore("https://db.example", "test-key", async_client=db)
        app = create_app(run_store=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with asyncio.timeout(5):
                response = await client.get("/api/runs/http-run/graph")

    assert response.status_code == 200
    assert [json.loads(request.content)["p_view"] for request in requests] == ["state", "state", "graph"]
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_http_read_persistence_errors_are_redacted_as_503() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="private backend details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as db:
        store = SupabaseCrawlRunStore("https://db.example", "test-key", async_client=db)
        app = create_app(run_store=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/runs/http-run")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "persistence_unavailable",
        "message": "Crawl persistence is unavailable. Try again later.",
    }
    assert "private backend details" not in response.text
