"""v2 Ticket 08 — physics contract: settle once, drag re-heat, real rest.

The graph animates once into place (FA2 web-worker settle), then comes fully
to rest: nothing moves while untouched, the first real drag movement re-heats
the layout and the burst decays back to stillness by itself, and a selection
click (down + up without movement) never disturbs a settled graph.

Everything is asserted through the headless-browser seam: real Playwright
mouse events drive the drag, and the state string shared with the UI status
chip (``window.__wikigraph.getState()``) is the physics state machine.

Requires the bundle built via ``npm run build`` (``frontend/``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tests.support.browser import boot_view, completed_run
from tests.support.graph import NODE_POS_JS, POSITIONS_JS, page_decay, populate, wait_static
from tests.support.mediawiki import FakeMediaWiki

FROST_PAUSE = 0.4

STATE_JS = "() => window.__wikigraph.getState()"
STATUS_JS = "() => document.getElementById('status').textContent"


async def test_settle_ends_static_and_stays_untouched(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """After the settle the layout is at rest and frozen while untouched."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)

    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)
    before = await browser_page.evaluate(POSITIONS_JS)
    await asyncio.sleep(FROST_PAUSE)
    after = await browser_page.evaluate(POSITIONS_JS)

    assert before == after


async def test_physics_states_surface_in_the_status_chip(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """The status chip shows settling → static, and re-heating while dragged."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)

    await boot_view(browser_page, view_server, run_id)
    assert await browser_page.evaluate(STATUS_JS) == "settling…"
    await wait_static(browser_page)
    assert await browser_page.evaluate(STATUS_JS) == ""

    pos = await browser_page.evaluate(NODE_POS_JS, "Ana")
    await browser_page.mouse.move(pos["x"], pos["y"])
    await browser_page.mouse.down()
    await browser_page.mouse.move(pos["x"] + 60, pos["y"] + 30, steps=6)
    assert await browser_page.evaluate(STATUS_JS) == "re-heating…"

    await browser_page.mouse.up()
    await page_decay(browser_page)
    assert await browser_page.evaluate(STATUS_JS) == ""


async def test_drag_re_heats_and_decays_back_to_static(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """First real drag movement re-heats; the burst decays back to stillness."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)

    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)
    before = await browser_page.evaluate(POSITIONS_JS)

    pos = await browser_page.evaluate(NODE_POS_JS, "Ana")
    await browser_page.mouse.move(pos["x"], pos["y"])
    await browser_page.mouse.down()
    await browser_page.mouse.move(pos["x"] + 60, pos["y"] + 30, steps=6)
    state = await browser_page.evaluate(STATE_JS)
    assert state == "re-heating", state

    await browser_page.mouse.move(pos["x"] + 120, pos["y"] + 60, steps=6)
    await browser_page.mouse.up()

    await page_decay(browser_page)
    after = await browser_page.evaluate(POSITIONS_JS)

    moved = set(before) ^ set(after)
    assert len(moved) > 0

    await asyncio.sleep(FROST_PAUSE)
    assert await browser_page.evaluate(POSITIONS_JS) == after


async def test_selection_click_leaves_the_layout_undisturbed(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A click without movement never re-heates or displaces a settled graph."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)

    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)
    before = await browser_page.evaluate(POSITIONS_JS)

    pos = await browser_page.evaluate(NODE_POS_JS, "Ana")
    await browser_page.mouse.move(pos["x"], pos["y"])
    await browser_page.mouse.down()
    await browser_page.mouse.up()
    await asyncio.sleep(0.2)

    assert await browser_page.evaluate(STATE_JS) == "static"
    assert await browser_page.evaluate(POSITIONS_JS) == before
