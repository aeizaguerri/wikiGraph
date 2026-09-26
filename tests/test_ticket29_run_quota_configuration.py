"""Run-attempt quota configuration and creation snapshot checks."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid

import httpx
import pytest

from tests.stub import FakeMediaWiki
from wikigraph.app import LaunchLimits, create_app
from wikigraph.crawler import CrawlRequest
from wikigraph.governor import GlobalWikimediaGovernor, SupabaseWikimediaAdmission
from wikigraph.runs import SupabaseCrawlRunStore

URL = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
JWT = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
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


def _headers() -> dict[str, str]:
    assert JWT is not None
    return {"apikey": JWT, "Authorization": f"Bearer {JWT}"}


def test_launch_limits_default_and_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIKIGRAPH_RUN_ATTEMPT_QUOTA", raising=False)
    assert LaunchLimits.from_environment().run_attempt_quota == 120
    for invalid in ("0", "-1", "not-an-integer"):
        monkeypatch.setenv("WIKIGRAPH_RUN_ATTEMPT_QUOTA", invalid)
        with pytest.raises(RuntimeError, match="WIKIGRAPH_RUN_ATTEMPT_QUOTA"):
            create_app()


async def test_postgrest_creation_defaults_snapshots_and_retry_preserves_quota(
    stub: FakeMediaWiki,
) -> None:
    assert URL and JWT
    async with httpx.AsyncClient(timeout=20) as client:
        run_id = str(uuid.uuid4())
        payload = {
            "p_run_id": run_id,
            "p_seed": "Quota Default",
            "p_language": "en",
            "p_depth": 1,
            "p_node_cap": 1,
            "p_checkpoint": {},
            "p_owner_token": str(uuid.uuid4()),
        }
        response = await client.post(
            f"{URL}/rpc/create_owned_crawl_run", headers=_headers(), json=payload
        )
        response.raise_for_status()
        assert _sql(f"select attempt_quota from public.crawl_runs where run_id='{run_id}'") == "120"

        for quota in (0, None):
            invalid_payload = {**payload, "p_run_id": str(uuid.uuid4()), "p_attempt_quota": quota}
            rejected = await client.post(
                f"{URL}/rpc/create_owned_crawl_run", headers=_headers(), json=invalid_payload
            )
            assert rejected.status_code == 400

    seed = "Quota Snapshot " + uuid.uuid4().hex[:8]
    stub.add_page(seed)
    gate = stub.gate(seed)
    governor = GlobalWikimediaGovernor(
        admission=SupabaseWikimediaAdmission(URL, JWT, rest_path="")
    )
    store = SupabaseCrawlRunStore(
        URL, JWT, rest_path="", attempt_quota=7, governor=governor
    )
    run = store.start_run(CrawlRequest(seed, 1, "en", node_cap=1), stub.transport)
    try:
        for _ in range(200):
            if stub.requests:
                break
            await asyncio.sleep(0.01)
        assert stub.requests, "crawl did not reach the gated upstream request"
        assert _sql(f"select attempt_quota from public.crawl_runs where run_id='{run.id}'") == "7"
        _sql(
            f"update public.crawl_runs set status='recoverable', owner_token=null, "
            f"lease_until=null, owner_version=owner_version+1 where run_id='{run.id}'"
        )
        gate.set()
        await run.wait_done()
        retry = store.retry_run(run.id, stub.transport)
        assert retry.id == run.id
        assert _sql(
            f"select attempt_quota=7 and attempts_used=1 "
            f"from public.crawl_runs where run_id='{run.id}'"
        ) == "t"
        await retry.wait_done()
    finally:
        await store.shutdown()
        store._client.close()
