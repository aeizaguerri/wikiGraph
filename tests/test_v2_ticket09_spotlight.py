"""v2 Ticket 09 — spotlight: hover preview, click pin, info card.

Hovering an article lights its neighbors and incident edges and dims the
rest (Sigma reducer recipe driven by a focus state); clicking pins the
selection and opens the info card (title, level, degree, community, plus
the "open on Wikipedia" link for the run's edition); a backdrop-level
gesture deselects and unpins. The full hover → click → info card →
deselect sequence is driven by real Playwright mouse events with zero
page errors.

Requires the bundle built via ``npm run build`` (``frontend/``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tests.conftest import boot_view, completed_run
from tests.stub import FakeMediaWiki

STATIC_WAIT = 14_000

STATE_SPOTLIGHT_JS = "() => window.__wikigraph.getSpotlight()"
STATE_SELECTED_JS = "() => window.__wikigraph.getSelected()"
CARD_TITLE_JS = "() => document.getElementById('card-title').textContent"
CARD_FACTS_JS = """
() => ({
  level: document.getElementById('card-level').textContent,
  degree: document.getElementById('card-degree').textContent,
  community: document.getElementById('card-community').textContent,
})
"""
CARD_LINK_JS = "() => document.getElementById('card-link').href"
CARD_HIDDEN_JS = "() => document.getElementById('info-card').hidden"

NODE_POS_JS = """
(node) => {
  const experience = window.__wikigraph;
  const attrs = experience.graph.getNodeAttributes(node);
  return experience.sigma.graphToViewport({ x: attrs.x, y: attrs.y });
}
"""

# A screen point over no node. Points under a visible info card are skipped,
# so the synthetic click lands on the graph backdrop, not on the DOM card.
EMPTY_POINT_JS = """
() => {
  const sigma = window.__wikigraph.sigma;
  const card = document.getElementById('info-card');
  const blocked = (!card.hidden)
    ? card.getBoundingClientRect()
    : null;
  const intersects = ({ x, y }) => blocked
    && y > blocked.top - 10 && y < blocked.bottom + 10
    && x > blocked.left - 10 && x < blocked.right + 10;
  for (let y = 40; y < 900; y += 20) {
    for (let x = 20; x < 1200; x += 20) {
      if (intersects({ x, y })) continue;
      if (!sigma.getNodeAtPosition({ x, y })) return { x, y };
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


async def wait_static(page: Any) -> None:
    await page.wait_for_function(
        "() => window.__wikigraph.getState() === 'static'", timeout=STATIC_WAIT
    )


async def read_card(page: Any) -> dict[str, Any]:
    return {
        "hidden": await page.evaluate(CARD_HIDDEN_JS),
        "title": await page.evaluate(CARD_TITLE_JS),
        "facts": await page.evaluate(CARD_FACTS_JS),
        "link": await page.evaluate(CARD_LINK_JS),
    }


async def test_hover_previews_and_releases(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Hovering lights the node and its neighbors; leaving clears the preview."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)
    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)

    assert await browser_page.evaluate(STATE_SPOTLIGHT_JS) is None
    assert await browser_page.evaluate(CARD_HIDDEN_JS) is True

    pos = await browser_page.evaluate(NODE_POS_JS, "Ana")
    await browser_page.mouse.move(pos["x"], pos["y"])
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getSpotlight() === 'Ana'"
    )
    halo = await browser_page.evaluate(
        "() => window.__wikigraph.sigma.getGraph().neighbors('Ana')"
    )
    assert set(halo) == {"Biología", "Química", "Astronomía", "Historia"}

    empty = await browser_page.evaluate(EMPTY_POINT_JS)
    await browser_page.mouse.move(empty["x"], empty["y"])
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getSpotlight() === null"
    )
    assert await browser_page.evaluate(STATE_SELECTED_JS) is None
    assert await browser_page.evaluate(CARD_HIDDEN_JS) is True


async def test_click_pins_spotlight_and_opens_info_card(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Clicking pins the spotlight (it survives leaving the node) and opens the card."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)
    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)

    pos = await browser_page.evaluate(NODE_POS_JS, "Ana")
    await browser_page.mouse.move(pos["x"], pos["y"])
    await browser_page.mouse.down()
    await browser_page.mouse.up()
    assert await browser_page.evaluate(STATE_SELECTED_JS) == "Ana"

    empty = await browser_page.evaluate(EMPTY_POINT_JS)
    await browser_page.mouse.move(empty["x"], empty["y"])
    await browser_page.wait_for_function(
        "() => document.getElementById('info-card') && !document.getElementById('info-card').hidden"
    )
    # The pinned spotlight holds even though the pointer moved away.
    assert await browser_page.evaluate(STATE_SPOTLIGHT_JS) == "Ana"

    card = await read_card(browser_page)
    assert card["title"] == "Ana"
    assert card["facts"]["level"] == "Level 0"
    assert card["facts"]["degree"] == "5 links"
    assert card["facts"]["community"].startswith("Community ")
    assert card["link"] == "https://es.wikipedia.org/wiki/Ana"

    # Keep the two clicks more than 300 ms apart: within Sigma's double-click
    # window the second click is consumed as a zoom gesture and does not
    # deselect anything.
    await asyncio.sleep(0.35)
    await browser_page.mouse.click(empty["x"], empty["y"])
    await browser_page.wait_for_function(
        "() => document.getElementById('info-card').hidden"
    )
    assert await browser_page.evaluate(STATE_SELECTED_JS) is None
    assert await browser_page.evaluate(STATE_SPOTLIGHT_JS) is None
