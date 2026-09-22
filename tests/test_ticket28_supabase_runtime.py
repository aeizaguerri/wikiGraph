from __future__ import annotations

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.runs import PersistenceError, SupabaseCrawlRunStore


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
        "params": {"select": "run_id", "limit": "1"},
    }
    assert len(client.requests) == 10


def test_supabase_healthcheck_fails_closed() -> None:
    store = SupabaseCrawlRunStore(
        "https://db.example", "secret", client=HealthClient(failing=True)
    )

    with pytest.raises(PersistenceError, match="persistence operation failed"):
        store.healthcheck()


def test_production_app_constructs_supabase_store_without_selection_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://db.example")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    monkeypatch.setenv("WIKIGRAPH_USER_AGENT", "wikiGraph/test")
    monkeypatch.setenv("WIKIGRAPH_IP_HASH_SECRET", "secret")

    app = create_app(production=True)

    assert app is not None
