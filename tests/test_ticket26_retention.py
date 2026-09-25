"""Ticket 26 retention acceptance against a real PostgREST/Postgres schema."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import subprocess
import uuid

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.runs import SupabaseCrawlRunStore


POSTGREST_URL = os.environ.get("WIKIGRAPH_TICKET26_POSTGREST_URL")
POSTGREST_KEY = os.environ.get("WIKIGRAPH_TICKET26_POSTGREST_KEY")
LOCAL_PSQL_ENABLED = os.environ.get("WIKIGRAPH_TICKET26_LOCAL_PSQL") == "1"
pytestmark = pytest.mark.skipif(
    not POSTGREST_URL or not POSTGREST_KEY or not LOCAL_PSQL_ENABLED,
    reason="ticket26 local PostgREST service and local psql fixture opt-in are required",
)


def _headers() -> dict[str, str]:
    assert POSTGREST_KEY is not None
    return {
        "apikey": POSTGREST_KEY,
        "Authorization": f"Bearer {POSTGREST_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _seed_historical_runs(completed_id: str, incomplete_id: str) -> None:
    """Seed fixture rows through the isolated local database admin connection."""
    sql = f"""
        INSERT INTO public.crawl_runs
            (run_id, seed, language, depth, node_cap, status, progress, checkpoint,
             graph, completed_at, expires_at, error)
        VALUES
            ('{completed_id}', 'Finished', 'en', 1, 500, 'completed',
             '{{"crawled":1,"discovered":1}}'::jsonb,
             '{{"secret":"checkpoint"}}'::jsonb,
             '{{"nodes":[{{"title":"Finished","level":0,"is_seed":true}}],"edges":[],"truncated":false,"crawled":1,"discovered":1,"community_ids":{{"Finished":0}},"community_count":1,"modularity":0.0}}'::jsonb,
             '2026-09-15T12:00:00Z', '2026-09-22T12:00:00Z', NULL),
            ('{incomplete_id}', 'Interrupted', 'en', 1, 500, 'recoverable',
             '{{"crawled":1,"discovered":2}}'::jsonb,
             '{{"secret":"checkpoint"}}'::jsonb,
             NULL, NULL, '2026-09-23T12:00:00Z', 'interrupted');
    """
    subprocess.run(
        [
            "podman", "exec", "-i", "ticket28-postgres", "psql", "-X",
            "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph",
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=True,
    )


def _delete_historical_runs(completed_id: str, incomplete_id: str) -> None:
    sql = (
        "DELETE FROM public.crawl_runs "
        f"WHERE run_id IN ('{completed_id}', '{incomplete_id}');"
    )
    subprocess.run(
        [
            "podman", "exec", "-i", "ticket28-postgres", "psql", "-X",
            "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph",
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=True,
    )


async def test_cleanup_preserves_tombstones_metrics_and_independent_cache() -> None:
    assert POSTGREST_URL is not None
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    completed_id = uuid.uuid4().hex
    incomplete_id = uuid.uuid4().hex
    cache_key = uuid.uuid4().hex
    database = httpx.Client(timeout=10.0)
    try:
        _seed_historical_runs(completed_id, incomplete_id)
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
                for path in ("", "/preview", "/graph"):
                    expired = await client.get(f"/api/runs/{completed_id}{path}")
                    assert expired.status_code == 410
                    assert expired.json()["error"]["code"] == "run_expired"
                current[0] = now + timedelta(days=1)
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
        assert by_day["2026-09-22"] == existing_metrics.get("2026-09-22", 0) + 1
        assert by_day["2026-09-23"] == existing_metrics.get("2026-09-23", 0) + 1
    finally:
        _delete_historical_runs(completed_id, incomplete_id)
        database.delete(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"eq.{cache_key}"},
            headers=_headers(),
        )
        database.close()
