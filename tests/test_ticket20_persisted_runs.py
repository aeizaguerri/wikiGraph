"""Ticket 20 - completed Crawl runs survive replacing the application process."""

from __future__ import annotations

import asyncio
import json

import httpx

from tests.helpers import collect_events, fetch_graph
from tests.stub import FakeMediaWiki
from wikigraph.app import create_app
from wikigraph.runs import SupabaseCrawlRunStore


class PostgrestRuns:
    """Small PostgREST-shaped transport; no application fallback is involved."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            row = json.loads(request.content)
            self.rows[str(row["run_id"])] = row
            return httpx.Response(201, json=[row])
        run_id = request.url.params.get("run_id", "").removeprefix("eq.")
        if request.method == "GET":
            row = self.rows.get(run_id)
            return httpx.Response(200, json=[] if row is None else [row])
        if request.method == "PATCH":
            row = self.rows[run_id]
            row.update(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(405)


async def test_completed_graph_is_reopened_from_a_fresh_store(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub", links=[(0, "Leaf")])
    database = PostgrestRuns()

    def store() -> SupabaseCrawlRunStore:
        return SupabaseCrawlRunStore(
            "https://supabase.test",
            "server-only-test-key",
            client=httpx.Client(transport=database.transport()),
        )

    first = create_app(
        mediawiki_transport=stub.transport,
        run_store=store(),
    )
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )
            run_id = response.json()["runId"]
            await asyncio.sleep(0.05)
            events = await collect_events(client, run_id)
            assert events[-1]["type"] == "completed"

    second = create_app(mediawiki_transport=stub.transport, run_store=store())
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            reopened = await fetch_graph(client, run_id)
            replayed = await collect_events(client, run_id)

    assert reopened["runId"] == run_id
    assert {node["title"] for node in reopened["nodes"]} == {"Hub", "Leaf"}
    assert replayed[-1]["type"] == "completed"


async def test_identical_launches_still_create_distinct_persisted_runs(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")
    database = PostgrestRuns()
    store = SupabaseCrawlRunStore(
        "https://supabase.test",
        "server-only-test-key",
        client=httpx.Client(transport=database.transport()),
    )
    app = create_app(mediawiki_transport=stub.transport, run_store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )
            second = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )

    assert first.status_code == second.status_code == 201
    assert first.json()["runId"] != second.json()["runId"]
    assert len(database.rows) == 2


async def test_persistence_failure_is_not_replaced_by_an_in_memory_run(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")

    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "database unavailable"})

    store = SupabaseCrawlRunStore(
        "https://supabase.test",
        "server-only-test-key",
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
    )
    app = create_app(mediawiki_transport=stub.transport, run_store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "persistence_unavailable"
