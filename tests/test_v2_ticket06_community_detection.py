"""v2 Ticket 06 — server-side community detection at Crawl run completion."""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers import collect_events, create_run, fetch_graph


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


def community_sizes(graph: dict[str, Any]) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for community_id in id_map(graph).values():
        sizes[community_id] = sizes.get(community_id, 0) + 1
    return sizes


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


async def test_same_stubbed_graph_yields_identical_ids_across_runs(client, stub):
    add_two_cluster_graph(stub)

    first = await run_graph(client, stub, seed="Seed", depth=2)
    second = await run_graph(client, stub, seed="Seed", depth=2)

    assert id_map(first) == id_map(second)
    assert first["communityCount"] == second["communityCount"]
    assert first["modularity"] == second["modularity"]


async def test_biggest_community_gets_id_0(client, stub):
    stub.add_page("Seed", links=[(0, "A1"), (0, "B1")])
    stub.add_page("A1", links=[(0, "A2"), (0, "A3"), (0, "A4")])
    stub.add_page("A2", links=[(0, "A1"), (0, "A3")])
    stub.add_page("A3", links=[(0, "A1"), (0, "A4")])
    stub.add_page("A4", links=[(0, "A1"), (0, "A2")])
    stub.add_page("B1", links=[(0, "B2")])
    stub.add_page("B2", links=[(0, "B1")])

    graph = await run_graph(client, stub, seed="Seed", depth=2)

    ids = id_map(graph)
    sizes = community_sizes(graph)
    assert sizes[0] == max(sizes.values())
    assert {"A1", "A2", "A3", "A4"} <= {
        title for title, community_id in ids.items() if community_id == 0
    }


async def test_size_ties_break_by_lexicographically_smallest_member(client, stub):
    stub.add_page("Seed", links=[(0, "P1"), (0, "Q1"), (0, "R1")])
    for cluster in "PQR":
        for i in range(1, 4):
            stub.add_page(
                f"{cluster}{i}",
                links=[(0, f"{cluster}{j}") for j in range(1, 4) if j != i],
            )

    graph = await run_graph(client, stub, seed="Seed", depth=2)

    members: dict[int, list[str]] = {}
    for node in graph["nodes"]:
        members.setdefault(node["communityId"], []).append(node["title"])
    by_id = sorted(members.items())
    size_counts: dict[int, int] = {}
    for _, titles in by_id:
        size_counts[len(titles)] = size_counts.get(len(titles), 0) + 1
    tied = [
        community_id
        for community_id, titles in by_id
        if size_counts[len(titles)] > 1 and len(titles) > 1
    ]
    assert len(tied) >= 2, members
    for (_, titles_i), (_, titles_j) in zip(by_id, by_id[1:]):
        assert len(titles_i) > len(titles_j) or (
            len(titles_i) == len(titles_j) and min(titles_i) < min(titles_j)
        ), (members)


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
    assert "detection exploded" in events[-1]["data"]["error"]

    response = await client.get(f"/api/runs/{run_id}/graph")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_failed"


async def test_all_isolated_graph_yields_singleton_communities(client, stub):
    stub.add_page("Seed", links=[(0, "Lonely")])
    stub.add_page("Lonely", links=[])

    graph = await run_graph(client, stub, seed="Seed", depth=1)

    assert graph["communityCount"] == 2
    ids = {node["communityId"] for node in graph["nodes"]}
    assert ids == {0, 1}


async def test_edgeless_graph_reports_zero_modularity(client, stub):
    stub.add_page("Seed", links=[])

    graph = await run_graph(client, stub, seed="Seed", depth=1)

    assert graph["communityCount"] == 1
    assert graph["modularity"] == 0.0
    assert graph["nodes"][0]["communityId"] == 0
