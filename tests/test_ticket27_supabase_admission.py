"""Atomic launch admission against a real PostgREST/Postgres service.

Run with an isolated local service and a valid PostgREST JWT:
WIKIGRAPH_TICKET27_POSTGREST_URL=http://127.0.0.1:33001 \
WIKIGRAPH_TICKET27_POSTGREST_KEY="$LOCAL_ANON_JWT" \
uv run pytest tests/test_ticket27_supabase_admission.py -q
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

URL = os.environ.get("WIKIGRAPH_TICKET27_POSTGREST_URL")
KEY = os.environ.get("WIKIGRAPH_TICKET27_POSTGREST_KEY")
pytestmark = pytest.mark.skipif(
    not URL or not KEY, reason="ticket27 real PostgREST service is not configured"
)


def test_real_atomic_per_ip_admission() -> None:
    assert URL is not None
    assert KEY is not None
    ip_hash = uuid.uuid4().hex + uuid.uuid4().hex
    limits = {
        "p_ip_hash": ip_hash,
        "p_per_ip_per_minute": 1,
        "p_deployment_per_minute": 1000,
        "p_per_ip_per_day": 2,
        "p_max_ip_keys": 10000,
    }
    with httpx.Client() as client:
        first = client.post(f"{URL}/rpc/admit_crawl_launch", json=limits)
        second = client.post(f"{URL}/rpc/admit_crawl_launch", json=limits)
    assert first.json()[0]["admitted"]
    assert second.json()[0]["code"] == "launch_rate_limited"
