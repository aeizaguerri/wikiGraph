import asyncio

from wikigraph.governor import GlobalWikimediaGovernor


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.current += delay
        await asyncio.sleep(0)


class SharedCanonicalAdmission:
    """Small in-process model of the Supabase lease/RPC boundary."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.pending: list[str] = []
        self.last_owner: str | None = None
        self.in_flight = False
        self.max_in_flight = 0
        self.starts: list[float] = []

    async def acquire(self, owner: str) -> None:
        self.pending.append(owner)
        while True:
            distinct_waiter = any(candidate != self.last_owner for candidate in self.pending)
            is_turn = self.pending[0] == owner and (
                owner != self.last_owner or not distinct_waiter
            )
            if not self.in_flight and is_turn:
                break
            await asyncio.sleep(0)
        self.pending.remove(owner)
        if self.starts:
            self.clock.current = max(self.clock.current, self.starts[-1] + 0.5)
        self.starts.append(self.clock.now())
        self.in_flight = True
        self.max_in_flight = max(self.max_in_flight, 1)
        self.last_owner = owner

    async def release(self) -> None:
        self.in_flight = False


async def test_governor_is_one_in_flight_and_round_robin_across_runs() -> None:
    clock = FakeClock()
    governor = GlobalWikimediaGovernor(clock)
    active = 0
    maximum = 0
    completed: list[str] = []

    async def operation(owner: str) -> str:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        completed.append(owner)
        return owner

    await asyncio.gather(
        *(governor.request(owner, lambda owner=owner: operation(owner)) for owner in ("es-1", "en-1") for _ in range(3))
    )

    assert maximum == 1
    assert completed == ["es-1", "en-1", "es-1", "en-1", "es-1", "en-1"]
    assert [timestamp for _, timestamp in governor.dispatch_log] == [0, 0.5, 1, 1.5, 2, 2.5]


async def test_governor_counts_all_attempts_in_global_second_and_minute_windows() -> None:
    clock = FakeClock()
    governor = GlobalWikimediaGovernor(clock)
    attempts: list[float] = []

    async def attempt() -> None:
        attempts.append(clock.now())

    await asyncio.gather(*(governor.request("run", attempt) for _ in range(121)))

    assert len(attempts) == 121
    assert max(sum(timestamp <= current < timestamp + 1 for timestamp in attempts) for current in attempts) <= 2
    assert attempts[119] < 60
    assert attempts[120] >= 60
    assert len(governor.dispatch_log) == 121


async def test_distinct_runtime_governors_share_canonical_concurrency_and_windows() -> None:
    clock = FakeClock()
    admission = SharedCanonicalAdmission(clock)
    first = GlobalWikimediaGovernor(clock, admission=admission)
    second = GlobalWikimediaGovernor(clock, admission=admission)
    started: list[str] = []

    async def operation(owner: str) -> None:
        started.append(owner)
        await asyncio.sleep(0)

    await asyncio.gather(
        *(first.request("es-run", lambda: operation("es-run")) for _ in range(2)),
        *(second.request("en-run", lambda: operation("en-run")) for _ in range(2)),
    )

    assert admission.max_in_flight == 1
    assert started == ["es-run", "en-run", "es-run", "en-run"]
    assert max(
        sum(start <= current < start + 1 for current in admission.starts)
        for start in admission.starts
    ) <= 2
