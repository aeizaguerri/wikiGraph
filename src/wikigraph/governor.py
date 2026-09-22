"""Deployment-wide scheduling for Wikimedia request attempts."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

import httpx
import uuid

T = TypeVar("T")


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, delay: float) -> None: ...


class WikimediaAdmission(Protocol):
    async def acquire(self, owner: str) -> None: ...

    async def release(self) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


@dataclass
class _Request:
    owner: str
    operation: Callable[[], Awaitable[object]]
    result: asyncio.Future[object]
    operation_task: asyncio.Task[object] | None = None
    cancelled: bool = False


class GlobalWikimediaGovernor:
    """Fair, immutable conservative Wikimedia budget for one deployment.

    Requests are admitted one at a time.  Each owner has a FIFO queue and the
    dispatcher visits non-empty queues round-robin, so a busy Crawl run cannot
    consume capacity ahead of another run indefinitely.  The limits are
    deliberately constructor constants; there is no runtime escalation path.
    """

    MAX_CONCURRENCY = 1
    MAX_STARTS_PER_SECOND = 2
    MAX_ATTEMPTS_PER_MINUTE = 120

    def __init__(
        self,
        clock: Clock | None = None,
        admission: WikimediaAdmission | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._admission = admission
        self._queues: dict[str, deque[_Request]] = {}
        self._owners: deque[str] = deque()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._starts: deque[float] = deque()
        self._attempts: deque[float] = deque()
        self._in_flight = 0
        self.max_in_flight = 0
        self.dispatch_log: list[tuple[str, float]] = []

    async def request(self, owner: str, operation: Callable[[], Awaitable[T]]) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()
        request = _Request(owner, operation, future)
        queue = self._queues.setdefault(owner, deque())
        queue.append(request)
        if owner not in self._owners:
            self._owners.append(owner)
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._dispatch())
        try:
            return await future  # type: ignore[return-value]
        except asyncio.CancelledError:
            request.cancelled = True
            future.cancel()
            if request.operation_task is not None:
                request.operation_task.cancel()
            raise

    async def _dispatch(self) -> None:
        while self._owners:
            owner = self._owners.popleft()
            queue = self._queues[owner]
            request = queue.popleft()
            if queue:
                self._owners.append(owner)
            else:
                del self._queues[owner]
            if request.cancelled or request.result.cancelled():
                continue
            await self._wait_for_capacity()
            if request.cancelled or request.result.cancelled():
                continue
            started = self._clock.now()
            self._starts.append(started)
            self._attempts.append(started)
            self.dispatch_log.append((request.owner, started))
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            try:
                request.operation_task = asyncio.create_task(
                    self._run_operation(request)
                )
                result = await request.operation_task
            except asyncio.CancelledError:
                if request.result.cancelled():
                    continue
                if not request.result.done():
                    request.result.cancel()
                raise
            except Exception as exc:
                if not request.result.done():
                    request.result.set_exception(exc)
            else:
                if not request.result.done():
                    request.result.set_result(result)
            finally:
                request.operation_task = None
                if self._admission is not None:
                    await self._admission.release()
                self._in_flight -= 1

    async def _run_operation(self, request: _Request) -> object:
        if self._admission is not None:
            await self._admission.acquire(request.owner)
        return await request.operation()

    async def _wait_for_capacity(self) -> None:
        while True:
            now = self._clock.now()
            self._discard_expired(now)
            waits: list[float] = []
            if self._starts:
                waits.append(
                    self._starts[-1] + 1.0 / self.MAX_STARTS_PER_SECOND - now
                )
            if len(self._starts) >= self.MAX_STARTS_PER_SECOND:
                waits.append(self._starts[0] + 1.0 - now)
            if len(self._attempts) >= self.MAX_ATTEMPTS_PER_MINUTE:
                waits.append(self._attempts[0] + 60.0 - now)
            delay = max(waits, default=0.0)
            if delay <= 0:
                return
            await self._clock.sleep(delay)

    def _discard_expired(self, now: float) -> None:
        while self._starts and now - self._starts[0] >= 1.0:
            self._starts.popleft()
        while self._attempts and now - self._attempts[0] >= 60.0:
            self._attempts.popleft()

    async def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        for queue in self._queues.values():
            for request in queue:
                if not request.result.done():
                    request.result.cancel()
        self._queues.clear()
        self._owners.clear()
        if self._admission is not None:
            close = getattr(self._admission, "aclose", None)
            if close is not None:
                await close()


class SupabaseWikimediaAdmission:
    """Cross-process governor lease backed by atomic Supabase RPCs."""

    acquire_function = "acquire_wikimedia_attempt"
    release_function = "release_wikimedia_attempt"

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        rest_path: str = "/rest/v1",
        clock: Clock | None = None,
    ) -> None:
        self._url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self._url or not self._key:
            raise RuntimeError("Supabase configuration is unavailable.")
        self._rest = rest_path.strip("/")
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._clock = clock or SystemClock()
        self._request_id: str | None = None

    def _endpoint(self, function: str) -> str:
        rest = f"/{self._rest}" if self._rest else ""
        return f"{self._url}{rest}/rpc/{function}"

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def acquire(self, owner: str) -> None:
        request_id = str(uuid.uuid4())
        self._request_id = request_id
        while True:
            response = await self._client.post(
                self._endpoint(self.acquire_function),
                headers=self._headers(),
                json={"p_request_id": request_id, "p_owner": owner},
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, list) or not body or not isinstance(body[0], dict):
                raise RuntimeError("Supabase returned an invalid governor decision.")
            decision = body[0]
            if bool(decision.get("granted")):
                return
            delay = max(float(decision.get("retry_after_seconds", 0.05)), 0.01)
            await self._clock.sleep(delay)

    async def release(self) -> None:
        if self._request_id is None:
            return
        response = await self._client.post(
            self._endpoint(self.release_function),
            headers=self._headers(),
            json={"p_request_id": self._request_id},
        )
        response.raise_for_status()
        self._request_id = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _configured_admission() -> SupabaseWikimediaAdmission | None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        if os.environ.get("WIKIGRAPH_ENV") == "production":
            raise RuntimeError(
                "Production Wikimedia arbitration requires Supabase configuration."
            )
        return None
    return SupabaseWikimediaAdmission(url, key)


DEFAULT_GOVERNOR = GlobalWikimediaGovernor(admission=_configured_admission())
