"""Crawl run lifecycle and store abstractions."""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol

import json
import logging
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


async def _persist_off_loop(operation: Callable[[], None]) -> None:
    """Run a durable write off-loop and settle it before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


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
logger = logging.getLogger(__name__)


def public_run_error(status: RunStatus, error: str | None) -> str | None:
    """Return only stable, intentional messages at public run boundaries."""
    if status is RunStatus.OVERLOAD_WAITING:
        return "Wikimedia is busy; retry later."
    if status is RunStatus.RECOVERABLE:
        return "The crawl process was interrupted; retry to resume."
    if status is RunStatus.FAILED:
        return "The crawl run failed."
    return None


def public_event(event: "RunEvent") -> "RunEvent":
    """Redact error-bearing historical events while retaining safe event data."""
    if event.type == FAILED_EVENT:
        return RunEvent(event.type, {"error": "The crawl run failed."})
    if event.type == OVERLOAD_WAITING_EVENT:
        data = {}
        if "retryAfter" in event.data:
            data["retryAfter"] = event.data["retryAfter"]
        data["error"] = "Wikimedia is busy; retry later."
        return RunEvent(event.type, data)
    if event.type == RECOVERABLE_EVENT:
        return RunEvent(
            event.type, {"error": "The crawl process was interrupted; retry to resume."}
        )
    return event


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
        owner_token: str | None = None,
        owner_version: int | None = None,
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
        self._ownership_lost = False
        self._owner_token = owner_token
        self._owner_version = owner_version
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
            persist_completion = self._persist_completion
            if persist_completion is not None:
                await _persist_off_loop(lambda: persist_completion(self))
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
            logger.warning("Crawl run entered overload wait", exc_info=True)
            self.error = public_run_error(self.status, str(exc))
            self.retry_state = {
                "attempts": exc.attempts,
                "retry_after": exc.retry_after,
            }
            persist_recovery = self._persist_recovery
            if persist_recovery is not None:
                callback = persist_recovery
                await _persist_off_loop(lambda: callback(self))
            self.publish(
                RunEvent(
                    OVERLOAD_WAITING_EVENT,
                    {"error": self.error, "retryAfter": exc.retry_after},
                )
            )
        except asyncio.CancelledError:
            self.status = RunStatus.RECOVERABLE
            self.error = "The crawl process was interrupted; retry to resume."
            persist_recovery = self._persist_recovery
            if persist_recovery is not None and not self._ownership_lost:
                await _persist_off_loop(lambda: persist_recovery(self))
            self.publish(RunEvent(RECOVERABLE_EVENT, {"error": self.error}))
            raise
        except Exception as exc:
            self.status = RunStatus.FAILED
            logger.exception("Crawl run failed", extra={"run_id": self.id})
            self.error = public_run_error(self.status, str(exc))
            persist_failure = self._persist_failure
            if persist_failure is not None:
                try:
                    await _persist_off_loop(lambda: persist_failure(self))
                except PersistenceError:
                    # The local terminal event still tells an attached client
                    # that this operation failed; the store remains canonical
                    # and no replacement in-memory run is made.
                    pass
            self.publish(RunEvent(FAILED_EVENT, {"error": self.error}))
        finally:
            self._done.set()

    async def _record_progress(self, progress: Progress) -> None:
        self.progress = _progress_json(progress)
        persist_progress = self._persist_progress
        if persist_progress is not None:
            await _persist_off_loop(lambda: persist_progress(progress))
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
        persist_checkpoint = self._persist_checkpoint
        if persist_checkpoint is not None:
            await _persist_off_loop(lambda: persist_checkpoint(checkpoint))


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


@dataclass(frozen=True)
class RunProgressSnapshot:
    crawled: int
    discovered: int
    depth: int
    recent: tuple[str, ...]


@dataclass(frozen=True)
class RunRetrySnapshot:
    attempts: int | None
    retry_after: float | None


@dataclass(frozen=True)
class RunStateSnapshot:
    id: str
    request: CrawlRequest
    status: RunStatus
    progress: RunProgressSnapshot
    error: str | None
    retry_state: RunRetrySnapshot | None
    expires_at: datetime | None
    owner_token: str | None = None
    owner_version: int | None = None
    truncated: bool | None = None


@dataclass(frozen=True)
class RunCheckpointSnapshot:
    level: int
    frontier: tuple[str, ...]
    batch_start: int
    next_frontier: tuple[str, ...]
    current_batch: tuple[str, ...]
    continuation: str | None
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    truncated: bool
    crawled: int
    recent: tuple[str, ...]


@dataclass(frozen=True)
class RunCommunitySnapshot:
    ids: tuple[tuple[str, int], ...]
    count: int
    modularity: float


@dataclass(frozen=True)
class RunGraphSnapshot:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    truncated: bool
    crawled: int
    discovered: int


@dataclass(frozen=True)
class RunPreviewSnapshot:
    state: RunStateSnapshot
    checkpoint: RunCheckpointSnapshot | None
    graph: RunGraphSnapshot | None
    communities: RunCommunitySnapshot | None


@dataclass(frozen=True)
class RunCompletedGraphSnapshot:
    state: RunStateSnapshot
    graph: RunGraphSnapshot | None
    communities: RunCommunitySnapshot | None


class CrawlRunStore(Protocol):
    """Domain operations needed by the API to own Crawl run lifecycles."""

    def start_run(
        self, request: CrawlRequest, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRunHandle: ...

    def retry_run(
        self, run_id: str, transport: httpx.AsyncBaseTransport | None
    ) -> CrawlRunHandle: ...

    def get(self, run_id: str) -> CrawlRunHandle | None: ...

    def get_local_worker(self, run_id: str) -> CrawlRunHandle | None: ...

    async def get_state(self, run_id: str) -> RunStateSnapshot | None: ...

    async def get_preview(self, run_id: str) -> RunPreviewSnapshot | None: ...

    async def get_graph(self, run_id: str) -> RunCompletedGraphSnapshot | None: ...

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
        self._remote_poll_seconds = 0.25
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

    def get_local_worker(self, run_id: str) -> CrawlRun | None:
        return self._runs.get(run_id)

    async def get_state(self, run_id: str) -> RunStateSnapshot | None:
        run = self._runs.get(run_id)
        return _state_snapshot(run) if run is not None else None

    async def get_preview(self, run_id: str) -> RunPreviewSnapshot | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        state = _state_snapshot(run)
        if run.status is RunStatus.EXPIRED:
            return RunPreviewSnapshot(state, None, None, None)
        if run.status is RunStatus.COMPLETED:
            graph, communities = _completed_payload(run)
            return RunPreviewSnapshot(state, None, graph, communities)
        checkpoint = _checkpoint_snapshot(run._checkpoint)
        return RunPreviewSnapshot(state, checkpoint, None, None)

    async def get_graph(self, run_id: str) -> RunCompletedGraphSnapshot | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        state = _state_snapshot(run)
        if run.status is not RunStatus.COMPLETED:
            return RunCompletedGraphSnapshot(state, None, None)
        graph, communities = _completed_payload(run)
        return RunCompletedGraphSnapshot(state, graph, communities)

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
    readiness_request_timeout_seconds = 2.0

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
        rest_path: str = "/rest/v1",
        governor: GlobalWikimediaGovernor = DEFAULT_GOVERNOR,
        cache: ResponseCache | None = None,
        clock: Callable[[], datetime] | None = None,
        heartbeat_interval_seconds: float = 30.0,
        attempt_quota: int = 120,
    ) -> None:
        self._url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self._rest_path = rest_path.strip("/")
        if not self._url or not self._key:
            raise PersistenceError("Supabase configuration is unavailable.")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None
        self._async_client = async_client or httpx.AsyncClient(timeout=20.0)
        self._owns_async_client = async_client is None
        self._governor = governor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if heartbeat_interval_seconds <= 0:
            raise ValueError("Heartbeat interval must be positive.")
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        if attempt_quota < 1:
            raise ValueError("Run attempt quota must be positive.")
        self._attempt_quota = attempt_quota
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
        self._remote_poll_seconds = 0.5
        self._last_reconcile_at = 0.0
        self._reconcile_lock = threading.Lock()

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
        """Verify the canonical persisted-run contract with one bounded query."""
        self._request(
            "GET",
            headers=self._headers(),
            params={"select": "run_id,owner_token,owner_version,checkpoint", "limit": "1"},
            timeout=self.readiness_request_timeout_seconds,
        )

    def validate_runtime_schema(self) -> None:
        """Verify every table and RPC required by the runtime at startup."""
        self._request(
            "GET", headers=self._headers(), params={"select": "run_id,truncated", "limit": "0"}
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
        self._rpc_request(
            "read_crawl_run_snapshot",
            {"p_run_id": str(uuid.uuid4()), "p_view": "state"},
        )

    def _rpc_request(self, function: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._request_url(
            self._rest_endpoint(f"rpc/{function}"),
            "POST",
            headers=self._headers(),
            json=payload,
        )

    def _rpc_boolean(self, function: str, payload: dict[str, Any]) -> bool:
        endpoint = self._rest_endpoint(f"rpc/{function}")
        started = time.perf_counter()
        response: httpx.Response | None = None
        operation = str(payload.get("p_operation", function))
        run_id = payload.get("p_run_id")
        payload_bytes = len(json.dumps(payload, separators=(",", ":")).encode())
        try:
            response = self._client.post(endpoint, headers=self._headers(), json=payload)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self._emit_persistence_write_failure(
                operation, run_id, payload_bytes, started, exc, response
            )
            raise PersistenceError("Supabase persistence operation failed.") from exc
        if not isinstance(value, bool):
            invalid_result = ValueError("invalid run update result")
            self._emit_persistence_write_failure(
                operation, run_id, payload_bytes, started, invalid_result, response
            )
            raise PersistenceError("Supabase returned an invalid run update result.")
        return value

    @staticmethod
    def _emit_persistence_write_failure(
        operation: str,
        run_id: Any,
        payload_bytes: int,
        started: float,
        exc: Exception,
        response: httpx.Response | None,
    ) -> None:
        emit(
            "persistence_write_failure",
            operation=operation,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            payload_bytes=payload_bytes,
            exception_class=type(exc).__name__,
            http_status_code=response.status_code if response is not None else None,
            run_id=run_id,
        )

    def _rpc_heartbeat(self, payload: dict[str, Any]) -> bool:
        endpoint = self._rest_endpoint("rpc/heartbeat_crawl_run")
        try:
            response = self._client.post(endpoint, headers=self._headers(), json=payload)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise PersistenceError("Supabase persistence operation failed.") from exc
        if value == []:
            return False
        if (
            isinstance(value, list)
            and len(value) == 1
            and isinstance(value[0], dict)
            and isinstance(value[0].get("lease_until"), str)
        ):
            try:
                lease_until = datetime.fromisoformat(
                    value[0]["lease_until"].replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise PersistenceError(
                    "Supabase returned an invalid heartbeat result."
                ) from exc
            if lease_until.tzinfo is None:
                raise PersistenceError("Supabase returned an invalid heartbeat result.")
            return True
        raise PersistenceError("Supabase returned an invalid heartbeat result.")

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
        run_id = str(uuid.uuid4())
        owner_token = str(uuid.uuid4())
        row = {
            "p_run_id": run_id,
            "p_seed": request.seed,
            "p_language": request.language,
            "p_depth": request.depth,
            "p_node_cap": request.node_cap,
            "p_checkpoint": _checkpoint_json(_initial_checkpoint(request)),
            "p_owner_token": owner_token,
            "p_attempt_quota": self._attempt_quota,
        }
        records = self._rpc_request("create_owned_crawl_run", row)
        if not records or records[0].get("run_id") != run_id:
            raise PersistenceError("Supabase did not create the crawl run.")
        owner_version = int(records[0]["owner_version"])
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
            persist_progress=lambda progress: self._save_progress(
                run_id, owner_token, owner_version, progress
            ),
            persist_checkpoint=lambda checkpoint: self._save_checkpoint(
                run_id, owner_token, owner_version, checkpoint
            ),
            persist_completion=lambda completed: self._save_completion(
                completed, owner_token, owner_version
            ),
            persist_failure=lambda failed: self._save_failure(
                failed, owner_token, owner_version
            ),
            governor=self._governor,
            cache=self._cache,
            persist_recovery=lambda recovered: self._save_recovery(
                recovered, owner_token, owner_version
            ),
            owner_token=owner_token,
            owner_version=owner_version,
        )
        run.task = asyncio.create_task(
            self._run_owned(run, transport, owner_token, owner_version)
        )
        self._runs[run_id] = run
        return run

    async def cancel_run(self, run_id: str) -> str:
        """Cancel a persisted run and stop any locally-owned worker promptly."""
        result = await asyncio.to_thread(self._cancel_run, run_id)
        if result == "cancelled":
            active = self._runs.get(run_id)
            if active is not None:
                active._ownership_lost = True
                if active.task is not None and not active.task.done():
                    active.task.cancel()
        return result

    def _cancel_run(self, run_id: str) -> str:
        endpoint = self._rest_endpoint("rpc/cancel_crawl_run")
        try:
            response = self._client.post(
                endpoint,
                headers=self._headers(),
                json={"p_run_id": run_id},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise PersistenceError("Supabase crawl run cancellation failed.") from exc
        result = body[0].get("result") if (
            isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict)
        ) else None
        if not isinstance(result, str) or result not in {
            "cancelled", "not_found", "not_running", "expired"
        }:
            raise PersistenceError(
                "Supabase returned an invalid crawl run cancellation result."
            )
        return result

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
        # Locally-owned work is authoritative only while its task is actually
        # running. A hydrated handle has no task and must follow the database.
        if (
            active is not None
            and not active._ownership_lost
            and active.task is not None
            and not active.task.done()
        ):
            return active
        if row.get("status") == RunStatus.RUNNING.value and row.get("owner_token") is None:
            raise PersistenceError(
                "Legacy crawl run has no owner lease; operator repair is required."
            )
        if row.get("status") == RunStatus.RUNNING.value and "owner_token" in row:
            # Reconciliation scans all expired leases; bound that global sweep
            # independently of the per-run observation polling frequency.
            with self._reconcile_lock:
                now = time.monotonic()
                if now - self._last_reconcile_at >= 5.0:
                    self._rpc_request("reconcile_expired_crawl_runs", {})
                    self._last_reconcile_at = now
            records = self._request(
                "GET",
                headers=self._headers(),
                params={"run_id": f"eq.{run_id}", "limit": "1"},
            )
            if not records:
                return None
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
            if (
                not active._ownership_lost
                and active.task is not None
                and not active.task.done()
            ):
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
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.RECOVERABLE,
            RunStatus.OVERLOAD_WAITING,
            RunStatus.EXPIRED,
        }:
            run._done.set()
        self._runs[run_id] = run
        return run

    def get_local_worker(self, run_id: str) -> CrawlRun | None:
        """Return an already-local lifecycle worker without reading persistence."""
        return self._runs.get(run_id)

    async def get_state(self, run_id: str) -> RunStateSnapshot | None:
        row = await self._read_snapshot_row(run_id, "state")
        if row is None:
            return None
        state = _snapshot_state_from_row(row)
        self._fence_local_worker(state)
        return state

    async def get_preview(self, run_id: str) -> RunPreviewSnapshot | None:
        row = await self._read_snapshot_row(run_id, "preview")
        if row is None:
            return None
        state = _snapshot_state_from_row(row)
        self._fence_local_worker(state)
        if state.status is RunStatus.EXPIRED:
            return RunPreviewSnapshot(state, None, None, None)
        if state.status is RunStatus.COMPLETED:
            graph, communities = _graph_snapshot_from_json(row["graph"])
            return RunPreviewSnapshot(state, None, graph, communities)
        return RunPreviewSnapshot(
            state, _checkpoint_snapshot_from_json(row["checkpoint"]), None, None
        )

    async def get_graph(self, run_id: str) -> RunCompletedGraphSnapshot | None:
        row = await self._read_snapshot_row(run_id, "graph")
        if row is None:
            return None
        state = _snapshot_state_from_row(row)
        self._fence_local_worker(state)
        if state.status is not RunStatus.COMPLETED:
            return RunCompletedGraphSnapshot(state, None, None)
        graph, communities = _graph_snapshot_from_json(row["graph"])
        return RunCompletedGraphSnapshot(state, graph, communities)

    async def wait_for_canonical(self, run_id: str) -> CrawlRun | None:
        """Wait for remote ownership to publish a terminal canonical record."""
        while True:
            run = await asyncio.to_thread(self.get, run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                return run
            await asyncio.sleep(self._remote_poll_seconds)

    async def poll_canonical(self, run_id: str) -> CrawlRun | None:
        """Refresh a remote-owned handle without taking or renewing ownership."""
        return await asyncio.to_thread(self.get, run_id)

    async def _read_snapshot_row(self, run_id: str, view: str) -> dict[str, Any] | None:
        endpoint = self._rest_endpoint("rpc/read_crawl_run_snapshot")
        try:
            response = await self._async_client.post(
                endpoint,
                headers=self._headers(),
                json={"p_run_id": run_id, "p_view": view},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise PersistenceError("Supabase persistence operation failed.") from exc
        if not isinstance(body, list) or len(body) > 1:
            raise PersistenceError("Supabase returned an invalid run snapshot.")
        if not body:
            return None
        if not isinstance(body[0], dict):
            raise PersistenceError("Supabase returned an invalid run snapshot.")
        return body[0]

    def _fence_local_worker(self, state: RunStateSnapshot) -> None:
        active = self._runs.get(state.id)
        if active is None or active._ownership_lost or active.task is None or active.task.done():
            return
        expired = state.status is RunStatus.EXPIRED
        ownership_changed = (
            active._owner_token is not None
            and (state.owner_token != active._owner_token
                 or state.owner_version != active._owner_version)
        )
        if (expired or ownership_changed) and self._runs.get(state.id) is active:
            active._ownership_lost = True
            active.task.cancel()

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
        owner_token = str(uuid.uuid4())
        claimed = self._rpc_request(
            "claim_crawl_run",
            {
                "p_run_id": run_id,
                "p_owner_token": owner_token,
                "p_expected_version": int(row.get("owner_version", 0)),
            },
        )
        if not claimed:
            raise PersistenceError(
                "Crawl run ownership changed before it could be claimed."
            )
        owner_version = int(claimed[0]["owner_version"])
        run = CrawlRun(
            run_id,
            request,
            governor=self._governor,
            cache=self._cache,
            checkpoint=checkpoint,
            persist_progress=lambda progress: self._save_progress(
                run_id, owner_token, owner_version, progress
            ),
            persist_checkpoint=lambda value: self._save_checkpoint(
                run_id, owner_token, owner_version, value
            ),
            persist_completion=lambda completed: self._save_completion(
                completed, owner_token, owner_version
            ),
            persist_failure=lambda failed: self._save_failure(
                failed, owner_token, owner_version
            ),
            persist_recovery=lambda recovered: self._save_recovery(
                recovered, owner_token, owner_version
            ),
            owner_token=owner_token,
            owner_version=owner_version,
        )
        run.task = asyncio.create_task(
            self._run_owned(run, transport, owner_token, owner_version)
        )
        self._runs[run_id] = run
        return run

    def _save_progress(
        self, run_id: str, token: str, version: int, progress: Progress
    ) -> None:
        self._fenced_update(
            run_id, token, version, "progress", {"progress": _progress_json(progress)}
        )

    async def _run_owned(
        self,
        run: CrawlRun,
        transport: httpx.AsyncBaseTransport | None,
        token: str,
        version: int,
    ) -> None:
        acquisition = asyncio.create_task(run.start(transport))
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(run.id, token, version, acquisition)
        )
        try:
            await acquisition
        finally:
            if not acquisition.done():
                acquisition.cancel()
                await asyncio.gather(acquisition, return_exceptions=True)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat_loop(
        self, run_id: str, token: str, version: int, acquisition: asyncio.Task[None]
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            try:
                renewed = await asyncio.to_thread(
                    self._rpc_heartbeat,
                    {
                        "p_run_id": run_id,
                        "p_owner_token": token,
                        "p_owner_version": version,
                    },
                )
            except Exception:
                renewed = False
            if not renewed:
                run = self._runs.get(run_id)
                if run is not None:
                    run._ownership_lost = True
                acquisition.cancel()
                return

    def _save_checkpoint(
        self, run_id: str, token: str, version: int, checkpoint: CrawlCheckpoint
    ) -> None:
        self._fenced_update(
            run_id,
            token,
            version,
            "checkpoint",
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

    def _save_completion(self, run: CrawlRun, token: str, version: int) -> None:
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
        self._fenced_update(run.id, token, version, "completed", {"graph": graph})

    def _save_failure(self, run: CrawlRun, token: str, version: int) -> None:
        self._fenced_update(run.id, token, version, "failed", {"error": run.error})

    def _save_recovery(self, run: CrawlRun, token: str, version: int) -> None:
        operation = "overload_waiting" if run.status is RunStatus.OVERLOAD_WAITING else "recoverable"
        self._fenced_update(
            run.id,
            token,
            version,
            operation,
            {
                "error": run.error,
                "retry_state": run.retry_state,
            },
        )

    def _fenced_update(
        self,
        run_id: str,
        token: str,
        version: int,
        operation: str,
        patch: dict[str, Any],
    ) -> None:
        updated = self._rpc_boolean(
            "fenced_update_crawl_run",
            {
                "p_run_id": run_id,
                "p_owner_token": token,
                "p_owner_version": version,
                "p_operation": operation,
                "p_patch": patch,
            },
        )
        if not updated:
            raise PersistenceError("Supabase rejected a stale or invalid crawl run update.")
        emit(
            "persistence_write",
            run_id=run_id,
            operation=operation,
            fields=sorted(patch),
            payload_bytes=len(str(patch).encode()),
        )

    async def shutdown(self) -> None:
        tasks = [run.task for run in self._runs.values() if run.task is not None]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_client:
            self._client.close()
        if self._owns_async_client:
            await self._async_client.aclose()


_RUN_SNAPSHOT_FIELDS = {
    "run_id", "seed", "language", "depth", "node_cap", "status", "progress",
    "error", "retry_state", "expires_at", "owner_token", "owner_version",
    "lease_until", "checkpoint", "graph", "truncated",
}


def _snapshot_state_from_row(row: dict[str, Any]) -> RunStateSnapshot:
    try:
        if set(row) != _RUN_SNAPSHOT_FIELDS:
            raise ValueError("unexpected run snapshot fields")
        status = RunStatus(row["status"])
        progress = row["progress"]
        if not isinstance(progress, dict):
            raise ValueError("invalid progress")
        recent = progress["recent"]
        if not isinstance(recent, list) or not all(isinstance(item, str) for item in recent):
            raise ValueError("invalid recent progress")
        retry_value = row["retry_state"]
        if retry_value is not None and not isinstance(retry_value, dict):
            raise ValueError("invalid retry state")
        retry = None
        if retry_value is not None:
            attempts = retry_value.get("attempts")
            retry_after = retry_value.get("retry_after")
            if attempts is not None and (isinstance(attempts, bool) or not isinstance(attempts, int)):
                raise ValueError("invalid retry attempts")
            if retry_after is not None and (isinstance(retry_after, bool) or not isinstance(retry_after, (int, float))):
                raise ValueError("invalid retry delay")
            retry = RunRetrySnapshot(attempts, float(retry_after) if retry_after is not None else None)
        expires_value = row["expires_at"]
        expires = _snapshot_datetime(expires_value) if expires_value is not None else None
        owner = row["owner_token"]
        version = row["owner_version"]
        if owner is not None and not isinstance(owner, str):
            raise ValueError("invalid owner token")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise ValueError("invalid owner version")
        request = CrawlRequest(
            seed=_snapshot_string(row["seed"]),
            depth=_snapshot_int(row["depth"]),
            language=_snapshot_string(row["language"]),
            node_cap=_snapshot_int(row["node_cap"]),
        )
        error = row["error"]
        if error is not None and not isinstance(error, str):
            raise ValueError("invalid run error")
        truncated_value = row["truncated"]
        if status is RunStatus.COMPLETED:
            truncated = _snapshot_bool(truncated_value)
        else:
            if truncated_value is not None:
                raise ValueError("truncated must be null outside completed status")
            truncated = None
        return RunStateSnapshot(
            id=_snapshot_string(row["run_id"]), request=request, status=status,
            progress=RunProgressSnapshot(
                crawled=_snapshot_int(progress["crawled"]),
                discovered=_snapshot_int(progress["discovered"]),
                depth=_snapshot_int(progress["depth"]), recent=tuple(recent),
            ),
            error=error, retry_state=retry, expires_at=expires,
            owner_token=owner, owner_version=version, truncated=truncated,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PersistenceError("Supabase returned an invalid run snapshot.") from exc


def _snapshot_string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string")
    return value


def _snapshot_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected integer")
    return value


def _snapshot_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expected timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def _checkpoint_snapshot_from_json(value: Any) -> RunCheckpointSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PersistenceError("Supabase returned an invalid run snapshot.")
    try:
        nodes = tuple(GraphNode(**node) for node in value["nodes"])
        edges = tuple(GraphEdge(**edge) for edge in value["edges"])
        frontier = value["frontier"]
        next_frontier = value["next_frontier"]
        current_batch = value["current_batch"]
        recent = value["recent"]
        if not all(isinstance(items, list) and all(isinstance(item, str) for item in items)
                   for items in (frontier, next_frontier, current_batch, recent)):
            raise ValueError("invalid checkpoint titles")
        continuation = value["continuation"]
        if continuation is not None and not isinstance(continuation, str):
            raise ValueError("invalid continuation")
        return RunCheckpointSnapshot(
            level=_snapshot_int(value["level"]), frontier=tuple(frontier),
            batch_start=_snapshot_int(value["batch_start"]),
            next_frontier=tuple(next_frontier), current_batch=tuple(current_batch),
            continuation=continuation, nodes=nodes, edges=edges,
            truncated=_snapshot_bool(value["truncated"]),
            crawled=_snapshot_int(value["crawled"]), recent=tuple(recent),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError("Supabase returned an invalid run snapshot.") from exc


def _graph_snapshot_from_json(
    value: Any,
) -> tuple[RunGraphSnapshot | None, RunCommunitySnapshot | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise PersistenceError("Supabase returned an invalid run snapshot.")
    try:
        graph = RunGraphSnapshot(
            nodes=tuple(GraphNode(**node) for node in value["nodes"]),
            edges=tuple(GraphEdge(**edge) for edge in value["edges"]),
            truncated=_snapshot_bool(value["truncated"]),
            crawled=_snapshot_int(value["crawled"]),
            discovered=_snapshot_int(value["discovered"]),
        )
        ids_value = value["community_ids"]
        if isinstance(ids_value, dict):
            ids = tuple(sorted((str(key), _snapshot_int(item)) for key, item in ids_value.items()))
        elif isinstance(ids_value, list):
            ids = tuple(sorted((str(pair[0]), _snapshot_int(pair[1])) for pair in ids_value))
        else:
            raise ValueError("invalid community ids")
        communities = RunCommunitySnapshot(
            ids=ids, count=_snapshot_int(value["community_count"]),
            modularity=float(value["modularity"]),
        )
        return graph, communities
    except (KeyError, TypeError, ValueError, OverflowError, IndexError) as exc:
        raise PersistenceError("Supabase returned an invalid run snapshot.") from exc


def _snapshot_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected boolean")
    return value


def _state_snapshot(run: CrawlRun) -> RunStateSnapshot:
    progress = run.progress
    retry_state = run.retry_state
    return RunStateSnapshot(
        id=run.id,
        request=run.request,
        status=run.status,
        progress=RunProgressSnapshot(
            crawled=int(progress.get("crawled", 0)),
            discovered=int(progress.get("discovered", 0)),
            depth=int(progress.get("depth", 0)),
            recent=tuple(str(title) for title in progress.get("recent", ())),
        ),
        error=run.error,
        retry_state=(
            RunRetrySnapshot(
                attempts=(
                    int(retry_state["attempts"])
                    if retry_state.get("attempts") is not None
                    else None
                ),
                retry_after=(
                    float(retry_state["retry_after"])
                    if retry_state.get("retry_after") is not None
                    else None
                ),
            )
            if retry_state is not None
            else None
        ),
        # The in-memory store has no retention clock or expiry policy.
        expires_at=None,
        truncated=(
            run.result.truncated
            if run.status is RunStatus.COMPLETED
            and run.result is not None
            and isinstance(run.result.truncated, bool)
            else None
        ),
    )


def _checkpoint_snapshot(
    checkpoint: CrawlCheckpoint | None,
) -> RunCheckpointSnapshot | None:
    if checkpoint is None:
        return None
    return RunCheckpointSnapshot(
        level=checkpoint.level,
        frontier=tuple(checkpoint.frontier),
        batch_start=checkpoint.batch_start,
        next_frontier=tuple(checkpoint.next_frontier),
        current_batch=tuple(checkpoint.current_batch),
        continuation=checkpoint.continuation,
        nodes=tuple(checkpoint.nodes),
        edges=tuple(checkpoint.edges),
        truncated=checkpoint.truncated,
        crawled=checkpoint.crawled,
        recent=tuple(checkpoint.recent),
    )


def _completed_payload(
    run: CrawlRun,
) -> tuple[RunGraphSnapshot | None, RunCommunitySnapshot | None]:
    if run.result is None or run.communities is None:
        return None, None
    return (
        RunGraphSnapshot(
            nodes=tuple(run.result.nodes),
            edges=tuple(run.result.edges),
            truncated=run.result.truncated,
            crawled=run.result.crawled,
            discovered=run.result.discovered,
        ),
        RunCommunitySnapshot(
            ids=tuple(sorted(run.communities.ids.items())),
            count=run.communities.count,
            modularity=run.communities.modularity,
        ),
    )


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
