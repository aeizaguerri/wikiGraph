from __future__ import annotations

import pytest

from tests.support.mediawiki import FakeMediaWiki
from wikigraph.crawler import CrawlRequest
from wikigraph.runs import InMemoryCrawlRunStore, RunStatus


@pytest.mark.asyncio
async def test_state_snapshot_is_detached_and_excludes_reconstructive_and_lifecycle_data(
    stub: FakeMediaWiki,
) -> None:
    store = InMemoryCrawlRunStore()
    run = store.start_run(CrawlRequest("Hub", 1, "en"), stub.transport)

    state = await store.get_state(run.id)

    assert state is not None
    assert state.status is RunStatus.RUNNING
    assert state.request == CrawlRequest("Hub", 1, "en")
    assert state.progress.recent == ()
    assert state.truncated is None
    assert not hasattr(state, "checkpoint")
    assert not hasattr(state, "result")
    assert not hasattr(state, "task")
    assert not hasattr(state, "subscribe")
    run.progress["recent"].append("worker mutation")
    assert state.progress.recent == ()

    await store.shutdown()


@pytest.mark.asyncio
async def test_preview_snapshot_copies_checkpoint_and_hides_it_for_expired_run(
    stub: FakeMediaWiki,
) -> None:
    store = InMemoryCrawlRunStore()
    run = store.start_run(CrawlRequest("Hub", 1, "en"), stub.transport)

    preview = await store.get_preview(run.id)

    assert preview is not None
    assert preview.state.status is RunStatus.RUNNING
    assert preview.checkpoint is not None
    assert preview.checkpoint.frontier == ("Hub",)
    assert preview.checkpoint.nodes[0].title == "Hub"
    assert isinstance(preview.checkpoint.frontier, tuple)
    assert not hasattr(preview, "task")
    run._checkpoint.frontier.append("worker mutation")
    assert preview.checkpoint.frontier == ("Hub",)

    run.status = RunStatus.EXPIRED
    expired_preview = await store.get_preview(run.id)
    assert expired_preview is not None
    assert expired_preview.state.status is RunStatus.EXPIRED
    assert expired_preview.checkpoint is None

    await store.shutdown()


@pytest.mark.asyncio
async def test_completed_graph_snapshot_is_detached_and_unknown_ids_return_none(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub", links=[(0, "Leaf")])
    stub.add_page("Leaf")
    store = InMemoryCrawlRunStore()
    run = store.start_run(CrawlRequest("Hub", 1, "en"), stub.transport)
    await run.wait_done()

    preview = await store.get_preview(run.id)
    assert preview is not None
    assert preview.checkpoint is None
    assert preview.graph is not None
    assert {node.title for node in preview.graph.nodes} == {"Hub", "Leaf"}

    graph = await store.get_graph(run.id)

    assert graph is not None
    assert graph.state.status is RunStatus.COMPLETED
    assert graph.state.truncated is run.result.truncated
    assert graph.graph is not None
    assert {node.title for node in graph.graph.nodes} == {"Hub", "Leaf"}
    assert graph.communities.count >= 1
    assert graph.communities.ids[0][0] == "Hub"
    run.result.nodes.clear()
    run.communities.ids.clear()
    assert {node.title for node in graph.graph.nodes} == {"Hub", "Leaf"}
    assert graph.communities.ids
    assert await store.get_state("missing") is None
    assert await store.get_preview("missing") is None
    assert await store.get_graph("missing") is None

    run.status = RunStatus.EXPIRED
    expired_graph = await store.get_graph(run.id)
    assert expired_graph is not None
    assert expired_graph.state.status is RunStatus.EXPIRED
    assert expired_graph.graph is None
    assert expired_graph.communities is None

    await store.shutdown()
