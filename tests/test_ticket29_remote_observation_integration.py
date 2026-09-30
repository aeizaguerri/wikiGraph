"""Two independent Supabase stores observe one locally-owned crawl."""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

from benchmarks.ticket19_representative import DeterministicClock
from tests.stub import FakeMediaWiki
from wikigraph.app import create_app
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.runs import SupabaseCrawlRunStore

URL = os.environ.get("WIKIGRAPH_TICKET29_POSTGREST_URL")
KEY = os.environ.get("WIKIGRAPH_TICKET29_POSTGREST_KEY")
pytestmark = pytest.mark.skipif(not URL or not KEY, reason="ticket29 local PostgREST is not configured")


def _store() -> SupabaseCrawlRunStore:
    assert URL and KEY
    return SupabaseCrawlRunStore(URL, KEY, rest_path="", governor=GlobalWikimediaGovernor(DeterministicClock()))


async def _wait_status(run_id: str, expected: str) -> None:
    assert URL and KEY
    async with httpx.AsyncClient() as client:
        for _ in range(300):
            response = await client.get(
                f"{URL}/crawl_runs", params={"run_id": f"eq.{run_id}"},
                headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
            )
            if response.json() and response.json()[0]["status"] == expected:
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {expected}")


async def test_second_store_observes_progress_completion_and_graph(stub: FakeMediaWiki) -> None:
    assert URL and KEY
    seed = "Remote" + uuid.uuid4().hex[:8]
    stub.add_page(seed, links=[(0, seed + " Leaf")])
    stub.add_page(seed + " Leaf")
    gate = stub.gate(seed + " Leaf")
    first = create_app(mediawiki_transport=stub.transport, run_store=_store())
    second = create_app(run_store=_store())
    async with first.router.lifespan_context(first), second.router.lifespan_context(second):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://test") as owner:
            created = await owner.post("/api/runs", json={"seed": seed, "depth": 1, "language": "es"})
            assert created.status_code == 201
            run_id = created.json()["runId"]
        await _wait_status(run_id, "running")
        await asyncio.sleep(0.05)
        gate.set()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://test") as observer:
            events = await observer.get(f"/api/runs/{run_id}/events", timeout=8)
            assert events.status_code == 200
            assert "event: progress" in events.text
            assert "event: completed" in events.text
            graph = await observer.get(f"/api/runs/{run_id}/graph", timeout=8)
            assert graph.status_code == 200
            assert {node["title"] for node in graph.json()["nodes"]} == {seed, seed + " Leaf"}


async def test_second_store_sse_returns_immediately_for_completed_run(stub: FakeMediaWiki) -> None:
    assert URL and KEY
    seed = "Done" + uuid.uuid4().hex[:8]
    stub.add_page(seed)
    first = create_app(mediawiki_transport=stub.transport, run_store=_store())
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://test") as owner:
            run_id = (await owner.post("/api/runs", json={"seed": seed, "depth": 1, "language": "es"})).json()["runId"]
            await _wait_status(run_id, "completed")
    second = create_app(run_store=_store())
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://test") as observer:
            response = await observer.get(f"/api/runs/{run_id}/events", timeout=3)
            assert response.status_code == 200
            assert response.text.count("event: completed") == 1
