"""Ticket 26 retention acceptance against a real PostgREST/Postgres schema."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import uuid

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.runs import SupabaseCrawlRunStore


POSTGREST_URL = os.environ.get("WIKIGRAPH_TICKET26_POSTGREST_URL")
POSTGREST_KEY = os.environ.get("WIKIGRAPH_TICKET26_POSTGREST_KEY")
pytestmark = pytest.mark.skipif(
    not POSTGREST_URL or not POSTGREST_KEY,
    reason="ticket26 local PostgREST service is not configured",
)


def _headers() -> dict[str, str]:
    assert POSTGREST_KEY is not None
    return {
        "apikey": POSTGREST_KEY,
        "Authorization": f"Bearer {POSTGREST_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def test_cleanup_preserves_tombstones_metrics_and_independent_cache() -> None:
    assert POSTGREST_URL is not None
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    completed_id = uuid.uuid4().hex
    incomplete_id = uuid.uuid4().hex
    cache_key = uuid.uuid4().hex
    database = httpx.Client(timeout=10.0)
    try:
        for row in (
            {
                "run_id": completed_id,
                "seed": "Finished",
                "language": "en",
                "depth": 1,
                "node_cap": 500,
                "status": "completed",
                "progress": {"crawled": 1, "discovered": 1},
                "checkpoint": {"secret": "checkpoint"},
                "graph": {
                    "nodes": [{"title": "Finished", "level": 0, "is_seed": True}],
                    "edges": [],
                    "truncated": False,
                    "crawled": 1,
                    "discovered": 1,
                    "community_ids": {"Finished": 0},
                    "community_count": 1,
                    "modularity": 0.0,
                },
                "completed_at": "2026-09-15T12:00:00Z",
                "expires_at": "2026-09-22T12:00:00Z",
            },
            {
                "run_id": incomplete_id,
                "seed": "Interrupted",
                "language": "en",
                "depth": 1,
                "node_cap": 500,
                "status": "recoverable",
                "progress": {"crawled": 1, "discovered": 2},
                "checkpoint": {"secret": "checkpoint"},
                "error": "interrupted",
                "expires_at": "2026-09-23T12:00:00Z",
            },
        ):
            response = database.post(
                f"{POSTGREST_URL}/crawl_runs", headers=_headers(), json=row
            )
            response.raise_for_status()
        cache = {
            "cache_key": cache_key,
            "kind": "redirects",
            "edition": "en",
            "normalized_request": '["Independent"]',
            "continuation": "initial",
            "schema_version": "1",
            "response": {"query": {"pages": []}},
            "fetched_at": "2026-09-22T11:59:00Z",
            "expires_at": "2026-09-29T11:59:00Z",
            "last_accessed_at": "2026-09-22T11:59:00Z",
        }
        database.post(
            f"{POSTGREST_URL}/upstream_response_cache", headers=_headers(), json=cache
        ).raise_for_status()
        existing_metrics = {
            row["metric_day"]: row["run_count"]
            for row in database.get(
                f"{POSTGREST_URL}/crawl_run_metrics",
                params={"language": "eq.en", "outcome": "eq.expired"},
                headers=_headers(),
            ).json()
        }

        current = [now - timedelta(seconds=1)]
        store = SupabaseCrawlRunStore(
            POSTGREST_URL,
            POSTGREST_KEY,
            rest_path="",
            clock=lambda: current[0],
        )
        app = create_app(run_store=store)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                retained = await client.get(f"/api/runs/{completed_id}/graph")
                assert retained.status_code == 200
                current[0] = now
                expired = await client.get(f"/api/runs/{completed_id}/graph")
                assert expired.status_code == 410
                assert (
                    await client.get(f"/api/runs/{completed_id}/graph")
                ).status_code == 410
                current[0] = now + timedelta(days=1)
                assert (
                    await client.get(f"/api/runs/{incomplete_id}/graph")
                ).status_code == 410

        completed = database.get(
            f"{POSTGREST_URL}/crawl_runs",
            params={"run_id": f"eq.{completed_id}"},
            headers=_headers(),
        ).json()[0]
        incomplete = database.get(
            f"{POSTGREST_URL}/crawl_runs",
            params={"run_id": f"eq.{incomplete_id}"},
            headers=_headers(),
        ).json()[0]
        assert completed["status"] == "expired"
        assert all(completed[field] is None for field in ("progress", "checkpoint", "graph", "error"))
        assert incomplete["status"] == "expired"
        assert all(incomplete[field] is None for field in ("progress", "checkpoint", "graph", "error"))
        assert database.get(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"eq.{cache_key}"},
            headers=_headers(),
        ).json()
        metrics = database.get(
            f"{POSTGREST_URL}/crawl_run_metrics",
            params={"language": "eq.en", "outcome": "eq.expired"},
            headers=_headers(),
        ).json()
        by_day = {row["metric_day"]: row["run_count"] for row in metrics}
        assert by_day["2026-09-22"] == existing_metrics.get("2026-09-22", 0) + 1
        assert by_day["2026-09-23"] == existing_metrics.get("2026-09-23", 0) + 1
    finally:
        database.delete(
            f"{POSTGREST_URL}/crawl_runs",
            params={"run_id": f"in.({completed_id},{incomplete_id})"},
            headers=_headers(),
        )
        database.delete(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"eq.{cache_key}"},
            headers=_headers(),
        )
        database.close()
