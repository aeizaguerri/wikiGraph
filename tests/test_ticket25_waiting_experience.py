"""Ticket 25 — the browser-facing durable waiting lifecycle."""

from __future__ import annotations

import asyncio

import pytest

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
    gate.set()
    await browser_page.click("#run-retry")
    await browser_page.wait_for_function(
        "() => document.getElementById('run-waiting').hidden && window.__wikigraph.graph.hasNode('After retry')",
        timeout=BOOT_TIMEOUT,
    )
    assert f"run={run_id}" in browser_page.url
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
