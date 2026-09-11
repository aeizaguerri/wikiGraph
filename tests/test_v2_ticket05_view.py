"""Frontend seam (ticket 05): a headless browser against the built dist.

The browser loads the real FastAPI app over real HTTP; the MediaWiki transport
is the one fake (``FakeMediaWiki``). Assertions are screen-level: the Sigma
camera and graphology graph exposed through ``window.__wikigraph`` describe
what a user sees, and any page error fails the suite.

Requires the bundle built via ``npm run build`` (``frontend/``) and the
Playwright Chromium browser (``uv run playwright install chromium``).
"""

from __future__ import annotations

import asyncio
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

VIEW_ENTRY = STATIC_DIR / "v2" / "index.html"
BOOT_TIMEOUT = 10_000
MOVE_TIMEOUT = 5_000
SETTLE_PAUSE = 0.35
ARTICLES = ["Ana", "Biología", "Química", "Astronomía", "Historia", "Selva", "Física"]
ARTICLE_LINKS = 10

INK_JS = """
(() => {
  const canvas = window.__wikigraph && window.__wikigraph.sigma.getCanvases().labels;
  if (!canvas || !canvas.width) return -1;
  const data = canvas.getContext("2d").getImageData(
    0, 0, canvas.width, canvas.height
  ).data;
  let ink = 0;
  for (let i = 3; i < data.length; i += 4) if (data[i] > 0) ink += 1;
  return ink;
})()
"""


def populate(stub: FakeMediaWiki) -> None:
    stub.add_page(
        "Ana",
        [(0, "Biología"), (0, "Química"), (0, "Astronomía"), (0, "Historia")],
    )
    stub.add_page(
        "Biología",
        [(0, "Química"), (0, "Astronomía"), (0, "Ana"), (0, "Selva")],
    )
    stub.add_page("Química", [(0, "Biología")])
    stub.add_page("Astronomía", [(0, "Física")])
    stub.add_page("Historia")
    stub.add_page("Selva")
    stub.add_page("Física")


def view_url(client: httpx.AsyncClient, run_id: str) -> str:
    return f"{client.base_url}/v2/?run={run_id}"


async def camera_state(page: Any) -> dict[str, float]:
    return await page.evaluate(
        "() => { const s = window.__wikigraph.sigma.getCamera().getState();"
        " return { x: s.x, y: s.y, ratio: s.ratio }; }"
    )


def console_errors(bucket: list[str]) -> Callable[[Any], None]:
    def handler(message: Any) -> None:
        if message.type == "error":
            bucket.append(message.text)

    return handler


async def completed_run(
    stub: FakeMediaWiki, client: httpx.AsyncClient, *, depth: int
) -> str:
    run_id = await create_run(client, seed="Ana", depth=depth)
    events = await collect_events(client, run_id)
    assert events[-1]["type"] == "completed", events
    return run_id


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
        pytest.skip("v2 view not built: run `npm run build` in frontend/")
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


async def test_boot_renders_the_completed_graph(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A real crawl's Graph renders as nodes and edges on the dark canvas."""
    populate(stub)
    run_id = await completed_run(stub, view_server, depth=2)
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "() => window.__wikigraph && window.__wikigraph.graph.order === 7",
        timeout=BOOT_TIMEOUT,
    )
    counts = await browser_page.evaluate(
        "() => ({ order: window.__wikigraph.graph.order, size: window.__wikigraph.graph.size })"
    )
    assert counts == {"order": len(ARTICLES), "size": ARTICLE_LINKS}
    layers = await browser_page.evaluate(
        "() => Object.keys(window.__wikigraph.sigma.getCanvases())"
    )
    for layer in ("edges", "nodes", "labels"):
        assert layer in layers


async def test_wheel_zoom_and_pan_drive_the_camera(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    populate(stub)
    run_id = await completed_run(stub, view_server, depth=2)
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)

    await browser_page.mouse.move(640, 360)
    for _ in range(3):
        await browser_page.mouse.wheel(0, -640)  # scroll-up zooms the camera in
        await asyncio.sleep(SETTLE_PAUSE)
    await browser_page.wait_for_function(
        "() => window.__wikigraph.sigma.getCamera().getState().ratio < 0.9",
        timeout=MOVE_TIMEOUT,
    )

    before_state = await camera_state(browser_page)
    await browser_page.mouse.move(20, 700)
    await browser_page.mouse.down()
    for step in range(6):
        await browser_page.mouse.move(20 + step * 160, 660)
        await asyncio.sleep(0.02)
    await browser_page.mouse.up()
    await asyncio.sleep(0.5)
    after_state = await camera_state(browser_page)
    travel = abs(after_state["x"] - before_state["x"]) + abs(
        after_state["y"] - before_state["y"]
    )
    assert travel > 0.005, {"before": before_state, "after": after_state}


async def test_labels_show_dots_first_then_names_on_approach(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    populate(stub)
    run_id = await completed_run(stub, view_server, depth=2)
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)

    assert await browser_page.evaluate(INK_JS) == 0  # structure only, no text yet

    await browser_page.mouse.move(640, 360)
    for _ in range(5):
        await browser_page.mouse.wheel(0, -640)
        await asyncio.sleep(SETTLE_PAUSE)
    await browser_page.wait_for_function(f"{INK_JS} > 0", timeout=MOVE_TIMEOUT)


@pytest.mark.parametrize("browser_page", [[409]], indirect=True)
async def test_failed_run_shows_the_honest_error_at_boot(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    populate(stub)
    stub.fail_links("Ana")
    run_id = await create_run(view_server, seed="Ana", depth=1)
    events = await collect_events(view_server, run_id)
    assert events[-1]["type"] == "failed"
    response = await view_server.get(f"/api/runs/{run_id}/graph")
    assert response.status_code == 409, response.text
    message = response.json()["error"]["message"]

    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "message => document.getElementById('notice').textContent === message",
        arg=message,
        timeout=BOOT_TIMEOUT,
    )
