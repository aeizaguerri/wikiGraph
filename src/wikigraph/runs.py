"""Crawl run lifecycle and store abstractions."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol

import httpx

from wikigraph.communities import CommunityAssignment, detect_communities
from wikigraph.crawler import (
    CrawlCheckpoint,
    CrawlRequest,
    Crawler,
    CrawlResult,
    GraphEdge,
    GraphNode,
    Progress,
)
from wikigraph.governor import DEFAULT_GOVERNOR, GlobalWikimediaGovernor
from wikigraph.response_cache import (
    InMemoryResponseCache,
    ResponseCache,
    SupabaseResponseCache,
)
from wikigraph.mediawiki import UpstreamOverload
from wikigraph.observability import emit, process_usage


class RunStatus(str, Enum):
    RUNNING = "running"
    OVERLOAD_WAITING = "overload_waiting"
    RECOVERABLE = "recoverable"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


PROGRESS_EVENT = "progress"
COMPLETED_EVENT = "completed"
FAILED_EVENT = "failed"
RECOVERABLE_EVENT = "recoverable"
OVERLOAD_WAITING_EVENT = "overload_waiting"
EXPIRED_EVENT = "expired"
TERMINAL_EVENT_TYPES = {
    COMPLETED_EVENT,
    FAILED_EVENT,
    RECOVERABLE_EVENT,
    OVERLOAD_WAITING_EVENT,
    EXPIRED_EVENT,
}


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
        persist_checkpoint: Callable[[CrawlCheckpoint], None] | None = None,
        persist_completion: Callable[["CrawlRun"], None] | None = None,
        persist_failure: Callable[["CrawlRun"], None] | None = None,
        governor: GlobalWikimediaGovernor = DEFAULT_GOVERNOR,
        cache: ResponseCache | None = None,
        persist_recovery: Callable[["CrawlRun"], None] | None = None,
        checkpoint: CrawlCheckpoint | None = None,
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
        self._persist_checkpoint = persist_checkpoint
        self._persist_completion = persist_completion
        self._persist_failure = persist_failure
        self._governor = governor
        self._cache = cache if cache is not None else InMemoryResponseCache()
        self._persist_recovery = persist_recovery
        self._checkpoint = checkpoint
        self.retry_state: dict[str, Any] | None = None
        self._started_at = time.perf_counter()
        self._start_usage = process_usage()
        self.progress: dict[str, Any] = {
            "crawled": 0, "discovered": 1, "depth": 0, "recent": []
        }

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
                self.request,
                transport=transport,
                on_progress=self._record_progress,
                governor=self._governor,
                owner=self.id,
                checkpoint=self._checkpoint,
                on_checkpoint=self._record_checkpoint,
                cache=self._cache,
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
            emit(
                "crawl_run_complete",
                run_id=self.id,
                language=self.request.language,
                status=self.status.value,
                duration_seconds=time.perf_counter() - self._started_at,
                persistence_effect="completion_write",
                result_nodes=len(result.nodes),
                result_edges=len(result.edges),
                crawled=result.crawled,
                discovered=result.discovered,
                start_usage=self._start_usage,
                end_usage=process_usage(),
            )
            self.publish(RunEvent(COMPLETED_EVENT, {"truncated": result.truncated}))
        except UpstreamOverload as exc:
            self.status = RunStatus.OVERLOAD_WAITING
            self.error = str(exc)
            self.retry_state = {
                "attempts": exc.attempts,
                "retry_after": exc.retry_after,
            }
            if self._persist_recovery is not None:
                self._persist_recovery(self)
            self.publish(
                RunEvent(
                    OVERLOAD_WAITING_EVENT,
                    {"error": self.error, "retryAfter": exc.retry_after},
                )
            )
        except asyncio.CancelledError:
            self.status = RunStatus.RECOVERABLE
            self.error = "The crawl process was interrupted; retry to resume."
            if self._persist_recovery is not None:
                self._persist_recovery(self)
            self.publish(RunEvent(RECOVERABLE_EVENT, {"error": self.error}))
            raise
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
        self.progress = _progress_json(progress)
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

    async def _record_checkpoint(self, checkpoint: CrawlCheckpoint) -> None:
        self._checkpoint = checkpoint
        if self._persist_checkpoint is not None:
            self._persist_checkpoint(checkpoint)


class CrawlRunHandle(Protocol):
    """Lifecycle view exposed to API consumers independently of storage."""

    id: str
    request: CrawlRequest
    status: RunStatus
    error: str | None
    result: CrawlResult | None
    communities: CommunityAssignment | None
    progress: dict[str, Any]
    retry_state: dict[str, Any] | None

    def subscribe(self) -> asyncio.Queue[RunEvent]: ...

    def unsubscribe(self, queue: asyncio.Queue[RunEvent]) -> None: ...

    async def wait_done(self) -> None: ...


class CrawlRunStore(Protocol):
    """Domain operations needed by the API to own Crawl run lifecycles."""

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRunHandle: ...

    def retry_run(
        self, run_id: str, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRunHandle: ...

    def get(self, run_id: str) -> CrawlRunHandle | None: ...

    def healthcheck(self) -> None: ...

    def cleanup_expired(self) -> int: ...

    async def shutdown(self) -> None: ...


class InMemoryCrawlRunStore:
    """Runs live in memory keyed by run identifier; nothing survives a restart."""

    def __init__(
        self,
        governor: GlobalWikimediaGovernor = DEFAULT_GOVERNOR,
        cache: ResponseCache | None = None,
    ) -> None:
        self._runs: dict[str, CrawlRun] = {}
        self._governor = governor
        self._cache = cache if cache is not None else InMemoryResponseCache()

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRun:
        run = CrawlRun(
            secrets.token_urlsafe(24),
            request,
            governor=self._governor,
            checkpoint=_initial_checkpoint(request),
            cache=self._cache,
        )
        run.task = asyncio.create_task(run.start(transport))
        self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> CrawlRun | None:
        return self._runs.get(run_id)

    def healthcheck(self) -> None:
        """Local tests have no external persistence dependency to verify."""

        return None

    def cleanup_expired(self) -> int:
        return 0

    def retry_run(
        self, run_id: str, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRun:
        current = self._runs.get(run_id)
        if current is None:
            raise PersistenceError("No crawl run with that identifier.")
        if current.status is not RunStatus.RECOVERABLE:
            if current.status is not RunStatus.OVERLOAD_WAITING:
                raise PersistenceError("Only recoverable crawl runs can be retried.")
        run = CrawlRun(
            run_id,
            current.request,
            governor=self._governor,
            checkpoint=current._checkpoint,
            cache=self._cache,
        )
        run.task = asyncio.create_task(run.start(transport))
        self._runs[run_id] = run
        return run

    async def shutdown(self) -> None:
        tasks = [
            run.task for run in self._runs.values() if run.task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class PersistenceError(RuntimeError):
    """The canonical run store could not accept or persist a run operation."""


@dataclass(frozen=True)
class LaunchAdmissionDecision:
    admitted: bool
    code: str


class LaunchAdmissionStore(Protocol):
    def admit(
        self,
        ip_hash: str,
        *,
        per_ip_per_minute: int,
        deployment_per_minute: int,
        per_ip_per_day: int,
        max_ip_keys: int,
    ) -> LaunchAdmissionDecision: ...

    def close(self) -> None: ...


class SupabaseLaunchAdmissionStore:
    """Atomic launch admission backed by the canonical Supabase database."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        client: httpx.Client | None = None,
        rest_path: str = "/rest/v1",
    ) -> None:
        self._url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self._rest_path = rest_path.strip("/")
        if not self._url or not self._key:
            raise PersistenceError("Supabase configuration is unavailable.")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    @property
    def _endpoint(self) -> str:
        rest_path = f"/{self._rest_path}" if self._rest_path else ""
        return f"{self._url}{rest_path}/rpc/admit_crawl_launch"

    def admit(
        self,
        ip_hash: str,
        *,
        per_ip_per_minute: int,
        deployment_per_minute: int,
        per_ip_per_day: int,
        max_ip_keys: int,
    ) -> LaunchAdmissionDecision:
        try:
            response = self._client.post(
                self._endpoint,
                headers={
                    "apikey": self._key,
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                json={
                    "p_ip_hash": ip_hash,
                    "p_per_ip_per_minute": per_ip_per_minute,
                    "p_deployment_per_minute": deployment_per_minute,
                    "p_per_ip_per_day": per_ip_per_day,
                    "p_max_ip_keys": max_ip_keys,
                },
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise PersistenceError("Supabase launch admission failed.") from exc
        if not isinstance(body, list) or not body or not isinstance(body[0], dict):
            raise PersistenceError("Supabase returned an invalid launch admission.")
        result = body[0]
        return LaunchAdmissionDecision(
            admitted=bool(result.get("admitted")), code=str(result.get("code", "unknown"))
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


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
        rest_path: str = "/rest/v1",
        governor: GlobalWikimediaGovernor = DEFAULT_GOVERNOR,
        cache: ResponseCache | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self._rest_path = rest_path.strip("/")
        if not self._url or not self._key:
            raise PersistenceError("Supabase configuration is unavailable.")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None
        self._governor = governor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache = (
            cache
            if cache is not None
            else (
                SupabaseResponseCache(
                    self._url, self._key, client=self._client, rest_path=rest_path
                )
                if client is None
                else InMemoryResponseCache()
            )
        )
        self._runs: dict[str, CrawlRun] = {}

    @property
    def _endpoint(self) -> str:
        return self._rest_endpoint(self.table)

    def _rest_endpoint(self, path: str) -> str:
        rest_url = self._url
        if self._rest_path and not rest_url.endswith(f"/{self._rest_path}"):
            rest_url = f"{rest_url}/{self._rest_path}"
        return f"{rest_url}/{path}"

    def _headers(self, *, representation: bool = False) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if representation:
            headers["Prefer"] = "return=representation"
        return headers

    def healthcheck(self) -> None:
        """Verify the runtime schema and the RPCs used by production."""
        self._request(
            "GET",
            headers=self._headers(),
            params={"select": "run_id", "limit": "1"},
        )
        for table in (
            "crawl_launch_admission",
            "crawl_launch_admission_total",
            "crawl_run_metrics",
            "upstream_response_cache",
            "wikimedia_governor_state",
            "wikimedia_governor_queue",
        ):
            self._request_url(
                self._rest_endpoint(table),
                "GET",
                headers=self._headers(),
                params={"select": "*", "limit": "0"},
            )
        self._rpc_request(
            "admit_crawl_launch",
            {
                "p_ip_hash": "invalid",
                "p_per_ip_per_minute": 1,
                "p_deployment_per_minute": 1,
                "p_per_ip_per_day": 1,
                "p_max_ip_keys": 1,
            },
        )
        self._rpc_request(
            "expire_crawl_runs", {"p_now": "1970-01-01T00:00:00+00:00"}
        )
        self._rpc_request(
            "release_wikimedia_attempt", {"p_request_id": str(uuid.uuid4())}
        )

    def _rpc_request(self, function: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._request_url(
            self._rest_endpoint(f"rpc/{function}"),
            "POST",
            headers=self._headers(),
            json=payload,
        )

    def _request(self, method: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._request_url(self._endpoint, method, **kwargs)

    def _request_url(self, endpoint: str, method: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            response = self._client.request(method, endpoint, **kwargs)
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
        run_id = secrets.token_urlsafe(24)
        row = {
            "run_id": run_id,
            "seed": request.seed,
            "language": request.language,
            "depth": request.depth,
            "node_cap": request.node_cap,
            "status": RunStatus.RUNNING.value,
            "progress": {"crawled": 0, "discovered": 1, "depth": 0, "recent": []},
            "checkpoint": _checkpoint_json(_initial_checkpoint(request)),
        }
        records = self._request(
            "POST", headers=self._headers(representation=True), json=row
        )
        if not records or records[0].get("run_id") != run_id:
            raise PersistenceError("Supabase did not create the crawl run.")
        emit(
            "persistence_write",
            run_id=run_id,
            operation="insert",
            fields=sorted(row),
            payload_bytes=len(str(row).encode()),
        )
        run = CrawlRun(
            run_id,
            request,
            persist_progress=lambda progress: self._save_progress(run_id, progress),
            persist_checkpoint=lambda checkpoint: self._save_checkpoint(run_id, checkpoint),
            persist_completion=lambda completed: self._save_completion(completed),
            persist_failure=lambda failed: self._save_failure(failed),
            governor=self._governor,
            cache=self._cache,
            persist_recovery=lambda recovered: self._save_recovery(recovered),
        )
        run.task = asyncio.create_task(run.start(transport))
        self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> CrawlRun | None:
        active = self._runs.get(run_id)
        records = self._request(
            "GET",
            headers=self._headers(),
            params={"run_id": f"eq.{run_id}", "limit": "1"},
        )
        if not records:
            return active
        row = records[0]
        # Older test doubles and pre-retention schemas do not expose expiry;
        # canonical migrated rows do, so only those reads invoke cleanup.
        if "expires_at" in row:
            self.cleanup_expired()
            records = self._request(
                "GET",
                headers=self._headers(),
                params={"run_id": f"eq.{run_id}", "limit": "1"},
            )
            if not records:
                return None
            row = records[0]
        if active is not None and row.get("status") != RunStatus.EXPIRED.value:
            return active
        request = CrawlRequest(
            seed=str(row["seed"]),
            language=str(row["language"]),
            depth=int(row["depth"]),
            node_cap=int(row["node_cap"]),
        )
        checkpoint = _checkpoint_from_json(row.get("checkpoint"))
        run = CrawlRun(
            run_id,
            request,
            governor=self._governor,
            checkpoint=checkpoint,
            cache=self._cache,
        )
        _hydrate_run(run, row)
        self._runs[run_id] = run
        return run

    def cleanup_expired(self, now: datetime | None = None) -> int:
        """Remove detailed state through the database-authorized cleanup RPC."""
        cleanup_now = now or self._clock()
        endpoint = self._rest_endpoint("rpc/expire_crawl_runs")
        try:
            response = self._client.post(
                endpoint,
                headers=self._headers(),
                json={"p_now": cleanup_now.isoformat()},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise PersistenceError("Supabase retention cleanup failed.") from exc
        if not isinstance(body, list) or not body or not isinstance(body[0], dict):
            raise PersistenceError("Supabase returned an invalid retention result.")
        try:
            return int(body[0]["expired_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError("Supabase returned an invalid retention count.") from exc

    def retry_run(
        self, run_id: str, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRun:
        records = self._request(
            "GET",
            headers=self._headers(),
            params={"run_id": f"eq.{run_id}", "limit": "1"},
        )
        if not records:
            raise PersistenceError("No crawl run with that identifier.")
        row = records[0]
        if row.get("status") not in {
            RunStatus.RECOVERABLE.value,
            RunStatus.OVERLOAD_WAITING.value,
        }:
            raise PersistenceError("Only recoverable crawl runs can be retried.")
        request = CrawlRequest(
            seed=str(row["seed"]),
            language=str(row["language"]),
            depth=int(row["depth"]),
            node_cap=int(row["node_cap"]),
        )
        checkpoint = _checkpoint_from_json(row.get("checkpoint"))
        if checkpoint is None:
            raise PersistenceError("Recoverable crawl run has no checkpoint.")
        self._patch(
            run_id,
            {"status": RunStatus.RUNNING.value, "error": None, "retry_state": None},
        )
        run = CrawlRun(
            run_id,
            request,
            governor=self._governor,
            cache=self._cache,
            checkpoint=checkpoint,
            persist_progress=lambda progress: self._save_progress(run_id, progress),
            persist_checkpoint=lambda value: self._save_checkpoint(run_id, value),
            persist_completion=lambda completed: self._save_completion(completed),
            persist_failure=lambda failed: self._save_failure(failed),
            persist_recovery=lambda recovered: self._save_recovery(recovered),
        )
        run.task = asyncio.create_task(run.start(transport))
        self._runs[run_id] = run
        return run

    def _save_progress(self, run_id: str, progress: Progress) -> None:
        self._patch(run_id, {"progress": _progress_json(progress)})

    def _save_checkpoint(self, run_id: str, checkpoint: CrawlCheckpoint) -> None:
        self._patch(
            run_id,
            {
                "checkpoint": _checkpoint_json(checkpoint),
                "progress": {
                    "crawled": checkpoint.crawled,
                    "discovered": len(checkpoint.nodes),
                    "depth": checkpoint.level,
                    "recent": checkpoint.recent,
                },
            },
        )

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

    def _save_recovery(self, run: CrawlRun) -> None:
        self._patch(
            run.id,
            {
                "status": (
                    RunStatus.OVERLOAD_WAITING.value
                    if run.status is RunStatus.OVERLOAD_WAITING
                    else RunStatus.RECOVERABLE.value
                ),
                "error": run.error,
                "retry_state": run.retry_state,
            },
        )

    def _patch(self, run_id: str, values: dict[str, Any]) -> None:
        records = self._request(
            "PATCH",
            headers=self._headers(representation=True),
            params={"run_id": f"eq.{run_id}"},
            json=values,
        )
        if not records or records[0].get("run_id") != run_id:
            raise PersistenceError("Supabase did not update the crawl run.")
        emit(
            "persistence_write",
            run_id=run_id,
            operation="patch",
            fields=sorted(values),
            payload_bytes=len(str(values).encode()),
        )

    async def shutdown(self) -> None:
        tasks = [run.task for run in self._runs.values() if run.task is not None]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_client:
            self._client.close()


def _progress_json(progress: Progress) -> dict[str, Any]:
    return {
        "crawled": progress.crawled,
        "discovered": progress.discovered,
        "depth": progress.level,
        "recent": progress.recent,
    }


def _initial_checkpoint(request: CrawlRequest) -> CrawlCheckpoint:
    return CrawlCheckpoint(
        level=1,
        frontier=[request.seed],
        batch_start=0,
        next_frontier=[],
        current_batch=[],
        continuation=None,
        nodes=[GraphNode(request.seed, 0, True)],
        edges=[],
        truncated=False,
        crawled=0,
        recent=[],
    )


def _checkpoint_json(checkpoint: CrawlCheckpoint) -> dict[str, Any]:
    return {
        "version": 1,
        "level": checkpoint.level,
        "frontier": checkpoint.frontier,
        "batch_start": checkpoint.batch_start,
        "next_frontier": checkpoint.next_frontier,
        "current_batch": checkpoint.current_batch,
        "continuation": checkpoint.continuation,
        "nodes": [node.__dict__ for node in checkpoint.nodes],
        "edges": [edge.__dict__ for edge in checkpoint.edges],
        "truncated": checkpoint.truncated,
        "crawled": checkpoint.crawled,
        "recent": checkpoint.recent,
    }


def _checkpoint_from_json(value: Any) -> CrawlCheckpoint | None:
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    try:
        return CrawlCheckpoint(
            level=int(value["level"]),
            frontier=[str(title) for title in value["frontier"]],
            batch_start=int(value["batch_start"]),
            next_frontier=[str(title) for title in value["next_frontier"]],
            current_batch=[str(title) for title in value["current_batch"]],
            continuation=(
                str(value["continuation"])
                if value.get("continuation") is not None
                else None
            ),
            nodes=[GraphNode(**node) for node in value["nodes"]],
            edges=[GraphEdge(**edge) for edge in value["edges"]],
            truncated=bool(value["truncated"]),
            crawled=int(value["crawled"]),
            recent=[str(title) for title in value["recent"]],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _hydrate_run(run: CrawlRun, row: dict[str, Any]) -> None:
    status = RunStatus(str(row.get("status", RunStatus.RUNNING.value)))
    run.status = status
    run.error = row.get("error")
    retry_state = row.get("retry_state")
    run.retry_state = retry_state if isinstance(retry_state, dict) else None
    progress = row.get("progress")
    if isinstance(progress, dict):
        run.progress = progress
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
    elif status is RunStatus.RECOVERABLE:
        run.publish(
            RunEvent(
                RECOVERABLE_EVENT,
                {"error": run.error or "The crawl run can be resumed."},
            )
        )
    elif status is RunStatus.OVERLOAD_WAITING:
        run.publish(
            RunEvent(
                OVERLOAD_WAITING_EVENT,
                {
                    "error": run.error or "Wikimedia overload; retry to resume.",
                    "retryAfter": (row.get("retry_state") or {}).get("retry_after"),
                },
            )
        )
    elif status is RunStatus.EXPIRED:
        run.publish(RunEvent(EXPIRED_EVENT, {"error": "This crawl run has expired."}))
    if status is not RunStatus.RUNNING:
        run._done.set()
