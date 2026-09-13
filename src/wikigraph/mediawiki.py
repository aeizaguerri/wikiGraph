"""MediaWiki API client: Article links and Redirect resolution for one edition."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

RESOLVE_BATCH_SIZE = 50
DEFAULT_USER_AGENT = "wikiGraph/0.1 (educational article-link crawler)"


def configured_user_agent() -> str:
    return os.environ.get("WIKIGRAPH_USER_AGENT", DEFAULT_USER_AGENT)


@dataclass(frozen=True)
class ArticleLink:
    title: str
    namespace: int


@dataclass(frozen=True)
class ResolvedTitle:
    title: str
    namespace: int
    missing: bool


class MediaWikiError(RuntimeError):
    pass


class MediaWikiClient:
    """Talks to `{language}.wikipedia.org/w/api.php` over HTTP."""

    def __init__(
        self, language: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._language = language
        self._http = httpx.AsyncClient(
            base_url=f"https://{language}.wikipedia.org",
            headers={"User-Agent": configured_user_agent()},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def article_links(self, title: str) -> list[ArticleLink]:
        params: dict[str, str] = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "links",
            "titles": title,
            "pllimit": "max",
        }
        links: list[ArticleLink] = []
        while True:
            body = await self._get(params)
            pages = body.get("query", {}).get("pages", [])
            if not pages:
                return links
            for entry in pages[0].get("links", []):
                links.append(ArticleLink(title=entry["title"], namespace=entry["ns"]))
            continuation = body.get("continue", {}).get("plcontinue")
            if continuation is None:
                return links
            params["plcontinue"] = continuation

    async def resolve_titles(self, titles: list[str]) -> dict[str, ResolvedTitle]:
        resolved: dict[str, ResolvedTitle] = {}
        for start in range(0, len(titles), RESOLVE_BATCH_SIZE):
            batch = titles[start : start + RESOLVE_BATCH_SIZE]
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "titles": "|".join(batch),
                "redirects": "1",
            }
            body = await self._get(params)
            query = body.get("query", {})
            mapping: dict[str, str] = {
                entry["from"]: entry["to"] for entry in query.get("normalized", [])
            }
            mapping.update(
                {entry["from"]: entry["to"] for entry in query.get("redirects", [])}
            )
            pages_by_title = {page["title"]: page for page in query.get("pages", [])}
            for original in batch:
                resolved[original] = self._resolve_one(original, mapping, pages_by_title)
        return resolved

    @staticmethod
    def _resolve_one(
        original: str,
        mapping: dict[str, str],
        pages_by_title: dict[str, dict[str, Any]],
    ) -> ResolvedTitle:
        current = original
        seen: set[str] = set()
        while current in mapping and current not in seen:
            seen.add(current)
            current = mapping[current]
        page = pages_by_title.get(current)
        if page is None:
            return ResolvedTitle(current, 0, True)
        return ResolvedTitle(page["title"], page.get("ns", 0), "missing" in page)

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        response = await self._http.get("/w/api.php", params=params)
        if response.status_code != 200:
            raise MediaWikiError(
                f"MediaWiki API returned HTTP {response.status_code}"
            )
        body: dict[str, Any] = response.json()
        if "error" in body:
            error = body["error"]
            raise MediaWikiError(
                f"MediaWiki API error: {error.get('code', 'unknown')}"
            )
        return body

    async def aclose(self) -> None:
        await self._http.aclose()
