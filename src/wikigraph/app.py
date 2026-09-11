"""FastAPI app: run creation, SSE progress, Graph delivery, and the static UI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.responses import Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from wikigraph.crawler import CrawlRequest
from wikigraph.mediawiki import MediaWikiClient
from wikigraph.runs import (
    CrawlRun,
    CrawlRunRegistry,
    RunStatus,
    TERMINAL_EVENT_TYPES,
)
from wikigraph.seed import SeedError, parse_seed

STATIC_DIR = Path(__file__).parent / "static"
SSE_HEARTBEAT_SECONDS = 15.0


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


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"error": {"code": code, "message": message}}
    )


def create_app(mediawiki_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    registry = CrawlRunRegistry()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await registry.shutdown()

    app = FastAPI(title="wikiGraph", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def structured_errors(request: Request, exc: HTTPException) -> Response:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return await http_exception_handler(request, exc)

    @app.post("/api/runs", status_code=201)
    async def create_run(body: CreateRunBody) -> RunCreated:
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
        client = MediaWikiClient(language, transport=mediawiki_transport)
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
        crawl_request = CrawlRequest(
            seed=seed_resolution.title,
            depth=body.depth,
            language=language,
            node_cap=body.node_cap,
        )
        run = registry.start_run(crawl_request, mediawiki_transport)
        return RunCreated(run_id=run.id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        run = _require_run(registry, run_id)
        return StreamingResponse(
            _event_stream(run),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/runs/{run_id}/graph")
    async def run_graph(run_id: str) -> GraphOut:
        run = _require_run(registry, run_id)
        await run.wait_done()
        if (
            run.status is RunStatus.FAILED
            or run.result is None
            or run.communities is None
        ):
            raise _error(
                409, "run_failed", run.error or "The crawl run failed."
            )
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


def _require_run(registry: CrawlRunRegistry, run_id: str) -> CrawlRun:
    run = registry.get(run_id)
    if run is None:
        raise _error(404, "unknown_run", "No crawl run with that identifier.")
    return run


async def _event_stream(run: CrawlRun) -> AsyncIterator[str]:
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
                if run.task is not None:
                    await run.task
                return
    finally:
        run.unsubscribe(queue)


app = create_app()
