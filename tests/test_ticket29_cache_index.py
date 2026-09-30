"""Ticket 29 regression coverage for the bounded Supabase cache identity."""

from __future__ import annotations

import hashlib
import json
import os

import httpx
import pytest

from wikigraph.response_cache import ResponseCacheKey, SupabaseResponseCache

POSTGREST_URL = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_URL")
POSTGREST_KEY = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_KEY")
pytestmark = pytest.mark.skipif(
    not POSTGREST_URL or not POSTGREST_KEY,
    reason="ticket29 local PostgREST service is not configured",
)


def _headers() -> dict[str, str]:
    assert POSTGREST_KEY is not None
    return {
        "apikey": POSTGREST_KEY,
        "Authorization": f"Bearer {POSTGREST_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def _long_title_batch() -> str:
    titles = [
        f"Article {index:02d} {hashlib.sha256(f'article-{index}'.encode()).hexdigest()}"
        for index in range(50)
    ]
    return json.dumps(sorted(titles), separators=(",", ":"))


def test_large_legitimate_batch_upserts_and_preserves_identity_isolation() -> None:
    assert POSTGREST_URL is not None and POSTGREST_KEY is not None
    client = httpx.Client(timeout=10.0)
    cache = SupabaseResponseCache(
        POSTGREST_URL, POSTGREST_KEY, client=client, rest_path=""
    )
    normalized_request = _long_title_batch()
    first = ResponseCacheKey("redirects", "en", normalized_request, "initial")
    second = ResponseCacheKey("redirects", "en", normalized_request, "continuation-2")
    first_digest = cache._digest(first)
    second_digest = cache._digest(second)
    try:
        cache.put(first, {"query": {"redirects": [{"from": "A", "to": "B"}]}})
        assert cache.get(first) == {
            "query": {"redirects": [{"from": "A", "to": "B"}]}
        }

        duplicate_identity = client.post(
            f"{POSTGREST_URL}/upstream_response_cache",
            headers=_headers(),
            params={"on_conflict": "cache_key"},
            json={
                **cache._row(first, {"query": {"redirects": []}}),
                "cache_key": "f" * 64,
            },
        )
        assert duplicate_identity.status_code == 409

        cache.put(first, {"query": {"redirects": [{"from": "A", "to": "C"}]}})
        assert cache.get(first) == {
            "query": {"redirects": [{"from": "A", "to": "C"}]}
        }

        cache.put(second, {"query": {"redirects": [{"from": "A", "to": "D"}]}})
        assert cache.get(second) == {
            "query": {"redirects": [{"from": "A", "to": "D"}]}
        }
        assert cache.get(first) != cache.get(second)

        collision = client.post(
            f"{POSTGREST_URL}/upstream_response_cache",
            headers=_headers(),
            params={"on_conflict": "cache_key"},
            json={
                **cache._row(first, {"query": {"redirects": []}}),
                "normalized_request": normalized_request + "-different",
            },
        )
        assert collision.status_code == 400
        assert cache.get(first) == {
            "query": {"redirects": [{"from": "A", "to": "C"}]}
        }
    finally:
        client.delete(
            f"{POSTGREST_URL}/upstream_response_cache",
            params={"cache_key": f"in.({first_digest},{second_digest})"},
            headers=_headers(),
        ).raise_for_status()
        client.close()
