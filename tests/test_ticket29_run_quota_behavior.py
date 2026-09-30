"""End-to-end run-attempt accounting through PostgREST and MediaWiki transport."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import pytest

from wikigraph.crawler import CrawlRequest
from wikigraph.governor import GlobalWikimediaGovernor, SupabaseWikimediaAdmission
from wikigraph.response_cache import InMemoryResponseCache
from wikigraph.runs import PersistenceError, RunStatus, SupabaseCrawlRunStore

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
JWT = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
pytestmark = pytest.mark.skipif(
    URL != "http://127.0.0.1:33028" or not JWT,
    reason="ticket28 disposable loopback PostgREST credentials are required",
)


class ScriptedWiki(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[tuple[int, dict[str, Any], dict[str, str]]]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body, headers = self.responses.pop(0)
        return httpx.Response(status, json=body, headers=headers, request=request)


def _headers() -> dict[str, str]:
    assert JWT is not None
    return {"apikey": JWT, "Authorization": f"Bearer {JWT}"}


async def _row(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    assert URL is not None
    response = await client.get(
        f"{URL}/crawl_runs", params={"run_id": f"eq.{run_id}", "select": "*"},
        headers=_headers(),
    )
    response.raise_for_status()
    return response.json()[0]


def _successful_responses() -> list[tuple[int, dict[str, Any], dict[str, str]]]:
    return [
        (429, {"error": {"code": "ratelimited"}}, {"Retry-After": "0"}),
        (503, {"error": {"code": "maxlag", "lag": 0}}, {}),
        (200, {"query": {"pages": [{"title": "Seed", "links": [{"ns": 0, "title": "Alias"}]}]}, "continue": {"plcontinue": "next", "pltitles": "Seed", "continue": "-||"}}, {}),
        (200, {"query": {"pages": [{"title": "Seed", "links": [{"ns": 0, "title": "Other"}]}]}}, {}),
        (200, {"query": {"redirects": [{"from": "Alias", "to": "Canonical"}], "pages": [{"title": "Canonical", "ns": 0}, {"title": "Other", "ns": 0}]}}, {}),
        # The overloaded initial request is not cached; serve its same successful page once more.
        (200, {"query": {"pages": [{"title": "Seed", "links": [{"ns": 0, "title": "Alias"}]}]}, "continue": {"plcontinue": "next", "pltitles": "Seed", "continue": "-||"}}, {}),
    ]


async def test_attempts_count_retries_continuation_and_redirect_but_not_cache_hits() -> None:
    assert URL and JWT
    governor = GlobalWikimediaGovernor(
        admission=SupabaseWikimediaAdmission(URL, JWT, rest_path="")
    )
    cache = InMemoryResponseCache()
    store = SupabaseCrawlRunStore(
        URL, JWT, rest_path="", governor=governor, cache=cache, attempt_quota=5
    )
    transport = ScriptedWiki(_successful_responses())
    try:
        first = store.start_run(CrawlRequest("Seed", 1, "en", node_cap=4), transport)
        await asyncio.wait_for(first.wait_done(), 30)
        assert first.status is RunStatus.COMPLETED
        assert len(transport.requests) == 5
        assert [dict(request.url.params).get("plcontinue") for request in transport.requests] == [None, None, None, "next", None]
        async with httpx.AsyncClient(timeout=10) as db:
            first_row = await _row(db, first.id)
            assert first_row["attempts_used"] == 5
            assert first_row["attempt_quota"] == 5

            second = store.start_run(CrawlRequest("Seed", 1, "en", node_cap=4), transport)
            await asyncio.wait_for(second.wait_done(), 30)
            assert second.status is RunStatus.COMPLETED
            assert len(transport.requests) == 6
            second_row = await _row(db, second.id)
            assert second_row["attempts_used"] == 1
            assert second_row["graph"] is not None

            third = store.start_run(CrawlRequest("Seed", 1, "en", node_cap=4), transport)
            await asyncio.wait_for(third.wait_done(), 30)
            assert third.status is RunStatus.COMPLETED
            assert len(transport.requests) == 6
            third_row = await _row(db, third.id)
            assert third_row["attempts_used"] == 0
            assert third_row["graph"] is not None
    finally:
        await store.shutdown()
        await governor.shutdown()


async def test_exhaustion_is_stable_failed_terminal_without_graph_or_retry() -> None:
    assert URL and JWT
    governor = GlobalWikimediaGovernor(
        admission=SupabaseWikimediaAdmission(URL, JWT, rest_path="")
    )
    store = SupabaseCrawlRunStore(
        URL,
        JWT,
        rest_path="",
        governor=governor,
        cache=InMemoryResponseCache(),
        attempt_quota=1,
    )
    transport = ScriptedWiki([
        (200, {"query": {"pages": [{"title": "Seed", "links": [{"ns": 0, "title": "Child"}]}]}}, {}),
        (200, {"query": {"pages": [{"title": "Child", "ns": 0}]}}, {}),
    ])
    try:
        run = store.start_run(CrawlRequest("Seed", 1, "en", node_cap=2), transport)
        await asyncio.wait_for(run.wait_done(), 30)
        assert run.status is RunStatus.FAILED
        assert run.error == "This Crawl run has reached its upstream request limit."
        assert run.result is None
        assert len(transport.requests) == 1
        async with httpx.AsyncClient(timeout=10) as db:
            row = await _row(db, run.id)
            assert row["status"] == "failed"
            assert row["error"] == run.error
            assert row["attempts_used"] == 1
            assert row["graph"] is None
        with pytest.raises(PersistenceError, match="Only recoverable"):
            store.retry_run(run.id, transport)
        assert len(transport.requests) == 1
        async with httpx.AsyncClient(timeout=10) as db:
            unchanged = await _row(db, run.id)
            assert unchanged["status"] == "failed"
            assert unchanged["attempts_used"] == 1
    finally:
        await store.shutdown()
        await governor.shutdown()
