"""Canned MediaWiki API served at the transport layer.

Tests stub the MediaWiki API here, so the whole backend is exercised through
the HTTP/SSE seam with engineered responses per scenario.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class CannedLink:
    namespace: int
    title: str


@dataclass
class CannedPage:
    title: str
    namespace: int = 0
    links: list[CannedLink] = field(default_factory=list)
    next_batch: list[CannedLink] | None = None


def _normalize(title: str) -> str:
    return title.replace("_", " ")


class FakeMediaWiki:
    """In-memory MediaWiki API surface keyed by the requests the app makes."""

    def __init__(self) -> None:
        self.pages: dict[str, CannedPage] = {}
        self.redirects: dict[str, str] = {}
        self.missing: set[str] = set()
        self.gates: dict[str, asyncio.Event] = {}
        self.fail_links_status: dict[str, int] = {}
        self.requests: list[httpx.Request] = []

    def add_page(
        self,
        title: str,
        links: list[tuple[int, str]] | None = None,
        *,
        namespace: int = 0,
        next_batch: list[tuple[int, str]] | None = None,
    ) -> CannedPage:
        page = CannedPage(
            title=title,
            namespace=namespace,
            links=[CannedLink(ns, link_title) for ns, link_title in (links or [])],
            next_batch=(
                [CannedLink(ns, link_title) for ns, link_title in next_batch]
                if next_batch
                else None
            ),
        )
        self.pages[title] = page
        return page

    def add_redirect(self, from_title: str, to_title: str) -> None:
        self.redirects[from_title] = to_title

    def add_missing(self, title: str) -> None:
        self.missing.add(title)

    def gate(self, title: str) -> asyncio.Event:
        event = asyncio.Event()
        self.gates[title] = event
        return event

    def fail_links(self, title: str, status: int = 500) -> None:
        self.fail_links_status[title] = status

    @property
    def transport(self) -> httpx.AsyncBaseTransport:
        return _StubTransport(self)

    def _resolve(self, title: str) -> tuple[str, list[tuple[str, str]]]:
        hops: list[tuple[str, str]] = []
        current = _normalize(title)
        seen: set[str] = set()
        while current in self.redirects and current not in seen:
            seen.add(current)
            target = _normalize(self.redirects[current])
            hops.append((current, target))
            current = target
        return current, hops

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        if params.get("action") != "query":
            return self._bad_request("unsupportedaction", params)
        if params.get("prop") == "links":
            return await self._links_response(params)
        if params.get("redirects") == "1":
            return self._resolve_response(params)
        return self._bad_request("unsupportedquery", params)

    def _bad_request(self, code: str, params: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": code, "info": f"stub cannot handle {sorted(params)!r}"}},
        )

    async def _links_response(self, params: dict[str, Any]) -> httpx.Response:
        title = _normalize(params.get("titles", ""))
        gate = self.gates.get(title)
        if gate is not None:
            await gate.wait()
        failure_status = self.fail_links_status.get(title)
        if failure_status is not None:
            return httpx.Response(
                failure_status,
                json={"error": {"code": "cannedfailure", "info": "stubbed MediaWiki failure"}},
            )
        page = self.pages.get(title)
        if page is None or title in self.missing:
            body: dict[str, Any] = {
                "batchcomplete": True,
                "query": {"pages": [{"title": title, "ns": 0, "missing": True}]},
            }
            return httpx.Response(200, json=body)

        requested_continue = params.get("plcontinue")
        links = page.links
        if requested_continue and page.next_batch is not None:
            links = page.next_batch
        entries = [{"ns": link.namespace, "title": link.title} for link in links]
        body = {
            "batchcomplete": True,
            "query": {
                "pages": [
                    {"title": page.title, "ns": page.namespace, "links": entries},
                ],
            },
        }
        if not requested_continue and page.next_batch is not None:
            body["continue"] = {"plcontinue": "stub-batch-2", "continue": "-||"}
        return httpx.Response(200, json=body)

    def _resolve_response(self, params: dict[str, Any]) -> httpx.Response:
        titles = params.get("titles", "").split("|")
        pages: list[dict[str, Any]] = []
        redirects: list[dict[str, str]] = []
        normalized: list[dict[str, str]] = []
        for title in titles:
            norm = _normalize(title)
            if norm != title:
                normalized.append({"from": title, "to": norm})
            final, hops = self._resolve(norm)
            redirects.extend({"from": source, "to": target} for source, target in hops)
            if final in self.missing:
                pages.append({"title": final, "ns": 0, "missing": True})
                continue
            page = self.pages.get(final)
            namespace = page.namespace if page is not None else 0
            pages.append({"title": final, "ns": namespace})
        body = {
            "batchcomplete": True,
            "query": {"pages": pages, "redirects": redirects, "normalized": normalized},
        }
        return httpx.Response(200, json=body)


class _StubTransport(httpx.AsyncBaseTransport):
    def __init__(self, fake: FakeMediaWiki) -> None:
        self._fake = fake

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._fake.handle(request)
