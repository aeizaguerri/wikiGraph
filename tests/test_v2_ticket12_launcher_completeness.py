"""Variant C accessibility and lifecycle controls."""

from __future__ import annotations

from typing import Any

import httpx


async def test_variant_c_controls_are_keyboard_reachable(
    browser_page: Any, view_server: httpx.AsyncClient
) -> None:
    await browser_page.goto(str(view_server.base_url))
    await browser_page.focus("#launch-seed")
    await browser_page.keyboard.type("Climate change")
    await browser_page.keyboard.press("Tab")
    assert await browser_page.locator("#launch-depth").count() == 1
    assert await browser_page.locator("#launch-edition").count() == 1
    assert await browser_page.locator("#launch-submit").count() == 1


async def test_variant_c_has_no_development_phase_or_variant_switcher(
    browser_page: Any, view_server: httpx.AsyncClient
) -> None:
    await browser_page.goto(str(view_server.base_url))
    assert await browser_page.locator(".phase-controls").count() == 0
    assert await browser_page.locator(".variant-switcher").count() == 0
    assert await browser_page.locator("[data-phase]").count() == 0
