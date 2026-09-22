"""Crawl run engine: breadth-first walk over Article links, bounded by the Node cap."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from wikigraph.mediawiki import ARTICLE_LINK_BATCH_SIZE, MediaWikiClient
from wikigraph.governor import DEFAULT_GOVERNOR, GlobalWikimediaGovernor
from wikigraph.titles import clean_title

DEFAULT_NODE_CAP = 500
RECENT_FEED_SIZE = 10
ARTICLE_NAMESPACE = 0


@dataclass(frozen=True)
class CrawlRequest:
    seed: str
    depth: int
    language: str
    node_cap: int = DEFAULT_NODE_CAP


@dataclass(frozen=True)
class GraphNode:
    title: str
    level: int
    is_seed: bool


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str


@dataclass(frozen=True)
class Progress:
    crawled: int
    discovered: int
    level: int
    recent: list[str]


@dataclass
class CrawlResult:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool
    crawled: int
    discovered: int


class Crawler:
    """Walks the graph level by level; each Article is crawled exactly once."""

    def __init__(
        self,
        request: CrawlRequest,
        transport: httpx.AsyncBaseTransport | None = None,
        on_progress: Callable[[Progress], Awaitable[None]] | None = None,
        governor: GlobalWikimediaGovernor = DEFAULT_GOVERNOR,
        owner: str | None = None,
    ) -> None:
        self._request = request
        self._transport = transport
        self._on_progress = on_progress
        self._governor = governor
        self._owner = owner or request.seed

    async def crawl(self) -> CrawlResult:
        client = MediaWikiClient(
            self._request.language,
            transport=self._transport,
            governor=self._governor,
            owner=self._owner,
        )
        try:
            return await self._crawl(client)
        finally:
            await client.aclose()

    async def _crawl(self, client: MediaWikiClient) -> CrawlResult:
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        seen_edges: set[tuple[str, str]] = set()
        truncated = False
        crawled = 0
        recent: deque[str] = deque(maxlen=RECENT_FEED_SIZE)
        seed = self._request.seed
        nodes[seed] = GraphNode(seed, 0, True)
        frontier = [seed]

        for level in range(1, self._request.depth + 1):
            if truncated or not frontier:
                break
            next_frontier: list[str] = []
            for start in range(0, len(frontier), ARTICLE_LINK_BATCH_SIZE):
                source_batch = frontier[start : start + ARTICLE_LINK_BATCH_SIZE]
                links_by_source = await client.article_links_batch(source_batch)
                candidates: list[tuple[str, str]] = []
                for crawled_title in source_batch:
                    crawled += 1
                    recent.append(crawled_title)
                    candidates.extend(
                        (crawled_title, cleaned)
                        for link in links_by_source[crawled_title]
                        if link.namespace == ARTICLE_NAMESPACE
                        and (cleaned := clean_title(link.title))
                    )
                # Publish acquisition progress before redirect resolution, which
                # may wait behind the same deployment-wide request budget.
                await self._emit_progress(crawled, len(nodes), level, recent)
                if candidates:
                    truncated |= await self._discover(
                        client,
                        candidates,
                        level,
                        nodes,
                        edges,
                        seen_edges,
                        next_frontier,
                    )
                await self._emit_progress(crawled, len(nodes), level, recent)
            frontier = next_frontier

        return CrawlResult(
            nodes=list(nodes.values()),
            edges=edges,
            truncated=truncated,
            crawled=crawled,
            discovered=len(nodes),
        )

    async def _discover(
        self,
        client: MediaWikiClient,
        candidates: list[tuple[str, str]],
        level: int,
        nodes: dict[str, GraphNode],
        edges: list[GraphEdge],
        seen_edges: set[tuple[str, str]],
        next_frontier: list[str],
    ) -> bool:
        """Canonicalize candidates and grow the graph. Returns True if truncated."""
        unique_titles = list(dict.fromkeys(title for _, title in candidates))
        resolved = await client.resolve_titles(unique_titles)
        truncated = False
        for source, title in candidates:
            resolution = resolved[title]
            if resolution.missing or resolution.namespace != ARTICLE_NAMESPACE:
                continue
            target = resolution.title
            if target == source:
                continue
            if target not in nodes:
                if len(nodes) >= self._request.node_cap:
                    truncated = True
                    continue
                nodes[target] = GraphNode(target, level, False)
                next_frontier.append(target)
            edge = (source, target)
            if edge not in seen_edges:
                seen_edges.add(edge)
                edges.append(GraphEdge(source, target))
        return truncated

    async def _emit_progress(
        self, crawled: int, discovered: int, level: int, recent: deque[str]
    ) -> None:
        if self._on_progress is not None:
            await self._on_progress(
                Progress(crawled, discovered, level, list(recent))
            )
