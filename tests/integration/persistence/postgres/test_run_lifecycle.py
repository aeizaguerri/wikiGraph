"""Public run lifecycle against the owned PostgREST/Postgres harness.

Use scripts/test_postgres.py with an explicit child test command:
owned local harness URL \
owned service-role JWT \
uv run pytest tests/integration/persistence/postgres/test_run_lifecycle.py -q
"""

from __future__ import annotations

import os
import asyncio
import uuid

import httpx
import pytest

from tests.support.api import DeterministicClock, fetch_graph
from tests.support.mediawiki import FakeMediaWiki
from wikigraph.app import create_app
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.runs import SupabaseCrawlRunStore


from tests.support.postgres import load_postgres_harness

HARNESS = load_postgres_harness()
POSTGREST_URL = HARNESS.url if HARNESS else None
POSTGREST_KEY = HARNESS.service_role_jwt if HARNESS else None
pytestmark = pytest.mark.skipif(
    not POSTGREST_URL or not POSTGREST_KEY,
    reason="owned PostgreSQL harness is not configured",
)


def _headers() -> dict[str, str]:
    assert POSTGREST_KEY is not None
    return {
        "apikey": POSTGREST_KEY,
        "Authorization": f"Bearer {POSTGREST_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _store() -> SupabaseCrawlRunStore:
    assert POSTGREST_URL is not None
    return SupabaseCrawlRunStore(
        POSTGREST_URL,
        POSTGREST_KEY,
        rest_path="",
        governor=GlobalWikimediaGovernor(DeterministicClock()),
    )


async def test_real_postgrest_constraints_and_fresh_process_reopen(
    stub: FakeMediaWiki,
) -> None:
    assert POSTGREST_URL is not None
    stub.add_page("Hub", links=[(0, "Leaf")])
    database = httpx.Client(timeout=10.0)
    created_ids: list[str] = []
    try:
        invalid = database.post(
            f"{POSTGREST_URL}/crawl_runs",
            headers=_headers(),
            json={
                "run_id": uuid.uuid4().hex,
                "seed": "Bad",
                "language": "fr",
                "depth": 1,
                "node_cap": 500,
                "status": "running",
            },
        )
        # Raw writes are denied before table constraints are evaluated.
        assert invalid.status_code in {401, 403}

        first = create_app(mediawiki_transport=stub.transport, run_store=_store())
        async with first.router.lifespan_context(first):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=first), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
                )
                assert response.status_code == 201
                run_id = response.json()["runId"]
                created_ids.append(run_id)
                for _ in range(100):
                    persisted = database.get(
                        f"{POSTGREST_URL}/crawl_runs",
                        params={"run_id": f"eq.{run_id}"},
                        headers=_headers(),
                    ).json()[0]
                    if persisted["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("real store did not persist completion")
                graph = await fetch_graph(client, run_id)
                assert graph["runId"] == run_id

        second = create_app(mediawiki_transport=stub.transport, run_store=_store())
        async with second.router.lifespan_context(second):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second), base_url="http://test"
            ) as client:
                reopened = await fetch_graph(client, run_id)
                assert reopened == graph

        mutation = database.patch(
            f"{POSTGREST_URL}/crawl_runs",
            params={"run_id": f"eq.{run_id}"},
            headers=_headers(),
            json={"graph": {"tampered": True}},
        )
        assert mutation.status_code in {401, 403}
    finally:
        for run_id in created_ids:
            database.delete(
                f"{POSTGREST_URL}/crawl_runs",
                params={"run_id": f"eq.{run_id}"},
                headers=_headers(),
            )
        database.close()
