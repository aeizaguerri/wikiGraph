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
