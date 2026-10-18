"""Atomic launch admission against the owned local PostgREST service.

Run through ``scripts/test_postgres.py`` with this file as the explicit child.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.support.postgres import load_postgres_harness

HARNESS = load_postgres_harness()
URL = HARNESS.url if HARNESS else None
KEY = HARNESS.service_role_jwt if HARNESS else None
pytestmark = pytest.mark.skipif(
    not URL or not KEY, reason="owned PostgreSQL harness is not configured"
)


def test_per_ip_limit_rejects_second_launch() -> None:
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
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    with httpx.Client() as client:
        first = client.post(f"{URL}/rpc/admit_crawl_launch", headers=headers, json=limits)
        second = client.post(f"{URL}/rpc/admit_crawl_launch", headers=headers, json=limits)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()[0]["admitted"] is True
    assert second.json()[0]["code"] == "launch_rate_limited"
