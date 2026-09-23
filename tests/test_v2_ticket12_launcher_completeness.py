"""Variant C accessibility and lifecycle controls."""

from __future__ import annotations

from typing import Any

import httpx


async def test_variant_c_controls_are_keyboard_reachable(
    stub, browser_page: Any, view_server: httpx.AsyncClient
) -> None:
    stub.add_page("Keyboard")
    await browser_page.goto(str(view_server.base_url))
    await browser_page.focus("#launch-seed")
    await browser_page.keyboard.press("Tab")
    assert await browser_page.evaluate("() => document.activeElement.dataset.depth") == "1"
    await browser_page.keyboard.press("Enter")
    assert await browser_page.get_attribute("#launch-depth [data-depth='1']", "aria-pressed") == "true"
    await browser_page.click("[data-language='en']")
    assert await browser_page.get_attribute("[data-language='en']", "aria-pressed") == "true"
    assert await browser_page.get_attribute("[data-language='es']", "aria-pressed") == "false"
    await browser_page.focus("#launch-seed")
    await browser_page.fill("#launch-seed", "Keyboard")
    await browser_page.keyboard.press("Enter")
    await browser_page.wait_for_function("() => new URL(location.href).searchParams.has('run')")
    await browser_page.wait_for_function("() => window.__wikigraph && window.__wikigraph.graph.hasNode('Keyboard')")


async def test_variant_c_has_no_development_phase_or_variant_switcher(
    browser_page: Any, view_server: httpx.AsyncClient
) -> None:
    await browser_page.goto(str(view_server.base_url))
    assert await browser_page.locator(".phase-controls").count() == 0
    assert await browser_page.locator(".variant-switcher").count() == 0
    assert await browser_page.locator("[data-phase]").count() == 0
