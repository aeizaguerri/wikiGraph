"""Deployment-wide scheduling for Wikimedia request attempts."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

T = TypeVar("T")


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, delay: float) -> None: ...


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

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
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
        queue = self._queues.setdefault(owner, deque())
        queue.append(_Request(owner, operation, future))
        if owner not in self._owners:
            self._owners.append(owner)
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._dispatch())
        return await future  # type: ignore[return-value]

    async def _dispatch(self) -> None:
        while self._owners:
            owner = self._owners.popleft()
            queue = self._queues[owner]
            request = queue.popleft()
            if queue:
                self._owners.append(owner)
            else:
                del self._queues[owner]
            await self._wait_for_capacity()
            started = self._clock.now()
            self._starts.append(started)
            self._attempts.append(started)
            self.dispatch_log.append((request.owner, started))
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            try:
                result = await request.operation()
            except asyncio.CancelledError:
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
                self._in_flight -= 1

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


DEFAULT_GOVERNOR = GlobalWikimediaGovernor()
