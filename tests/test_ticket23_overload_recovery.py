"""Ticket 23 - durable upstream overload flow control and recovery."""

from __future__ import annotations

import asyncio

import httpx

from benchmarks.ticket19_representative import DeterministicClock
from wikigraph.crawler import CrawlRequest
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.mediawiki import MediaWikiClient, UpstreamOverload
from wikigraph.runs import InMemoryCrawlRunStore, RunStatus


class ScriptedUpstream(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        del request
        self.requests += 1
        return self.responses.pop(0)


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
