"""Pure-rule tests for deterministic community assignment."""

from wikigraph.communities import detect_communities
from wikigraph.crawler import GraphEdge, GraphNode


def test_same_stubbed_graph_yields_identical_ids_across_runs():
    nodes = [
        GraphNode(title="Seed", level=0, is_seed=True),
        *(GraphNode(title=f"{cluster}{i}", level=1, is_seed=False)
          for cluster in "PQ" for i in range(1, 4)),
    ]
    edges = [
        GraphEdge(source="Seed", target=f"{cluster}1")
        for cluster in "PQ"
    ]
    edges.extend(
        GraphEdge(source=f"{cluster}{i}", target=f"{cluster}{j}")
        for cluster in "PQ"
        for i in range(1, 4)
        for j in range(1, 4)
        if i != j
    )

    first = detect_communities(nodes, edges)
    second = detect_communities(nodes, edges)

    assert first.ids == second.ids
    assert first.count == second.count
    assert first.modularity == second.modularity


def test_biggest_community_gets_id_0():
    titles = ("Seed", "A1", "A2", "A3", "A4", "B1", "B2")
    nodes = [
        GraphNode(title=title, level=0 if title == "Seed" else 1, is_seed=title == "Seed")
        for title in titles
    ]
    edges = [
        GraphEdge("Seed", "A1"),
        GraphEdge("Seed", "B1"),
        GraphEdge("A1", "A2"),
        GraphEdge("A1", "A3"),
        GraphEdge("A1", "A4"),
        GraphEdge("A2", "A1"),
        GraphEdge("A2", "A3"),
        GraphEdge("A3", "A1"),
        GraphEdge("A3", "A4"),
        GraphEdge("A4", "A1"),
        GraphEdge("A4", "A2"),
        GraphEdge("B1", "B2"),
        GraphEdge("B2", "B1"),
    ]

    assignment = detect_communities(nodes, edges)

    sizes: dict[int, int] = {}
    for community_id in assignment.ids.values():
        sizes[community_id] = sizes.get(community_id, 0) + 1
    assert sizes[0] == max(sizes.values())
    assert {"A1", "A2", "A3", "A4"} <= {
        title for title, community_id in assignment.ids.items() if community_id == 0
    }


def test_size_ties_break_by_lexicographically_smallest_member():
    titles = ("Seed", *(f"{cluster}{i}" for cluster in "PQR" for i in range(1, 4)))
    nodes = [
        GraphNode(title=title, level=0 if title == "Seed" else 1, is_seed=title == "Seed")
        for title in titles
    ]
    edges = [GraphEdge("Seed", f"{cluster}1") for cluster in "PQR"]
    edges.extend(
        GraphEdge(f"{cluster}{i}", f"{cluster}{j}")
        for cluster in "PQR"
        for i in range(1, 4)
        for j in range(1, 4)
        if i != j
    )

    assignment = detect_communities(nodes, edges)

    members: dict[int, list[str]] = {}
    for title, community_id in assignment.ids.items():
        members.setdefault(community_id, []).append(title)
    by_id = sorted(members.items())
    size_counts: dict[int, int] = {}
    for _, member_titles in by_id:
        size_counts[len(member_titles)] = size_counts.get(len(member_titles), 0) + 1
    tied = [
        community_id
        for community_id, member_titles in by_id
        if size_counts[len(member_titles)] > 1 and len(member_titles) > 1
    ]
    assert len(tied) >= 2, members
    for (_, titles_i), (_, titles_j) in zip(by_id, by_id[1:]):
        assert len(titles_i) > len(titles_j) or (
            len(titles_i) == len(titles_j) and min(titles_i) < min(titles_j)
        ), members


def test_all_isolated_graph_yields_singleton_communities():
    nodes = [
        GraphNode("Seed", level=0, is_seed=True),
        GraphNode("Lonely", level=1, is_seed=False),
    ]

    assignment = detect_communities(nodes, [GraphEdge("Seed", "Lonely")])

    assert assignment.count == 2
    assert set(assignment.ids.values()) == {0, 1}


def test_edgeless_graph_reports_zero_modularity():
    nodes = [GraphNode("Seed", level=0, is_seed=True)]

    assignment = detect_communities(nodes, [])

    assert assignment.count == 1
    assert assignment.modularity == 0.0
    assert assignment.ids["Seed"] == 0
