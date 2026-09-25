"""Ticket 20 - completed Crawl runs survive replacing the application process."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from benchmarks.ticket19_representative import DeterministicClock
from tests.helpers import collect_events, fetch_graph
from tests.stub import FakeMediaWiki
from wikigraph.app import _event_stream, create_app
from wikigraph.crawler import CrawlRequest
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.runs import PersistenceError, SupabaseCrawlRunStore


class PostgrestRuns:
    """Small PostgREST-shaped transport; no application fallback is involved."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.heartbeats = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rpc/create_owned_crawl_run"):
            args = json.loads(request.content)
            row = {
                "run_id": args["p_run_id"], "seed": args["p_seed"],
                "language": args["p_language"], "depth": args["p_depth"],
                "node_cap": args["p_node_cap"], "status": "running",
                "progress": {"crawled": 0, "discovered": 1, "depth": 0, "recent": []},
                "checkpoint": args["p_checkpoint"], "owner_token": args["p_owner_token"],
                "owner_version": 1,
            }
            self.rows[str(row["run_id"])] = row
            return httpx.Response(200, json=[{"run_id": row["run_id"], "owner_version": 1}])
        if request.url.path.endswith("/rpc/claim_crawl_run"):
            args = json.loads(request.content)
            row = self.rows[args["p_run_id"]]
            if row["owner_version"] != args["p_expected_version"] or row["status"] not in {"recoverable", "overload_waiting"}:
                return httpx.Response(200, json=[])
            row.update(status="running", owner_token=args["p_owner_token"], owner_version=row["owner_version"] + 1)
            return httpx.Response(200, json=[{"owner_version": row["owner_version"]}])
        if request.url.path.endswith("/rpc/heartbeat_crawl_run"):
            self.heartbeats += 1
            args = json.loads(request.content)
            row = self.rows.get(args["p_run_id"])
            owned = bool(row and row.get("owner_token") == args["p_owner_token"] and row.get("owner_version") == args["p_owner_version"] and row.get("status") == "running")
            return httpx.Response(
                200, json=[{"lease_until": "2026-09-25T00:00:00+00:00"}] if owned else []
            )
        if request.url.path.endswith("/rpc/fenced_update_crawl_run"):
            args = json.loads(request.content)
            row = self.rows.get(args["p_run_id"])
            if row is None or row.get("owner_token") != args["p_owner_token"] or row.get("owner_version") != args["p_owner_version"]:
                return httpx.Response(200, json=False)
            row.update(args["p_patch"])
            op = args["p_operation"]
            if op in {"completed", "failed", "recoverable", "overload_waiting"}:
                row["status"] = op
                row["owner_token"] = None
                row["owner_version"] += 1
            return httpx.Response(200, json=True)
        run_id = request.url.params.get("run_id", "").removeprefix("eq.")
        if request.method == "GET":
            row = self.rows.get(run_id)
            return httpx.Response(200, json=[] if row is None else [row])
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
            governor=GlobalWikimediaGovernor(DeterministicClock()),
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
        governor=GlobalWikimediaGovernor(DeterministicClock()),
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
        governor=GlobalWikimediaGovernor(DeterministicClock()),
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


async def test_long_owned_acquisition_keeps_lease_renewed(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")
    gate = stub.gate("Hub")
    database = PostgrestRuns()
    store = SupabaseCrawlRunStore(
        "https://supabase.test", "server-only-test-key",
        client=httpx.Client(transport=database.transport()),
        heartbeat_interval_seconds=0.01,
    )
    run = store.start_run(CrawlRequest("Hub", 1, "es"), stub.transport)
    for _ in range(100):
        if database.heartbeats >= 2:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("owned crawl did not renew its lease while acquisition waited")
    assert database.rows[run.id]["status"] == "running"
    gate.set()
    await asyncio.wait_for(run.wait_done(), 1)
    assert database.rows[run.id]["status"] == "completed"
    await store.shutdown()


async def test_failed_heartbeat_cancels_acquisition_without_publishing_graph(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")
    gate = stub.gate("Hub")

    def heartbeat_fails(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rpc/create_owned_crawl_run"):
            args = json.loads(request.content)
            return httpx.Response(200, json=[{"run_id": args["p_run_id"], "owner_version": 1}])
        if request.url.path.endswith("/rpc/heartbeat_crawl_run"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=True)

    store = SupabaseCrawlRunStore(
        "https://supabase.test", "server-only-test-key",
        client=httpx.Client(transport=httpx.MockTransport(heartbeat_fails)),
        heartbeat_interval_seconds=0.01,
    )
    run = store.start_run(CrawlRequest("Hub", 1, "es"), stub.transport)
    await asyncio.wait_for(run.wait_done(), 1)
    assert run.result is None
    assert run.communities is None
    assert run.status.value == "recoverable"
    await store.shutdown()


async def test_missing_update_row_is_reported_as_persistence_failure(
    stub: FakeMediaWiki,
) -> None:
    store = SupabaseCrawlRunStore(
        "https://supabase.test",
        "server-only-test-key",
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=False))),
        governor=GlobalWikimediaGovernor(DeterministicClock()),
    )
    with pytest.raises(PersistenceError, match="stale or invalid"):
        store._fenced_update("missing-run", "token", 1, "failed", {"error": "failure"})


async def test_update_failure_turns_the_owning_run_into_a_failure(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")

    def update_drops(_: httpx.Request) -> httpx.Response:
        if _.url.path.endswith("/rpc/create_owned_crawl_run"):
            return httpx.Response(200, json=[{"run_id": json.loads(_.content)["p_run_id"], "owner_version": 1}])
        return httpx.Response(200, json=False)

    store = SupabaseCrawlRunStore(
        "https://supabase.test",
        "server-only-test-key",
        client=httpx.Client(transport=httpx.MockTransport(update_drops)),
        governor=GlobalWikimediaGovernor(DeterministicClock()),
    )
    run = store.start_run(CrawlRequest("Hub", 1, "es"), stub.transport)
    await run.wait_done()

    assert run.status.value == "failed"
    assert run.error is not None
    assert "stale or invalid" in run.error


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
        governor=GlobalWikimediaGovernor(DeterministicClock()),
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
