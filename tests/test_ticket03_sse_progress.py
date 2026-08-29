"""Ticket 03 — background Crawl runs + SSE live progress."""

from __future__ import annotations

import asyncio

import pytest

from tests.helpers import collect_events, create_run, fetch_graph


async def test_submit_returns_immediately_with_a_run_identifier(client, stub):
    gate = stub.gate("Barn owl")
    stub.add_page("Barn owl", links=[(0, "Owl")])

    response = await client.post(
        "/api/runs", json={"seed": "Barn owl", "depth": 1, "language": "es"}
    )

    assert response.status_code == 201
    assert response.json()["runId"]
    assert not gate.is_set(), "the crawl must still be pending"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            client.get(f"/api/runs/{response.json()['runId']}/graph"), timeout=0.5
        )

    gate.set()
    graph = await fetch_graph(client, response.json()["runId"])
    assert {node["title"] for node in graph["nodes"]} == {"Barn owl", "Owl"}


async def test_progress_events_carry_counters_depth_and_recent(client, stub):
    stub.add_page("Hub", links=[(0, "A"), (0, "B")])
    stub.add_page("A", links=[(0, "C")])
    stub.add_page("B", links=[(0, "D")])

    run_id = await create_run(client, seed="Hub", depth=2)
    events = await collect_events(client, run_id)

    assert events, "expected at least one event"
    terminal = events[-1]
    assert terminal["type"] == "completed"
    assert terminal["data"] == {"truncated": False}

    progress_events = [event for event in events if event["type"] == "progress"]
    assert progress_events, "expected progress events before completion"
    crawled = [event["data"]["crawled"] for event in progress_events]
    discovered = [event["data"]["discovered"] for event in progress_events]
    depths = [event["data"]["depth"] for event in progress_events]
    assert all(b >= a for a, b in zip(crawled, crawled[1:])), crawled
    assert all(b >= a for a, b in zip(discovered, discovered[1:])), discovered
    assert all(b >= a for a, b in zip(depths, depths[1:])), depths
    assert crawled[0] >= 1

    final = progress_events[-1]["data"]
    assert final["crawled"] == 3
    assert final["discovered"] == 5
    seen_recent = {title for event in progress_events for title in event["data"]["recent"]}
    assert {"Hub", "A", "B"} <= seen_recent
    for event in progress_events:
        assert "percent" not in event["data"]
        assert len(event["data"]["recent"]) <= 10


async def test_failed_run_terminates_the_stream_with_an_error(client, stub):
    stub.add_page("Barn owl")
    stub.fail_links("Barn owl", 500)

    run_id = await create_run(client, seed="Barn owl", depth=1)
    events = await collect_events(client, run_id)

    assert events[-1]["type"] == "failed"
    assert events[-1]["data"]["error"]

    response = await client.get(f"/api/runs/{run_id}/graph")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_failed"


async def test_late_subscriber_receives_replayed_events_including_terminal(client, stub):
    stub.add_page("Barn owl", links=[(0, "Owl")])

    run_id = await create_run(client, seed="Barn owl", depth=1)
    await fetch_graph(client, run_id)

    events = await collect_events(client, run_id)

    assert events[-1]["type"] == "completed"
    assert any(event["type"] == "progress" for event in events)
