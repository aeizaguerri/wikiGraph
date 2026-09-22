"""Ticket 25 — the browser-facing durable waiting lifecycle."""

from __future__ import annotations

from tests.helpers import collect_events, create_run, fetch_graph


async def test_run_state_and_preview_are_available_before_completion(client, stub):
    gate = stub.gate("Durable waiting")
    stub.add_page("Durable waiting", links=[(0, "Checkpoint")])
    run_id = await create_run(client, seed="Durable waiting", depth=1)

    state = await client.get(f"/api/runs/{run_id}")
    assert state.status_code == 200
    assert state.json()["runId"] == run_id
    assert state.json()["status"] == "running"

    preview = await client.get(f"/api/runs/{run_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["complete"] is False
    assert preview.json()["runId"] == run_id
    assert {node["title"] for node in preview.json()["nodes"]} == {"Durable waiting"}

    gate.set()
    graph = await fetch_graph(client, run_id)
    assert graph["runId"] == run_id


async def test_reopening_completed_run_keeps_identity_and_does_not_launch(client, stub):
    stub.add_page("Reopen me", links=[(0, "Saved link")])
    run_id = await create_run(client, seed="Reopen me", depth=1)
    assert (await collect_events(client, run_id))[-1]["type"] == "completed"

    reopened = await client.get(f"/api/runs/{run_id}")
    graph = await client.get(f"/api/runs/{run_id}/graph")
    preview = await client.get(f"/api/runs/{run_id}/preview")
    assert reopened.json()["status"] == "completed"
    assert graph.json()["runId"] == run_id
    assert preview.json()["complete"] is True
    assert preview.json()["runId"] == run_id
