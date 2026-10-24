"""Real Chromium touch input at the existing graph-view browser seam."""

from typing import Any

import httpx
import pytest

from tests.support.browser import boot_view, completed_run
from tests.support.mediawiki import FakeMediaWiki
from tests.support.graph import NODE_POS_JS, page_decay, populate, wait_static

CAMERA_JS = "() => window.__wikigraph.sigma.getCamera().getState()"


async def touch(driver: Any, kind: str, *points: dict[str, float]) -> None:
    await driver.send("Input.dispatchTouchEvent", {
        "type": kind,
        "touchPoints": [{"id": i, **point} for i, point in enumerate(points)],
    })


@pytest.fixture
async def mobile_graph(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> Any:
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)
    await browser_page.set_viewport_size({"width": 430, "height": 900})
    driver = await browser_page.context.new_cdp_session(browser_page)
    await driver.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)
    return browser_page, driver


@pytest.mark.parametrize("distance", [6, 60])
@pytest.mark.parametrize("finish", ["touchEnd", "touchCancel"])
async def test_touch_moves_node_not_camera_and_releases(
    mobile_graph: Any, distance: int, finish: str
) -> None:
    browser_page, driver = mobile_graph
    start = await browser_page.evaluate(NODE_POS_JS, "Ana")
    camera = await browser_page.evaluate(CAMERA_JS)

    await touch(driver, "touchStart", start)
    for step in range(1, 7):
        end = {"x": start["x"] + distance * step / 6, "y": start["y"]}
        await touch(driver, "touchMove", end)

    actual = await browser_page.evaluate(NODE_POS_JS, "Ana")
    assert abs(actual["x"] - end["x"]) < 3
    assert abs(actual["y"] - end["y"]) < 3
    assert await browser_page.evaluate(CAMERA_JS) == camera
    assert await browser_page.evaluate("() => window.__wikigraph.getState()") == "re-heating"
    assert await browser_page.evaluate("() => window.__wikigraph.getSelected()") is None

    await touch(driver, finish)
    assert await browser_page.evaluate("() => window.__wikigraph.getSelected()") is None
    await page_decay(browser_page)
    assert await browser_page.evaluate(CAMERA_JS) == camera
    assert await browser_page.evaluate(
        "() => window.__wikigraph.graph.getNodeAttribute('Ana', 'fixed')"
    ) is False


async def test_touch_tap_and_jitter_keep_selection_and_static_layout(mobile_graph: Any) -> None:
    page, driver = mobile_graph
    before = await page.evaluate(
        "() => window.__wikigraph.graph.mapNodes((n, a) => [n, a.x, a.y])"
    )
    camera = await page.evaluate(CAMERA_JS)
    start = await page.evaluate(NODE_POS_JS, "Ana")
    await touch(driver, "touchStart", start)
    await touch(driver, "touchMove", {"x": start["x"] + 2, "y": start["y"]})
    await touch(driver, "touchEnd")
    assert await page.evaluate("() => window.__wikigraph.getSelected()") == "Ana"
    assert await page.evaluate("() => window.__wikigraph.getState()") == "static"
    assert await page.evaluate(CAMERA_JS) == camera
    assert await page.evaluate(
        "() => window.__wikigraph.graph.mapNodes((n, a) => [n, a.x, a.y])"
    ) == before


async def test_first_tap_after_pinch_selects_node(mobile_graph: Any) -> None:
    page, driver = mobile_graph
    start = await page.evaluate(NODE_POS_JS, "Ana")
    camera = await page.evaluate(CAMERA_JS)
    await touch(driver, "touchStart", start)
    await touch(driver, "touchStart", start, {"x": start["x"] - 80, "y": start["y"]})
    await touch(driver, "touchEnd", start)
    await touch(driver, "touchEnd")
    start = await page.evaluate(NODE_POS_JS, "Ana")
    await touch(driver, "touchStart", start)
    await touch(driver, "touchEnd")
    assert await page.evaluate("() => window.__wikigraph.getSelected()") == "Ana"
    assert await page.evaluate(CAMERA_JS) == camera
    assert await page.evaluate("() => window.__wikigraph.getState()") == "static"


def background_js() -> str:
    return """() => {
      const s = window.__wikigraph.sigma;
      const rect = document.getElementById('graph').getBoundingClientRect();
      for (let y = 100; y < rect.height - 100; y += 40)
        for (let x = 60; x < rect.width - 100; x += 40)
          if (!s.getNodeAtPosition({x, y}))
            return {x: rect.left + x, y: rect.top + y};
      throw new Error('No background point found');
    }"""


async def test_touch_background_pan_keeps_nodes_static(mobile_graph: Any) -> None:
    page, driver = mobile_graph
    start = await page.evaluate(background_js())
    camera = await page.evaluate(CAMERA_JS)
    before = await page.evaluate(
        "() => window.__wikigraph.graph.mapNodes((n, a) => [n, a.x, a.y])"
    )
    await touch(driver, "touchStart", start)
    for step in range(1, 7):
        await touch(driver, "touchMove", {"x": start["x"] + step * 10, "y": start["y"]})
    assert await page.evaluate(CAMERA_JS) != camera
    await touch(driver, "touchEnd")
    assert await page.evaluate("() => window.__wikigraph.getState()") == "static"
    assert await page.evaluate(
        "() => window.__wikigraph.graph.mapNodes((n, a) => [n, a.x, a.y])"
    ) == before


async def test_touch_pinch_releases_node_and_preserves_zoom(mobile_graph: Any) -> None:
    page, driver = mobile_graph
    start = await page.evaluate(NODE_POS_JS, "Ana")
    ratio = (await page.evaluate(CAMERA_JS))["ratio"]
    before = await page.evaluate(
        "() => window.__wikigraph.graph.mapNodes((n, a) => [n, a.x, a.y])"
    )
    other = {"x": start["x"] - 80, "y": start["y"]}
    await touch(driver, "touchStart", start)
    await touch(driver, "touchStart", start, other)
    assert await page.evaluate(
        "() => window.__wikigraph.graph.getNodeAttribute('Ana', 'fixed')"
    ) is False
    for step in range(1, 7):
        await touch(driver, "touchMove", start, {"x": other["x"] - step * 5, "y": other["y"]})
    assert (await page.evaluate(CAMERA_JS))["ratio"] < ratio
    await touch(driver, "touchEnd", start)
    await touch(driver, "touchMove", {"x": start["x"] + 20, "y": start["y"]})
    await touch(driver, "touchEnd")
    assert await page.evaluate("() => window.__wikigraph.getState()") == "static"
    assert await page.evaluate("() => window.__wikigraph.getSelected()") is None
    assert await page.evaluate(
        "() => window.__wikigraph.graph.mapNodes((n, a) => [n, a.x, a.y])"
    ) == before
