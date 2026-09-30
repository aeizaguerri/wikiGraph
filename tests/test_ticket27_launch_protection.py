from __future__ import annotations

import httpx
import pytest

from tests.stub import FakeMediaWiki
from wikigraph.app import LaunchLimits, create_app
from wikigraph.runs import LaunchAdmissionDecision
from wikigraph.runs import InMemoryCrawlRunStore


class RecordingAdmission:
    def __init__(self) -> None:
        self.ip_hashes: list[str] = []

    def admit(self, ip_hash: str, **_: int) -> LaunchAdmissionDecision:
        self.ip_hashes.append(ip_hash)
        if len(self.ip_hashes) > 1:
            return LaunchAdmissionDecision(False, "launch_rate_limited")
        return LaunchAdmissionDecision(True, "admitted")

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_invalid_launch_is_rejected_before_a_run_is_queued(stub: FakeMediaWiki) -> None:
    store = InMemoryCrawlRunStore()
    app = create_app(mediawiki_transport=stub.transport, run_store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/runs", json={"seed": "https://example.invalid/not-wikipedia"}
            )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "malformed_seed"
    assert store.get("anything") is None
    assert stub.requests == []


@pytest.mark.asyncio
async def test_launch_limits_are_stable_and_apply_to_accepted_launches(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")
    app = create_app(
        mediawiki_transport=stub.transport,
        launch_limits=LaunchLimits(per_ip_per_minute=1, deployment_per_minute=10, per_ip_per_day=10),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/runs", json={"seed": "Hub"})
            second = await client.post("/api/runs", json={"seed": "Hub"})
    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json() == {
        "error": {
            "code": "launch_rate_limited",
            "message": "Too many crawl launches from this address. Try again shortly.",
        }
    }


@pytest.mark.asyncio
async def test_mediawiki_requests_use_descriptive_configured_user_agent(
    stub: FakeMediaWiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIKIGRAPH_USER_AGENT", "wikiGraph/test (https://example.test/contact)")
    stub.add_page("Hub")
    app = create_app(mediawiki_transport=stub.transport)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/runs", json={"seed": "Hub"})
            assert response.status_code == 201
    assert stub.requests
    assert all(
        request.headers["user-agent"] == "wikiGraph/test (https://example.test/contact)"
        for request in stub.requests
    )


@pytest.mark.asyncio
async def test_shared_admission_covers_editions_and_does_not_trust_forwarded_ip(
    stub: FakeMediaWiki,
) -> None:
    stub.add_page("Hub")
    admission = RecordingAdmission()
    app = create_app(mediawiki_transport=stub.transport, launch_admission_store=admission)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/runs", json={"seed": "Hub", "language": "es"})
            second = await client.post(
                "/api/runs",
                headers={"X-Forwarded-For": "203.0.113.42"},
                json={"seed": "Hub", "language": "en"},
            )
    assert first.status_code == 201
    assert second.status_code == 429
    assert admission.ip_hashes[0] == admission.ip_hashes[1]


@pytest.mark.asyncio
async def test_production_startup_requires_server_only_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "WIKIGRAPH_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="required server-only settings"):
        create_app(production=True)


def test_frontend_assets_contain_no_server_credentials() -> None:
    from wikigraph.app import STATIC_DIR

    forbidden = ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL", "WIKIGRAPH_USER_AGENT")
    assets = list(STATIC_DIR.rglob("*.js")) + list(STATIC_DIR.rglob("*.css"))
    assert assets
    assert all(not any(secret in path.read_text() for secret in forbidden) for path in assets)
