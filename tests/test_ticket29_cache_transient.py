from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from wikigraph.response_cache import ResponseCacheKey, SupabaseResponseCache


KEY = ResponseCacheKey("links", "en", '["Example"]', "initial")


@pytest.mark.parametrize("operation", ["get", "put"])
def test_real_http_read_timeout_is_transient(operation: str) -> None:
    class DelayedPostgrest(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(0.2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]")

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            time.sleep(0.2)
            self.send_response(201)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), DelayedPostgrest)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = httpx.Client(timeout=0.05)
    cache = SupabaseResponseCache(
        f"http://127.0.0.1:{server.server_port}", "secret", client=client
    )
    try:
        if operation == "get":
            assert cache.get(KEY) is None
        else:
            cache.put(KEY, {"query": {"pages": []}})
    finally:
        client.close()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timeout"), 503])
def test_transient_cache_read_failure_is_a_miss(failure: Exception | int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, int):
            return httpx.Response(failure, request=request)
        raise failure

    client = httpx.Client(transport=httpx.MockTransport(respond))
    cache = SupabaseResponseCache("http://cache.test", "secret", client=client)

    assert cache.get(KEY) is None


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timeout"), 503])
def test_transient_cache_write_failure_is_ignored(failure: Exception | int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, int):
            return httpx.Response(failure, request=request)
        raise failure

    client = httpx.Client(transport=httpx.MockTransport(respond))
    cache = SupabaseResponseCache("http://cache.test", "secret", client=client)

    cache.put(KEY, {"query": {"pages": []}})


@pytest.mark.parametrize("status", [401, 403, 409])
def test_cache_configuration_and_schema_failures_remain_closed(status: int) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, request=request)
        )
    )
    cache = SupabaseResponseCache("http://cache.test", "secret", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        cache.get(KEY)
