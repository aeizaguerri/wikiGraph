"""In-memory registry of Crawl runs and their event streams."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import httpx

from wikigraph.communities import CommunityAssignment, detect_communities
from wikigraph.crawler import CrawlRequest, Crawler, CrawlResult, Progress


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


PROGRESS_EVENT = "progress"
COMPLETED_EVENT = "completed"
FAILED_EVENT = "failed"
TERMINAL_EVENT_TYPES = {COMPLETED_EVENT, FAILED_EVENT}


@dataclass(frozen=True)
class RunEvent:
    type: str
    data: dict[str, Any]


class CrawlRun:
    """One Crawl run: its job, its event log, and its subscriber queues."""

    def __init__(self, run_id: str, request: CrawlRequest) -> None:
        self.id = run_id
        self.request = request
        self.status = RunStatus.RUNNING
        self.truncated = False
        self.error: str | None = None
        self.result: CrawlResult | None = None
        self.communities: CommunityAssignment | None = None
        self.task: asyncio.Task[None] | None = None
        self._events: list[RunEvent] = []
        self._subscribers: list[asyncio.Queue[RunEvent]] = []
        self._done = asyncio.Event()

    def subscribe(self) -> asyncio.Queue[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        for event in self._events:
            queue.put_nowait(event)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[RunEvent]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def wait_done(self) -> None:
        await self._done.wait()

    def publish(self, event: RunEvent) -> None:
        self._events.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)

    async def start(self, transport: httpx.AsyncBaseTransport | None) -> None:
        try:
            crawler = Crawler(
                self.request, transport=transport, on_progress=self._record_progress
            )
            result = await crawler.crawl()
            self.result = result
            self.communities = await asyncio.to_thread(
                detect_communities, result.nodes, result.edges
            )
            self.truncated = result.truncated
            self.status = RunStatus.COMPLETED
            self.publish(RunEvent(COMPLETED_EVENT, {"truncated": result.truncated}))
        except Exception as exc:
            self.status = RunStatus.FAILED
            self.error = str(exc)
            self.publish(RunEvent(FAILED_EVENT, {"error": str(exc)}))
        finally:
            self._done.set()

    async def _record_progress(self, progress: Progress) -> None:
        self.publish(
            RunEvent(
                PROGRESS_EVENT,
                {
                    "crawled": progress.crawled,
                    "discovered": progress.discovered,
                    "depth": progress.level,
                    "recent": progress.recent,
                },
            )
        )


class CrawlRunStore(Protocol):
    """Domain operations needed by the API to own Crawl run lifecycles."""

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRun: ...

    def get(self, run_id: str) -> CrawlRun | None: ...

    async def shutdown(self) -> None: ...


class InMemoryCrawlRunStore:
    """Runs live in memory keyed by run identifier; nothing survives a restart."""

    def __init__(self) -> None:
        self._runs: dict[str, CrawlRun] = {}

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRun:
        run = CrawlRun(uuid.uuid4().hex, request)
        run.task = asyncio.create_task(run.start(transport))
        self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> CrawlRun | None:
        return self._runs.get(run_id)

    async def shutdown(self) -> None:
        tasks = [
            run.task for run in self._runs.values() if run.task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
