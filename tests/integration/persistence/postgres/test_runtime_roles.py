"""Real PostgREST role-boundary checks for the production migration."""

from __future__ import annotations

import httpx
import pytest

from tests.support.postgres import load_postgres_harness


HARNESS = load_postgres_harness()
POSTGREST_URL = HARNESS.url if HARNESS else None
SERVICE_ROLE_JWT = HARNESS.service_role_jwt if HARNESS else None
ANON_JWT = HARNESS.anon_jwt if HARNESS else None
AUTHENTICATED_JWT = HARNESS.authenticated_jwt if HARNESS else None
pytestmark = pytest.mark.skipif(
    HARNESS is None,
    reason="owned local PostgreSQL harness is not configured",
)

TABLES = (
    "crawl_runs",
    "crawl_launch_admission",
    "crawl_launch_admission_total",
    "crawl_run_metrics",
    "upstream_response_cache",
    "wikimedia_governor_state",
    "wikimedia_governor_queue",
)


def _headers(token: str) -> dict[str, str]:
    return {
        "apikey": token,
        "Authorization": f"Bearer {token}",
    }


def test_service_role_can_read_runtime_schema_and_call_required_rpcs() -> None:
    assert POSTGREST_URL is not None
    assert SERVICE_ROLE_JWT is not None
    database = httpx.Client(timeout=10.0)
    try:
        headers = _headers(SERVICE_ROLE_JWT)
        for table in TABLES:
            response = database.get(
                f"{POSTGREST_URL}/{table}",
                params={"select": "*", "limit": "0"},
                headers=headers,
            )
            assert response.status_code == 200, (table, response.status_code)

        admission = database.post(
            f"{POSTGREST_URL}/rpc/admit_crawl_launch",
            headers=headers,
            json={
                "p_ip_hash": "invalid",
                "p_per_ip_per_minute": 1,
                "p_deployment_per_minute": 1,
                "p_per_ip_per_day": 1,
                "p_max_ip_keys": 1,
            },
        )
        assert admission.status_code == 200
        assert admission.json()[0]["code"] == "invalid_client_identity"

        expiry = database.post(
            f"{POSTGREST_URL}/rpc/expire_crawl_runs",
            headers=headers,
            json={"p_now": "1970-01-01T00:00:00+00:00"},
        )
        assert expiry.status_code == 200
        assert "expired_count" in expiry.json()[0]

        release = database.post(
            f"{POSTGREST_URL}/rpc/release_wikimedia_attempt",
            headers=headers,
            json={"p_request_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert release.status_code in {200, 204}

        snapshot = database.post(
            f"{POSTGREST_URL}/rpc/read_crawl_run_snapshot",
            headers=headers,
            json={"p_run_id": "missing-role-probe", "p_view": "state"},
        )
        assert snapshot.status_code == 200 and snapshot.json() == []
    finally:
        database.close()


@pytest.mark.parametrize("role_env", ["ANON_JWT", "AUTHENTICATED_JWT"])
def test_browser_roles_cannot_read_private_runtime_tables(role_env: str) -> None:
    assert POSTGREST_URL is not None
    assert HARNESS is not None
    token = ANON_JWT if role_env == "ANON_JWT" else AUTHENTICATED_JWT
    assert token is not None
    database = httpx.Client(timeout=10.0)
    try:
        for table in TABLES:
            response = database.get(
                f"{POSTGREST_URL}/{table}",
                params={"select": "*", "limit": "1"},
                headers=_headers(token),
            )
            assert response.status_code in {401, 403}, (table, response.status_code)
        response = database.post(
            f"{POSTGREST_URL}/rpc/expire_crawl_runs",
            headers=_headers(token),
            json={"p_now": "1970-01-01T00:00:00+00:00"},
        )
        assert response.status_code in {401, 403}
        snapshot = database.post(
            f"{POSTGREST_URL}/rpc/read_crawl_run_snapshot",
            headers=_headers(token),
            json={"p_run_id": "missing-role-probe", "p_view": "state"},
        )
        assert snapshot.status_code in {401, 403}
    finally:
        database.close()
