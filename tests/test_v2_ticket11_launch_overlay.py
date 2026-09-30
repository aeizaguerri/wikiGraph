"""Browser regression coverage for the selected Variant C launch surface."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.conftest import BOOT_TIMEOUT, view_url
from tests.helpers import collect_events, create_run
from tests.stub import FakeMediaWiki


async def test_variant_c_landing_is_search_first_with_supported_defaults(
    browser_page: Any, view_server: httpx.AsyncClient
) -> None:
    await browser_page.goto(str(view_server.base_url))
    await browser_page.wait_for_function("() => !document.getElementById('landing-view').hidden")

    assert await browser_page.is_visible("#launch-form")
    assert await browser_page.is_visible("#launch-seed")
    assert await browser_page.get_attribute("#launch-depth .chip[data-depth='2']", "aria-pressed") == "true"
    assert await browser_page.get_attribute("#launch-edition [data-language='es']", "aria-pressed") == "true"
    assert await browser_page.is_visible("#launch-submit")
    assert await browser_page.locator(".variant-switcher").count() == 0
    assert await browser_page.locator("#launch-chip").count() == 0


async def test_variant_c_keeps_the_real_node_cap_launch_default(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    stub.add_page("Cap default")
    requests = []
    browser_page.on("request", lambda request: requests.append(request))
    await browser_page.goto(str(view_server.base_url))
    assert await browser_page.get_attribute("#launch-cap", "type") == "hidden"
    assert await browser_page.input_value("#launch-cap") == "500"
    await browser_page.fill("#launch-seed", "Cap default")
    await browser_page.keyboard.press("Enter")
    await browser_page.wait_for_function("() => new URL(location.href).searchParams.has('run')", timeout=BOOT_TIMEOUT)
    launch = next(request for request in requests if request.method == "POST" and request.url.endswith("/api/runs"))
    assert launch.post_data_json["nodeCap"] == 500


@pytest.mark.parametrize("browser_page", [(404,)], indirect=True)
async def test_variant_c_invalid_seed_error_recovers_inline(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    stub.add_missing("Inexistente")
    stub.add_page("Historia")
    await browser_page.goto(str(view_server.base_url))
    await browser_page.fill("#launch-seed", "Inexistente")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function("() => document.getElementById('launch-error').textContent.length > 0", timeout=BOOT_TIMEOUT)
    assert await browser_page.is_visible("#landing-view")
    await browser_page.fill("#launch-seed", "Historia")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function("() => new URL(location.href).searchParams.has('run')", timeout=BOOT_TIMEOUT)
    await browser_page.wait_for_function("() => window.__wikigraph && window.__wikigraph.graph.hasNode('Historia')", timeout=BOOT_TIMEOUT)


async def test_variant_c_mid_crawl_failure_is_visible_without_graph_or_retry(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    stub.add_page("Historia")
    stub.fail_links("Historia", 500)
    await browser_page.goto(str(view_server.base_url))
    await browser_page.fill("#launch-seed", "Historia")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function("() => document.getElementById('run-state').textContent.includes('failed')", timeout=BOOT_TIMEOUT)
    assert await browser_page.is_visible("#run-waiting")
    assert not await browser_page.is_visible("#run-retry")
    assert await browser_page.text_content("#run-detail") == "This run failed and did not produce a completed Graph."


async def test_variant_c_real_run_uses_live_graph_preview_and_same_run_url(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    gate = stub.gate("Selva")
    stub.add_page("Selva", [(0, "Rosario")])
    stub.add_page("Rosario")

    await browser_page.goto(str(view_server.base_url))
    await browser_page.fill("#launch-seed", "Selva")
    await browser_page.click("#launch-depth .chip:has-text('1')")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function("() => new URL(location.href).searchParams.has('run')", timeout=BOOT_TIMEOUT)
    run_id = await browser_page.evaluate("() => new URL(location.href).searchParams.get('run')")
    await browser_page.wait_for_function("() => window.__wikigraph && window.__wikigraph.graph.hasNode('Selva')", timeout=BOOT_TIMEOUT)
    assert await browser_page.is_visible("#run-view")
    assert "Building your Graph" in await browser_page.text_content("#run-state")
    assert await browser_page.text_content("#run-detail") == "You can leave this tab. The server keeps the run alive."
    assert await browser_page.text_content("#run-percent") == "…"
    assert await browser_page.text_content("#run-crawled") == "0"
    assert await browser_page.locator("#launch-chip").count() == 0

    gate.set()
    await browser_page.wait_for_function("() => document.getElementById('run-waiting').hidden && window.__wikigraph.graph.hasNode('Rosario')", timeout=BOOT_TIMEOUT)
    assert f"run={run_id}" in browser_page.url
    assert int(await browser_page.text_content("#run-crawled")) > 0
    assert int(await browser_page.text_content("#run-discovered")) >= int(await browser_page.text_content("#run-crawled"))


async def test_variant_c_reopened_run_does_not_launch_again(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    stub.add_page("Saved", [(0, "Link")])
    run_id = await create_run(view_server, seed="Saved", depth=1)
    assert (await collect_events(view_server, run_id))[-1]["type"] == "completed"

    requests = []
    browser_page.on("request", lambda request: requests.append(request))
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function("() => window.__wikigraph && window.__wikigraph.graph.hasNode('Link')", timeout=BOOT_TIMEOUT)
    assert await browser_page.is_hidden("#landing-view")
    assert not any(request.method == "POST" and request.url.endswith("/api/runs") for request in requests)
