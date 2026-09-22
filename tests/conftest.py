from __future__ import annotations

import asyncio
import os
import socket
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import uvicorn

from tests.helpers import collect_events, create_run
from tests.stub import FakeMediaWiki
from wikigraph.app import STATIC_DIR, create_app

VIEW_ENTRY = STATIC_DIR / "index.html"
BOOT_TIMEOUT = 10_000


@pytest.fixture(autouse=True)
def isolate_real_upstream_cache() -> None:
    """Keep real integration tests independent while exercising shared cache wiring."""
    url = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_URL")
    key = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_KEY")
    if url and key:
        response = httpx.delete(
            f"{url}/upstream_response_cache",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
        response.raise_for_status()


@pytest.fixture
def stub() -> FakeMediaWiki:
    return FakeMediaWiki()


@pytest.fixture
async def client(stub: FakeMediaWiki) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(mediawiki_transport=stub.transport)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


def console_errors(bucket: list[str]) -> Callable[[Any], None]:
    def handler(message: Any) -> None:
        if message.type == "error":
            bucket.append(message.text)

    return handler


def view_url(client: httpx.AsyncClient, run_id: str) -> str:
    return f"{client.base_url}/?run={run_id}"


async def completed_run(
    stub: FakeMediaWiki, client: httpx.AsyncClient, *, seed: str, depth: int
) -> str:
    """Create a run and wait for its terminal event; returns the run id."""
    run_id = await create_run(client, seed=seed, depth=depth)
    events = await collect_events(client, run_id)
    assert events[-1]["type"] == "completed", events
    return run_id


async def boot_view(page: Any, client: httpx.AsyncClient, run_id: str) -> None:
    """Load the v2 view for a run and wait until the experience is up."""
    await page.goto(view_url(client, run_id))
    await page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)


@pytest.fixture
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
async def view_server(
    stub: FakeMediaWiki, free_port: int
) -> AsyncIterator[httpx.AsyncClient]:
    """The real FastAPI app on a real port, MediaWiki transport stubbed."""
    if not VIEW_ENTRY.is_file():
        pytest.skip("view not built: run `npm run build` in frontend/")
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(mediawiki_transport=stub.transport),
            host="127.0.0.1",
            port=free_port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("the FastAPI server never finished starting")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{free_port}") as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
async def browser_page(
    request: pytest.FixtureRequest, view_server: httpx.AsyncClient
) -> AsyncIterator[Any]:
    """A Chromium page wired to fail on any error the view produces.

    A test may expect specific failing HTTP statuses (their console trace is
    the honest failure path itself); everything else fails the teardown.
    """
    expected_statuses = getattr(request, "param", ())
    playwright = pytest.importorskip("playwright.async_api")
    page_errors: list[str] = []
    driver = await playwright.async_playwright().start()
    browser = await driver.chromium.launch()
    page = await browser.new_page()
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", console_errors(page_errors))
    yield page
    await page.close()
    await browser.close()
    await driver.stop()
    unexpected = [
        message
        for message in page_errors
        if not any(f"status of {status} " in message for status in expected_statuses)
    ]
    assert unexpected == [], f"the view produced errors: {page_errors}"
