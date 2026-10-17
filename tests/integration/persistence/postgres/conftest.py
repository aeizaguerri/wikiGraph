from __future__ import annotations

import os

import httpx
import pytest


@pytest.fixture(autouse=True)
def isolate_real_upstream_cache() -> None:
    """Keep real integration tests independent while exercising shared cache wiring."""
    url = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_URL")
    key = os.environ.get("WIKIGRAPH_TICKET24_POSTGREST_KEY")
    if url and key:
        response = httpx.delete(
            f"{url}/upstream_response_cache",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
        response.raise_for_status()
