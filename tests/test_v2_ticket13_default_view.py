"""Ticket 13: the assembled v2 experience replaces the v1 served view."""

from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import BOOT_TIMEOUT, view_url
from tests.helpers import collect_events, create_run
from tests.stub import FakeMediaWiki
from wikigraph.app import STATIC_DIR


async def test_root_serves_the_v2_experience_and_retires_v1(client) -> None:
    assert not (STATIC_DIR / "prototype-v2").exists()
    assert not (STATIC_DIR / "v2").exists()

    page = await client.get("/")

    assert page.status_code == 200
    assert 'id="landing-view"' in page.text
    assert 'class="c-search"' in page.text
    assert 'id="run-view"' in page.text
    assert 'id="lens-community"' in page.text
    assert "cytoscape" not in page.text.lower()

    for retired_asset in ("/app.js", "/style.css", "/v2/"):
        response = await client.get(retired_asset)
        assert response.status_code == 404, retired_asset


async def test_capped_graph_keeps_truncation_visible(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    stub.add_page("Seed", [(0, "A"), (0, "B"), (0, "C")])
    for title in ("A", "B", "C"):
        stub.add_page(title)
    run_id = await create_run(view_server, seed="Seed", node_cap=2)
    events = await collect_events(view_server, run_id)
    assert events[-1] == {"type": "completed", "data": {"truncated": True}}

    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)

    truncation = browser_page.locator("#truncation")
    await truncation.wait_for(state="visible")
    assert "node cap" in (await truncation.text_content()).lower()


async def test_capped_graph_chrome_does_not_collide_and_legend_is_expanded_only(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    stub.add_page("Seed", [(0, "A"), (0, "B"), (0, "C")])
    for title in ("A", "B", "C"):
        stub.add_page(title)
    run_id = await create_run(view_server, seed="Seed", node_cap=2)

    for width, height in ((320, 740), (393, 852), (1280, 800)):
        await browser_page.set_viewport_size({"width": width, "height": height})
        await browser_page.goto(view_url(view_server, run_id))
        await browser_page.wait_for_function(
            "() => window.__wikigraph && !document.getElementById('truncation').hidden",
            timeout=BOOT_TIMEOUT,
        )
        await browser_page.wait_for_function(
            "() => /settling|re-heating/.test(document.getElementById('status').textContent)",
            timeout=BOOT_TIMEOUT,
        )

        for lens_id in ("lens-community", "lens-level"):
            if lens_id == "lens-level":
                await browser_page.click("#lens-level")
            else:
                await browser_page.click("#lens-community")

            collapsed = await browser_page.evaluate("""() => {
              const rect = selector => {
                const {left, top, right, bottom} = document.querySelector(selector).getBoundingClientRect();
                return {left, top, right, bottom};
              };
              return {
                lens: rect('#lens'), warning: rect('#truncation'),
                header: rect('#run-view .c-header'), status: rect('#status'),
                viewport: {width: innerWidth, height: innerHeight},
              };
            }""")
            assert collapsed["lens"]["right"] <= collapsed["viewport"]["width"]
            assert collapsed["warning"]["left"] >= 0
            assert collapsed["warning"]["right"] <= collapsed["viewport"]["width"]
            for overlay in ("lens", "warning"):
                for target in ("header", "status"):
                    a, b = collapsed[overlay], collapsed[target]
                    assert not (
                        a["left"] < b["right"] and a["right"] > b["left"]
                        and a["top"] < b["bottom"] and a["bottom"] > b["top"]
                    ), f"{overlay} overlaps {target} at {width}px: {collapsed}"
            a, b = collapsed["lens"], collapsed["warning"]
            assert not (
                a["left"] < b["right"] and a["right"] > b["left"]
                and a["top"] < b["bottom"] and a["bottom"] > b["top"]
            ), f"lens overlaps partial-graph warning at {width}px: {collapsed}"
            assert not await browser_page.locator("#legend").is_visible(), (
                width,
                "legend must be hidden while the graph is collapsed",
            )

            await browser_page.click("#graph-expand")
            assert await browser_page.locator("#legend").is_visible()
            expanded = await browser_page.evaluate("""() => {
              const rect = selector => {
                const {left, top, right, bottom} = document.querySelector(selector).getBoundingClientRect();
                return {left, top, right, bottom};
              };
              return {lens: rect('#lens'), warning: rect('#truncation'),
                legend: rect('#legend'), status: rect('#status'), close: rect('#graph-collapse'),
                viewport: {width: innerWidth, height: innerHeight}};
            }""")
            for name in ("lens", "warning", "legend"):
                box = expanded[name]
                assert 0 <= box["left"] <= box["right"] <= expanded["viewport"]["width"]
                assert 0 <= box["top"] <= box["bottom"] <= expanded["viewport"]["height"]
            for index, first in enumerate(("lens", "warning", "legend")):
                for second in ("lens", "warning", "legend")[index + 1 :]:
                    a, b = expanded[first], expanded[second]
                    assert not (
                        a["left"] < b["right"] and a["right"] > b["left"]
                        and a["top"] < b["bottom"] and a["bottom"] > b["top"]
                    ), f"expanded {first} overlaps {second} at {width}px: {expanded}"
            for target in ("lens", "warning", "legend", "close"):
                a, b = expanded["status"], expanded[target]
                assert not (
                    a["left"] < b["right"] and a["right"] > b["left"]
                    and a["top"] < b["bottom"] and a["bottom"] > b["top"]
                ), f"expanded status overlaps {target} at {width}px: {expanded}"
            await browser_page.get_by_role("button", name="Close expanded graph").click()
            assert not await browser_page.locator("#legend").is_visible()
            await browser_page.click("#graph-expand")
            await browser_page.keyboard.press("Escape")
            assert not await browser_page.locator("#legend").is_visible()
            assert await browser_page.get_attribute(f"#{lens_id}", "aria-pressed") == "true"
