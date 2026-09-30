"""Ticket 19 — bounded frontier and Redirect acquisition."""

from __future__ import annotations

from urllib.parse import parse_qs

from tests.helpers import create_run, fetch_graph


def _query(request):
    return parse_qs(request.url.query.decode())


async def test_frontier_links_and_redirects_are_batched_and_continuations_drained(
    client, stub
):
    stub.add_page("S", [(0, "A"), (0, "Alias")], next_batch=[(0, "B")])
    stub.add_page("A", [(0, "C")])
    stub.add_page("B")
    stub.add_page("C")
    stub.add_redirect("Alias", "B")

    graph = await fetch_graph(client, await create_run(client, seed="S", depth=1))

    assert {node["title"] for node in graph["nodes"]} == {"S", "A", "B"}
    assert {tuple(edge.values()) for edge in graph["edges"]} == {
        ("S", "A"),
        ("S", "B"),
    }
    link_requests = [request for request in stub.requests if _query(request).get("prop") == ["links"]]
    assert len(link_requests) == 2
    assert _query(link_requests[0])["titles"] == ["S"]
    assert "plcontinue" in _query(link_requests[1])

    resolve_requests = [
        request
        for request in stub.requests
        if _query(request).get("redirects") == ["1"]
        and _query(request).get("titles") != ["S"]
    ]
    assert len(resolve_requests) == 1
    assert _query(resolve_requests[0])["titles"] == ["A|Alias|B"]


async def test_large_frontier_uses_source_batches_of_at_most_fifty(client, stub):
    stub.add_page("S", [(0, f"A{i}") for i in range(101)])
    for i in range(101):
        stub.add_page(f"A{i}", [(0, f"B{i}")])
        stub.add_page(f"B{i}")

    await fetch_graph(client, await create_run(client, seed="S", depth=2, node_cap=303))

    link_requests = [request for request in stub.requests if _query(request).get("prop") == ["links"]]
    assert [len(_query(request)["titles"][0].split("|")) for request in link_requests] == [1, 50, 50, 1]


async def test_duplicate_discovery_preserves_first_depth_and_all_edges(client, stub):
    stub.add_page("S", [(0, "A"), (0, "B")])
    stub.add_page("A", [(0, "C")])
    stub.add_page("B", [(0, "C")])
    stub.add_page("C", [(0, "A")])

    graph = await fetch_graph(client, await create_run(client, seed="S", depth=3))

    levels = {node["title"]: node["level"] for node in graph["nodes"]}
    assert levels["C"] == 2
    assert {tuple(edge.values()) for edge in graph["edges"]} == {
        ("S", "A"),
        ("S", "B"),
        ("A", "C"),
        ("B", "C"),
        ("C", "A"),
    }
    resolve_requests = [
        request for request in stub.requests if _query(request).get("redirects") == ["1"]
    ]
    assert any(_query(request)["titles"] == ["C"] for request in resolve_requests)
