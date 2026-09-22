from __future__ import annotations

import asyncio
import httpx
import pytest

from tests.stub import FakeMediaWiki
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.mediawiki import MediaWikiClient, MediaWikiError
from wikigraph.response_cache import InMemoryResponseCache, ResponseCacheKey


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_cache_expires_at_seven_days_and_evicts_lru() -> None:
    clock = FakeClock()
    cache = InMemoryResponseCache(capacity=1, clock=clock)
    first = ResponseCacheKey("redirects", "en", "[\"A\"]", "initial")
    second = ResponseCacheKey("redirects", "en", "[\"B\"]", "initial")
    cache.put(first, {"ok": 1})
    cache.put(second, {"ok": 2})
    assert cache.get(first) is None
    assert cache.get(second) == {"ok": 2}
    clock.value = 7 * 24 * 60 * 60
    assert cache.get(second) is None


@pytest.mark.asyncio
async def test_concurrent_writes_remain_within_capacity() -> None:
    cache = InMemoryResponseCache(capacity=4)

    async def write(index: int) -> None:
        cache.put(ResponseCacheKey("redirects", "en", str(index), "initial"), {})
        await asyncio.sleep(0)

    await asyncio.gather(*(write(index) for index in range(50)))
    assert len(cache) == 4


@pytest.mark.asyncio
async def test_hits_skip_governor_and_failures_are_not_cached() -> None:
    stub = FakeMediaWiki()
    stub.add_page("Hub", links=[(0, "Leaf")])
    clock = FakeClock()
    cache = InMemoryResponseCache(clock=clock)
    governor = GlobalWikimediaGovernor()
    client = MediaWikiClient("en", stub.transport, governor=governor, cache=cache)
    try:
        await client.article_links("Hub")
        first_dispatches = len(governor.dispatch_log)
        await client.article_links("Hub")
        assert len(governor.dispatch_log) == first_dispatches
        assert len(stub.requests) == 1

        stub.fail_links("Broken")
        with pytest.raises(MediaWikiError):
            await client.article_links("Broken")
        stub.fail_links_status.clear()
        stub.add_page("Broken")
        await client.article_links("Broken")
        assert len(stub.requests) == 3
    finally:
        await client.aclose()
        await governor.shutdown()


@pytest.mark.asyncio
async def test_stale_and_evicted_entries_return_to_governor() -> None:
    stub = FakeMediaWiki()
    stub.add_page("A")
    stub.add_page("B")
    clock = FakeClock()
    cache = InMemoryResponseCache(capacity=1, clock=clock)
    governor = GlobalWikimediaGovernor()
    client = MediaWikiClient("en", stub.transport, governor=governor, cache=cache)
    try:
        await client.article_links("A")
        await client.article_links("B")
        await client.article_links("A")
        assert len(governor.dispatch_log) == 3
        clock.value = 7 * 24 * 60 * 60
        await client.article_links("A")
        assert len(governor.dispatch_log) == 4
    finally:
        await client.aclose()
        await governor.shutdown()


@pytest.mark.asyncio
async def test_matching_runs_keep_distinct_ids_while_sharing_upstream_cache(
    stub: FakeMediaWiki,
) -> None:
    from wikigraph.app import create_app

    stub.add_page("Hub")
    cache = InMemoryResponseCache()
    governor = GlobalWikimediaGovernor()
    from wikigraph.runs import InMemoryCrawlRunStore

    store = InMemoryCrawlRunStore(governor=governor, cache=cache)
    app = create_app(mediawiki_transport=stub.transport, run_store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            first = await http.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "en"}
            )
            second = await http.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "en"}
            )
            assert first.json()["runId"] != second.json()["runId"]
    await governor.shutdown()
