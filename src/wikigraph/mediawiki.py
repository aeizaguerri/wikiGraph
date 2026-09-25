"""MediaWiki API client: Article links and Redirect resolution for one edition."""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import asyncio
import httpx

from wikigraph.governor import (
    Clock,
    DEFAULT_GOVERNOR,
    GlobalWikimediaGovernor,
    SystemClock,
)
from wikigraph.observability import emit
from wikigraph.response_cache import (
    InMemoryResponseCache,
    ResponseCache,
    ResponseCacheKey,
    SupabaseResponseCache,
    continuation_identity,
    normalized_titles,
)

RESOLVE_BATCH_SIZE = 50
ARTICLE_LINK_BATCH_SIZE = 50
DEFAULT_USER_AGENT = "wikiGraph/0.1 (educational article-link crawler)"


def configured_user_agent() -> str:
    configured = os.environ.get("WIKIGRAPH_USER_AGENT")
    if configured:
        return configured
    if os.environ.get("WIKIGRAPH_ENV") == "production":
        raise RuntimeError(
            "WIKIGRAPH_USER_AGENT is required in production and must identify wikiGraph."
        )
    return DEFAULT_USER_AGENT


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


class UpstreamOverload(MediaWikiError):
    """The provider asked us to stop and the run may safely be resumed."""

    def __init__(self, message: str, *, attempts: int, retry_after: float) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.retry_after = retry_after


class MediaWikiClient:
    """Talks to `{language}.wikipedia.org/w/api.php` over HTTP."""

    def __init__(
        self,
        language: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        governor: GlobalWikimediaGovernor = DEFAULT_GOVERNOR,
        owner: str = "launch-validation",
        cache: ResponseCache | None = None,
        clock: Clock | None = None,
        jitter: Callable[[float], float] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._language = language
        self._governor = governor
        self._owner = owner
        self._cache = cache if cache is not None else InMemoryResponseCache()
        self._clock = clock or SystemClock()
        self._jitter = jitter or (lambda value: value * random.uniform(0.5, 1.5))
        self._wall_clock = wall_clock
        self._last_request_had_overload = False
        self._http = httpx.AsyncClient(
            base_url=f"https://{language}.wikipedia.org",
            headers={"User-Agent": configured_user_agent()},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def article_links(self, title: str) -> list[ArticleLink]:
        return (await self.article_links_batch([title]))[title]

    async def article_links_batch(
        self, titles: list[str]
    ) -> dict[str, list[ArticleLink]]:
        """Fetch and fully drain links for up to 50 source Articles."""
        if not titles:
            return {}
        if len(titles) > ARTICLE_LINK_BATCH_SIZE:
            raise ValueError("article link batches cannot exceed 50 Articles")
        params: dict[str, str] = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "links",
            "titles": "|".join(titles),
            "pllimit": "max",
        }
        links_by_title: dict[str, list[ArticleLink]] = {title: [] for title in titles}
        request_identity = normalized_titles(titles)
        params_identity = dict(params)
        while True:
            body = await self._cached_get(
                params,
                ResponseCacheKey(
                    "article-links", self._language, request_identity,
                    continuation_identity(params_identity),
                ),
            )
            pages = body.get("query", {}).get("pages", [])
            if not pages:
                return links_by_title
            for page in pages:
                page_title = page.get("title")
                if page_title not in links_by_title:
                    continue
                links_by_title[page_title].extend(
                    ArticleLink(title=entry["title"], namespace=entry["ns"])
                    for entry in page.get("links", [])
                )
            continuation = body.get("continue")
            if not continuation:
                return links_by_title
            params.update(
                {
                    key: str(value)
                    for key, value in continuation.items()
                    if key != "continue"
                }
            )
            params_identity = dict(params)

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
            body = await self._cached_get(
                params,
                ResponseCacheKey(
                    "redirects", self._language, normalized_titles(batch),
                    continuation_identity(params),
                ),
            )
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
        self._last_request_had_overload = False
        for attempt in range(1, 11):
            response = await self._governor.request(
                self._owner, lambda: self._http.get("/w/api.php", params=params)
            )
            overload = response.status_code in {429, 503}
            try:
                body: dict[str, Any] = response.json()
            except ValueError:
                body = {}
            error = body.get("error")
            if (
                isinstance(error, dict)
                and str(error.get("code", "")).lower() == "maxlag"
            ):
                overload = True
            delay = None
            if overload:
                delay = self._retry_delay(response, error, attempt)
            emit(
                "wikimedia_attempt",
                owner=self._owner,
                edition=self._language,
                attempt=attempt,
                continuation=bool(params.get("plcontinue")),
                status_code=response.status_code,
                overload=overload,
                retry_delay_seconds=delay,
            )
            if not overload:
                if response.status_code != 200:
                    raise MediaWikiError(
                        f"MediaWiki API returned HTTP {response.status_code}"
                    )
                if "error" in body:
                    code = (
                        error.get("code", "unknown")
                        if isinstance(error, dict)
                        else "unknown"
                    )
                    raise MediaWikiError(f"MediaWiki API error: {code}")
                return body

            self._last_request_had_overload = True
            assert delay is not None
            await self._governor.record_overload(delay)
            if attempt == 10:
                raise UpstreamOverload(
                    "Wikimedia is still applying flow control after ten attempts.",
                    attempts=attempt,
                    retry_after=delay,
                )
            await self._clock.sleep(delay)
        raise AssertionError("overload retry loop did not terminate")

    def _retry_delay(
        self, response: httpx.Response, error: Any, attempt: int
    ) -> float:
        supplied = response.headers.get("Retry-After")
        if supplied is None and isinstance(error, dict):
            lag = error.get("lag")
            if isinstance(lag, (int, float)):
                supplied = str(lag)
        if supplied is not None:
            try:
                return min(45.0, max(0.0, float(supplied)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(supplied).timestamp()
                except (TypeError, ValueError, OverflowError):
                    pass
                else:
                    return min(45.0, max(0.0, retry_at - self._wall_clock()))
        return min(45.0, max(0.0, self._jitter(float(2 ** (attempt - 1)))))

    async def _cached_get(
        self, params: dict[str, str], key: ResponseCacheKey
    ) -> dict[str, Any]:
        if isinstance(self._cache, SupabaseResponseCache):
            cached = await asyncio.to_thread(self._cache.get, key)
        else:
            cached = self._cache.get(key)
        emit(
            "upstream_cache_lookup",
            owner=self._owner,
            edition=self._language,
            kind=key.kind,
            continuation=bool(params.get("plcontinue")),
            hit=cached is not None,
        )
        if cached is not None:
            return cached
        body = await self._get(params)
        if not self._last_request_had_overload:
            if isinstance(self._cache, SupabaseResponseCache):
                await asyncio.to_thread(self._cache.put, key, body)
            else:
                self._cache.put(key, body)
        return body

    async def aclose(self) -> None:
        await self._http.aclose()
