"""Market-data line budget: static split + per-share accounting."""
import asyncio

import pytest

from spx_trade_desk.ib.line_budget import LineBudget, split_lines


def test_split_at_default_100_lines():
    assert split_lines(100) == {"fixed": 4, "order": 4, "poll": 12, "stream": 78}


@pytest.mark.parametrize("total", [24, 60, 100, 101, 137, 200, 500])
def test_split_always_keeps_two_spare_lines(total):
    assert sum(split_lines(total).values()) == total - 2


def test_split_stream_is_even_and_capped_extra_goes_to_poll():
    assert split_lines(200) == {"fixed": 4, "order": 4, "poll": 30, "stream": 160}
    assert split_lines(101)["stream"] % 2 == 0


def test_split_standalone_gives_everything_to_poll():
    assert split_lines(100, stream_cap=0) == {"fixed": 4, "order": 4, "poll": 90, "stream": 0}


def test_split_rejects_tiny_budget():
    with pytest.raises(ValueError, match="MARKET_DATA_LINES"):
        split_lines(23)


def test_try_acquire_respects_capacity_and_release():
    b = LineBudget({"fixed": 2})
    assert b.try_acquire("fixed") and b.try_acquire("fixed")
    assert not b.try_acquire("fixed")
    b.release("fixed")
    assert b.try_acquire("fixed")


def test_order_reserve_is_never_lent_to_the_stream():
    b = LineBudget(split_lines(100))
    for _ in range(78):
        assert b.try_acquire("stream")
    assert not b.try_acquire("stream")
    assert b.free("order") == 4


def test_release_never_goes_negative():
    b = LineBudget({"poll": 3})
    b.release("poll", 5)
    assert b.used("poll") == 0


def test_unknown_share_raises_keyerror():
    with pytest.raises(KeyError):
        LineBudget({"poll": 1}).try_acquire("nope")


@pytest.mark.asyncio
async def test_acquire_waits_fifo_until_release():
    b = LineBudget({"poll": 4})
    await b.acquire("poll", 4)
    order = []

    async def waiter(name, n):
        await b.acquire("poll", n)
        order.append(name)

    t1 = asyncio.create_task(waiter("first", 3))
    t2 = asyncio.create_task(waiter("second", 1))
    await asyncio.sleep(0)
    b.release("poll", 1)          # 1 free: the head needs 3, and nobody overtakes it
    await asyncio.sleep(0)
    assert order == []
    b.release("poll", 3)          # 4 free: first (3), then second (1)
    await asyncio.gather(t1, t2)
    assert order == ["first", "second"]
    assert b.used("poll") == 4


@pytest.mark.asyncio
async def test_acquire_more_than_capacity_raises():
    with pytest.raises(ValueError):
        await LineBudget({"poll": 2}).acquire("poll", 3)


@pytest.mark.asyncio
async def test_cancelled_waiter_leaves_the_queue():
    b = LineBudget({"poll": 1})
    await b.acquire("poll", 1)
    t = asyncio.create_task(b.acquire("poll", 1))
    await asyncio.sleep(0)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    b.release("poll", 1)
    assert b.used("poll") == 0
