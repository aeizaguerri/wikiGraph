from __future__ import annotations

from urllib.parse import parse_qs

from tests.helpers import create_run, fetch_graph


def _query(request):
    return parse_qs(request.url.query.decode())


async def test_node_cap_stops_after_current_batch(client, stub):
    frontier = [f"Source {index:02}" for index in range(101)]
    stub.add_page("Seed", [(0, title) for title in frontier])
    for title in frontier:
        stub.add_page(title, [(0, f"Leaf {title}")])
        stub.add_page(f"Leaf {title}")

    run_id = await create_run(client, seed="Seed", depth=2, node_cap=120)
    graph = await fetch_graph(client, run_id)

    assert graph["truncated"] is True
    assert graph["discovered"] == 120
    assert graph["crawled"] == 51
    nodes = {node["title"] for node in graph["nodes"]}
    edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
    assert all(source in nodes and target in nodes for source, target in edges)
    assert edges == {
        *(('Seed', title) for title in frontier),
        *((title, f"Leaf {title}") for title in frontier[:18]),
    }
    assert len(edges) == 119

    link_sources = [
        title
        for request in stub.requests
        if _query(request).get("prop") == ["links"]
        for title in _query(request)["titles"][0].split("|")
    ]
    # Seed plus exactly the first 50 admitted sources are acquired. The next
    # frontier batch must not start once this batch reaches the cap.
    assert link_sources == ["Seed", *frontier[:50]]
