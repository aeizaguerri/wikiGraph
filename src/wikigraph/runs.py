"""Crawl run lifecycle and store abstractions."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

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

    def __init__(
        self,
        run_id: str,
        request: CrawlRequest,
        *,
        persist_progress: Callable[[Progress], None] | None = None,
        persist_completion: Callable[["CrawlRun"], None] | None = None,
        persist_failure: Callable[["CrawlRun"], None] | None = None,
    ) -> None:
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
        self._persist_progress = persist_progress
        self._persist_completion = persist_completion
        self._persist_failure = persist_failure

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
            if self._persist_completion is not None:
                self._persist_completion(self)
            self.publish(RunEvent(COMPLETED_EVENT, {"truncated": result.truncated}))
        except Exception as exc:
            self.status = RunStatus.FAILED
            self.error = str(exc)
            if self._persist_failure is not None:
                try:
                    self._persist_failure(self)
                except PersistenceError:
                    # The local terminal event still tells an attached client
                    # that this operation failed; the store remains canonical
                    # and no replacement in-memory run is made.
                    pass
            self.publish(RunEvent(FAILED_EVENT, {"error": str(exc)}))
        finally:
            self._done.set()

    async def _record_progress(self, progress: Progress) -> None:
        if self._persist_progress is not None:
            self._persist_progress(progress)
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


class CrawlRunHandle(Protocol):
    """Lifecycle view exposed to API consumers independently of storage."""

    id: str
    request: CrawlRequest
    status: RunStatus
    error: str | None
    result: CrawlResult | None
    communities: CommunityAssignment | None

    def subscribe(self) -> asyncio.Queue[RunEvent]: ...

    def unsubscribe(self, queue: asyncio.Queue[RunEvent]) -> None: ...

    async def wait_done(self) -> None: ...


class CrawlRunStore(Protocol):
    """Domain operations needed by the API to own Crawl run lifecycles."""

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRunHandle: ...

    def get(self, run_id: str) -> CrawlRunHandle | None: ...

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


class PersistenceError(RuntimeError):
    """The canonical run store could not accept or persist a run operation."""


class SupabaseCrawlRunStore:
    """Crawl run store backed by Supabase's server-side PostgREST API.

    The store deliberately has no fallback path.  A failed insert or update is
    raised to the caller (or fails the owning run), so an unavailable Supabase
    project cannot create a run that exists only in the application process.
    """

    table = "crawl_runs"

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self._url or not self._key:
            raise PersistenceError("Supabase configuration is unavailable.")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    @property
    def _endpoint(self) -> str:
        return f"{self._url}/rest/v1/{self.table}"

    def _headers(self, *, representation: bool = False) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if representation:
            headers["Prefer"] = "return=representation"
        return headers

    def _request(self, method: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            response = self._client.request(method, self._endpoint, **kwargs)
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise PersistenceError("Supabase persistence operation failed.") from exc
        if not response.content:
            return []
        body = response.json()
        if not isinstance(body, list):
            raise PersistenceError("Supabase returned an invalid run record.")
        return body

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRun:
        run_id = uuid.uuid4().hex
        row = {
            "run_id": run_id,
            "seed": request.seed,
            "language": request.language,
            "depth": request.depth,
            "node_cap": request.node_cap,
            "status": RunStatus.RUNNING.value,
            "progress": {"crawled": 0, "discovered": 1, "depth": 0, "recent": []},
        }
        records = self._request(
            "POST", headers=self._headers(representation=True), json=row
        )
        if not records or records[0].get("run_id") != run_id:
            raise PersistenceError("Supabase did not create the crawl run.")
        run = CrawlRun(
            run_id,
            request,
            persist_progress=lambda progress: self._save_progress(run_id, progress),
            persist_completion=lambda completed: self._save_completion(completed),
            persist_failure=lambda failed: self._save_failure(failed),
        )
        run.task = asyncio.create_task(run.start(transport))
        return run

    def get(self, run_id: str) -> CrawlRun | None:
        records = self._request(
            "GET",
            headers=self._headers(),
            params={"run_id": f"eq.{run_id}", "limit": "1"},
        )
        if not records:
            return None
        row = records[0]
        request = CrawlRequest(
            seed=str(row["seed"]),
            language=str(row["language"]),
            depth=int(row["depth"]),
            node_cap=int(row["node_cap"]),
        )
        run = CrawlRun(run_id, request)
        _hydrate_run(run, row)
        return run

    def _save_progress(self, run_id: str, progress: Progress) -> None:
        self._patch(run_id, {"progress": _progress_json(progress)})

    def _save_completion(self, run: CrawlRun) -> None:
        if run.result is None or run.communities is None:
            raise PersistenceError("Cannot persist a completion without a Graph.")
        graph = {
            "nodes": [node.__dict__ for node in run.result.nodes],
            "edges": [edge.__dict__ for edge in run.result.edges],
            "truncated": run.result.truncated,
            "crawled": run.result.crawled,
            "discovered": run.result.discovered,
            "community_ids": run.communities.ids,
            "community_count": run.communities.count,
            "modularity": run.communities.modularity,
        }
        self._patch(run.id, {"status": RunStatus.COMPLETED.value, "graph": graph})

    def _save_failure(self, run: CrawlRun) -> None:
        self._patch(
            run.id,
            {"status": RunStatus.FAILED.value, "error": run.error},
        )

    def _patch(self, run_id: str, values: dict[str, Any]) -> None:
        self._request(
            "PATCH",
            headers=self._headers(),
            params={"run_id": f"eq.{run_id}"},
            json=values,
        )

    async def shutdown(self) -> None:
        if self._owns_client:
            self._client.close()


def _progress_json(progress: Progress) -> dict[str, Any]:
    return {
        "crawled": progress.crawled,
        "discovered": progress.discovered,
        "depth": progress.level,
        "recent": progress.recent,
    }


def _hydrate_run(run: CrawlRun, row: dict[str, Any]) -> None:
    status = RunStatus(str(row.get("status", RunStatus.RUNNING.value)))
    run.status = status
    run.error = row.get("error")
    progress = row.get("progress")
    if isinstance(progress, dict):
        run.publish(RunEvent(PROGRESS_EVENT, progress))
    graph = row.get("graph")
    if isinstance(graph, dict) and status is RunStatus.COMPLETED:
        from wikigraph.crawler import GraphEdge, GraphNode

        run.result = CrawlResult(
            nodes=[GraphNode(**node) for node in graph["nodes"]],
            edges=[GraphEdge(**edge) for edge in graph["edges"]],
            truncated=bool(graph["truncated"]),
            crawled=int(graph["crawled"]),
            discovered=int(graph["discovered"]),
        )
        run.communities = CommunityAssignment(
            ids={str(key): int(value) for key, value in graph["community_ids"].items()},
            count=int(graph["community_count"]),
            modularity=float(graph["modularity"]),
        )
    if status is RunStatus.COMPLETED:
        run.publish(
            RunEvent(
                COMPLETED_EVENT,
                {"truncated": bool(run.result and run.result.truncated)},
            )
        )
    elif status is RunStatus.FAILED:
        run.publish(RunEvent(FAILED_EVENT, {"error": run.error or "The crawl run failed."}))
    run._done.set()
