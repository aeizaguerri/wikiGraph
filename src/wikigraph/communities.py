"""Community detection over the crawled Graph: directed Louvain, deterministic ids."""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx
from networkx.algorithms.community import louvain_communities, modularity

from wikigraph.crawler import GraphEdge, GraphNode

LOUVAIN_SEED = 0
LOUVAIN_RESOLUTION = 1.0


@dataclass(frozen=True)
class CommunityAssignment:
    ids: dict[str, int]
    count: int
    modularity: float


def detect_communities(
    nodes: list[GraphNode], edges: list[GraphEdge]
) -> CommunityAssignment:
    """Partition the directed Graph with Louvain and normalize the ids.

    The directed Graph is fed unchanged (ADR 0003). Ids are normalized
    deterministically: communities sorted by size descending, ties broken by
    the lexicographically smallest member title, so the biggest cluster is
    always `communityId 0`.
    """
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(node.title for node in nodes)
    graph.add_edges_from((edge.source, edge.target) for edge in edges)
    partition = louvain_communities(
        graph, seed=LOUVAIN_SEED, resolution=LOUVAIN_RESOLUTION
    )
    ordered = sorted(partition, key=lambda community: (-len(community), min(community)))
    ids = {
        title: community_id
        for community_id, community in enumerate(ordered)
        for title in community
    }
    return CommunityAssignment(
        ids=ids,
        count=len(ordered),
        modularity=modularity(graph, ordered) if graph.number_of_edges() else 0.0,
    )
