"""v2 Ticket 06 — server-side community detection at Crawl run completion."""

from __future__ import annotations

from typing import Any

from tests.support.api import collect_events, create_run, fetch_graph


def add_two_cluster_graph(stub: Any) -> None:
    stub.add_page("Seed", links=[(0, "P1"), (0, "Q1")])
    for cluster in "PQ":
        for i in range(1, 4):
            stub.add_page(
                f"{cluster}{i}",
                links=[(0, f"{cluster}{j}") for j in range(1, 4) if j != i],
            )


async def run_graph(client: Any, stub: Any, **kwargs: Any) -> dict[str, Any]:
    run_id = await create_run(client, **kwargs)
    events = await collect_events(client, run_id)
    assert events[-1]["type"] == "completed"
    return await fetch_graph(client, run_id)


def id_map(graph: dict[str, Any]) -> dict[str, int]:
    return {node["title"]: node["communityId"] for node in graph["nodes"]}


async def test_completed_nodes_carry_community_ids_and_graph_carries_counts(
    client, stub
):
    add_two_cluster_graph(stub)

    graph = await run_graph(client, stub, seed="Seed", depth=2)

    ids = {node["title"]: node["communityId"] for node in graph["nodes"]}
    assert len(ids) == len(graph["nodes"])
    assert all(isinstance(value, int) and value >= 0 for value in ids.values())
    distinct = set(ids.values())
    assert graph["communityCount"] == len(distinct)
    assert isinstance(graph["modularity"], float)


async def test_completed_event_payload_shape_is_unchanged(client, stub):
    stub.add_page("Seed", links=[(0, "P1")])
    stub.add_page("P1", links=[])

    run_id = await create_run(client, seed="Seed", depth=1)
    events = await collect_events(client, run_id)

    assert events[-1] == {"type": "completed", "data": {"truncated": False}}


async def test_no_api_knobs_change_the_partition(client, stub):
    add_two_cluster_graph(stub)

    baseline = await run_graph(client, stub, seed="Seed", depth=2)
    payload = {
        "seed": "Seed",
        "depth": 2,
        "language": "es",
        "louvainSeed": 99,
        "resolution": 2.0,
    }
    response = await client.post("/api/runs", json=payload)
    assert response.status_code == 201, response.text
    events = await collect_events(client, response.json()["runId"])
    assert events[-1]["type"] == "completed"
    tweaked = await fetch_graph(client, response.json()["runId"])

    assert id_map(tweaked) == id_map(baseline)


async def test_detection_failure_fails_the_run(client, stub, monkeypatch):
    stub.add_page("Seed", links=[(0, "P1")])
    stub.add_page("P1", links=[])

    def explode(*args: Any) -> Any:
        raise RuntimeError("detection exploded")

    monkeypatch.setattr("wikigraph.runs.detect_communities", explode)

    run_id = await create_run(client, seed="Seed", depth=1)
    events = await collect_events(client, run_id)

    assert events[-1]["type"] == "failed"
    assert events[-1]["data"]["error"] == "The crawl run failed."
    assert "detection exploded" not in events[-1]["data"]["error"]

    response = await client.get(f"/api/runs/{run_id}/graph")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_failed"
    assert response.json()["error"]["message"] == "The crawl run failed."
    assert "detection exploded" not in response.text
