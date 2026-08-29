"""Helpers to drive the app exactly the way the browser does: the HTTP/SSE seam."""

from __future__ import annotations

import json
from typing import Any

import httpx

from wikigraph.runs import TERMINAL_EVENT_TYPES


async def create_run(
    client: httpx.AsyncClient,
    *,
    seed: str,
    depth: int = 1,
    language: str = "es",
    node_cap: int | None = None,
) -> str:
    payload: dict[str, Any] = {"seed": seed, "depth": depth, "language": language}
    if node_cap is not None:
        payload["nodeCap"] = node_cap
    response = await client.post("/api/runs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["runId"]


async def fetch_graph(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/runs/{run_id}/graph")
    assert response.status_code == 200, response.text
    return response.json()


async def collect_events(client: httpx.AsyncClient, run_id: str) -> list[dict[str, Any]]:
    """Consume the SSE stream of a run until its terminal event."""
    events: list[dict[str, Any]] = []
    async with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        assert response.status_code == 200, response.text
        current: dict[str, Any] = {}
        async for line in response.aiter_lines():
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current["type"] = line[len("event:") :].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line[len("data:") :].strip())
            elif line == "":
                if current:
                    events.append(current)
                    if current.get("type") in TERMINAL_EVENT_TYPES:
                        return events
                    current = {}
    return events
