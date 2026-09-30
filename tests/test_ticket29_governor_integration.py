"""Real PostgREST proof of cross-client Wikimedia admission sharing."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from wikigraph.governor import GlobalWikimediaGovernor, SupabaseWikimediaAdmission

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
        ],
        input=query,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


async def test_independent_clients_share_global_admission_and_alternate_owners() -> None:
    assert URL is not None and JWT is not None
    started: list[str] = []
    release_first = asyncio.Event()
    entered_first = asyncio.Event()

    def make_governor() -> GlobalWikimediaGovernor:
        return GlobalWikimediaGovernor(
            admission=SupabaseWikimediaAdmission(URL, JWT, rest_path="")
        )

    spanish = make_governor()
    english = make_governor()

    async def operation(owner: str, *, hold: bool = False) -> str:
        started.append(owner)
        if hold:
            entered_first.set()
            await release_first.wait()
        return owner

    spanish_first = asyncio.create_task(
        spanish.request("es-run", lambda: operation("es-run", hold=True))
    )
    queued: list[asyncio.Task[str]] = []
    try:
        await asyncio.wait_for(entered_first.wait(), timeout=10)
        state = _sql(
            "select in_flight::text || ':' || current_owner from public.wikimedia_governor_state where singleton"
        )
        assert state == "true:es-run"

        # Queue one request from each independent client while ES holds the lease.
        queued = [
            asyncio.create_task(english.request("en-run", lambda: operation("en-run"))),
            asyncio.create_task(spanish.request("es-run", lambda: operation("es-run"))),
        ]
        for _ in range(200):
            queue = _sql(
                "select string_agg(owner, ',' order by owner) from public.wikimedia_governor_queue"
            )
            if queue == "en-run":
                break
            await asyncio.sleep(0.01)
        assert queue == "en-run", f"EN run did not queue behind ES: {queue!r}"
        release_first.set()
        assert await asyncio.wait_for(spanish_first, timeout=10) == "es-run"
        assert await asyncio.wait_for(asyncio.gather(*queued), timeout=10) == [
            "en-run", "es-run"
        ]
    finally:
        release_first.set()
        for task in [spanish_first, *queued]:
            if not task.done():
                task.cancel()
        await asyncio.gather(spanish_first, *queued, return_exceptions=True)
        await asyncio.gather(spanish.shutdown(), english.shutdown())

    # The DB lease was held by exactly one owner, and the DB-selected order let
    # the waiting EN run proceed before ES's next queued attempt.
    assert started == ["es-run", "en-run", "es-run"]
    assert _sql(
        "select in_flight::text || ':' || coalesce(current_owner, '') from public.wikimedia_governor_state where singleton"
    ) == "false:"

    # Check the actual persisted start window without asserting wall-clock exactness.
    gaps = _sql(
        "select coalesce(min(extract(epoch from (b - a))), 0)::numeric(10,3)::text "
        "from (select value as a, lead(value) over (order by ord) as b "
        "from public.wikimedia_governor_state, unnest(starts) with ordinality as s(value, ord) "
        "where singleton) windows where b is not null"
    )
    assert float(gaps) >= 0.45, f"persisted start window violated spacing: {gaps}s"
    counts = _sql(
        "select cardinality(starts)::text || ':' || cardinality(attempts)::text "
        "from public.wikimedia_governor_state where singleton"
    )
    starts_count, attempts_count = map(int, counts.split(":"))
    assert starts_count <= 2
    assert attempts_count <= 120
    assert attempts_count >= 3
