"""Ticket 18 - the Crawl run store is replaceable at the application seam."""

from __future__ import annotations

import httpx

from tests.support.api import collect_events, fetch_graph
from tests.support.mediawiki import FakeMediaWiki
from wikigraph.crawler import CrawlRequest
from wikigraph.runs import (
    CrawlRunHandle,
    InMemoryCrawlRunStore,
    RunCompletedGraphSnapshot,
    RunPreviewSnapshot,
    RunStateSnapshot,
)


class RecordingRunStore:
    """A store implementation that proves the app uses the injected seam."""

    def __init__(self) -> None:
        self._delegate = InMemoryCrawlRunStore()
        self.lookups: list[str] = []
        self.purpose_reads: list[tuple[str, str]] = []
        self.worker_accesses: list[str] = []
        self.shutdown_called = False

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRunHandle:
        return self._delegate.start_run(request, transport)

    def get(self, run_id: str) -> CrawlRunHandle | None:
        self.lookups.append(run_id)
        return self._delegate.get(run_id)

    def get_local_worker(self, run_id: str) -> CrawlRunHandle | None:
        self.worker_accesses.append(run_id)
        return self._delegate.get_local_worker(run_id)

    async def get_state(self, run_id: str) -> RunStateSnapshot | None:
        self.purpose_reads.append(("state", run_id))
        return await self._delegate.get_state(run_id)

    async def get_preview(self, run_id: str) -> RunPreviewSnapshot | None:
        self.purpose_reads.append(("preview", run_id))
        return await self._delegate.get_preview(run_id)

    async def get_graph(self, run_id: str) -> RunCompletedGraphSnapshot | None:
        self.purpose_reads.append(("graph", run_id))
        return await self._delegate.get_graph(run_id)

    async def shutdown(self) -> None:
        self.shutdown_called = True
        await self._delegate.shutdown()


async def test_api_uses_an_injected_store_without_changing_run_experience(
    stub: FakeMediaWiki,
) -> None:
    from wikigraph.app import create_app

    stub.add_page("Hub", links=[(0, "Leaf")])
    store = RecordingRunStore()
    app = create_app(mediawiki_transport=stub.transport, run_store=store)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )

            assert response.status_code == 201
            run_id = response.json()["runId"]
            events = await collect_events(client, run_id)
            preview_response = await client.get(f"/api/runs/{run_id}/preview")
            graph = await fetch_graph(client, run_id)

    assert events[-1] == {"type": "completed", "data": {"truncated": False}}
    assert preview_response.status_code == 200
    assert {node["title"] for node in preview_response.json()["nodes"]} == {"Hub", "Leaf"}
    assert {node["title"] for node in graph["nodes"]} == {"Hub", "Leaf"}
    assert {purpose for purpose, found_id in store.purpose_reads if found_id == run_id} == {
        "state",
        "preview",
        "graph",
    }
    assert store.lookups == []
    assert store.worker_accesses
    assert store.shutdown_called
