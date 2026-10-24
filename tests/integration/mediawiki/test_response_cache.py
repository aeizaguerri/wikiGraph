from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from tests.support.mediawiki import FakeMediaWiki
from wikigraph.governor import GlobalWikimediaGovernor
from wikigraph.mediawiki import ArticleLink, MediaWikiClient, MediaWikiError
from wikigraph.response_cache import (
    InMemoryResponseCache,
    ResponseCacheKey,
    SupabaseResponseCache,
)


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


@pytest.mark.parametrize("adapter", ["memory", "postgrest"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "links"),
        ("edition", "es"),
        ("normalized_request", '["Other"]'),
        ("continuation", "next-page"),
        ("schema_version", "2"),
    ],
    ids=["operation", "edition", "titles", "continuation", "schema"],
)
def test_cache_identities_do_not_overwrite_other_responses(adapter: str, field: str, value: str) -> None:
    # Treat the durable key as opaque: the server stores what the client sends,
    # without reproducing its hash algorithm in the expected result.
    rows = {}
    methods = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            row = json.loads(request.content)
            rows[row["cache_key"]] = row
            return httpx.Response(201, json=[row])
        assert request.method == "GET"
        lookup = request.url.params["cache_key"]
        assert lookup.startswith("eq.")
        row = rows.get(lookup[3:])
        return httpx.Response(200, json=[row] if row else [])

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        cache = (InMemoryResponseCache() if adapter == "memory" else
                 SupabaseResponseCache("https://cache.test", "secret", client=http))
        first = ResponseCacheKey("redirects", "en", '["Hub"]', "initial")
        other = replace(first, **{field: value})

        cache.put(first, {"article": "First response"})
        cache.put(other, {"article": "Other response"})
        assert cache.get(first) == {"article": "First response"}
        assert cache.get(other) == {"article": "Other response"}
        if adapter == "postgrest":
            # Hits must not touch the durable row or incur a prune-trigger write.
            assert methods == ["POST", "POST", "GET", "GET"]


@pytest.mark.asyncio
async def test_concurrent_writes_remain_within_capacity() -> None:
    cache = InMemoryResponseCache(capacity=4)

    async def write(index: int) -> None:
        cache.put(ResponseCacheKey("redirects", "en", str(index), "initial"), {})
        await asyncio.sleep(0)

    await asyncio.gather(*(write(index) for index in range(50)))
    assert len(cache) == 4


@pytest.mark.asyncio
async def test_durable_cache_hit_returns_links_without_provider_charge() -> None:
    cache_requests: list[httpx.Request] = []
    provider = FakeMediaWiki()

    def read_cached_response(request: httpx.Request) -> httpx.Response:
        cache_requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "response": {
                        "query": {
                            "pages": [
                                {
                                    "title": "Hub",
                                    "links": [{"title": "Leaf", "ns": 0}],
                                }
                            ]
                        }
                    },
                    "expires_at": "2099-01-01T00:00:00+00:00",
                }
            ],
            request=request,
        )

    cache_http = httpx.Client(transport=httpx.MockTransport(read_cached_response))
    cache = SupabaseResponseCache("https://db.example", "secret", client=cache_http)
    governor = GlobalWikimediaGovernor()
    client = MediaWikiClient("en", provider.transport, governor=governor, cache=cache)
    try:
        assert await client.article_links("Hub") == [ArticleLink("Leaf", 0)]
        assert len(cache_requests) == 1
        assert cache_requests[0].method == "GET"
        assert cache_requests[0].url.path == "/rest/v1/upstream_response_cache"
        assert provider.requests == []
        assert governor.dispatch_log == []
    finally:
        await client.aclose()
        cache.close()
        await governor.shutdown()
        cache_http.close()


@pytest.mark.asyncio
async def test_hits_skip_governor_and_failures_are_not_cached() -> None:
    stub = FakeMediaWiki()
    stub.add_page("Hub", links=[(0, "Leaf")])
    clock = FakeClock()
    cache = InMemoryResponseCache(clock=clock)
    governor = GlobalWikimediaGovernor()
    client = MediaWikiClient("en", stub.transport, governor=governor, cache=cache)
    try:
        expected = [ArticleLink("Leaf", 0)]
        assert await client.article_links("Hub") == expected
        first_dispatches = len(governor.dispatch_log)
        assert await client.article_links("Hub") == expected
        assert len(governor.dispatch_log) == first_dispatches
        assert len(stub.requests) == 1

        stub.fail_links("Broken")
        with pytest.raises(MediaWikiError, match="HTTP 500"):
            await client.article_links("Broken")
        stub.fail_links_status.clear()
        stub.add_page("Broken")
        assert await client.article_links("Broken") == []
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
