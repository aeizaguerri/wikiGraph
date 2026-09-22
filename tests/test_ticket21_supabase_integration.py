"""Ticket 21 acceptance against the real migrated PostgREST schema.

Run with the disposable harness from ``scripts/ticket21-postgrest.sh`` and:
WIKIGRAPH_TICKET21_POSTGREST_URL=http://127.0.0.1:33002 \
WIKIGRAPH_TICKET21_POSTGREST_KEY=<ephemeral-local-jwt> \
uv run pytest tests/test_ticket21_supabase_integration.py -q
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from tests.helpers import fetch_graph
from tests.stub import FakeMediaWiki
from benchmarks.ticket19_representative import DeterministicClock
from wikigraph.app import create_app
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.runs import SupabaseCrawlRunStore


POSTGREST_URL = os.environ.get("WIKIGRAPH_TICKET21_POSTGREST_URL")
POSTGREST_KEY = os.environ.get("WIKIGRAPH_TICKET21_POSTGREST_KEY")
pytestmark = pytest.mark.skipif(
    not POSTGREST_URL or not POSTGREST_KEY,
    reason="ticket21 local PostgREST service is not configured",
)


def _store() -> SupabaseCrawlRunStore:
    assert POSTGREST_URL is not None and POSTGREST_KEY is not None
    return SupabaseCrawlRunStore(
        POSTGREST_URL,
        POSTGREST_KEY,
        rest_path="",
        governor=GlobalWikimediaGovernor(DeterministicClock()),
    )


async def _wait_status(run_id: str, status: str) -> None:
    assert POSTGREST_URL is not None and POSTGREST_KEY is not None
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            response = await client.get(
                f"{POSTGREST_URL}/crawl_runs",
                params={"run_id": f"eq.{run_id}"},
                headers={"apikey": POSTGREST_KEY, "Authorization": f"Bearer {POSTGREST_KEY}"},
            )
            rows = response.json()
            if rows and rows[0]["status"] == status:
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"real run did not reach {status}")


async def test_real_schema_recovers_before_and_after_checkpoint(
    stub: FakeMediaWiki,
) -> None:
    assert POSTGREST_URL is not None and POSTGREST_KEY is not None
    stub.add_page("Hub", links=[(0, "A"), (0, "B")])
    stub.add_page("A")
    stub.add_page("B")
    gate = stub.gate("A")

    first = create_app(mediawiki_transport=stub.transport, run_store=_store())
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 2, "language": "es"}
            )
            assert response.status_code == 201
            run_id = response.json()["runId"]
            await _wait_status(run_id, "running")
            await asyncio.sleep(0.05)

    await _wait_status(run_id, "recoverable")
    gate.set()

    second = create_app(mediawiki_transport=stub.transport, run_store=_store())
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            retried = await client.post(f"/api/runs/{run_id}/retry")
            assert retried.status_code == 202
            assert retried.json()["runId"] == run_id
            await _wait_status(run_id, "completed")
            graph = await fetch_graph(client, run_id)

    assert {node["title"] for node in graph["nodes"]} == {"Hub", "A", "B"}
    assert len(graph["edges"]) == 2


async def test_real_schema_recovers_before_first_checkpoint(
    stub: FakeMediaWiki,
) -> None:
    assert POSTGREST_URL is not None and POSTGREST_KEY is not None
    stub.add_page("Before", links=[(0, "Leaf")])
    stub.add_page("Leaf")
    gate = stub.gate("Before")

    first = create_app(mediawiki_transport=stub.transport, run_store=_store())
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "Before", "depth": 1, "language": "es"}
            )
            run_id = response.json()["runId"]
            await asyncio.sleep(0.05)

    await _wait_status(run_id, "recoverable")
    gate.set()
    second = create_app(mediawiki_transport=stub.transport, run_store=_store())
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            retried = await client.post(f"/api/runs/{run_id}/retry")
            assert retried.status_code == 202
            await _wait_status(run_id, "completed")
            graph = await fetch_graph(client, run_id)

    assert {node["title"] for node in graph["nodes"]} == {"Before", "Leaf"}
