"""Shared browser scenario helpers (not pytest fixtures)."""

from __future__ import annotations

from typing import Any

import httpx

from tests.support.api import collect_events, create_run

BOOT_TIMEOUT = 10_000


def view_url(client: httpx.AsyncClient, run_id: str) -> str:
    return f"{client.base_url}/?run={run_id}"


async def completed_run(
    stub: Any, client: httpx.AsyncClient, *, seed: str, depth: int
) -> str:
    """Create a run and wait for its terminal event; returns the run id."""
    run_id = await create_run(client, seed=seed, depth=depth)
    events = await collect_events(client, run_id)
    assert events[-1]["type"] == "completed", events
    return run_id


async def boot_view(page: Any, client: httpx.AsyncClient, run_id: str) -> None:
    """Load the v2 view for a run and wait until the experience is up."""
    await page.goto(view_url(client, run_id))
    await page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)
