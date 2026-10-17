from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

from tests.support.api import create_run, fetch_graph
from wikigraph.crawler import CrawlCheckpoint, Crawler, CrawlRequest, GraphNode
from wikigraph.governor import GlobalWikimediaGovernor


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


async def test_truncated_checkpoint_resumes_without_reacquiring_sources(stub):
    # Cancellation at the public checkpoint callback models interruption after
    # acquisition; resume must preserve the partial graph without fetching again.
    stub.add_page("Seed", [(0, "A"), (0, "B")])
    stub.add_page("A", [(0, "Leaf")])
    stub.add_page("B")
    stub.add_page("Leaf")
    request = CrawlRequest("Seed", depth=2, language="en", node_cap=2)
    governor = GlobalWikimediaGovernor()
    captured: list[CrawlCheckpoint] = []

    async def interrupt_after_truncation(checkpoint: CrawlCheckpoint) -> None:
        if checkpoint.truncated:
            captured.append(checkpoint)
            raise asyncio.CancelledError

    try:
        await Crawler(
            request, transport=stub.transport, governor=governor,
            on_checkpoint=interrupt_after_truncation,
        ).crawl()
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("crawl should stop at the truncated checkpoint")

    assert len(captured) == 1
    checkpoint = captured[0]
    requests_at_checkpoint = len(stub.requests)
    resumed = await Crawler(
        request, transport=stub.transport, governor=governor, checkpoint=checkpoint,
    ).crawl()

    assert len(stub.requests) == requests_at_checkpoint
    assert checkpoint.truncated is True
    assert checkpoint.crawled == 1
    assert checkpoint.nodes == [GraphNode("Seed", 0, True), GraphNode("A", 1, False)]
    assert [(edge.source, edge.target) for edge in checkpoint.edges] == [("Seed", "A")]
    assert resumed.truncated is True
    assert resumed.crawled == 1
    assert resumed.discovered == 2
    assert {node.title for node in resumed.nodes} == {"Seed", "A"}
    assert {(edge.source, edge.target) for edge in resumed.edges} == {("Seed", "A")}
