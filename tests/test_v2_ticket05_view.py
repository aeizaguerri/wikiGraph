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
from typing import Any

import httpx
import pytest

from tests.conftest import BOOT_TIMEOUT, completed_run, view_url
from tests.helpers import collect_events, create_run
from tests.stub import FakeMediaWiki

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

EMPTY_POINT_JS = """
() => {
  const sigma = window.__wikigraph.sigma;
  const rect = document.getElementById("graph").getBoundingClientRect();
  for (let y = 20; y < rect.height - 20; y += 20) {
    for (let x = 20; x < rect.width - 20; x += 20) {
      if (!sigma.getNodeAtPosition({ x, y })) return { x: rect.left + x, y: rect.top + y };
    }
  }
  return null;
}
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


async def camera_state(page: Any) -> dict[str, float]:
    return await page.evaluate(
        "() => { const s = window.__wikigraph.sigma.getCamera().getState();"
        " return { x: s.x, y: s.y, ratio: s.ratio }; }"
    )


async def test_boot_renders_the_completed_graph(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A real crawl's Graph renders as nodes and edges on the dark canvas."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)
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
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)

    graph_center = await browser_page.locator("#graph").bounding_box()
    assert graph_center is not None
    await browser_page.mouse.move(
        graph_center["x"] + graph_center["width"] / 2,
        graph_center["y"] + graph_center["height"] / 2,
    )
    await browser_page.locator("#graph").hover()
    for _ in range(3):
        await browser_page.mouse.wheel(0, -640)  # scroll-up zooms the camera in
        await asyncio.sleep(SETTLE_PAUSE)
    await browser_page.wait_for_function(
        "() => window.__wikigraph.sigma.getCamera().getState().ratio < 0.9",
        timeout=MOVE_TIMEOUT,
    )

    before_state = await camera_state(browser_page)
    # Pan from a node-free screen point: grabbing a node is a node drag, not
    # a camera pan (ticket 08's physics contract) — Sigma suppresses it.
    empty_point = await browser_page.evaluate(EMPTY_POINT_JS)
    await browser_page.mouse.move(empty_point["x"], empty_point["y"])
    await browser_page.mouse.down()
    for step in range(6):
        await browser_page.mouse.move(empty_point["x"] + step * 160, empty_point["y"] - 40)
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
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)

    assert await browser_page.evaluate(INK_JS) == 0  # structure only, no text yet

    graph_center = await browser_page.locator("#graph").bounding_box()
    assert graph_center is not None
    await browser_page.mouse.move(
        graph_center["x"] + graph_center["width"] / 2,
        graph_center["y"] + graph_center["height"] / 2,
    )
    await browser_page.locator("#graph").hover()
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
