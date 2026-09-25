from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.crawler import CrawlRequest, Progress
from wikigraph.mediawiki import MediaWikiClient
from wikigraph.response_cache import ResponseCacheKey, SupabaseResponseCache
from wikigraph.runs import CrawlRun, PersistenceError, SupabaseCrawlRunStore


class HealthClient:
    def __init__(self, failing: bool = False) -> None:
        self.failing = failing
        self.requests: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append({"method": method, "url": url, **kwargs})
        if self.failing:
            request = httpx.Request(method, url)
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, request=httpx.Request(method, url), json=[])


def test_supabase_healthcheck_uses_canonical_postgrest_table() -> None:
    client = HealthClient()
    store = SupabaseCrawlRunStore("https://db.example", "secret", client=client)

    store.healthcheck()

    assert client.requests[0] == {
        "method": "GET",
        "url": "https://db.example/rest/v1/crawl_runs",
        "headers": {
            "apikey": "secret",
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
        },
        "params": {"select": "run_id,owner_token,owner_version,checkpoint", "limit": "1"},
    }
    assert len(client.requests) == 1


def test_supabase_healthcheck_fails_closed() -> None:
    store = SupabaseCrawlRunStore(
        "https://db.example", "secret", client=HealthClient(failing=True)
    )

    with pytest.raises(PersistenceError, match="persistence operation failed"):
        store.healthcheck()


def test_startup_schema_validation_retains_all_runtime_probes() -> None:
    client = HealthClient()
    store = SupabaseCrawlRunStore("https://db.example", "secret", client=client)

    store.validate_runtime_schema()

    assert len(client.requests) == 10


def test_readyz_offloads_slow_canonical_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://db.example")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    monkeypatch.setenv("WIKIGRAPH_USER_AGENT", "wikiGraph/test")
    monkeypatch.setenv("WIKIGRAPH_IP_HASH_SECRET", "secret")
    entered = threading.Event()
    release = threading.Event()

    class SlowClient(HealthClient):
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            entered.set()
            release.wait(timeout=3)
            return super().request(method, url, **kwargs)

    store = SupabaseCrawlRunStore(
        "https://db.example", "secret", client=SlowClient()
    )
    app = create_app(production=True, run_store=store)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ready = asyncio.create_task(client.get("/readyz"))
            assert await asyncio.to_thread(entered.wait, 1)
            started = time.monotonic()
            health = await client.get("/healthz")
            assert time.monotonic() - started < 0.2
            assert health.status_code == 200
            release.set()
            assert (await ready).status_code == 200

    asyncio.run(exercise())


def test_readyz_returns_503_when_canonical_contract_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://db.example")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    monkeypatch.setenv("WIKIGRAPH_USER_AGENT", "wikiGraph/test")
    monkeypatch.setenv("WIKIGRAPH_IP_HASH_SECRET", "secret")
    app = create_app(
        production=True,
        run_store=SupabaseCrawlRunStore(
            "https://db.example", "secret", client=HealthClient(failing=True)
        ),
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/readyz")
            assert response.status_code == 503

    asyncio.run(exercise())


def test_durable_cache_lookup_does_not_block_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowCache(SupabaseResponseCache):
        def __init__(self) -> None:
            pass

        def get(self, key: ResponseCacheKey) -> dict[str, object] | None:
            entered.set()
            release.wait(timeout=3)
            return {"cached": True}

    client = MediaWikiClient("en", cache=SlowCache())

    async def exercise() -> None:
        params = {"titles": "Argentina"}
        key = ResponseCacheKey("test", "en", "Argentina", "")
        lookup = asyncio.create_task(client._cached_get(params, key))
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        release.set()
        assert await lookup == {"cached": True}
        await client.aclose()

    asyncio.run(exercise())


def test_durable_cache_write_does_not_block_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowCache(SupabaseResponseCache):
        def __init__(self) -> None:
            pass

        def get(self, key: ResponseCacheKey) -> dict[str, object] | None:
            return None

        def put(self, key: ResponseCacheKey, value: dict[str, object]) -> None:
            entered.set()
            release.wait(timeout=3)

    client = MediaWikiClient("en", cache=SlowCache())

    async def fake_get(params: dict[str, str]) -> dict[str, object]:
        return {"response": "fresh"}

    client._get = fake_get  # type: ignore[method-assign]

    async def exercise() -> None:
        key = ResponseCacheKey("test", "en", "Argentina", "")
        lookup = asyncio.create_task(client._cached_get({"titles": "Argentina"}, key))
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        release.set()
        assert await lookup == {"response": "fresh"}
        await client.aclose()

    asyncio.run(exercise())


def test_durable_progress_persistence_does_not_block_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()

    def persist(progress: Progress) -> None:
        entered.set()
        release.wait(timeout=3)

    run = CrawlRun(
        "run-id",
        CrawlRequest(seed="Argentina", depth=1, language="en"),
        persist_progress=persist,
    )

    async def exercise() -> None:
        recording = asyncio.create_task(
            run._record_progress(Progress(1, 2, 1, ["Argentina"]))
        )
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        release.set()
        await recording
        assert run.progress["crawled"] == 1

    asyncio.run(exercise())


def test_production_app_constructs_supabase_store_without_selection_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://db.example")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    monkeypatch.setenv("WIKIGRAPH_USER_AGENT", "wikiGraph/test")
    monkeypatch.setenv("WIKIGRAPH_IP_HASH_SECRET", "secret")

    app = create_app(production=True)

    assert app is not None
