"""FastAPI app: run creation, SSE progress, Graph delivery, and the static UI."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler
from starlette.responses import Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from wikigraph.crawler import CrawlRequest
from wikigraph.governor import DEFAULT_GOVERNOR, GlobalWikimediaGovernor
from wikigraph.mediawiki import MediaWikiClient
from wikigraph.runs import (
    CrawlRunHandle,
    CrawlRunStore,
    InMemoryCrawlRunStore,
    LaunchAdmissionDecision,
    LaunchAdmissionStore,
    PersistenceError,
    RunStatus,
    SupabaseCrawlRunStore,
    TERMINAL_EVENT_TYPES,
)
from wikigraph.seed import SeedError, parse_seed

STATIC_DIR = Path(__file__).parent / "static"
SSE_HEARTBEAT_SECONDS = 15.0
LAUNCH_WINDOW_SECONDS = 60.0
QUOTA_WINDOW_SECONDS = 24 * 60 * 60
RETENTION_CLEANUP_INTERVAL_SECONDS = 15 * 60
logger = logging.getLogger(__name__)


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CreateRunBody(CamelModel):
    seed: str = Field(min_length=1)
    depth: int = Field(default=1, ge=1, le=3)
    language: Literal["es", "en"] = "es"
    node_cap: int = Field(default=500, ge=1, le=5000)


class RunCreated(CamelModel):
    run_id: str


class GraphNodeOut(CamelModel):
    title: str
    level: int
    is_seed: bool
    community_id: int


class GraphEdgeOut(CamelModel):
    source: str
    target: str


class GraphOut(CamelModel):
    run_id: str
    seed: str
    language: str
    depth: int
    truncated: bool
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    crawled: int
    discovered: int
    community_count: int
    modularity: float


class RunStateOut(CamelModel):
    run_id: str
    seed: str
    language: str
    depth: int
    node_cap: int
    status: str
    crawled: int
    discovered: int
    current_depth: int
    recent: list[str]
    error: str | None = None
    retry_after: float | None = None


class PreviewOut(GraphOut):
    complete: bool


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"error": {"code": code, "message": message}}
    )


@dataclass(frozen=True)
class LaunchLimits:
    """Limits on accepted Crawl launches, separate from MediaWiki accounting."""

    per_ip_per_minute: int = 5
    deployment_per_minute: int = 30
    per_ip_per_day: int = 50
    max_ip_keys: int = 10_000

    @classmethod
    def from_environment(cls) -> "LaunchLimits":
        return cls(
            per_ip_per_minute=_positive_setting("WIKIGRAPH_LAUNCHES_PER_IP", 5),
            deployment_per_minute=_positive_setting("WIKIGRAPH_LAUNCHES_PER_MINUTE", 30),
            per_ip_per_day=_positive_setting("WIKIGRAPH_LAUNCH_QUOTA", 50),
            max_ip_keys=_positive_setting("WIKIGRAPH_LAUNCH_IP_KEYS", 10_000),
        )


def _positive_setting(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer.") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer.")
    return value


def _positive_float_setting(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number.")
    return value


class LaunchLimiter:
    def __init__(
        self, limits: LaunchLimits, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._per_ip: dict[str, deque[float]] = defaultdict(deque)
        self._deployment: deque[float] = deque()
        self._quota: dict[str, deque[float]] = defaultdict(deque)

    def admit(self, ip: str) -> None:
        now = self._clock()
        self._trim(self._deployment, now, LAUNCH_WINDOW_SECONDS)
        per_ip = self._per_ip[ip]
        quota = self._quota[ip]
        self._trim(per_ip, now, LAUNCH_WINDOW_SECONDS)
        self._trim(quota, now, QUOTA_WINDOW_SECONDS)
        if len(per_ip) >= self._limits.per_ip_per_minute:
            raise _error(
                429,
                "launch_rate_limited",
                "Too many crawl launches from this address. Try again shortly.",
            )
        if len(self._deployment) >= self._limits.deployment_per_minute:
            raise _error(
                429,
                "launch_rate_limited",
                "The crawl launch service is busy. Try again shortly.",
            )
        if len(quota) >= self._limits.per_ip_per_day:
            raise _error(
                429,
                "launch_quota_exceeded",
                "This address has reached its crawl quota. Try again tomorrow.",
            )
        per_ip.append(now)
        self._deployment.append(now)
        quota.append(now)

    @staticmethod
    def _trim(values: deque[float], now: float, window: float) -> None:
        while values and now - values[0] >= window:
            values.popleft()


def validate_production_configuration() -> None:
    """Fail startup rather than silently running without canonical persistence."""
    missing = [
        name
        for name in (
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "WIKIGRAPH_USER_AGENT",
            "WIKIGRAPH_IP_HASH_SECRET",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            "Production configuration is missing required server-only settings: "
            + ", ".join(missing)
        )


def _hash_client_ip(client_ip: str) -> str:
    secret = os.environ.get("WIKIGRAPH_IP_HASH_SECRET", "local-development-only")
    return hmac.new(secret.encode(), client_ip.encode(), hashlib.sha256).hexdigest()


def _require_admission(decision: LaunchAdmissionDecision) -> None:
    if decision.admitted:
        return
    messages = {
        "launch_rate_limited": "Too many crawl launches right now. Try again shortly.",
        "launch_quota_exceeded": "This address has reached its crawl quota. Try again tomorrow.",
        "storage_quota_exceeded": "The crawl launch service is at capacity. Try again later.",
    }
    status = 429 if decision.code in messages else 503
    raise _error(status, decision.code, messages.get(decision.code, "Launch admission is unavailable."))


def create_app(
    mediawiki_transport: httpx.AsyncBaseTransport | None = None,
    run_store: CrawlRunStore | None = None,
    *,
    launch_limits: LaunchLimits | None = None,
    clock: Callable[[], float] = time.monotonic,
    production: bool | None = None,
    launch_admission_store: LaunchAdmissionStore | None = None,
    governor: GlobalWikimediaGovernor = DEFAULT_GOVERNOR,
) -> FastAPI:
    limits = launch_limits or LaunchLimits.from_environment()
    require_production_config = (
        production
        if production is not None
        else os.environ.get("WIKIGRAPH_ENV") == "production"
    )
    if require_production_config:
        validate_production_configuration()
    if run_store is None:
        store: CrawlRunStore = (
            SupabaseCrawlRunStore(governor=governor)
            if require_production_config
            else InMemoryCrawlRunStore(governor)
        )
    else:
        store = run_store
    admission_store = launch_admission_store
    if admission_store is None and require_production_config:
        from wikigraph.runs import SupabaseLaunchAdmissionStore

        admission_store = SupabaseLaunchAdmissionStore()
    limiter = None if admission_store is not None else LaunchLimiter(limits, clock)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        retention_task: asyncio.Task[None] | None = None
        if require_production_config:
            validate_production_configuration()
            try:
                if isinstance(store, SupabaseCrawlRunStore):
                    store.validate_runtime_schema()
                else:
                    store.healthcheck()
                store.cleanup_expired()
            except PersistenceError as exc:
                raise RuntimeError("Canonical Supabase persistence is unavailable.") from exc
            interval = _positive_float_setting(
                "WIKIGRAPH_RETENTION_INTERVAL_SECONDS",
                RETENTION_CLEANUP_INTERVAL_SECONDS,
            )

            async def retention_loop() -> None:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await asyncio.to_thread(store.cleanup_expired)
                    except PersistenceError:
                        logger.exception("Supabase retention cleanup failed")

            retention_task = asyncio.create_task(retention_loop())
        try:
            yield
        finally:
            if retention_task is not None:
                retention_task.cancel()
                await asyncio.gather(retention_task, return_exceptions=True)
            if admission_store is not None:
                admission_store.close()
            await store.shutdown()
        # The module-level governor is shared by the default app and clients;
        # closing it here would leave later app instances with a worker tied to
        # an already-closed event loop. Injected governors remain app-owned.
        if governor is not DEFAULT_GOVERNOR:
            await governor.shutdown()

    app = FastAPI(title="wikiGraph", lifespan=lifespan)

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readiness() -> dict[str, str]:
        try:
            await asyncio.to_thread(store.healthcheck)
        except PersistenceError as exc:
            raise _error(503, "persistence_unavailable", str(exc)) from exc
        return {
            "status": "ready",
            "persistence": "supabase" if require_production_config else "local",
        }

    @app.exception_handler(HTTPException)
    async def structured_errors(request: Request, exc: HTTPException) -> Response:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_errors(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The launch request is invalid. Check the seed, edition, depth, and node cap.",
                }
            },
        )

    @app.post("/api/runs", status_code=201)
    async def create_run(request: Request, body: CreateRunBody) -> RunCreated:
        try:
            parsed = parse_seed(body.seed)
        except SeedError as exc:
            raise _error(422, "malformed_seed", str(exc)) from exc
        if parsed.language is not None and parsed.language != body.language:
            raise _error(
                422,
                "edition_mismatch",
                f'The seed URL points at the "{parsed.language}" edition, '
                f'but the selected edition is "{body.language}". Match them and try again.',
            )
        language = parsed.language or body.language
        client = MediaWikiClient(
            language,
            transport=mediawiki_transport,
            governor=governor,
            owner="launch-validation",
        )
        try:
            resolved = await client.resolve_titles([parsed.title])
        finally:
            await client.aclose()
        seed_resolution = resolved.get(parsed.title)
        if seed_resolution is None or seed_resolution.missing:
            raise _error(
                404,
                "missing_article",
                f'There is no article "{parsed.title}" in this edition.',
            )
        if seed_resolution.namespace != 0:
            raise _error(
                422,
                "not_an_article",
                f'"{seed_resolution.title}" is not an article in this edition.',
            )
        client_ip = request.client.host if request.client is not None else "unknown"
        ip_hash = _hash_client_ip(client_ip)
        if admission_store is not None:
            try:
                decision = admission_store.admit(
                    ip_hash,
                    per_ip_per_minute=limits.per_ip_per_minute,
                    deployment_per_minute=limits.deployment_per_minute,
                    per_ip_per_day=limits.per_ip_per_day,
                    max_ip_keys=limits.max_ip_keys,
                )
            except PersistenceError as exc:
                raise _error(503, "launch_admission_unavailable", str(exc)) from exc
            _require_admission(decision)
        else:
            assert limiter is not None
            limiter.admit(client_ip)
        crawl_request = CrawlRequest(
            seed=seed_resolution.title,
            depth=body.depth,
            language=language,
            node_cap=body.node_cap,
        )
        try:
            run = store.start_run(crawl_request, mediawiki_transport)
        except PersistenceError as exc:
            raise _error(503, "persistence_unavailable", str(exc)) from exc
        return RunCreated(run_id=run.id)

    @app.post("/api/runs/{run_id}/retry", status_code=202)
    async def retry_run(run_id: str) -> RunCreated:
        try:
            run = store.retry_run(run_id, mediawiki_transport)
        except PersistenceError as exc:
            raise _error(409, "run_not_recoverable", str(exc)) from exc
        return RunCreated(run_id=run.id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        try:
            run = _require_run(store, run_id)
        except PersistenceError as exc:
            raise _error(503, "persistence_unavailable", str(exc)) from exc
        stream = (
            _remote_event_stream(store, run_id, run)
            if isinstance(store, SupabaseCrawlRunStore) and getattr(run, "task", None) is None
            else _event_stream(run)
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/runs/{run_id}", response_model=RunStateOut)
    async def run_state(run_id: str) -> RunStateOut:
        try:
            run = _require_run(store, run_id)
        except PersistenceError as exc:
            raise _error(503, "persistence_unavailable", str(exc)) from exc
        if run.status is RunStatus.EXPIRED:
            raise _error(
                410,
                "run_expired",
                "This crawl run has expired and its detailed state was removed.",
            )
        progress = run.progress
        retry_after = None
        if run.retry_state is not None:
            value = run.retry_state.get("retry_after")
            retry_after = float(value) if isinstance(value, (int, float)) else None
        return RunStateOut(
            run_id=run.id,
            seed=run.request.seed,
            language=run.request.language,
            depth=run.request.depth,
            node_cap=run.request.node_cap,
            status=run.status.value,
            crawled=int(progress.get("crawled", 0)),
            discovered=int(progress.get("discovered", 1)),
            current_depth=int(progress.get("depth", 0)),
            recent=[str(item) for item in progress.get("recent", [])],
            error=run.error,
            retry_after=retry_after,
        )

    @app.get("/api/runs/{run_id}/preview", response_model=PreviewOut)
    async def run_preview(run_id: str) -> PreviewOut:
        try:
            run = _require_run(store, run_id)
        except PersistenceError as exc:
            raise _error(503, "persistence_unavailable", str(exc)) from exc
        if run.status is RunStatus.EXPIRED:
            raise _error(
                410,
                "run_expired",
                "This crawl run has expired and its detailed state was removed.",
            )
        if run.result is not None and run.communities is not None:
            graph = _graph_out(run, complete=True)
            return PreviewOut(**graph.model_dump(), complete=True)
        checkpoint = getattr(run, "_checkpoint", None)
        if checkpoint is None:
            raise _error(409, "preview_unavailable", "No committed preview is available yet.")
        return PreviewOut(
            run_id=run.id,
            seed=run.request.seed,
            language=run.request.language,
            depth=run.request.depth,
            truncated=checkpoint.truncated,
            nodes=[GraphNodeOut(title=node.title, level=node.level, is_seed=node.is_seed, community_id=0) for node in checkpoint.nodes],
            edges=[GraphEdgeOut(source=edge.source, target=edge.target) for edge in checkpoint.edges],
            crawled=checkpoint.crawled,
            discovered=len(checkpoint.nodes),
            community_count=1,
            modularity=0.0,
            complete=False,
        )

    @app.get("/api/runs/{run_id}/graph")
    async def run_graph(run_id: str) -> GraphOut:
        try:
            run = _require_run(store, run_id)
        except PersistenceError as exc:
            raise _error(503, "persistence_unavailable", str(exc)) from exc
        try:
            if isinstance(store, SupabaseCrawlRunStore) and getattr(run, "task", None) is None:
                canonical = await store.wait_for_canonical(run_id)
                if canonical is None:
                    raise _error(404, "unknown_run", "No crawl run with that identifier.")
                run = canonical
            else:
                await run.wait_done()
        except PersistenceError as exc:
            raise _error(503, "persistence_unavailable", str(exc)) from exc
        if (
            run.status is RunStatus.FAILED
            or run.result is None
            or run.communities is None
        ):
            if run.status is RunStatus.OVERLOAD_WAITING:
                raise _error(
                    409,
                    "run_overload_waiting",
                    run.error or "Wikimedia overload; retry to resume.",
                )
            if run.status is RunStatus.RECOVERABLE:
                raise _error(
                    409,
                    "run_recoverable",
                    run.error or "The crawl run can be resumed.",
                )
            if run.status is RunStatus.EXPIRED:
                raise _error(
                    410,
                    "run_expired",
                    "This crawl run has expired and its detailed state was removed.",
                )
            raise _error(
                409, "run_failed", run.error or "The crawl run failed."
            )
        return _graph_out(run, complete=True)

    def _graph_out(run: CrawlRunHandle, *, complete: bool) -> GraphOut | PreviewOut:
        assert run.result is not None
        assert run.communities is not None
        result = run.result
        communities = run.communities
        return GraphOut(
            run_id=run.id,
            seed=run.request.seed,
            language=run.request.language,
            depth=run.request.depth,
            truncated=result.truncated,
            nodes=[
                GraphNodeOut(
                    title=node.title,
                    level=node.level,
                    is_seed=node.is_seed,
                    community_id=communities.ids[node.title],
                )
                for node in result.nodes
            ],
            edges=[
                GraphEdgeOut(source=edge.source, target=edge.target)
                for edge in result.edges
            ],
            crawled=result.crawled,
            discovered=result.discovered,
            community_count=communities.count,
            modularity=communities.modularity,
        )

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


def _require_run(store: CrawlRunStore, run_id: str) -> CrawlRunHandle:
    run = store.get(run_id)
    if run is None:
        raise _error(404, "unknown_run", "No crawl run with that identifier.")
    return run


async def _event_stream(run: CrawlRunHandle) -> AsyncIterator[str]:
    queue = run.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
            if event.type in TERMINAL_EVENT_TYPES:
                await run.wait_done()
                return
    finally:
        run.unsubscribe(queue)


async def _remote_event_stream(
    store: SupabaseCrawlRunStore, run_id: str, run: CrawlRunHandle
) -> AsyncIterator[str]:
    """Poll canonical progress/status for a run owned by another process."""
    previous_progress: dict[str, Any] = {}
    previous_status: RunStatus | None = None
    last_heartbeat = asyncio.get_running_loop().time()
    try:
        while True:
            if previous_status is not None:
                await asyncio.sleep(store._remote_poll_seconds)
            current = await store.poll_canonical(run_id)
            if current is None:
                yield "event: error\ndata: {\"code\":\"run_unavailable\"}\n\n"
                return
            if current.progress != previous_progress:
                previous_progress = dict(current.progress)
                yield f"event: progress\ndata: {json.dumps(previous_progress)}\n\n"
            if current.status is not previous_status:
                previous_status = current.status
                event_type = {
                    RunStatus.COMPLETED: "completed",
                    RunStatus.FAILED: "failed",
                    RunStatus.RECOVERABLE: "recoverable",
                    RunStatus.OVERLOAD_WAITING: "overload_waiting",
                    RunStatus.EXPIRED: "expired",
                }.get(current.status)
                if event_type is not None:
                    payload: dict[str, Any] = {"error": current.error} if current.error else {}
                    if event_type == "completed":
                        payload = {"truncated": current.truncated}
                    yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
                    return
            now = asyncio.get_running_loop().time()
            if current.status is not RunStatus.RUNNING:
                return
            if now - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
                last_heartbeat = now
                yield ": ping\n\n"
    except PersistenceError:
        # An established stream cannot change its HTTP status; terminate rather
        # than inventing progress or presenting a partial Graph as authoritative.
        yield "event: error\ndata: {\"code\":\"persistence_unavailable\"}\n\n"
        return


app = create_app()
