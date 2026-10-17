"""Real PostgREST checks for durable per-run Wikimedia admission quotas."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from wikigraph.governor import RunAttemptQuotaExceeded, SupabaseWikimediaAdmission

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
JWT = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
_active_requests: dict[str, str] | None = None
_active_grants: set[str] | None = None
pytestmark = pytest.mark.skipif(
    URL != "http://127.0.0.1:33028" or not JWT,
    reason="ticket28 disposable loopback PostgREST credentials are required",
)


def _sql(query: str) -> str:
    result = subprocess.run(
        [
            "podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph",
        ], input=query, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
async def drain_test_admissions() -> AsyncIterator[None]:
    """Release grants and drain only this test's pending identities by retry."""
    global _active_requests, _active_grants
    requests: dict[str, str] = {}
    grants: set[str] = set()
    _active_requests, _active_grants = requests, grants
    cooldown = _sql("select coalesce(cooldown_until::text, '') from public.wikimedia_governor_state where singleton") if URL == "http://127.0.0.1:33028" and JWT else ""
    try:
        yield
    finally:
        if URL == "http://127.0.0.1:33028" and JWT:
            async with httpx.AsyncClient(timeout=20) as client:
                # Restore the test's shared cooldown state before draining requests.
                cooldown_value = f"'{cooldown}'::timestamptz" if cooldown else "null"
                _sql(f"update public.wikimedia_governor_state set cooldown_until={cooldown_value} where singleton")
                for request_id, owner in requests.items():
                    if request_id in grants or _request_consumed(request_id, owner):
                        await _release(client, request_id)
                for request_id, owner in requests.items():
                    for _ in range(8):
                        if not _request_queued(request_id) or _request_consumed(request_id, owner):
                            break
                        response = await client.post(
                            f"{URL}/rpc/acquire_wikimedia_attempt", headers=_headers(),
                            json={"p_request_id": request_id, "p_owner": owner},
                        )
                        assert response.is_success, response.text
                        decision = response.json()[0]
                        if decision["granted"]:
                            await _release(client, request_id)
                            break
                        await asyncio.sleep(min(max(float(decision["retry_after_seconds"]), 0.01), 0.5))
                    assert not _request_queued(request_id), f"test request remains queued: {request_id}"
        _active_requests, _active_grants = None, None


def _request_consumed(request_id: str, owner: str) -> bool:
    safe_id = str(uuid.UUID(request_id))
    if owner != safe_id and not _is_uuid(owner):
        return _sql(f"select count(*) from public.wikimedia_governor_state where current_request='{safe_id}' and in_flight") == "1"
    if _is_uuid(owner):
        return _sql(f"select count(*) from public.crawl_runs where run_id='{owner}' and '{safe_id}'::uuid = any(admitted_request_ids)") == "1"
    return False


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _request_queued(request_id: str) -> bool:
    safe_id = str(uuid.UUID(request_id))
    return _sql(f"select count(*) from public.wikimedia_governor_queue where request_id='{safe_id}'") == "1"


async def _release(client: httpx.AsyncClient, request_id: str) -> None:
    response = await client.post(
        f"{URL}/rpc/release_wikimedia_attempt", headers=_headers(),
        json={"p_request_id": request_id},
    )
    response.raise_for_status()


def _track_request(request_id: str, owner: str) -> None:
    if _active_requests is not None:
        _active_requests[request_id] = owner


def _headers() -> dict[str, str]:
    assert JWT is not None
    return {"apikey": JWT, "Authorization": f"Bearer {JWT}", "Content-Type": "application/json"}


async def _create_run(client: httpx.AsyncClient, quota: int) -> str:
    run_id, owner = str(uuid.uuid4()), str(uuid.uuid4())
    response = await client.post(
        f"{URL}/rpc/create_owned_crawl_run", headers=_headers(),
        json={
            "p_run_id": run_id, "p_seed": "https://es.wikipedia.org/wiki/Test",
            "p_language": "es", "p_depth": 1, "p_node_cap": 1,
            "p_checkpoint": {}, "p_owner_token": owner,
        },
    )
    response.raise_for_status()
    assert _sql(f"select attempt_quota from public.crawl_runs where run_id='{run_id}'") == "120"
    _sql(f"update public.crawl_runs set attempt_quota={quota} where run_id='{run_id}'")
    return run_id


async def _admit(client: httpx.AsyncClient, owner: str, request_id: str) -> dict[str, object]:
    _track_request(request_id, owner)
    response = await client.post(
        f"{URL}/rpc/acquire_wikimedia_attempt", headers=_headers(),
        json={"p_request_id": request_id, "p_owner": owner},
    )
    assert response.is_success, response.text
    decision = response.json()[0]
    if decision.get("granted") and _active_grants is not None:
        _active_grants.add(request_id)
    return decision


def _reset_window() -> None:
    _sql("update public.wikimedia_governor_state set in_flight=false,current_request=null,current_owner=null,lease_until=null,starts='{}',attempts='{}' where singleton")


async def test_exact_run_limit_and_consumed_identity_cannot_be_replayed() -> None:
    assert URL is not None
    async with httpx.AsyncClient(timeout=20) as client:
        run_id = await _create_run(client, 3)
        for _ in range(3):
            _reset_window()
            request_id = str(uuid.uuid4())
            decision = await _admit(client, run_id, request_id)
            assert decision["granted"] is True
            await _release(client, request_id)
        _reset_window()
        denied = await _admit(client, run_id, str(uuid.uuid4()))
        assert denied["granted"] is False
        assert denied["denial_code"] == "run_quota_exhausted"
        assert _sql(f"select attempts_used from public.crawl_runs where run_id='{run_id}'") == "3"

        replay_run = await _create_run(client, 3)
        admission_id = str(uuid.uuid4())
        _reset_window()
        assert (await _admit(client, replay_run, admission_id))["granted"] is True
        await _release(client, admission_id)

        other_run = await _create_run(client, 2)
        _reset_window()
        other_id = str(uuid.uuid4())
        assert (await _admit(client, other_run, other_id))["granted"] is True
        await _release(client, other_id)
        replay = await _admit(client, replay_run, admission_id)
        assert replay["denial_code"] == "request_already_admitted"
        assert _sql(f"select attempts_used from public.crawl_runs where run_id='{replay_run}'") == "1"
        assert _sql(f"select admitted_request_ids @> array['{admission_id}'::uuid] from public.crawl_runs where run_id='{replay_run}'") == "t"


@pytest.mark.parametrize(
    ("mutation", "denial_code"),
    [
        ("status='failed',error='fixture',owner_token=null,lease_until=null", "run_not_running"),
        ("expires_at=clock_timestamp()-interval '1 second'", "run_expired"),
        ("lease_until=clock_timestamp()-interval '1 second'", "run_lease_expired"),
    ],
)
async def test_ineligible_run_is_denied_without_quota_charge(
    mutation: str, denial_code: str
) -> None:
    assert URL is not None
    async with httpx.AsyncClient(timeout=20) as client:
        run_id = await _create_run(client, 2)
        _sql(f"update public.crawl_runs set {mutation} where run_id='{run_id}'")
        decision = await _admit(client, run_id, str(uuid.uuid4()))
        assert decision["granted"] is False
        assert decision["denial_code"] == denial_code
        assert _sql(f"select attempts_used from public.crawl_runs where run_id='{run_id}'") == "0"
        assert _sql(f"select count(*) from public.wikimedia_governor_queue where owner='{run_id}'") == "0"


async def test_expiry_transition_clears_admission_ids_but_preserves_used_quota() -> None:
    assert URL is not None
    async with httpx.AsyncClient(timeout=20) as client:
        run_id = await _create_run(client, 2)
        request_id = str(uuid.uuid4())
        assert (await _admit(client, run_id, request_id))["granted"] is True
        await _release(client, request_id)
        _sql(f"update public.crawl_runs set expires_at=clock_timestamp()-interval '1 second' where run_id='{run_id}'")
        response = await client.post(f"{URL}/rpc/expire_crawl_runs", headers=_headers(), json={})
        response.raise_for_status()
        assert _sql(f"select status from public.crawl_runs where run_id='{run_id}'") == "expired"
        assert _sql(f"select cardinality(admitted_request_ids) from public.crawl_runs where run_id='{run_id}'") == "0"
        assert _sql(f"select attempts_used from public.crawl_runs where run_id='{run_id}'") == "1"


async def test_cooldown_denies_without_charging_run_quota_then_allows_after_reset() -> None:
    assert URL is not None
    async with httpx.AsyncClient(timeout=20) as client:
        run_id = await _create_run(client, 2)
        _reset_window()
        _sql("update public.wikimedia_governor_state set cooldown_until=clock_timestamp()+interval '3 seconds' where singleton")
        request_id = str(uuid.uuid4())
        try:
            denied = await _admit(client, run_id, request_id)
            assert denied["granted"] is False
            assert float(denied["retry_after_seconds"]) > 0
            assert _sql(f"select attempts_used from public.crawl_runs where run_id='{run_id}'") == "0"
        finally:
            _sql("update public.wikimedia_governor_state set cooldown_until=null where singleton")
        _reset_window()
        granted = await _admit(client, run_id, request_id)
        assert granted["granted"] is True
        assert _sql(f"select attempts_used from public.crawl_runs where run_id='{run_id}'") == "1"


async def test_independent_clients_obey_independent_run_quotas_and_legacy_owner_bypasses_quota() -> None:
    assert URL is not None and JWT is not None
    async with httpx.AsyncClient(timeout=20) as one, httpx.AsyncClient(timeout=20) as two:
        run_one, run_two = await asyncio.gather(_create_run(one, 1), _create_run(two, 1))
        _reset_window()
        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
        first, second = await asyncio.gather(
            _admit(one, run_one, first_id),
            _admit(two, run_two, second_id),
        )
        assert sorted([first["granted"], second["granted"]]) == [False, True]
        # Release whichever run acquired the singleton lease before retrying the other.
        for request_id, result in ((first_id, first), (second_id, second)):
            if result["granted"]:
                await _release(one if request_id == first_id else two, request_id)
        _reset_window()
        for run_id, result, client, request_id in (
            (run_one, first, one, first_id), (run_two, second, two, second_id)
        ):
            if not result["granted"]:
                retried = await _admit(client, run_id, request_id)
                assert retried["granted"] is True
                await _release(client, request_id)
        assert _sql(f"select sum(attempts_used) from public.crawl_runs where run_id in ('{run_one}','{run_two}')") == "2"

        # A non-UUID owner is launch validation: it still counts globally, not per run.
        _reset_window()
        legacy_id = str(uuid.uuid4())
        legacy = await _admit(one, "launch-validation", legacy_id)
        assert legacy["granted"] is True
        await _release(one, legacy_id)
        assert _sql("select cardinality(attempts) from public.wikimedia_governor_state where singleton") == "1"
        assert _sql(f"select count(*) from public.crawl_runs where '{legacy_id}'::uuid = any(admitted_request_ids)") == "0"


async def test_client_translates_stable_quota_denial_to_safe_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    assert URL is not None
    async with httpx.AsyncClient(timeout=20) as client:
        run_id = await _create_run(client, 1)
        _reset_window()
        decision = await _admit(client, run_id, str(uuid.uuid4()))
        assert decision["granted"] is True
        await _release(client, str(next(request_id for request_id, owner in _active_requests.items() if owner == run_id)))
        _reset_window()
        request_id = str(uuid.uuid4())
        _track_request(request_id, run_id)
        monkeypatch.setattr("wikigraph.governor.uuid.uuid4", lambda: uuid.UUID(request_id))
        admission = SupabaseWikimediaAdmission(URL, JWT, rest_path="")
        with pytest.raises(RunAttemptQuotaExceeded, match="upstream request limit"):
            await admission.acquire(run_id)
        await admission.aclose()
