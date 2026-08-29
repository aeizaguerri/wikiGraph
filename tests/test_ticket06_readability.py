"""Ticket 06 — graph readability: orientation at scale.

The rendering itself is a manual smoke check (no JS tooling in v1); the seam
tests pin the contract the renderer relies on: distinct seed node, level data,
truncation flag, and the static UI markers (legend, banner, tooltip).
"""

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
    assert 'id="truncated-banner"' in html
    assert 'id="tooltip"' in html
    assert 'id="graph"' in html

    css = await client.get("/style.css")
    assert css.status_code == 200
    assert ".swatch.level-0" in css.text
    assert ".swatch.level-1" in css.text
    assert ".swatch.level-2" in css.text
    assert ".swatch.level-3" in css.text

    app_js = await client.get("/app.js")
    assert app_js.status_code == 200
    javascript = app_js.text
    assert "seed" in javascript
    assert "level-" in javascript
    assert "truncated" in javascript
    assert "tooltip" in javascript
