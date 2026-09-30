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
