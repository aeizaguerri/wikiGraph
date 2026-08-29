"""Ticket 04 — deep crawl: levels up to 3, Node cap, truncation."""

from __future__ import annotations

from tests.helpers import collect_events, create_run, fetch_graph


async def test_depth2_reaches_two_links_away_depth3_reaches_three(client, stub):
    stub.add_page("S", links=[(0, "A")])
    stub.add_page("A", links=[(0, "B")])
    stub.add_page("B", links=[(0, "C")])
    stub.add_page("C")

    run_two = await fetch_graph(
        client, await create_run(client, seed="S", depth=2)
    )
    assert {node["title"] for node in run_two["nodes"]} == {"S", "A", "B"}

    run_three = await fetch_graph(
        client, await create_run(client, seed="S", depth=3)
    )
    assert {node["title"] for node in run_three["nodes"]} == {"S", "A", "B", "C"}
    levels = {node["title"]: node["level"] for node in run_three["nodes"]}
    assert levels == {"S": 0, "A": 1, "B": 2, "C": 3}


async def test_cycle_yields_one_node_per_article_and_no_duplicate_crawl(client, stub):
    stub.add_page("S", links=[(0, "A")])
    stub.add_page("A", links=[(0, "B")])
    stub.add_page("B", links=[(0, "A")])

    graph = await fetch_graph(client, await create_run(client, seed="S", depth=3))

    titles = sorted(node["title"] for node in graph["nodes"])
    assert titles == ["A", "B", "S"]
    assert graph["crawled"] == 3
    edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
    assert edges == {("S", "A"), ("A", "B"), ("B", "A")}


async def test_node_cap_stops_discovery_and_marks_run_truncated(client, stub):
    stub.add_page(
        "Hub", links=[(0, f"Article {i}") for i in range(60)]
    )

    run_id = await create_run(client, seed="Hub", depth=2, node_cap=10)
    events = await collect_events(client, run_id)
    graph = await fetch_graph(client, run_id)

    assert events[-1] == {"type": "completed", "data": {"truncated": True}}
    assert graph["truncated"] is True
    assert len(graph["nodes"]) == 10
    assert graph["discovered"] == 10
    node_titles = {node["title"] for node in graph["nodes"]}
    assert "Hub" in node_titles
    # every edge points at a node that exists: the partial graph stays consistent
    for edge in graph["edges"]:
        assert edge["source"] in node_titles
        assert edge["target"] in node_titles


async def test_node_cap_defaults_to_500_and_is_not_hit_below_it(client, stub):
    stub.add_page("Hub", links=[(0, f"Article {i}") for i in range(499)])

    default_run = await fetch_graph(
        client, await create_run(client, seed="Hub", depth=1)
    )
    assert len(default_run["nodes"]) == 500
    assert default_run["truncated"] is False

    capped_run = await fetch_graph(
        client, await create_run(client, seed="Hub", depth=1, node_cap=300)
    )
    assert len(capped_run["nodes"]) == 300
    assert capped_run["truncated"] is True
