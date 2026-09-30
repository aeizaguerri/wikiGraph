from __future__ import annotations

import httpx
import pytest

from wikigraph.app import create_app
from wikigraph.crawler import CrawlRequest
from wikigraph.runs import (
    CrawlRun,
    InMemoryCrawlRunStore,
    PersistenceError,
    RunEvent,
    RunStatus,
)

PRIVATE_MARKER = "sentinel-private-value"
PRIVATE_URL = f"https://private.invalid/rest/v1/rpc/acquire?key={PRIVATE_MARKER}"


class RaisingTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPStatusError(
            f"upstream failed at {PRIVATE_URL}",
            request=request,
            response=httpx.Response(503, request=request),
        )


async def test_internal_http_exception_is_redacted_from_state_sse_and_graph() -> None:
    store = InMemoryCrawlRunStore()
    run = CrawlRun("synthetic-run", CrawlRequest("Synthetic", 1, "en"))
    await run.start(RaisingTransport())
    store._runs[run.id] = run
    app = create_app(run_store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        state = await client.get(f"/api/runs/{run.id}")
        events = await client.get(f"/api/runs/{run.id}/events")
        graph = await client.get(f"/api/runs/{run.id}/graph")

    assert state.status_code == 200
    assert state.json()["error"] == "The crawl run failed."
    assert events.status_code == 200
    assert '"error": "The crawl run failed."' in events.text
    assert PRIVATE_URL not in events.text
    assert PRIVATE_MARKER not in events.text
    assert graph.status_code == 409
    assert graph.json()["error"] == {
        "code": "run_failed",
        "message": "The crawl run failed.",
    }
    assert PRIVATE_URL not in state.text + graph.text
    assert PRIVATE_MARKER not in state.text + graph.text


async def test_legacy_stored_errors_and_sse_history_are_redacted() -> None:
    store = InMemoryCrawlRunStore()
    run = CrawlRun("legacy-run", CrawlRequest("Synthetic", 1, "en"))
    run.status = RunStatus.FAILED
    run.error = PRIVATE_URL
    run._events.append(RunEvent("failed", {"error": PRIVATE_URL, "detail": PRIVATE_MARKER}))
    run._done.set()
    store._runs[run.id] = run
    app = create_app(run_store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        state = await client.get(f"/api/runs/{run.id}")
        events = await client.get(f"/api/runs/{run.id}/events")
        graph = await client.get(f"/api/runs/{run.id}/graph")

    assert state.json()["error"] == "The crawl run failed."
    assert graph.status_code == 409
    assert events.text.count('"error": "The crawl run failed."') == 1
    assert PRIVATE_URL not in state.text + events.text + graph.text
    assert PRIVATE_MARKER not in events.text


async def test_generic_persistence_handler_failure_uses_safe_message() -> None:
    class FailingStore(InMemoryCrawlRunStore):
        def get(self, run_id: str):
            raise PersistenceError(f"database failure at {PRIVATE_URL}")

    app = create_app(run_store=FailingStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/runs/synthetic-run")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "persistence_unavailable",
        "message": "Crawl persistence is unavailable. Try again later.",
    }
    assert PRIVATE_URL not in response.text
    assert PRIVATE_MARKER not in response.text


async def test_safe_overload_message_and_retry_timing_are_preserved() -> None:
    store = InMemoryCrawlRunStore()
    run = CrawlRun("overload-run", CrawlRequest("Synthetic", 1, "en"))
    run.status = RunStatus.OVERLOAD_WAITING
    run.error = PRIVATE_URL
    run.retry_state = {"attempts": 10, "retry_after": 17.5}
    run.publish(
        RunEvent(
            "overload_waiting",
            {"error": PRIVATE_URL, "retryAfter": 17.5},
        )
    )
    run._done.set()
    store._runs[run.id] = run
    app = create_app(run_store=store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        state = await client.get(f"/api/runs/{run.id}")
        events = await client.get(f"/api/runs/{run.id}/events")
        graph = await client.get(f"/api/runs/{run.id}/graph")

    assert state.json()["error"] == "Wikimedia is busy; retry later."
    assert state.json()["retryAfter"] == 17.5
    assert '"error": "Wikimedia is busy; retry later."' in events.text
    assert graph.status_code == 409
    assert graph.json()["error"]["code"] == "run_overload_waiting"
    assert graph.json()["error"]["message"] == "Wikimedia is busy; retry later."
    assert PRIVATE_URL not in state.text + events.text + graph.text
