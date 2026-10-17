from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tests.support.api import DeterministicClock
from tests.support.mediawiki import FakeMediaWiki
from wikigraph.app import create_app
from wikigraph.governor import GlobalWikimediaGovernor


@pytest.fixture
def stub() -> FakeMediaWiki:
    return FakeMediaWiki()


@pytest.fixture
async def client(stub: FakeMediaWiki) -> AsyncIterator[httpx.AsyncClient]:
    # Keep the real governor policy; only its external clock is simulated.
    clock = DeterministicClock()
    governor = GlobalWikimediaGovernor(clock=clock)
    app = create_app(mediawiki_transport=stub.transport, governor=governor)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as async_client:
            yield async_client
