"""Ticket 20 - completed Crawl runs survive replacing the application process."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.helpers import collect_events, fetch_graph
from tests.stub import FakeMediaWiki
from wikigraph.app import _event_stream, create_app
from wikigraph.crawler import CrawlRequest
from wikigraph.runs import PersistenceError, SupabaseCrawlRunStore


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
            return httpx.Response(200, json=[row])
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


async def test_active_progress_is_persisted_before_terminal_sse(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub", links=[(0, "A"), (0, "B")])
    gate = stub.gate("B")
    stub.add_page("A")
    stub.add_page("B")
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
            response = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 2, "language": "es"}
            )
            run_id = response.json()["runId"]
            for _ in range(100):
                progress = database.rows[run_id]["progress"]
                if isinstance(progress, dict) and progress["crawled"] >= 1:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("active progress was not persisted")
            assert database.rows[run_id]["status"] == "running"
            assert progress["crawled"] >= 1

            # A reconnect while the original run is still active must receive
            # the canonical progress snapshot, not wait for terminal replay.
            reconnected = store.get(run_id)
            assert reconnected is not None
            stream = _event_stream(reconnected)
            first_event = await asyncio.wait_for(anext(stream), 1)
            await stream.aclose()
            assert '"crawled":' in first_event

            gate.set()
            for _ in range(100):
                if database.rows[run_id]["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("run did not reach persisted completion")

    assert database.rows[run_id]["status"] == "completed"


async def test_missing_update_row_is_reported_as_persistence_failure(
    stub: FakeMediaWiki,
) -> None:
    store = SupabaseCrawlRunStore(
        "https://supabase.test",
        "server-only-test-key",
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))),
    )
    with pytest.raises(PersistenceError, match="did not update"):
        store._patch("missing-run", {"status": "failed"})


async def test_update_failure_turns_the_owning_run_into_a_failure(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")

    def update_drops(_: httpx.Request) -> httpx.Response:
        if _.method == "POST":
            return httpx.Response(201, json=[{"run_id": json.loads(_.content)["run_id"]}])
        return httpx.Response(200, json=[])

    store = SupabaseCrawlRunStore(
        "https://supabase.test",
        "server-only-test-key",
        client=httpx.Client(transport=httpx.MockTransport(update_drops)),
    )
    run = store.start_run(CrawlRequest("Hub", 1, "es"), stub.transport)
    await run.wait_done()

    assert run.status.value == "failed"
    assert run.error is not None
    assert "did not update" in run.error


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
