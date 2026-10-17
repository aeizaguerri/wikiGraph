from __future__ import annotations

import httpx
import pytest

from wikigraph.response_cache import ResponseCacheKey, SupabaseResponseCache


KEY = ResponseCacheKey("links", "en", '["Example"]', "initial")


@pytest.mark.parametrize("operation", ["get", "put"])
def test_read_timeout_is_transient_and_uses_cache_budget(operation: str) -> None:
    requests: list[httpx.Request] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 0.05
        raise httpx.ReadTimeout("cache read timed out", request=request)

    client = httpx.Client(
        timeout=20.0, transport=httpx.MockTransport(timeout)
    )
    cache = SupabaseResponseCache(
        "http://cache.test",
        "secret",
        client=client,
        request_timeout_seconds=0.05,
    )
    try:
        if operation == "get":
            assert cache.get(KEY) is None
            assert requests[0].method == "GET"
        else:
            cache.put(KEY, {"query": {"pages": []}})
            assert requests[0].method == "POST"
        assert len(requests) == 1
        assert client.timeout.read == 20.0
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["timeout", 503])
def test_transient_cache_read_failure_is_a_miss(failure: str | int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, request=request)
        raise httpx.ReadTimeout("cache read timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    cache = SupabaseResponseCache("http://cache.test", "secret", client=client)
    try:
        assert cache.get(KEY) is None
        assert len(requests) == 1
        assert requests[0].method == "GET"
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["timeout", 503])
def test_transient_cache_write_failure_is_ignored(failure: str | int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, request=request)
        raise httpx.ReadTimeout("cache write timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    cache = SupabaseResponseCache("http://cache.test", "secret", client=client)
    try:
        cache.put(KEY, {"query": {"pages": []}})
        assert len(requests) == 1
        assert requests[0].method == "POST"
    finally:
        client.close()


@pytest.mark.parametrize("status", [401, 403, 409])
def test_cache_configuration_and_schema_failures_remain_closed(status: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, request=request)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    cache = SupabaseResponseCache("http://cache.test", "secret", client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError) as error:
            cache.get(KEY)
        assert error.value.response.status_code == status
        assert len(requests) == 1
    finally:
        client.close()
