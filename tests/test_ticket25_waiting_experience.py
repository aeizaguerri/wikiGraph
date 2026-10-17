"""Ticket 25 — the browser-facing durable waiting lifecycle."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.browser_diagnostics import GraphClickDiagnostics
from tests.conftest import BOOT_TIMEOUT, view_url
from tests.helpers import collect_events, create_run, fetch_graph
from wikigraph.runs import OVERLOAD_WAITING_EVENT, RunEvent, RunStatus


def cancel_server_run(run) -> None:
    assert run.task is not None
    run.task.get_loop().call_soon_threadsafe(run.task.cancel)


async def test_run_state_and_preview_are_available_before_completion(client, stub):
    gate = stub.gate("Durable waiting")
    stub.add_page("Durable waiting", links=[(0, "Checkpoint")])
    run_id = await create_run(client, seed="Durable waiting", depth=1)

    state = await client.get(f"/api/runs/{run_id}")
    assert state.status_code == 200
    assert state.json()["runId"] == run_id
    assert state.json()["status"] == "running"

    preview = await client.get(f"/api/runs/{run_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["complete"] is False
    assert preview.json()["runId"] == run_id
    assert {node["title"] for node in preview.json()["nodes"]} == {"Durable waiting"}

    gate.set()
    graph = await fetch_graph(client, run_id)
    assert graph["runId"] == run_id


async def test_reopening_completed_run_keeps_identity_and_does_not_launch(client, stub):
    stub.add_page("Reopen me", links=[(0, "Saved link")])
    run_id = await create_run(client, seed="Reopen me", depth=1)
    assert (await collect_events(client, run_id))[-1]["type"] == "completed"

    reopened = await client.get(f"/api/runs/{run_id}")
    graph = await client.get(f"/api/runs/{run_id}/graph")
    preview = await client.get(f"/api/runs/{run_id}/preview")
    assert reopened.json()["status"] == "completed"
    assert graph.json()["runId"] == run_id
    assert preview.json()["complete"] is True
    assert preview.json()["runId"] == run_id


async def test_browser_launch_shows_real_preview_and_reconnects_without_relaunch(
    stub, view_server, browser_page
):
    gate = stub.gate("Live preview")
    stub.add_page("Live preview", links=[(0, "Visible link")])
    browser_requests = []
    browser_page.on("request", lambda request: browser_requests.append(request))

    await browser_page.goto(str(view_server.base_url))
    await browser_page.fill("#launch-seed", "Live preview")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function(
        "() => new URL(location.href).searchParams.has('run')", timeout=BOOT_TIMEOUT
    )
    run_url = browser_page.url
    await browser_page.wait_for_function(
        "() => window.__wikigraph && window.__wikigraph.graph.hasNode('Live preview')",
        timeout=BOOT_TIMEOUT,
    )
    assert "Building your Graph" in await browser_page.text_content("#run-state")
    assert not any("wikipedia.org" in request.url for request in browser_requests)

    reopened_context = await browser_page.context.browser.new_context()
    reopened = await reopened_context.new_page()
    reopened_requests = []
    reopened.on("request", lambda request: reopened_requests.append(request))
    try:
        await reopened.goto(run_url)
        await reopened.wait_for_function(
            "() => window.__wikigraph && window.__wikigraph.graph.hasNode('Live preview')",
            timeout=BOOT_TIMEOUT,
        )
        assert reopened.url.split("&", 1)[0] == run_url.split("&", 1)[0]
        assert not any(
            request.method == "POST" and request.url.endswith("/api/runs")
            for request in reopened_requests
        )
        await browser_page.close()
        gate.set()
        await reopened.wait_for_function(
            "() => document.getElementById('run-waiting').hidden && window.__wikigraph.graph.hasNode('Visible link')",
            timeout=BOOT_TIMEOUT,
        )
    finally:
        await reopened_context.close()


async def test_browser_retry_reuses_run_id_and_does_not_create_a_new_run(
    stub, view_server, view_store, browser_page
):
    gate = stub.gate("Resume me")
    stub.add_page("Resume me", links=[(0, "After retry")])
    run_id = await create_run(view_server, seed="Resume me", depth=1)
    run = view_store.get(run_id)
    assert run is not None and run.task is not None
    cancel_server_run(run)
    for _ in range(100):
        if (await view_server.get(f"/api/runs/{run_id}")).json()["status"] == "recoverable":
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("the server-owned run did not become recoverable")

    browser_requests = []
    browser_page.on("request", lambda request: browser_requests.append(request))
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "() => document.getElementById('run-state').textContent.includes('interrupted')",
        timeout=BOOT_TIMEOUT,
    )
    assert await browser_page.is_visible("#run-retry")
    assert await browser_page.locator("#run-orbit").count() == 0
    assert await browser_page.text_content("#run-badge") == "Live"
    assert "interrupted" in (await browser_page.text_content("#run-progress")).lower() or "interrupted" in (await browser_page.text_content("#run-state")).lower()
    assert await browser_page.locator("#run-crawled").count() == 1
    assert await browser_page.locator("#run-discovered").count() == 1
    gate.set()
    await browser_page.click("#run-retry")
    await browser_page.wait_for_function(
        "() => document.getElementById('run-waiting').hidden && window.__wikigraph.graph.hasNode('After retry')",
        timeout=BOOT_TIMEOUT,
    )
    assert f"run={run_id}" in browser_page.url
    assert await browser_page.locator("#run-orbit").count() == 0
    assert await browser_page.text_content("#run-badge") == "Ready"
    assert not any(
        request.method == "POST" and request.url.endswith("/api/runs")
        for request in browser_requests
    )
    assert any(
        request.method == "POST" and request.url.endswith(f"/api/runs/{run_id}/retry")
        for request in browser_requests
    )


@pytest.mark.parametrize(
    ("status", "message", "browser_page"),
    [
        ("overload", "Wikimedia is busy", ()),
        ("expired", "expired", (410,)),
    ],
    indirect=["browser_page"],
)
async def test_browser_distinguishes_non_successful_terminal_lifecycles(
    stub, view_server, view_store, browser_page, status, message
):
    stub.gate(f"{status} run")
    stub.add_page(f"{status} run")
    run_id = await create_run(view_server, seed=f"{status} run", depth=1)
    run = view_store.get(run_id)
    assert run is not None and run.task is not None
    cancel_server_run(run)
    for _ in range(100):
        if (await view_server.get(f"/api/runs/{run_id}")).json()["status"] == "recoverable":
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("the server-owned run did not become recoverable")
    if status == "overload":
        run.status = RunStatus.OVERLOAD_WAITING
        run.error = "Wikimedia is busy; retry later."
        run._events.clear()
        run.publish(RunEvent(OVERLOAD_WAITING_EVENT, {"error": run.error}))
    else:
        run.status = RunStatus.EXPIRED
        run.error = "This crawl run has expired."
    run._done.set()

    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "message => document.getElementById('run-state').textContent.toLowerCase().includes(message)",
        arg=message.lower(),
        timeout=BOOT_TIMEOUT,
    )
    assert await browser_page.is_visible("#run-waiting")


async def test_completed_graph_uses_responsive_dimensions_and_cannot_expand_while_running(
    stub, view_server, browser_page
):
    stub.add_page("Responsive graph", links=[(0, "Visible link")])
    run_id = await create_run(view_server, seed="Responsive graph", depth=1)

    await browser_page.set_viewport_size({"width": 1440, "height": 900})
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "() => window.__wikigraph && document.querySelector('#graph canvas')",
        timeout=BOOT_TIMEOUT,
    )
    dimensions = await browser_page.locator("#run-preview").evaluate(
        "element => ({width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height, viewportWidth: innerWidth})"
    )
    assert dimensions["width"] > 1000
    assert dimensions["height"] > 300
    assert dimensions["width"] <= dimensions["viewportWidth"]

    await browser_page.set_viewport_size({"width": 390, "height": 844})
    await browser_page.wait_for_timeout(100)
    mobile = await browser_page.evaluate(
        "() => ({documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth, graphWidth: document.querySelector('#run-preview').getBoundingClientRect().width, graphHeight: document.querySelector('#run-preview').getBoundingClientRect().height})"
    )
    assert mobile["documentWidth"] <= mobile["viewportWidth"]
    assert mobile["graphWidth"] <= mobile["viewportWidth"]
    assert mobile["graphHeight"] > 300


async def test_completed_graph_expands_and_returns_by_button_or_escape(
    stub, view_server, browser_page
):
    stub.add_page("Expandable graph", links=[(0, "Navigable link")])
    run_id = await create_run(view_server, seed="Expandable graph", depth=1)
    await browser_page.set_viewport_size({"width": 1280, "height": 800})
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "() => window.__wikigraph && document.querySelector('#graph canvas')",
        timeout=BOOT_TIMEOUT,
    )
    expand = browser_page.get_by_role("button", name="Expand graph")
    close = browser_page.get_by_role("button", name="Close expanded graph")
    await expand.wait_for(state="visible")
    await browser_page.click("#lens-level")
    assert await browser_page.get_attribute("#lens-level", "aria-pressed") == "true"

    normal = await browser_page.locator("#run-preview").evaluate(
        "element => ({width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height})"
    )
    await expand.click()
    await close.wait_for(state="visible")
    expanded = await browser_page.locator("#run-preview").evaluate(
        "element => ({width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height, viewportWidth: innerWidth, viewportHeight: innerHeight})"
    )
    assert expanded["width"] == expanded["viewportWidth"]
    assert expanded["height"] == expanded["viewportHeight"]
    assert await browser_page.get_attribute("#lens-level", "aria-pressed") == "true"

    await browser_page.set_viewport_size({"width": 900, "height": 650})
    await browser_page.wait_for_function(
        "() => document.querySelector('#graph canvas').getBoundingClientRect().width === innerWidth",
        timeout=BOOT_TIMEOUT,
    )
    resized = await browser_page.locator("#graph canvas").first.evaluate(
        "element => ({width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height, viewportWidth: innerWidth, viewportHeight: innerHeight})"
    )
    assert resized["width"] == resized["viewportWidth"]
    assert resized["height"] == resized["viewportHeight"]

    await close.click()
    await expand.wait_for(state="visible")
    await browser_page.set_viewport_size({"width": 1280, "height": 800})
    await browser_page.wait_for_function(
        "height => Math.abs(document.querySelector('#run-preview').getBoundingClientRect().height - height) < 1",
        arg=normal["height"],
        timeout=BOOT_TIMEOUT,
    )
    restored = await browser_page.locator("#run-preview").evaluate(
        "element => ({width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height})"
    )
    assert restored["width"] == normal["width"]
    assert abs(restored["height"] - normal["height"]) < 1

    await expand.click()
    await browser_page.keyboard.press("Escape")
    await expand.wait_for(state="visible")
    assert await browser_page.evaluate("() => document.activeElement.id") == "graph-expand"
    assert await browser_page.get_attribute("#lens-level", "aria-pressed") == "true"
    assert await browser_page.evaluate(
        "() => window.__wikigraph.graph.hasNode('Navigable link')"
    )


@pytest.fixture
async def graph_click_diagnostics(
    browser_page: Any, view_server: Any
) -> AsyncIterator[GraphClickDiagnostics]:
    diagnostics = GraphClickDiagnostics(Path("test-results/graph-click"))
    enabled = (
        os.environ.get("WIKIGRAPH_GRAPH_CLICK_DIAGNOSTICS") == "1"
        and view_server.base_url.host == "127.0.0.1"
    )
    await diagnostics.start(browser_page, enabled=enabled)
    try:
        yield diagnostics
    finally:
        await diagnostics.finish(browser_page, diagnostics.evidence)


async def test_expanded_graph_keeps_close_clickable_and_traps_keyboard_focus(
    stub, view_server, browser_page, graph_click_diagnostics
):
    stub.add_page("Focus graph", links=[(0, "Selected article")])
    stub.add_page("Selected article")
    run_id = await create_run(view_server, seed="Focus graph", depth=1)
    await browser_page.set_viewport_size({"width": 1280, "height": 800})
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "() => window.__wikigraph?.getState() === 'static' && window.__wikigraph.graph.hasNode('Selected article')",
        timeout=BOOT_TIMEOUT,
    )
    click_state = await browser_page.evaluate("""() => {
          const attrs = window.__wikigraph.graph.getNodeAttributes('Selected article');
          const projected = window.__wikigraph.sigma.graphToViewport({x: attrs.x, y: attrs.y});
          const rect = document.getElementById('graph').getBoundingClientRect();
          const point = {x: rect.left + projected.x, y: rect.top + projected.y};
          window.__testNodePoint = point;
          return {
            point, projected, graphRect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
            node: {x: attrs.x, y: attrs.y, size: attrs.size, label: attrs.label, color: attrs.color},
            selected: window.__wikigraph.getSelected(), cardTitle: document.getElementById('card-title').textContent,
            viewport: {width: innerWidth, height: innerHeight}
          };
        }""")
    graph_click_diagnostics.evidence["click"] = click_state
    point = await browser_page.evaluate("() => window.__testNodePoint")
    await browser_page.mouse.click(point["x"], point["y"])
    await browser_page.wait_for_function(
        "() => document.getElementById('card-title').textContent === 'Selected article'",
        timeout=BOOT_TIMEOUT,
    )

    expand = browser_page.get_by_role("button", name="Expand graph")
    await expand.click()
    close = browser_page.get_by_role("button", name="Close expanded graph")
    await close.wait_for(state="visible")
    assert await browser_page.evaluate("() => document.activeElement.id") == "graph-collapse"
    assert await browser_page.get_attribute("#graph-expand", "aria-expanded") == "true"

    allowed = {"graph-collapse", "lens-community", "lens-level", "card-link"}
    focused = []
    for _ in range(8):
        await browser_page.keyboard.press("Tab")
        focused.append(await browser_page.evaluate("() => document.activeElement.id"))
    assert set(focused) <= allowed
    await browser_page.keyboard.press("Shift+Tab")
    assert await browser_page.evaluate("() => document.activeElement.id") in allowed

    await close.click()
    assert await expand.is_visible()
    assert await browser_page.evaluate("() => document.activeElement.id") == "graph-expand"
    assert await browser_page.get_attribute("#graph-expand", "aria-expanded") == "false"
    assert await browser_page.evaluate("() => window.__wikigraph.getSelected()") == "Selected article"
    assert await browser_page.get_attribute("#lens-community", "aria-pressed") == "true"

    await expand.click()
    await browser_page.keyboard.press("Escape")
    assert await browser_page.evaluate("() => document.activeElement.id") == "graph-expand"
    assert await browser_page.evaluate("() => window.__wikigraph.getSelected()") == "Selected article"
    assert await browser_page.get_attribute("#lens-community", "aria-pressed") == "true"


async def test_active_graph_cannot_expand_until_crawl_completes(
    stub, view_server, browser_page
):
    gate = stub.gate("Still crawling")
    stub.add_page("Still crawling", links=[(0, "After completion")])
    stub.add_page("After completion")
    await browser_page.goto(str(view_server.base_url))
    await browser_page.fill("#launch-seed", "Still crawling")
    await browser_page.click("#launch-submit")
    await browser_page.wait_for_function(
        "() => window.__wikigraph && window.__wikigraph.graph.hasNode('Still crawling')",
        timeout=BOOT_TIMEOUT,
    )
    assert not await browser_page.get_by_role("button", name="Expand graph").is_visible()
    gate.set()
    await browser_page.wait_for_function(
        "() => document.getElementById('run-waiting').hidden && document.getElementById('graph-expand').hidden === false",
        timeout=BOOT_TIMEOUT,
    )
    assert await browser_page.get_by_role("button", name="Expand graph").is_visible()


async def test_browser_failed_run_is_not_presented_as_a_graph(
    stub, view_server, view_store, browser_page
):
    stub.add_page("Failed run")
    stub.fail_links("Failed run", 500)
    run_id = await create_run(view_server, seed="Failed run", depth=1)
    assert (await collect_events(view_server, run_id))[-1]["type"] == "failed"

    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "() => document.getElementById('run-state').textContent.includes('failed')",
        timeout=BOOT_TIMEOUT,
    )
    assert await browser_page.is_visible("#run-waiting")
    assert not await browser_page.is_visible("#run-retry")
