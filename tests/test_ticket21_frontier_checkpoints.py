"""Ticket 21 - durable frontier checkpoints and same-run manual recovery."""

from __future__ import annotations

import asyncio
import json

import httpx

from tests.helpers import fetch_graph
from tests.stub import FakeMediaWiki
from benchmarks.ticket19_representative import DeterministicClock
from wikigraph.app import create_app
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.runs import SupabaseCrawlRunStore


class PostgrestRuns:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

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
        if request.url.path.endswith("/rpc/fenced_update_crawl_run"):
            args = json.loads(request.content)
            row = self.rows.get(args["p_run_id"])
            if row is None or row.get("owner_token") != args["p_owner_token"] or row.get("owner_version") != args["p_owner_version"]:
                return httpx.Response(200, json=False)
            row.update(args["p_patch"])
            operation = args["p_operation"]
            if operation in {"completed", "failed", "recoverable", "overload_waiting"}:
                row["status"] = operation
                row["owner_token"] = None
                row["owner_version"] += 1
            return httpx.Response(200, json=True)
        run_id = request.url.params.get("run_id", "").removeprefix("eq.")
        if request.method == "GET":
            row = self.rows.get(run_id)
            return httpx.Response(200, json=[] if row is None else [row])
        return httpx.Response(405)


def store(database: PostgrestRuns) -> SupabaseCrawlRunStore:
    return SupabaseCrawlRunStore(
        "https://supabase.test",
        "test-only-key",
        client=httpx.Client(transport=database.transport()),
        governor=GlobalWikimediaGovernor(DeterministicClock()),
    )


async def wait_for_status(database: PostgrestRuns, run_id: str, status: str) -> None:
    for _ in range(100):
        if database.rows[run_id]["status"] == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not reach {status}")


async def test_interruption_before_checkpoint_retries_same_run_and_graph(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub", links=[(0, "Leaf")])
    stub.add_page("Leaf")
    gate = stub.gate("Hub")
    database = PostgrestRuns()

    first = create_app(mediawiki_transport=stub.transport, run_store=store(database))
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )
            run_id = response.json()["runId"]
            await asyncio.sleep(0.02)
            assert database.rows[run_id]["status"] == "running"
            assert database.rows[run_id]["checkpoint"]["crawled"] == 0

    assert database.rows[run_id]["status"] == "recoverable"
    gate.set()

    second = create_app(mediawiki_transport=stub.transport, run_store=store(database))
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            retried = await client.post(f"/api/runs/{run_id}/retry")
            assert retried.status_code == 202
            assert retried.json()["runId"] == run_id
            await wait_for_status(database, run_id, "completed")
            graph = await fetch_graph(client, run_id)

    assert {node["title"] for node in graph["nodes"]} == {"Hub", "Leaf"}
    assert len({(edge["source"], edge["target"]) for edge in graph["edges"]}) == 1


async def test_committed_checkpoint_survives_process_replacement(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub", links=[(0, "A"), (0, "B")])
    stub.add_page("A")
    stub.add_page("B")
    gate = stub.gate("A")
    database = PostgrestRuns()

    first = create_app(mediawiki_transport=stub.transport, run_store=store(database))
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 2, "language": "es"}
            )
            run_id = response.json()["runId"]
            for _ in range(100):
                checkpoint = database.rows[run_id]["checkpoint"]
                if isinstance(checkpoint, dict) and checkpoint["crawled"] == 1:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("frontier checkpoint was not committed")
            assert database.rows[run_id]["status"] == "running"

    assert database.rows[run_id]["status"] == "recoverable"
    checkpoint = database.rows[run_id]["checkpoint"]
    assert checkpoint["current_batch"] == ["Hub"]
    gate.set()

    second = create_app(mediawiki_transport=stub.transport, run_store=store(database))
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            retried = await client.post(f"/api/runs/{run_id}/retry")
            assert retried.json()["runId"] == run_id
            await wait_for_status(database, run_id, "completed")
            graph = await fetch_graph(client, run_id)

    titles = {node["title"] for node in graph["nodes"]}
    assert titles == {"Hub", "A", "B"}
    assert len(graph["edges"]) == 2
