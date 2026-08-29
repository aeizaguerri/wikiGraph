"""Ticket 01 — thinnest tracer: title seed, depth 1, graph through the HTTP seam."""

from __future__ import annotations

from tests.helpers import create_run, fetch_graph


async def test_depth1_run_renders_seed_plus_linked_articles(client, stub):
    stub.add_page("Barn owl", links=[(0, "Barn star"), (0, "Owl"), (0, "Bird of prey")])
    run_id = await create_run(client, seed="Barn owl", depth=1, language="es")

    graph = await fetch_graph(client, run_id)

    assert graph["seed"] == "Barn owl"
    assert graph["language"] == "es"
    assert graph["truncated"] is False
    nodes = {node["title"]: node for node in graph["nodes"]}
    assert set(nodes) == {"Barn owl", "Barn star", "Owl", "Bird of prey"}
    assert nodes["Barn owl"]["isSeed"] is True
    assert nodes["Barn owl"]["level"] == 0
    for other in ("Barn star", "Owl", "Bird of prey"):
        assert nodes[other]["isSeed"] is False
        assert nodes[other]["level"] == 1
    edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
    assert edges == {
        ("Barn owl", "Barn star"),
        ("Barn owl", "Owl"),
        ("Barn owl", "Bird of prey"),
    }


async def test_links_fetch_follows_pagination_until_exhausted(client, stub):
    stub.add_page("Hub", links=[(0, "A"), (0, "B")], next_batch=[(0, "C"), (0, "D")])
    run_id = await create_run(client, seed="Hub", depth=1)

    graph = await fetch_graph(client, run_id)

    assert {node["title"] for node in graph["nodes"]} == {"Hub", "A", "B", "C", "D"}


async def test_missing_article_is_rejected_before_a_run_starts(client, stub):
    stub.add_missing("Not a page")

    response = await client.post(
        "/api/runs", json={"seed": "Not a page", "depth": 1, "language": "es"}
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "missing_article"
    assert body["error"]["message"]


async def test_mediawiki_requests_carry_descriptive_user_agent(client, stub):
    stub.add_page("Barn owl", links=[(0, "Owl")])
    run_id = await create_run(client, seed="Barn owl", depth=1)
    await fetch_graph(client, run_id)

    assert stub.requests, "expected at least one MediaWiki request"
    for request in stub.requests:
        user_agent = request.headers["user-agent"]
        assert user_agent.startswith("wikiGraph/")
        assert len(user_agent) > len("wikiGraph/")


async def test_index_serves_the_crawl_form_and_graph_container(client):
    response = await client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'id="run-form"' in html
    assert 'id="seed"' in html
    assert "cytoscape" in html
