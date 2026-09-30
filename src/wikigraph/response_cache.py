"""Bounded, seven-day cache for successful MediaWiki response facts."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, cast

import httpx

from wikigraph.observability import emit

FRESHNESS_SECONDS = 7 * 24 * 60 * 60
CACHE_SCHEMA_VERSION = "1"


class _TransientCacheFailure(Exception):
    """A disposable cache operation failed without affecting crawl work."""


@dataclass(frozen=True)
class ResponseCacheKey:
    kind: str
    edition: str
    normalized_request: str
    continuation: str
    schema_version: str = CACHE_SCHEMA_VERSION

    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.kind,
            self.edition,
            self.normalized_request,
            self.continuation,
            self.schema_version,
        )


class ResponseCache(Protocol):
    def get(self, key: ResponseCacheKey) -> dict[str, Any] | None: ...

    def put(self, key: ResponseCacheKey, response: dict[str, Any]) -> None: ...


class InMemoryResponseCache:
    """Process-local cache used when no durable cache is configured."""

    def __init__(
        self,
        *,
        capacity: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if capacity < 1:
            raise ValueError("cache capacity must be positive")
        self.capacity = capacity
        self._clock = clock
        self._entries: OrderedDict[
            tuple[str, str, str, str, str], tuple[float, dict[str, Any]]
        ] = OrderedDict()

    def get(self, key: ResponseCacheKey) -> dict[str, Any] | None:
        entry = self._entries.get(key.identity())
        if entry is None:
            return None
        expires_at, response = entry
        if self._clock() >= expires_at:
            del self._entries[key.identity()]
            return None
        self._entries.move_to_end(key.identity())
        return response

    def put(self, key: ResponseCacheKey, response: dict[str, Any]) -> None:
        now = self._clock()
        identity = key.identity()
        self._entries[identity] = (now + FRESHNESS_SECONDS, response)
        self._entries.move_to_end(identity)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class SupabaseResponseCache:
    """PostgREST-backed cache; cache failures are handled as misses by callers.

    Durable capacity eviction is write-recency/FIFO: reads do not update the
    row's ``last_accessed_at``. This avoids a write and prune-trigger execution
    for every cache hit; an older hot entry may therefore be evicted early.
    """

    table = "upstream_response_cache"

    def __init__(
        self,
        url: str,
        key: str,
        *,
        client: httpx.Client | None = None,
        rest_path: str = "/rest/v1",
        capacity: int = 10_000,
        request_timeout_seconds: float = 3.0,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("cache request timeout must be positive")
        self._url = url.rstrip("/")
        self._key = key
        self._rest_path = rest_path.strip("/")
        self.capacity = capacity
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client or httpx.Client(timeout=3.0)
        self._owns_client = client is None

    @property
    def _endpoint(self) -> str:
        prefix = self._url
        if self._rest_path and not prefix.endswith(f"/{self._rest_path}"):
            prefix = f"{prefix}/{self._rest_path}"
        return f"{prefix}/{self.table}"

    def _headers(self, *, representation: bool = False) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if representation:
            headers["Prefer"] = "resolution=merge-duplicates,return=representation"
        return headers

    @staticmethod
    def _digest(key: ResponseCacheKey) -> str:
        return hashlib.sha256(
            json.dumps(key.identity(), separators=(",", ":")).encode()
        ).hexdigest()

    def _row(self, key: ResponseCacheKey, response: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "cache_key": self._digest(key),
            "kind": key.kind,
            "edition": key.edition,
            "normalized_request": key.normalized_request,
            "continuation": key.continuation,
            "schema_version": key.schema_version,
            "response": response,
            "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=FRESHNESS_SECONDS)).isoformat(),
            "last_accessed_at": now.isoformat(),
        }

    @staticmethod
    def _transient_failure(
        key: ResponseCacheKey, operation: str, failure: Exception
    ) -> bool:
        status_code = (
            failure.response.status_code
            if isinstance(failure, httpx.HTTPStatusError)
            else None
        )
        if status_code is not None and status_code < 500:
            return False
        if status_code is None and not isinstance(failure, httpx.RequestError):
            return False
        emit(
            "upstream_cache_transient_failure",
            kind=key.kind,
            edition=key.edition,
            operation=operation,
            exception_class=type(failure).__name__,
            status_code=status_code,
        )
        return True

    def _request(
        self, operation: str, key: ResponseCacheKey, **kwargs: Any
    ) -> httpx.Response:
        kwargs.setdefault("timeout", self._request_timeout_seconds)
        try:
            response = cast(
                httpx.Response,
                getattr(self._client, operation)(self._endpoint, **kwargs),
            )
            response.raise_for_status()
            return response
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if self._transient_failure(key, operation, exc):
                raise _TransientCacheFailure from exc
            raise

    def get(self, key: ResponseCacheKey) -> dict[str, Any] | None:
        try:
            response = self._request(
                "get",
                key,
                headers=self._headers(),
                params={"cache_key": f"eq.{self._digest(key)}", "limit": "1"},
            )
        except _TransientCacheFailure:
            return None
        rows = response.json()
        if not rows:
            return None
        row = rows[0]
        expires_at = datetime.fromisoformat(str(row["expires_at"])).timestamp()
        if time.time() >= expires_at:
            try:
                self._request(
                    "delete",
                    key,
                    headers=self._headers(),
                    params={"cache_key": f"eq.{self._digest(key)}"},
                )
            except _TransientCacheFailure:
                pass
            return None
        response_body = row["response"]
        if not isinstance(response_body, dict):
            raise ValueError("cached response must be a JSON object")
        return response_body

    def put(self, key: ResponseCacheKey, response: dict[str, Any]) -> None:
        try:
            self._request(
                "post",
                key,
                headers=self._headers(representation=True),
                params={"on_conflict": "cache_key"},
                json=self._row(key, response),
            )
        except _TransientCacheFailure:
            return

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


DEFAULT_RESPONSE_CACHE = InMemoryResponseCache()


def normalized_titles(titles: list[str]) -> str:
    return json.dumps(sorted(set(titles)), separators=(",", ":"))


def continuation_identity(params: dict[str, str]) -> str:
    continuation = {key: value for key, value in params.items() if key != "continue"}
    return json.dumps(continuation, sort_keys=True, separators=(",", ":"))
