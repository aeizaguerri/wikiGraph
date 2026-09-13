"""Ticket 06 — graph readability: orientation at scale."""

from __future__ import annotations

from tests.helpers import create_run, fetch_graph


async def test_graph_payload_marks_seed_and_levels_for_rendering(client, stub):
    stub.add_page("S", links=[(0, "A")])
    stub.add_page("A", links=[(0, "B")])
    stub.add_page("B")

    graph = await fetch_graph(client, await create_run(client, seed="S", depth=2))

    nodes = {node["title"]: node for node in graph["nodes"]}
    assert nodes["S"]["isSeed"] is True
    assert nodes["S"]["level"] == 0
    assert nodes["A"]["isSeed"] is False
    assert nodes["A"]["level"] == 1
    assert nodes["B"]["isSeed"] is False
    assert nodes["B"]["level"] == 2
    assert graph["truncated"] is False


async def test_static_ui_carries_the_readability_markers(client):
    page = await client.get("/")
    assert page.status_code == 200
    html = page.text
    assert 'id="legend"' in html
    assert 'id="truncation"' in html
    assert 'id="info-card"' in html
    assert 'id="graph"' in html
    assert 'id="lens-community"' in html
    assert 'id="lens-level"' in html
