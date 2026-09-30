"""ASGI operator-cancellation checks against disposable loopback PostgREST."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid

import httpx
import pytest

from tests.stub import FakeMediaWiki
from wikigraph.app import create_app
from wikigraph.crawler import CrawlRequest
from wikigraph.governor import GlobalWikimediaGovernor, SupabaseWikimediaAdmission
from wikigraph.response_cache import InMemoryResponseCache
from wikigraph.runs import SupabaseCrawlRunStore

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
KEY = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
pytestmark = pytest.mark.skipif(
    URL != "http://127.0.0.1:33028" or not KEY,
    reason="ticket28 disposable loopback PostgREST credentials are required",
)


async def _eventually(read, predicate, *, timeout: float = 10) -> object:
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = await asyncio.to_thread(read)
        if predicate(value):
            return value
        await asyncio.sleep(0.025)
    raise AssertionError(f"condition not reached; last value: {value!r}")


def _row(run_id: str) -> dict[str, object]:
    assert URL and KEY
    response = httpx.get(
        f"{URL}/crawl_runs",
        params={"run_id": f"eq.{run_id}"},
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    assert len(rows) == 1
    return rows[0]


def _error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_operator_cancel_snapshots_token_and_cancels_durably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert URL and KEY
    governor = GlobalWikimediaGovernor(
        admission=SupabaseWikimediaAdmission(URL, KEY, rest_path="")
    )
    store = SupabaseCrawlRunStore(
        URL,
        KEY,
        rest_path="",
        governor=governor,
        cache=InMemoryResponseCache(),
        attempt_quota=7,
    )
    seed = "OperatorCancel" + uuid.uuid4().hex[:10]
    leaf = seed + " Leaf"
    stub = FakeMediaWiki()
    stub.add_page(seed, links=[(0, leaf)])
    stub.add_page(leaf)
    gate = stub.gate(leaf)
    run = store.start_run(
        CrawlRequest(seed=seed, language="en", depth=2, node_cap=10), stub.transport
    )
    cancel_calls: list[str] = []
    original_cancel = store.cancel_run

    async def record_cancel(run_id: str) -> str:
        cancel_calls.append(run_id)
        return await original_cancel(run_id)

    monkeypatch.setattr(store, "cancel_run", record_cancel)
    token = secrets.token_urlsafe(32)
    missing_app = weak_app = whitespace_app = valid_app = None
    try:
        await _eventually(
            lambda: _row(run.id),
            lambda row: row["status"] == "running"
            and row["checkpoint"].get("crawled", 0) >= 1,
        )
        await _eventually(lambda: len(stub.requests), lambda count: count >= 3)
        before = await asyncio.to_thread(_row, run.id)
        assert before["attempts_used"] > 0
        assert not gate.is_set()

        monkeypatch.delenv("WIKIGRAPH_OPERATOR_CANCEL_TOKEN", raising=False)
        missing_app = create_app(mediawiki_transport=stub.transport, run_store=store)
        monkeypatch.setenv("WIKIGRAPH_OPERATOR_CANCEL_TOKEN", "too-short")
        weak_app = create_app(mediawiki_transport=stub.transport, run_store=store)
        monkeypatch.setenv("WIKIGRAPH_OPERATOR_CANCEL_TOKEN", " " * 40)
        whitespace_app = create_app(mediawiki_transport=stub.transport, run_store=store)
        monkeypatch.setenv("WIKIGRAPH_OPERATOR_CANCEL_TOKEN", token)
        valid_app = create_app(mediawiki_transport=stub.transport, run_store=store)
        monkeypatch.setenv("WIKIGRAPH_OPERATOR_CANCEL_TOKEN", secrets.token_urlsafe(32))

        assert (
            missing_app is not None
            and weak_app is not None
            and whitespace_app is not None
            and valid_app is not None
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=missing_app), base_url="http://test"
        ) as missing_client:
            missing = await missing_client.post(f"/api/operator/runs/{run.id}/cancel")
            _error(missing, 503, "operator_cancel_unconfigured")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=weak_app), base_url="http://test"
        ) as weak_client:
            weak = await weak_client.post(f"/api/operator/runs/{run.id}/cancel")
            _error(weak, 503, "operator_cancel_unconfigured")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=whitespace_app), base_url="http://test"
        ) as whitespace_client:
            whitespace = await whitespace_client.post(
                f"/api/operator/runs/{run.id}/cancel"
            )
            _error(whitespace, 503, "operator_cancel_unconfigured")
        assert not cancel_calls

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=valid_app), base_url="http://test"
        ) as client:
            unknown_id = str(uuid.uuid4())
            missing_auth = await client.post(
                f"/api/operator/runs/{unknown_id}/cancel"
            )
            _error(missing_auth, 401, "operator_unauthorized")
            wrong_auth = await client.post(
                f"/api/operator/runs/{unknown_id}/cancel",
                headers={"Authorization": "Bearer wrong-token"},
            )
            _error(wrong_auth, 401, "operator_unauthorized")
            assert (await asyncio.to_thread(_row, run.id))["status"] == "running"
            assert not cancel_calls
            assert not gate.is_set()

            rotated_token = os.environ["WIKIGRAPH_OPERATOR_CANCEL_TOKEN"]
            rotated = await client.post(
                f"/api/operator/runs/{run.id}/cancel",
                headers={"Authorization": f"Bearer {rotated_token}"},
            )
            _error(rotated, 401, "operator_unauthorized")
            cancelled = await client.post(
                f"/api/operator/runs/{run.id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json() == {"runId": run.id, "result": "cancelled"}

            canonical = await _eventually(
                lambda: _row(run.id), lambda row: row["status"] == "recoverable"
            )
            await asyncio.wait_for(run.wait_done(), timeout=2)
            assert canonical["graph"] is None
            assert canonical["attempts_used"] == before["attempts_used"]
            assert canonical["attempt_quota"] == 7

            unknown = await client.post(
                f"/api/operator/runs/{unknown_id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )
            _error(unknown, 404, "unknown_run")
            repeated = await client.post(
                f"/api/operator/runs/{run.id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )
            _error(repeated, 409, "run_not_running")

            gate.set()
            retried = await client.post(f"/api/runs/{run.id}/retry")
            assert retried.status_code == 202, retried.text
            assert retried.json() == {"runId": run.id}
            final = await _eventually(
                lambda: _row(run.id), lambda row: row["status"] == "completed"
            )
            assert final["attempts_used"] >= before["attempts_used"]
            assert final["attempt_quota"] == 7
            assert final["graph"] is not None
    finally:
        gate.set()
        try:
            await store.shutdown()
        finally:
            await governor.shutdown()
