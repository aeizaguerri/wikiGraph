from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tests.stub import FakeMediaWiki
from wikigraph.app import create_app


@pytest.fixture
def stub() -> FakeMediaWiki:
    return FakeMediaWiki()


@pytest.fixture
async def client(stub: FakeMediaWiki) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(mediawiki_transport=stub.transport)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
