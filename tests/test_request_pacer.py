"""RequestPacer: the token bucket that keeps data-lane requests from queueing ahead of orders."""
import asyncio

import pytest

from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib.pacing import RequestPacer


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    async def sleep(self, d):
        self.sleeps.append(d)
        self.t += d
        await asyncio.sleep(0)            # let other waiters run, as a real sleep would


def _pacer(rate=10.0, burst=5):
    ft = FakeTime()
    return RequestPacer(rate, burst, clock=ft.clock, sleep=ft.sleep), ft


@pytest.mark.asyncio
async def test_burst_is_free_then_the_rate_applies():
    p, ft = _pacer(rate=10, burst=5)
    for _ in range(5):
        assert await p.acquire() == 0.0
    assert await p.acquire() == pytest.approx(0.1)
    assert ft.t == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_tokens_refill_with_time_but_never_past_the_burst():
    p, ft = _pacer(rate=10, burst=5)
    await p.acquire(5)
    ft.t += 100.0
    assert p.available == pytest.approx(5.0)
    assert await p.acquire(5) == 0.0
    assert await p.acquire() == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_a_request_larger_than_the_burst_waits_for_the_deficit():
    p, ft = _pacer(rate=10, burst=5)
    assert await p.acquire(12) == pytest.approx(0.7)       # 5 free, 7 more at 10/s


@pytest.mark.asyncio
async def test_fifo_a_small_request_cannot_overtake_a_large_one():
    p, ft = _pacer(rate=10, burst=5)
    await p.acquire(5)
    done = []

    async def take(name, n):
        await p.acquire(n)
        done.append(name)

    big = asyncio.create_task(take("big", 8))
    small = asyncio.create_task(take("small", 1))
    await asyncio.gather(big, small)
    assert done == ["big", "small"]


@pytest.mark.asyncio
async def test_debit_counts_messages_without_waiting():
    p, ft = _pacer(rate=10, burst=5)
    p.debit(5)
    assert ft.sleeps == [] and p.available == pytest.approx(0.0)
    assert await p.acquire() == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_a_cancelled_waiter_takes_no_tokens():
    ft = FakeTime()
    gate = asyncio.Event()

    async def blocked_sleep(d):
        await gate.wait()

    p = RequestPacer(10, 5, clock=ft.clock, sleep=blocked_sleep)
    await p.acquire(5)
    waiter = asyncio.create_task(p.acquire(1))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert p.available == pytest.approx(0.0)               # no debt left behind
    ft.t += 0.1
    assert p.available == pytest.approx(1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [0, -1])
async def test_a_disabled_pacer_never_waits(rate):
    p, ft = _pacer(rate=rate)
    for _ in range(50):
        assert await p.acquire() == 0.0
    p.debit(100)
    assert ft.sleeps == []


@pytest.mark.asyncio
async def test_waiting_is_recorded_as_a_perf_span():
    perf.reset()
    p, ft = _pacer(rate=10, burst=1)
    await p.acquire()
    await p.acquire()
    assert perf.snapshot()["metrics"]["pacer.wait"]["n"] == 1
