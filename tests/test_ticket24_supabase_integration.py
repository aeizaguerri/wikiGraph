"""Ticket 24 acceptance against the real migrated response-cache schema.

Run with an isolated PostgREST service and the unique ticket-24 environment:
WIKIGRAPH_TICKET24_POSTGREST_URL=http://127.0.0.1:33024 \
WIKIGRAPH_TICKET24_POSTGREST_KEY=<ephemeral-local-jwt> \
uv run pytest tests/test_ticket24_supabase_integration.py -q
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from wikigraph.response_cache import ResponseCacheKey, SupabaseResponseCache

POSTGREST_URL = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_URL")
POSTGREST_KEY = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_KEY")
pytestmark = pytest.mark.skipif(
    not POSTGREST_URL or not POSTGREST_KEY,
    reason="ticket24 local PostgREST service is not configured",
)


def _headers() -> dict[str, str]:
    assert POSTGREST_KEY is not None
    return {
        "apikey": POSTGREST_KEY,
        "Authorization": f"Bearer {POSTGREST_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def test_real_schema_enforces_identity_and_seven_day_maximum() -> None:
    assert POSTGREST_URL is not None and POSTGREST_KEY is not None
    client = httpx.Client(timeout=10.0)
    cache = SupabaseResponseCache(
        POSTGREST_URL, POSTGREST_KEY, client=client, rest_path=""
    )
    key = ResponseCacheKey("redirects", "en", '["Cache real"]', "initial")
    digest = cache._digest(key)
    try:
        cache.put(key, {"query": {"pages": []}})
        before = client.get(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"eq.{digest}", "select": "last_accessed_at"},
            headers=_headers(),
        )
        assert before.status_code == 200, before.text
        accessed_at_before = before.json()[0]["last_accessed_at"]

        assert cache.get(key) == {"query": {"pages": []}}

        after = client.get(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"eq.{digest}", "select": "last_accessed_at"},
            headers=_headers(),
        )
        assert after.status_code == 200, after.text
        assert after.json()[0]["last_accessed_at"] == accessed_at_before

        invalid = client.post(
            f"{POSTGREST_URL}/upstream_response_cache",
            headers=_headers(),
            json={
                "cache_key": uuid.uuid4().hex,
                "kind": "redirects",
                "edition": "en",
                "normalized_request": "[\"invalid\"]",
                "continuation": "initial",
                "schema_version": "1",
                "response": {},
                "fetched_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-09T00:00:01Z",
                "last_accessed_at": "2026-01-01T00:00:00Z",
            },
        )
        assert invalid.status_code == 400
    finally:
        client.delete(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"eq.{digest}"},
            headers=_headers(),
        )
        client.close()
