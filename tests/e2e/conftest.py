from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import uvicorn

from tests.support.browser import boot_view, completed_run, view_url
from tests.support.mediawiki import FakeMediaWiki
from wikigraph.app import STATIC_DIR, create_app
from wikigraph.runs import InMemoryCrawlRunStore

VIEW_ENTRY = STATIC_DIR / "index.html"


def console_errors(bucket: list[str]) -> Callable[[Any], None]:
    def handler(message: Any) -> None:
        if message.type == "error":
            bucket.append(message.text)

    return handler


@pytest.fixture
async def view_server(
    stub: FakeMediaWiki, free_port: int, view_store: InMemoryCrawlRunStore
) -> AsyncIterator[httpx.AsyncClient]:
    """The real FastAPI app on a real port, MediaWiki transport stubbed."""
    if not VIEW_ENTRY.is_file():
        pytest.skip("view not built: run `npm run build` in frontend/")
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(mediawiki_transport=stub.transport, run_store=view_store),
            host="127.0.0.1", port=free_port, log_level="error",
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
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def view_store() -> InMemoryCrawlRunStore:
    return InMemoryCrawlRunStore()


@pytest.fixture
async def browser_page(
    request: pytest.FixtureRequest, view_server: httpx.AsyncClient
) -> AsyncIterator[Any]:
    """Chromium page that fails teardown on unexpected browser errors."""
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
        message for message in page_errors
        if not any(f"status of {status} " in message for status in expected_statuses)
    ]
    assert unexpected == [], f"the view produced errors: {page_errors}"
