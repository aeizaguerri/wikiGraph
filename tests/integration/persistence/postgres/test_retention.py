"""Ticket 26 retention acceptance against a real PostgREST/Postgres schema."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.runs import SupabaseCrawlRunStore


from tests.support.postgres import load_postgres_harness, run_postgres_admin_sql

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


def _seed_expiring_runs(
    completed_id: str,
    incomplete_id: str,
    completed_expiry: datetime,
    incomplete_expiry: datetime,
) -> None:
    """Seed controlled expiry fixtures through the current owned admin connection."""
    completed_progress = {
        "crawled": 2,
        "discovered": 2,
        "depth": 1,
        "recent": ["Finished", "Related"],
    }
    checkpoint = {
        "level": 1,
        "frontier": ["Related"],
        "batch_start": 0,
        "next_frontier": [],
        "current_batch": [],
        "continuation": "retention-checkpoint",
        "nodes": [{"title": "Finished", "level": 0, "is_seed": True}],
        "edges": [],
        "truncated": False,
        "crawled": 1,
        "recent": ["Finished"],
    }
    completed_graph = {
        "nodes": [
            {"title": "Finished", "level": 0, "is_seed": True},
            {"title": "Related", "level": 1, "is_seed": False},
        ],
        "edges": [{"source": "Finished", "target": "Related"}],
        "truncated": False,
        "crawled": 2,
        "discovered": 2,
        "community_ids": {"Finished": 0, "Related": 0},
        "community_count": 1,
        "modularity": 0.0,
    }
    incomplete_progress = {
        "crawled": 1,
        "discovered": 2,
        "depth": 1,
        "recent": ["Interrupted", "Related"],
    }
    incomplete_checkpoint = {
        **checkpoint,
        "nodes": [
            {"title": "Interrupted", "level": 0, "is_seed": True},
            {"title": "Related", "level": 1, "is_seed": False},
        ],
        "edges": [{"source": "Interrupted", "target": "Related"}],
        "recent": ["Interrupted", "Related"],
    }
    sql = f"""
        INSERT INTO public.crawl_runs
            (run_id, seed, language, depth, node_cap, status, progress, checkpoint,
             graph, completed_at, expires_at, error)
        VALUES
            ('{completed_id}', 'Finished', 'en', 1, 500, 'completed',
             '{json.dumps(completed_progress)}'::jsonb,
             '{json.dumps(checkpoint)}'::jsonb,
             '{json.dumps(completed_graph)}'::jsonb,
             '{(completed_expiry - timedelta(days=7)).isoformat()}', '{completed_expiry.isoformat()}', NULL),
            ('{incomplete_id}', 'Interrupted', 'en', 1, 500, 'recoverable',
             '{json.dumps(incomplete_progress)}'::jsonb,
             '{json.dumps(incomplete_checkpoint)}'::jsonb,
             NULL, NULL, '{incomplete_expiry.isoformat()}', 'interrupted');
    """
    run_postgres_admin_sql(sql)


def _delete_expiring_runs(completed_id: str, incomplete_id: str) -> None:
    sql = (
        "DELETE FROM public.crawl_runs "
        f"WHERE run_id IN ('{completed_id}', '{incomplete_id}');"
    )
    run_postgres_admin_sql(sql)


async def test_cleanup_preserves_tombstones_metrics_and_independent_cache() -> None:
    assert POSTGREST_URL is not None
    now = datetime.now(timezone.utc).replace(microsecond=0)
    completed_expiry = now + timedelta(minutes=1)
    incomplete_expiry = completed_expiry + timedelta(minutes=1)
    completed_id = uuid.uuid4().hex
    incomplete_id = uuid.uuid4().hex
    cache_key = uuid.uuid4().hex
    database = httpx.Client(timeout=10.0)
    try:
        _seed_expiring_runs(
            completed_id, incomplete_id, completed_expiry, incomplete_expiry
        )
        cache = {
            "cache_key": cache_key,
            "kind": "redirects",
            "edition": "en",
            "normalized_request": '["Independent"]',
            "continuation": "initial",
            "schema_version": "1",
            "response": {"query": {"pages": []}},
            "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(days=7)).isoformat(),
            "last_accessed_at": now.isoformat(),
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

        store = SupabaseCrawlRunStore(POSTGREST_URL, POSTGREST_KEY, rest_path="")
        app = create_app(run_store=store)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                retained = await client.get(f"/api/runs/{completed_id}/graph")
                assert retained.status_code == 200
                assert store.cleanup_expired(completed_expiry) == 1
                for path in ("", "/preview", "/graph"):
                    expired = await client.get(f"/api/runs/{completed_id}{path}")
                    assert expired.status_code == 410
                    assert expired.json()["error"]["code"] == "run_expired"
                assert store.cleanup_expired(incomplete_expiry) == 1
                for path in ("", "/preview", "/graph"):
                    expired = await client.get(f"/api/runs/{incomplete_id}{path}")
                    assert expired.status_code == 410
                    assert expired.json()["error"]["code"] == "run_expired"

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
        expected_by_day: dict[str, int] = {}
        for expiry in (completed_expiry, incomplete_expiry):
            day = expiry.date().isoformat()
            expected_by_day[day] = expected_by_day.get(day, 0) + 1
        for day, added in expected_by_day.items():
            assert by_day[day] == existing_metrics.get(day, 0) + added
    finally:
        _delete_expiring_runs(completed_id, incomplete_id)
        database.delete(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"eq.{cache_key}"},
            headers=_headers(),
        )
        database.close()
