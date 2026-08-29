"""Ticket 02 — edge normalization: redirects, fragments, self-links, namespaces."""

from __future__ import annotations

from tests.helpers import create_run, fetch_graph


async def test_redirect_link_resolves_to_canonical_article(client, stub):
    stub.add_page("Barn owl", links=[(0, "Barnstar")])
    stub.add_redirect("Barnstar", "Barn star")
    stub.add_page("Barn star", links=[(0, "Barn owl")])

    run_id = await create_run(client, seed="Barn owl", depth=2)
    graph = await fetch_graph(client, run_id)

    titles = {node["title"] for node in graph["nodes"]}
    assert titles == {"Barn owl", "Barn star"}
    assert ("Barn owl", "Barn star") in {
        (edge["source"], edge["target"]) for edge in graph["edges"]
    }
    assert ("Barn owl", "Barnstar") not in {
        (edge["source"], edge["target"]) for edge in graph["edges"]
    }
    # The canonical article was crawled (its own link is in the graph), not the redirect.
    assert ("Barn star", "Barn owl") in {
        (edge["source"], edge["target"]) for edge in graph["edges"]
    }


async def test_section_link_links_to_the_article_itself(client, stub):
    stub.add_page("Owl", links=[(0, "Barn owl#Hunting")])
    stub.add_page("Barn owl")

    run_id = await create_run(client, seed="Owl", depth=2)
    graph = await fetch_graph(client, run_id)

    titles = {node["title"] for node in graph["nodes"]}
    assert "Owl#Hunting" not in titles
    assert "Barn owl" in titles
    assert ("Owl", "Barn owl") in {
        (edge["source"], edge["target"]) for edge in graph["edges"]
    }


async def test_self_link_produces_no_self_edge(client, stub):
    stub.add_page("Mirror", links=[(0, "Mirror")])

    run_id = await create_run(client, seed="Mirror", depth=1)
    graph = await fetch_graph(client, run_id)

    assert {node["title"] for node in graph["nodes"]} == {"Mirror"}
    assert graph["edges"] == []


async def test_links_to_non_article_never_become_nodes(client, stub):
    stub.add_page(
        "Barn owl",
        links=[
            (1, "Talk:Barn owl"),
            (6, "File:Barn owl"),
            (12, "Help:Barn owl"),
            (-1, "Special:RecentChanges"),
            (0, "Owl"),
        ],
    )
    stub.add_page("Talk:Barn owl", links=[(0, "Poisoned node")], namespace=1)
    stub.add_page("File:Barn owl", links=[(0, "Poisoned node")], namespace=6)

    run_id = await create_run(client, seed="Barn owl", depth=3)
    graph = await fetch_graph(client, run_id)

    titles = {node["title"] for node in graph["nodes"]}
    assert titles == {"Barn owl", "Owl"}
    assert "Poisoned node" not in titles


async def test_canonical_article_is_crawled_once_under_two_names(client, stub):
    stub.add_page(
        "Barn owl",
        links=[(0, "Barnstar"), (0, "Barn star")],
    )
    stub.add_redirect("Barnstar", "Barn star")
    stub.add_page("Barn star", links=[(0, "Owl")])

    run_id = await create_run(client, seed="Barn owl", depth=2)
    graph = await fetch_graph(client, run_id)

    titles = [node["title"] for node in graph["nodes"]]
    assert sorted(titles) == ["Barn owl", "Barn star", "Owl"]
    assert graph["crawled"] == 2
    edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
    assert edges == {("Barn owl", "Barn star"), ("Barn star", "Owl")}
