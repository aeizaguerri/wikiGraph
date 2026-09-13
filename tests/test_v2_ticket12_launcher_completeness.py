"""v2 Ticket 12 — launcher modes and keyboard completeness."""

from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import boot_view, completed_run
from tests.stub import FakeMediaWiki


def populate(stub: FakeMediaWiki) -> None:
    stub.add_page("Ana", [(0, "Biología")])
    stub.add_page("Biología", [(0, "Ana")])
    stub.add_page("Selva", [(0, "Rosario"), (0, "Mendoza")])
    stub.add_page("Rosario")
    stub.add_page("Mendoza")


async def test_f_cycles_overlay_and_inline_without_stealing_input(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """F changes the open layout, while a focused field receives normal typing."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, run_id)
    await browser_page.click("#launch-chip")

    await browser_page.keyboard.type("F")
    assert await browser_page.input_value("#launch-seed") == "F"
    assert await browser_page.get_attribute("#launch-overlay", "data-mode") == "overlay"

    await browser_page.keyboard.press("Tab")
    await browser_page.keyboard.press("f")
    assert await browser_page.get_attribute("#launch-overlay", "data-mode") == "inline"
    assert await browser_page.is_visible("#launch-form")
    for selector in (
        "#launch-seed",
        "#launch-depth",
        "#launch-edition",
        "#launch-cap",
        "#launch-submit",
    ):
        assert await browser_page.is_visible(selector), selector

    await browser_page.keyboard.press("F")
    assert await browser_page.get_attribute("#launch-overlay", "data-mode") == "overlay"


async def test_modified_f_shortcuts_are_not_intercepted(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Browser and platform shortcuts keep their default modified-F behavior."""
    populate(stub)
    run_id = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, run_id)
    await browser_page.evaluate(
        """
        () => {
          window.__modifiedFEvents = [];
          window.addEventListener("keydown", (event) => {
            if (event.key.toLowerCase() === "f") {
              window.__modifiedFEvents.push({
                ctrl: event.ctrlKey,
                meta: event.metaKey,
                alt: event.altKey,
                defaultPrevented: event.defaultPrevented,
              });
            }
          });
        }
        """
    )

    for shortcut in ("Control+f", "Meta+f", "Alt+f"):
        await browser_page.keyboard.press(shortcut)

    assert await browser_page.get_attribute("#launch-overlay", "data-mode") == "overlay"
    assert await browser_page.is_hidden("#launch-overlay")
    assert await browser_page.evaluate("() => window.__modifiedFEvents") == [
        {"ctrl": True, "meta": False, "alt": False, "defaultPrevented": False},
        {"ctrl": False, "meta": True, "alt": False, "defaultPrevented": False},
        {"ctrl": False, "meta": False, "alt": True, "defaultPrevented": False},
    ]


async def test_live_run_survives_mode_cycle_and_chip_reopens_current_mode(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A running crawl keeps its progress through inline mode and completion."""
    populate(stub)
    first_run = await completed_run(stub, view_server, seed="Ana", depth=1)
    await boot_view(browser_page, view_server, first_run)
    rosario_gate = stub.gate("Rosario")
    mendoza_gate = stub.gate("Mendoza")

    await browser_page.click("#launch-chip")
    await browser_page.fill("#launch-seed", "Selva")
    await browser_page.click("#launch-depth .chip:has-text('2')")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function(
        "() => /Crawled 1/.test(document.getElementById('launch-progress').textContent)",
        timeout=10_000,
    )
    progress = await browser_page.text_content("#launch-progress")

    await browser_page.keyboard.press("F")
    assert await browser_page.get_attribute("#launch-overlay", "data-mode") == "inline"
    assert await browser_page.text_content("#launch-progress") == progress
    assert await browser_page.is_disabled("#launch-submit")

    rosario_gate.set()
    mendoza_gate.set()
    await browser_page.wait_for_function(
        "() => document.getElementById('launch-overlay').hidden"
    )
    await browser_page.wait_for_function(
        "() => window.__wikigraph.graph.hasNode('Mendoza')", timeout=10_000
    )

    await browser_page.click("#launch-chip")
    assert await browser_page.get_attribute("#launch-overlay", "data-mode") == "inline"
    assert await browser_page.is_visible("#launch-form")
