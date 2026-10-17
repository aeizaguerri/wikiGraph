from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from tests.support.api import collect_events, fetch_graph
from wikigraph.app import create_app
from wikigraph.governor import GlobalWikimediaGovernor, SupabaseWikimediaAdmission
from wikigraph.response_cache import InMemoryResponseCache
from wikigraph.runs import SupabaseCrawlRunStore

REAL_URL = os.environ.get("WIKIGRAPH_TICKET23_POSTGREST_URL")
REAL_KEY = os.environ.get("WIKIGRAPH_TICKET23_POSTGREST_KEY")


class PublicLifecycleTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.overload_attempts = 0
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        if params.get("redirects") == "1":
            titles = params["titles"].split("|")
            pages = [{"title": title, "ns": 0} for title in titles]
            return httpx.Response(200, json={"query": {"pages": pages}})
        if params.get("prop") == "links":
            if self.overload_attempts < 10:
                self.overload_attempts += 1
                return httpx.Response(
                    503,
                    headers={"Retry-After": "0.01"},
                    json={"error": {"code": "maxlag"}},
                )
            return httpx.Response(
                200,
                json={
                    "batchcomplete": True,
                    "query": {
                        "pages": [
                            {"title": "Hub", "links": [{"ns": 0, "title": "Leaf"}]}
                        ]
                    },
                },
            )
        return httpx.Response(400, json={"error": {"code": "unsupported"}})


@pytest.mark.skipif(
    not REAL_URL or not REAL_KEY,
    reason="ticket23 isolated PostgREST service is not configured",
)
async def test_real_public_lifecycle_persists_overload_wait_and_resumes_same_run() -> None:
    assert REAL_URL is not None
    assert REAL_KEY is not None
    transport = PublicLifecycleTransport()

    def make_app() -> Any:
        admission = SupabaseWikimediaAdmission(REAL_URL, REAL_KEY, rest_path="")
        governor = GlobalWikimediaGovernor(admission=admission)
        store = SupabaseCrawlRunStore(
            REAL_URL,
            REAL_KEY,
            rest_path="",
            governor=governor,
            cache=InMemoryResponseCache(),
        )
        return create_app(
            mediawiki_transport=transport,
            run_store=store,
            governor=governor,
        )

    first = make_app()
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/runs", json={"seed": "Hub", "depth": 1, "language": "es"}
            )
            assert created.status_code == 201, created.text
            run_id = created.json()["runId"]
            events = await collect_events(client, run_id)
            assert events[-1]["type"] == "overload_waiting"
            async with httpx.AsyncClient() as database:
                persisted = await database.get(
                    f"{REAL_URL}/crawl_runs",
                    params={"run_id": f"eq.{run_id}"},
                    headers={"apikey": REAL_KEY, "Authorization": f"Bearer {REAL_KEY}"},
                )
            row = persisted.json()[0]
            assert row["status"] == "overload_waiting"
            assert row["retry_state"]["attempts"] == 10
            assert row["graph"] is None
            waiting_graph = await client.get(f"/api/runs/{run_id}/graph")
            assert waiting_graph.status_code == 409
            assert waiting_graph.json()["error"]["code"] == "run_overload_waiting"

    second = make_app()
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            retried = await client.post(f"/api/runs/{run_id}/retry")
            assert retried.status_code == 202
            assert retried.json()["runId"] == run_id
            events = await collect_events(client, run_id)
            assert events[-1]["type"] == "completed"
            graph = await fetch_graph(client, run_id)

    assert transport.overload_attempts == 10
    overload_requests = transport.requests[1:11]
    assert all(dict(request.url.params).get("prop") == "links" for request in overload_requests)
    assert {dict(request.url.params)["titles"] for request in overload_requests} == {"Hub"}
    assert {node["title"] for node in graph["nodes"]} == {"Hub", "Leaf"}
    assert len(graph["edges"]) == 1
