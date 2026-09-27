"""Real loopback PostgREST checks for durable run cancellation."""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import httpx
import pytest

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
JWT = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
pytestmark = pytest.mark.skipif(
    URL != "http://127.0.0.1:33028" or not JWT,
    reason="ticket28 disposable loopback PostgREST credentials are required",
)


def _headers() -> dict[str, str]:
    assert JWT is not None
    return {"apikey": JWT, "Authorization": f"Bearer {JWT}"}


def _sql(query: str) -> str:
    result = subprocess.run(
        [
            "podman", "exec", "-i", "ticket28-postgres", "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph",
        ], input=query, text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def _rpc(name: str, args: dict[str, object], *, headers: dict[str, str] | None = None) -> httpx.Response:
    assert URL is not None
    return httpx.post(
        f"{URL}/rpc/{name}", headers=_headers() if headers is None else headers,
        json=args, timeout=15,
    )


def _new_run() -> tuple[str, str, int]:
    run_id, token = str(uuid.uuid4()), str(uuid.uuid4())
    response = _rpc("create_owned_crawl_run", {
        "p_run_id": run_id, "p_seed": "Cancellation test", "p_language": "en",
        "p_depth": 1, "p_node_cap": 10,
        "p_checkpoint": {"nodes": ["checkpoint-kept"], "edges": []},
        "p_owner_token": token, "p_attempt_quota": 7,
    })
    assert response.status_code == 200, response.text
    assert response.json()[0]["owner_version"] == 1
    return run_id, token, 1


def _cancel(run_id: str) -> str:
    response = _rpc("cancel_crawl_run", {"p_run_id": run_id})
    assert response.status_code == 200, response.text
    return response.json()[0]["result"]


def _acquire_until_granted(request_id: str, owner: str) -> None:
    deadline = time.monotonic() + 45
    while True:
        response = _rpc("acquire_wikimedia_attempt", {
            "p_request_id": request_id, "p_owner": owner,
        })
        assert response.status_code == 200, response.text
        decision = response.json()[0]
        if decision["granted"]:
            return
        assert decision["denial_code"] is None, decision
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"admission did not grant within 45 seconds: {decision}"
        wait = min(max(float(decision["retry_after_seconds"]), 0.01), 1.0, remaining)
        time.sleep(wait)


def _release(request_id: str) -> None:
    response = _rpc("release_wikimedia_attempt", {"p_request_id": request_id})
    assert response.is_success, response.text


def test_cancel_results_preserve_run_state_and_fence_stale_owner() -> None:
    assert URL is not None
    missing = str(uuid.uuid4())
    assert _cancel(missing) == "not_found"

    run_id, token, version = _new_run()
    assert _cancel(run_id) == "cancelled"
    assert _cancel(run_id) == "not_running"
    assert _sql(
        f"select status='recoverable' and owner_token is null and owner_version=2 "
        f"and lease_until is null and error='Run cancelled by operator' "
        f"and checkpoint->'nodes'='[\"checkpoint-kept\"]'::jsonb "
        f"and attempt_quota=7 and attempts_used=0 from public.crawl_runs where run_id='{run_id}'"
    ) == "t"

    for operation, patch in (
        ("progress", {"progress": {"crawled": 99}}),
        ("checkpoint", {"checkpoint": {"nodes": ["stale"]}, "progress": {}}),
        ("completed", {"graph": {"nodes": [], "edges": []}}),
    ):
        stale = _rpc("fenced_update_crawl_run", {
            "p_run_id": run_id, "p_owner_token": token,
            "p_owner_version": version, "p_operation": operation, "p_patch": patch,
        })
        assert stale.status_code == 200 and stale.json() is False
    assert _sql(
        f"select status='recoverable' and checkpoint->'nodes'='[\"checkpoint-kept\"]'::jsonb "
        f"and owner_version=2 from public.crawl_runs where run_id='{run_id}'"
    ) == "t"

    # Cancellation is recoverable; only an explicit same-ID claim resumes it.
    claimed = _rpc("claim_crawl_run", {
        "p_run_id": run_id, "p_owner_token": str(uuid.uuid4()),
        "p_expected_version": 2, "p_lease_seconds": 90,
    })
    assert claimed.status_code == 200 and len(claimed.json()) == 1
    assert _sql(
        f"select status='running' and attempt_quota=7 and attempts_used=0 "
        f"and checkpoint->'nodes'='[\"checkpoint-kept\"]'::jsonb "
        f"from public.crawl_runs where run_id='{run_id}'"
    ) == "t"


def test_terminal_and_expired_runs_are_unchanged() -> None:
    for status in ("completed", "failed", "expired"):
        run_id, _, _ = _new_run()
        if status == "expired":
            _sql(
                f"update public.crawl_runs set expires_at=clock_timestamp()-interval '1 second' "
                f"where run_id='{run_id}'"
            )
            assert _cancel(run_id) == "expired"
            assert _sql(
                f"select status='running' and owner_token is not null and owner_version=1 "
                f"and attempt_quota=7 and attempts_used=0 "
                f"from public.crawl_runs where run_id='{run_id}'"
            ) == "t"
            expired = _rpc("expire_crawl_runs", {})
            assert expired.status_code == 200, expired.text
            assert _sql(
                f"select status='expired' and owner_version=1 and attempt_quota=7 "
                f"and attempts_used=0 from public.crawl_runs where run_id='{run_id}'"
            ) == "t"
            assert _cancel(run_id) == "not_running"
        else:
            if status == "completed":
                _sql(
                    f"update public.crawl_runs set status='completed', "
                    f"graph='{{\"nodes\":[],\"edges\":[]}}'::jsonb, "
                    f"completed_at=clock_timestamp(),owner_token=null,lease_until=null "
                    f"where run_id='{run_id}'"
                )
            else:
                _sql(
                    f"update public.crawl_runs set status='failed',owner_token=null,lease_until=null "
                    f"where run_id='{run_id}'"
                )
            assert _cancel(run_id) == "not_running"
        assert _sql(
            f"select status='{status}' and attempt_quota=7 and attempts_used=0 "
            f"and owner_version=1 from public.crawl_runs where run_id='{run_id}'"
        ) == "t"


def test_cancel_clears_pending_queue_and_denies_before_and_after_wait() -> None:
    run_id, _, _ = _new_run()
    blocker = str(uuid.uuid4())
    queued = str(uuid.uuid4())
    blocker_granted = False
    try:
        # Wait through unrelated in-flight/rate-window denials without resetting
        # shared governor state, then hold the grant while this run queues.
        _acquire_until_granted(blocker, "cancel-test-blocker")
        blocker_granted = True
        waiting = _rpc("acquire_wikimedia_attempt", {
            "p_request_id": queued, "p_owner": run_id,
        })
        assert waiting.status_code == 200 and waiting.json()[0]["granted"] is False
        assert _sql(
            f"select count(*) from public.wikimedia_governor_queue "
            f"where request_id='{queued}'::uuid and owner='{run_id}'"
        ) == "1"

        assert _cancel(run_id) == "cancelled"
        assert _sql(
            f"select count(*) from public.wikimedia_governor_queue where owner='{run_id}'"
        ) == "0"
        assert _sql(
            f"select in_flight and current_request='{blocker}'::uuid and current_owner='cancel-test-blocker' "
            f"from public.wikimedia_governor_state where singleton"
        ) == "t"

        # A request already waiting when cancellation commits, and a later retry,
        # are both denied by the durable run status rather than receiving a grant.
        after_wait = _rpc("acquire_wikimedia_attempt", {
            "p_request_id": queued, "p_owner": run_id,
        })
        assert after_wait.status_code == 200
        assert after_wait.json()[0]["denial_code"] == "run_not_running"
        assert _sql(
            f"select count(*) from public.wikimedia_governor_queue "
            f"where request_id='{queued}'::uuid"
        ) == "0"
    finally:
        try:
            _cancel(run_id)
        finally:
            if blocker_granted:
                _release(blocker)
            else:
                _sql(
                    f"delete from public.wikimedia_governor_queue "
                    f"where request_id='{blocker}'::uuid"
                )


def test_cancel_preserves_already_granted_charge_and_request_history() -> None:
    run_id, _, _ = _new_run()
    request_id = str(uuid.uuid4())
    admitted = False
    try:
        _acquire_until_granted(request_id, run_id)
        admitted = True
        assert _cancel(run_id) == "cancelled"
        assert _sql(
            f"select status='recoverable' and attempts_used=1 and attempt_quota=7 "
            f"and '{request_id}'::uuid=any(admitted_request_ids) "
            f"from public.crawl_runs where run_id='{run_id}'"
        ) == "t"
        assert _sql(
            f"select in_flight and current_request='{request_id}'::uuid and current_owner='{run_id}' "
            f"from public.wikimedia_governor_state where singleton"
        ) == "t"
    finally:  # Cancel the run and release any granted lease for local isolation.
        try:
            _cancel(run_id)
        finally:
            if admitted:
                _release(request_id)
            else:
                _sql(
                    f"delete from public.wikimedia_governor_queue "
                    f"where request_id='{request_id}'::uuid"
                )


def test_cancel_rpc_is_not_executable_by_public_roles() -> None:
    assert _sql(
        "select has_function_privilege('anon','public.cancel_crawl_run(text)','execute') "
        "or has_function_privilege('authenticated','public.cancel_crawl_run(text)','execute')"
    ) == "f"
    assert _sql(
        "select has_function_privilege('service_role','public.cancel_crawl_run(text)','execute')"
    ) == "t"
    # Missing credentials cannot invoke the RPC through the local PostgREST surface.
    denied = _rpc("cancel_crawl_run", {"p_run_id": str(uuid.uuid4())}, headers={})
    assert denied.status_code in {401, 403}
