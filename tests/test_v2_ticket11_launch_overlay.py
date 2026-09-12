"""v2 Ticket 11 — launch form: boxless overlay wired to the real crawl.

A "+ grafo" chip opens a full-bleed overlay over the blurred live graph:
uppercase micro-title, hero underline seed field (autofocused, title or
pasted URL), depth and edition chips, node cap as an underlined text
field (numeric, focus selects its content, clamped 1-5000), full-width
accent launch button. No ✕: clicking the backdrop closes it. Submitting
drives the real v1 contract end-to-end: POST create run → SSE progress
visible while the overlay is open → MediaWiki/API errors inline in the
form → completion auto-closes the overlay → the new Graph swaps in
place with a fresh settle — all with zero page errors.

Requires the bundle built via ``npm run build`` (``frontend/``).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.conftest import boot_view, completed_run
from tests.helpers import fetch_graph, wait_static
from tests.stub import FakeMediaWiki

OVERLAY_OPEN_JS = "() => !document.getElementById('launch-overlay').hidden"
OVERLAY_BLUR_JS = """
() => getComputedStyle(document.getElementById('launch-overlay')).backdropFilter
"""
SEED_FOCUSED_JS = (
    "() => document.activeElement === document.getElementById('launch-seed')"
)
CAP_VALUE_JS = "() => document.getElementById('launch-cap').value"
CLOSE_BUTTON_JS = """
() => [...document.getElementById('launch-overlay').querySelectorAll('button')]
  .filter((button) => button.textContent.includes('✕') || button.dataset.close)
  .length === 0
"""

# A point guaranteed to sit on the overlay backdrop, not on the form panel
# (the panel is centered, so mid-right of the viewport is always backdrop).
BACKDROP_POINT_JS = """
() => {
  const x = window.innerWidth - 24;
  const y = window.innerHeight / 2;
  const hit = document.elementFromPoint(x, y);
  return (hit && hit.id === 'launch-overlay') ? { x, y } : null;
}
"""


def populate(stub: FakeMediaWiki) -> None:
    stub.add_page("Ana", [(0, "Biología"), (0, "Química")])
    stub.add_page("Biología", [(0, "Ana")])
    stub.add_page("Química", [(0, "Ana")])
    stub.add_page("Selva", [(0, "Rosario"), (0, "Mendoza")])
    stub.add_page("Rosario")
    stub.add_page("Mendoza")


async def open_overlay(page: Any) -> None:
    await page.click("#launch-chip")
    await page.wait_for_function(OVERLAY_OPEN_JS)


async def wait_overlay_closed(page: Any) -> None:
    await page.wait_for_function(
        "() => document.getElementById('launch-overlay').hidden"
    )


async def test_overlay_opens_over_blurred_graph_and_backdrop_closes(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """The chip opens a full-bleed overlay with a blurred backdrop; no ✕ exists."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)

    assert await browser_page.evaluate(OVERLAY_OPEN_JS) is False
    await open_overlay(browser_page)

    # Autofocus lands on the hero seed field; the boxless anatomy is present.
    assert await browser_page.evaluate(SEED_FOCUSED_JS) is True
    assert await browser_page.evaluate(OVERLAY_BLUR_JS) != "none"
    assert await browser_page.evaluate(
        "() => !!document.getElementById('launch-form')"
    )
    assert await browser_page.evaluate(
        "() => !!document.getElementById('launch-submit')"
    )
    assert await browser_page.evaluate(CLOSE_BUTTON_JS) is True

    # Clicking the backdrop (not the form) closes it; there is no close button.
    point = await browser_page.evaluate(BACKDROP_POINT_JS)
    assert point is not None, "no open backdrop point found"
    await browser_page.mouse.click(point["x"], point["y"])
    await wait_overlay_closed(browser_page)
    assert await browser_page.evaluate(OVERLAY_OPEN_JS) is False


async def test_cap_field_selects_on_focus_and_clamps_to_range(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Focusing selects the whole value (typing replaces it); blur clamps 1-5000."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, run_id)
    await open_overlay(browser_page)

    # No browser spinners: the cap is a text field.
    mode = await browser_page.evaluate(
        "() => document.getElementById('launch-cap').getAttribute('inputmode')"
    )
    assert mode == "numeric"
    assert await browser_page.evaluate(CAP_VALUE_JS) == "500"

    # Focus selects the content, so typing replaces rather than appends.
    await browser_page.focus("#launch-cap")
    await browser_page.wait_for_function(
        "() => document.activeElement === document.getElementById('launch-cap')"
    )
    await browser_page.keyboard.type("9999")
    assert await browser_page.evaluate(CAP_VALUE_JS) == "9999"

    await browser_page.keyboard.press("Tab")
    await browser_page.wait_for_function(
        "() => document.getElementById('launch-cap').value === '5000'"
    )

    await browser_page.focus("#launch-cap")
    await browser_page.wait_for_function(
        "() => document.activeElement === document.getElementById('launch-cap')"
    )
    await browser_page.keyboard.type("0")
    await browser_page.keyboard.press("Tab")
    await browser_page.wait_for_function(
        "() => document.getElementById('launch-cap').value === '1'"
    )


async def test_launch_runs_real_crawl_and_auto_swaps_graph(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A real run: progress legible while open, auto-close, in-place swap, re-settle."""
    populate(stub)
    first_run = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, first_run)
    await wait_static(browser_page)
    old_nodes = set(
        await browser_page.evaluate("() => window.__wikigraph.graph.nodes()")
    )

    # The second run pauses mid-crawl so progress is readable while open:
    # both level-2 fetches are gated, so "Crawled 1" persists on screen.
    rosario_gate = stub.gate("Rosario")
    mendoza_gate = stub.gate("Mendoza")

    await open_overlay(browser_page)
    # A pasted Wikipedia URL is accepted: server-side normalization as in v1.
    await browser_page.fill(
        "#launch-seed", "https://es.wikipedia.org/wiki/Selva"
    )
    await browser_page.click("#launch-depth .chip:has-text('2')")
    await browser_page.click("#launch-submit")

    await browser_page.wait_for_function(
        "() => /Crawled/.test(document.getElementById('launch-progress').textContent)",
        timeout=10_000,
    )
    # Honest counters while the gates hold the run open: crawled=1.
    await browser_page.wait_for_function(
        "() => /Crawled 1/.test(document.getElementById('launch-progress').textContent)",
        timeout=10_000,
    )
    assert await browser_page.evaluate(OVERLAY_OPEN_JS) is True

    # Run completion auto-closes the overlay and swaps the graph in place.
    rosario_gate.set()
    mendoza_gate.set()
    await wait_overlay_closed(browser_page)
    await browser_page.wait_for_function(
        "() => window.__wikigraph.graph.hasNode('Mendoza')", timeout=10_000
    )
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getState() === 'settling'"
    )
    await wait_static(browser_page)

    new_run = await browser_page.evaluate(
        "() => new URLSearchParams(window.location.search).get('run')"
    )
    assert new_run and new_run != first_run
    graph = await fetch_graph(view_server, new_run)
    new_titles = {node["title"] for node in graph["nodes"]}
    assert new_titles == {"Selva", "Rosario", "Mendoza"}
    assert new_titles != old_nodes
    # The URL now carries the new run so the view stays shareable.
    assert f"?run={new_run}" in browser_page.url


@pytest.mark.parametrize("browser_page", [[404]], indirect=True)
async def test_form_errors_surface_inline(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A missing seed article surfaces the API error inline; the form stays usable."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, run_id)
    await open_overlay(browser_page)

    await browser_page.fill("#launch-seed", "Inexistente")
    stub.add_missing("Inexistente")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function(
        "() => document.getElementById('launch-error').textContent.length > 0",
        timeout=10_000,
    )
    assert await browser_page.evaluate(OVERLAY_OPEN_JS) is True
    assert await browser_page.evaluate(SEED_FOCUSED_JS) is False

    # The form recovers: a valid seed launches fine after the error.
    await browser_page.fill("#launch-seed", "Historia")
    stub.add_page("Historia")
    await browser_page.click("#launch-submit")
    await wait_overlay_closed(browser_page)
    await browser_page.wait_for_function(
        "() => window.__wikigraph.graph.hasNode('Historia')", timeout=10_000
    )


async def test_mid_crawl_failure_surfaces_in_the_form(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A run that dies mid-crawl ends as an inline error, overlay open."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, run_id)
    await open_overlay(browser_page)

    stub.fail_links("Historia", 500)
    await browser_page.fill("#launch-seed", "Historia")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function(
        "() => document.getElementById('launch-error').textContent.length > 0",
        timeout=10_000,
    )
    assert await browser_page.evaluate(OVERLAY_OPEN_JS) is True
    # Editable again: the failed run released the form.
    disabled = await browser_page.evaluate(
        "() => document.getElementById('launch-seed').disabled"
    )
    assert disabled is False
