"""Ticket 23 - durable upstream overload flow control and recovery."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from benchmarks.ticket19_representative import DeterministicClock
from wikigraph.crawler import CrawlRequest
from wikigraph.governor import GlobalWikimediaGovernor, SupabaseWikimediaAdmission
from wikigraph.mediawiki import MediaWikiClient, UpstreamOverload
from wikigraph.runs import InMemoryCrawlRunStore, RunStatus, SupabaseCrawlRunStore
from wikigraph.app import create_app
from tests.helpers import collect_events, fetch_graph


REAL_URL = os.environ.get("WIKIGRAPH_TICKET23_POSTGREST_URL")
REAL_KEY = os.environ.get("WIKIGRAPH_TICKET23_POSTGREST_KEY")


class ScriptedUpstream(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        del request
        self.requests += 1
        return self.responses.pop(0)


class PublicLifecycleTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.overload_attempts = 0
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        if params.get("redirects") == "1":
            titles = params["titles"].split("|")
            pages = [{"title": title, "ns": 0} for title in titles]
            return httpx.Response(200, json={"query": {"pages": pages}})
        if params.get("prop") == "links":
            if self.overload_attempts < 10:
                self.overload_attempts += 1
                return httpx.Response(
                    503,
                    headers={"Retry-After": "0.01"},
                    json={"error": {"code": "maxlag"}},
                )
            return httpx.Response(
                200,
                json={
                    "batchcomplete": True,
                    "query": {
                        "pages": [
                            {
                                "title": "Hub",
                                "links": [{"ns": 0, "title": "Leaf"}],
                            }
                        ]
                    },
                },
            )
        return httpx.Response(400, json={"error": {"code": "unsupported"}})


def ok_links() -> httpx.Response:
    return httpx.Response(
        200,
        json={"batchcomplete": True, "query": {"pages": [{"title": "Hub", "links": []}]}},
    )


async def test_retry_after_precedes_jitter_and_overload_is_bounded() -> None:
    clock = DeterministicClock()
    transport = ScriptedUpstream(
        [
            httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"code": "maxlag"}})
            for _ in range(10)
        ]
    )
    client = MediaWikiClient(
        "es",
        transport=transport,
        governor=GlobalWikimediaGovernor(clock),
        clock=clock,
        jitter=lambda _: 999.0,
    )
    try:
        try:
            await client.article_links("Hub")
        except UpstreamOverload as exc:
            assert exc.attempts == 10
            assert exc.retry_after == 45.0
        else:
            raise AssertionError("overload should become recoverable after ten attempts")
    finally:
        await client.aclose()
    assert transport.requests == 10
    assert clock.current == 405.0


async def test_retry_after_accepts_zero_seconds_and_http_dates() -> None:
    clock = DeterministicClock()
    retry_at = format_datetime(datetime.fromtimestamp(1020, timezone.utc), usegmt=True)
    transport = ScriptedUpstream(
        [
            httpx.Response(503, headers={"Retry-After": "0"}),
            ok_links(),
            httpx.Response(503, headers={"Retry-After": retry_at}),
            ok_links(),
        ]
    )
    client = MediaWikiClient(
        "es",
        transport=transport,
        governor=GlobalWikimediaGovernor(clock),
        clock=clock,
        wall_clock=lambda: 1000.0,
    )
    try:
        await client.article_links("Hub")
        assert clock.current == 0.5
        await client.article_links("Hub")
        assert clock.current == 21.0
    finally:
        await client.aclose()


async def test_maxlag_retry_preserves_identical_request_and_run_identity() -> None:
    clock = DeterministicClock()
    transport = ScriptedUpstream(
        [
            *[
                httpx.Response(503, json={"error": {"code": "maxlag"}})
                for _ in range(10)
            ],
            ok_links(),
        ]
    )
    governor = GlobalWikimediaGovernor(clock)
    store = InMemoryCrawlRunStore(governor)
    run = store.start_run(CrawlRequest("Hub", 1, "es"), transport)
    await asyncio.wait_for(run.wait_done(), 2)
    assert run.status is RunStatus.OVERLOAD_WAITING
    assert run.result is None
    run = store.retry_run(run.id, transport)
    await asyncio.wait_for(run.wait_done(), 2)
    assert run.status is RunStatus.COMPLETED
    assert run.result is not None
    assert run.id == next(iter(store._runs))
    assert transport.requests == 11
    await store.shutdown()


@pytest.mark.skipif(
    not REAL_URL or not REAL_KEY,
    reason="ticket23 isolated PostgREST service is not configured",
)
async def test_real_public_lifecycle_persists_overload_wait_and_resumes_same_run() -> None:
    assert REAL_URL is not None
    assert REAL_KEY is not None
    transport = PublicLifecycleTransport()

    def make_app() -> Any:
        admission = SupabaseWikimediaAdmission(REAL_URL, REAL_KEY, rest_path="")
        governor = GlobalWikimediaGovernor(admission=admission)
        store = SupabaseCrawlRunStore(
            REAL_URL,
            REAL_KEY,
            rest_path="",
            governor=governor,
        )
        return create_app(
            mediawiki_transport=transport,
            run_store=store,
            governor=governor,
        )

    first = make_app()
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )
            assert created.status_code == 201, created.text
            run_id = created.json()["runId"]
            events = await collect_events(client, run_id)
            assert events[-1]["type"] == "overload_waiting"
            async with httpx.AsyncClient() as database:
                persisted = await database.get(
                    f"{REAL_URL}/crawl_runs",
                    params={"run_id": f"eq.{run_id}"},
                    headers={
                        "apikey": REAL_KEY,
                        "Authorization": f"Bearer {REAL_KEY}",
                    },
                )
            row = persisted.json()[0]
            assert row["status"] == "overload_waiting"
            assert row["retry_state"]["attempts"] == 10
            assert row["graph"] is None
            waiting_graph = await client.get(f"/api/runs/{run_id}/graph")
            assert waiting_graph.status_code == 409
            assert waiting_graph.json()["error"]["code"] == "run_overload_waiting"

    second = make_app()
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            retried = await client.post(f"/api/runs/{run_id}/retry")
            assert retried.status_code == 202
            assert retried.json()["runId"] == run_id
            events = await collect_events(client, run_id)
            assert events[-1]["type"] == "completed"
            graph = await fetch_graph(client, run_id)

    assert transport.overload_attempts == 10
    overload_requests = transport.requests[1:11]
    assert all(
        dict(request.url.params).get("prop") == "links"
        for request in overload_requests
    )
    assert {dict(request.url.params)["titles"] for request in overload_requests} == {"Hub"}
    assert {node["title"] for node in graph["nodes"]} == {"Hub", "Leaf"}
    assert len(graph["edges"]) == 1


async def test_non_throttling_failure_isolated_without_partial_graph() -> None:
    transport = ScriptedUpstream(
        [
            httpx.Response(500, json={"error": {"code": "servererror"}}),
            ok_links(),
        ]
    )
    store = InMemoryCrawlRunStore(GlobalWikimediaGovernor(DeterministicClock()))
    failed = store.start_run(CrawlRequest("Hub", 1, "es"), transport)
    successful = store.start_run(CrawlRequest("Hub", 1, "es"), transport)
    await asyncio.gather(failed.wait_done(), successful.wait_done())
    assert failed.status is RunStatus.FAILED
    assert failed.result is None
    assert successful.status is RunStatus.COMPLETED
    assert successful.result is not None
    await store.shutdown()
