"""Execute the representative cold 2,500-Article ticket 19 crawl shape.

This is an actual Crawler run against a deterministic MediaWiki wire fixture,
not a request-plan calculation.  The fixture matches the level sizes and
redirect distribution from the ignored historical asset at
``.scratch/sustainable-crawl-architecture/benchmark_representative_graph_builds.py``
while implementing batched titles and one continuation stream per source
Article.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

from wikigraph.crawler import CrawlRequest, Crawler

TOTAL_NODES = 2_500
LINK_PAGE_SIZE = 4
LEVEL_SIZES = [1, 20, 100, 400, 1_000, 979]


@dataclass(frozen=True)
class Dataset:
    levels: list[list[str]]
    links: dict[str, list[str]]
    redirects: dict[str, str]


def build_dataset() -> Dataset:
    levels: list[list[str]] = []
    cursor = 0
    for size in LEVEL_SIZES:
        level = [f"Article {cursor + index:04d}" for index in range(size)]
        levels.append(level)
        cursor += size

    links: dict[str, list[str]] = {title: [] for level in levels for title in level}
    redirects: dict[str, str] = {}
    for sources, targets in zip(levels[:-1], levels[1:], strict=True):
        fanout = math.ceil(len(targets) / len(sources))
        for source_index, source in enumerate(sources):
            target_indexes = [
                (source_index * fanout + offset) % len(targets)
                for offset in range(fanout)
            ]
            target_indexes += [
                (source_index * 3 + offset) % len(targets) for offset in range(4)
            ]
            for target_index in dict.fromkeys(target_indexes):
                target = targets[target_index]
                raw_title = target
                if int(target.rsplit(" ", 1)[1]) % 10 == 0:
                    raw_title = f"Alias {target}"
                    redirects[raw_title] = target
                links[source].append(raw_title)
    return Dataset(levels, links, redirects)


class RepresentativeTransport(httpx.AsyncBaseTransport):
    """Deterministic MediaWiki transport with real batch/continuation shape."""

    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset
        self.requests: list[dict[str, Any]] = []
        self._continuations: dict[str, deque[tuple[str, int]]] = {}
        self._next_token = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        kind = "links" if params.get("prop") == "links" else "resolve"
        self.requests.append({"kind": kind, "params": params})
        if kind == "links":
            return self._links_response(params)
        return self._resolve_response(params)

    def _links_response(self, params: dict[str, str]) -> httpx.Response:
        token = params.get("plcontinue")
        if token is None:
            titles = params["titles"].split("|")
            pending: deque[tuple[str, int]] = deque()
            pages = []
            for title in titles:
                chunks = self._chunks(title)
                pages.append(self._page(title, chunks[0]))
                if len(chunks) > 1:
                    pending.append((title, 1))
        else:
            pending = self._continuations.pop(token)
            title, chunk_index = pending.popleft()
            chunks = self._chunks(title)
            pages = [self._page(title, chunks[chunk_index])]
            if chunk_index + 1 < len(chunks):
                pending.append((title, chunk_index + 1))

        body: dict[str, Any] = {"batchcomplete": True, "query": {"pages": pages}}
        if pending:
            next_token = self._new_token()
            self._continuations[next_token] = pending
            body["continue"] = {
                "plcontinue": next_token,
                "pltitles": pending[0][0],
                "continue": "-||",
            }
        return httpx.Response(200, json=body)

    def _chunks(self, title: str) -> list[list[str]]:
        links = self.dataset.links.get(title, [])
        return [links[start : start + LINK_PAGE_SIZE] for start in range(0, len(links), LINK_PAGE_SIZE)] or [[]]

    @staticmethod
    def _page(title: str, links: list[str]) -> dict[str, Any]:
        return {
            "title": title,
            "ns": 0,
            "links": [{"ns": 0, "title": link} for link in links],
        }

    def _resolve_response(self, params: dict[str, str]) -> httpx.Response:
        pages = []
        redirect_rows = []
        for requested in params["titles"].split("|"):
            canonical = self.dataset.redirects.get(requested, requested)
            if canonical != requested:
                redirect_rows.append({"from": requested, "to": canonical})
            pages.append({"title": canonical, "ns": 0})
        return httpx.Response(
            200,
            json={
                "batchcomplete": True,
                "query": {
                    "pages": pages,
                    "redirects": redirect_rows,
                    "normalized": [],
                },
            },
        )

    def _new_token(self) -> str:
        self._next_token += 1
        return f"representative-{self._next_token}"


def expected_edges(dataset: Dataset) -> set[tuple[str, str]]:
    return {
        (source, dataset.redirects.get(raw_target, raw_target))
        for sources in dataset.levels[:-1]
        for source in sources
        for raw_target in dataset.links[source]
    }


async def run_benchmark() -> dict[str, Any]:
    dataset = build_dataset()
    transport = RepresentativeTransport(dataset)
    result = await Crawler(
        CrawlRequest("Article 0000", len(dataset.levels) - 1, "en", TOTAL_NODES),
        transport=transport,
    ).crawl()
    node_titles = {node.title for node in result.nodes}
    edge_pairs = {(edge.source, edge.target) for edge in result.edges}
    expected_levels = {
        title: level for level, titles in enumerate(dataset.levels) for title in titles
    }
    link_requests = [item for item in transport.requests if item["kind"] == "links"]
    initial_link_requests = [
        item for item in link_requests if "plcontinue" not in item["params"]
    ]
    resolve_requests = [item for item in transport.requests if item["kind"] == "resolve"]
    initial_source_counts = [
        len(item["params"]["titles"].split("|")) for item in initial_link_requests
    ]
    resolve_batch_counts = [
        len(item["params"]["titles"].split("|")) for item in resolve_requests
    ]
    invariants = {
        "node_count": len(node_titles) == TOTAL_NODES,
        "unique_nodes": len(result.nodes) == len(node_titles),
        "edge_endpoints_admitted": all(
            edge.source in node_titles and edge.target in node_titles
            for edge in result.edges
        ),
        "expected_edges_preserved": edge_pairs == expected_edges(dataset),
        "redirect_aliases_absent": not any(
            node.title in dataset.redirects for node in result.nodes
        ),
        "complete_not_truncated": not result.truncated,
        "depth_first_discovery": all(
            node.level == expected_levels[node.title] for node in result.nodes
        ),
    }
    return {
        "dataset_nodes": TOTAL_NODES,
        "result_nodes": len(result.nodes),
        "result_edges": len(result.edges),
        "crawled": result.crawled,
        "link_requests": len(link_requests),
        "link_initial_batch_requests": len(initial_link_requests),
        "link_continuation_requests": len(link_requests) - len(initial_link_requests),
        "max_sources_in_initial_link_request": max(initial_source_counts),
        "redirect_requests": len(resolve_requests),
        "redirect_titles": sum(resolve_batch_counts),
        "max_titles_in_redirect_request": max(resolve_batch_counts),
        "invariants": invariants,
    }


def main() -> None:
    print(json.dumps(asyncio.run(run_benchmark()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
