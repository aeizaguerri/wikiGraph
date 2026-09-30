"""Ticket 19 representative cold Graph benchmark acceptance."""

from benchmarks.ticket19_representative import run_benchmark


async def test_representative_cold_graph_exercises_actual_batching():
    evidence = await run_benchmark()

    assert evidence["dataset_nodes"] == 2_500
    assert evidence["result_nodes"] == 2_500
    assert evidence["crawled"] == 1_521
    assert evidence["link_initial_batch_requests"] == 32
    assert evidence["max_sources_in_initial_link_request"] == 50
    assert evidence["max_titles_in_redirect_request"] == 50
    assert evidence["invariants"] == {
        "node_count": True,
        "unique_nodes": True,
        "edge_endpoints_admitted": True,
        "expected_edges_preserved": True,
        "redirect_aliases_absent": True,
        "complete_not_truncated": True,
        "depth_first_discovery": True,
    }
