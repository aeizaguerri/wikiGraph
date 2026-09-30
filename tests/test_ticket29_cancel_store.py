"""Real loopback PostgREST lifecycle checks for store-level run cancellation."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
import pytest

from wikigraph.crawler import CrawlRequest
from wikigraph.governor import GlobalWikimediaGovernor, SupabaseWikimediaAdmission
from wikigraph.response_cache import InMemoryResponseCache
from wikigraph.runs import RunStatus, SupabaseCrawlRunStore
from tests.stub import FakeMediaWiki

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
KEY = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
pytestmark = pytest.mark.skipif(
    URL != "http://127.0.0.1:33028" or not KEY,
    reason="ticket28 disposable loopback PostgREST credentials are required",
)


def _headers() -> dict[str, str]:
    assert KEY is not None
    return {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def _store(
    cache: InMemoryResponseCache,
) -> tuple[SupabaseCrawlRunStore, GlobalWikimediaGovernor]:
    assert URL and KEY
    governor = GlobalWikimediaGovernor(
        admission=SupabaseWikimediaAdmission(URL, KEY, rest_path="")
    )
    store = SupabaseCrawlRunStore(
        URL, KEY, rest_path="", cache=cache, attempt_quota=7, governor=governor
    )
    return store, governor


def _row(run_id: str) -> dict[str, object]:
    assert URL
    response = httpx.get(
        f"{URL}/crawl_runs",
        params={"run_id": f"eq.{run_id}"},
        headers=_headers(),
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    assert len(rows) == 1
    return rows[0]


async def _eventually(read, predicate, *, timeout: float = 10) -> object:
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = await asyncio.to_thread(read)
        if predicate(value):
            return value
        await asyncio.sleep(0.025)
    raise AssertionError(f"condition not reached; last value: {value!r}")


async def test_cancel_stops_worker_and_same_id_retry_uses_durable_state() -> None:
    assert URL and KEY
    cache = InMemoryResponseCache()
    store, governor = _store(cache)
    seed = "Cancel" + uuid.uuid4().hex[:10]
    leaf = seed + " Leaf"
    stub = FakeMediaWiki()
    stub.add_page(seed, links=[(0, leaf)])
    stub.add_page(leaf)
    gate = stub.gate(leaf)
    request = CrawlRequest(seed=seed, language="en", depth=2, node_cap=10)
    run = store.start_run(request, stub.transport)
    try:
        await _eventually(
            lambda: _row(run.id),
            lambda row: row["status"] == "running"
            and row["checkpoint"].get("crawled", 0) >= 1,
        )
        await _eventually(lambda: len(stub.requests), lambda count: count >= 3)
        before = await asyncio.to_thread(_row, run.id)
        checkpoint = before["checkpoint"]
        attempts_used = before["attempts_used"]
        assert attempts_used > 0

        assert await store.cancel_run(run.id) == "cancelled"
        canonical = await _eventually(
            lambda: _row(run.id), lambda row: row["status"] == "recoverable"
        )
        await asyncio.wait_for(run.wait_done(), timeout=2)
        assert canonical["checkpoint"] == checkpoint
        assert canonical["graph"] is None
        assert canonical["attempts_used"] == attempts_used
        assert canonical["attempt_quota"] == 7
        assert not run.result
        assert await asyncio.to_thread(store.get, run.id) is not run
        assert (await asyncio.to_thread(store.get, run.id)).status is RunStatus.RECOVERABLE

        gate.set()
        retried = store.retry_run(run.id, stub.transport)
        await asyncio.wait_for(retried.wait_done(), timeout=20)
        assert retried.status is RunStatus.COMPLETED
        final = await asyncio.to_thread(_row, run.id)
        assert final["status"] == "completed"
        assert final["attempts_used"] >= attempts_used
        assert final["attempt_quota"] == 7
        assert final["checkpoint"]["crawled"] >= checkpoint["crawled"]
        assert final["graph"] is not None
        assert len([node for node in final["graph"]["nodes"] if node["title"] == leaf]) == 1
    finally:
        gate.set()
        try:
            await store.shutdown()
        finally:
            await governor.shutdown()


async def test_cancel_unknown_and_terminal_run_results() -> None:
    assert URL and KEY
    store, governor = _store(InMemoryResponseCache())
    try:
        assert await store.cancel_run(str(uuid.uuid4())) == "not_found"
        stub = FakeMediaWiki()
        seed = "Terminal" + uuid.uuid4().hex[:10]
        stub.add_page(seed)
        run = store.start_run(
            CrawlRequest(seed=seed, language="en", depth=1, node_cap=2), stub.transport
        )
        await asyncio.wait_for(run.wait_done(), timeout=10)
        assert run.status is RunStatus.COMPLETED
        assert await store.cancel_run(run.id) == "not_running"
    finally:
        try:
            await store.shutdown()
        finally:
            await governor.shutdown()


async def test_remote_store_can_cancel_without_local_worker() -> None:
    assert URL and KEY
    owner, owner_governor = _store(InMemoryResponseCache())
    remote, remote_governor = _store(InMemoryResponseCache())
    seed = "RemoteCancel" + uuid.uuid4().hex[:10]
    leaf = seed + " Leaf"
    stub = FakeMediaWiki()
    stub.add_page(seed, links=[(0, leaf)])
    stub.add_page(leaf)
    gate = stub.gate(leaf)
    run = owner.start_run(
        CrawlRequest(seed=seed, language="en", depth=2, node_cap=10), stub.transport
    )
    try:
        await _eventually(
            lambda: _row(run.id),
            lambda row: row["status"] == "running"
            and row["checkpoint"].get("crawled", 0) >= 1,
        )
        assert await remote.cancel_run(run.id) == "cancelled"
        row = await _eventually(
            lambda: _row(run.id), lambda value: value["status"] == "recoverable"
        )
        assert row["graph"] is None
    finally:
        gate.set()
        try:
            await owner.shutdown()
        finally:
            try:
                await owner_governor.shutdown()
            finally:
                try:
                    await remote.shutdown()
                finally:
                    await remote_governor.shutdown()
