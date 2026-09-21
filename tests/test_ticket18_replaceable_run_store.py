"""Ticket 18 - the Crawl run store is replaceable at the application seam."""

from __future__ import annotations

import httpx

from tests.helpers import collect_events, fetch_graph
from tests.stub import FakeMediaWiki
from wikigraph.crawler import CrawlRequest
from wikigraph.runs import CrawlRun, InMemoryCrawlRunStore


class RecordingRunStore:
    """A store implementation that proves the app uses the injected seam."""

    def __init__(self) -> None:
        self._delegate = InMemoryCrawlRunStore()
        self.lookups: list[str] = []
        self.shutdown_called = False

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRun:
        return self._delegate.start_run(request, transport)

    def get(self, run_id: str) -> CrawlRun | None:
        self.lookups.append(run_id)
        return self._delegate.get(run_id)

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
            graph = await fetch_graph(client, run_id)

    assert events[-1] == {"type": "completed", "data": {"truncated": False}}
    assert {node["title"] for node in graph["nodes"]} == {"Hub", "Leaf"}
    assert store.lookups == [run_id, run_id]
    assert store.shutdown_called
