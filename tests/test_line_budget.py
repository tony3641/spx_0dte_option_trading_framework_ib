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


@pytest.mark.asyncio
async def test_cancelled_head_waiter_unblocks_the_waiter_behind_it():
    b = LineBudget({"poll": 4})
    await b.acquire("poll", 2)                      # 2 free
    head = asyncio.create_task(b.acquire("poll", 3))    # cannot fit, blocks the queue
    behind = asyncio.create_task(b.acquire("poll", 1))  # fits, but must wait behind head
    await asyncio.sleep(0)
    assert not behind.done()
    head.cancel()
    with pytest.raises(asyncio.CancelledError):
        await head
    await asyncio.wait_for(behind, timeout=1)       # no release() needed to wake it
    assert b.used("poll") == 3


@pytest.mark.asyncio
async def test_grant_then_cancel_before_resume_does_not_leak_lines():
    b = LineBudget({"poll": 1})
    await b.acquire("poll", 1)
    t = asyncio.create_task(b.acquire("poll", 1))
    await asyncio.sleep(0)
    b.release("poll", 1)                            # grants t's future; t has not resumed yet
    assert b.used("poll") == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert b.used("poll") == 0


def test_observe_limit_shrinks_stream_first_then_poll_never_fixed_or_order():
    b = LineBudget(split_lines(100))                      # fixed 4, order 4, poll 12, stream 78
    assert b.observe_limit(90) == {"fixed": 4, "order": 4, "poll": 12, "stream": 70}
    assert b.observe_limit(30) == {"fixed": 4, "order": 4, "poll": 12, "stream": 10}
    assert b.observe_limit(10) == {"fixed": 4, "order": 4, "poll": 2, "stream": 0}
    assert b.ceiling == 10


def test_observe_limit_keeps_the_stream_share_even():
    b = LineBudget(split_lines(100))
    assert b.observe_limit(97)["stream"] == 76            # 77 would split a call/put pair


def test_observe_limit_never_grows_the_budget():
    b = LineBudget(split_lines(100))
    b.observe_limit(60)
    shrunk = b.shares()
    assert b.observe_limit(10_000) == shrunk
    assert b.ceiling == 60


@pytest.mark.asyncio
async def test_observe_limit_fails_a_queued_waiter_that_no_longer_fits_and_unblocks_the_queue():
    b = LineBudget({"fixed": 4, "order": 4, "poll": 12, "stream": 0})
    await b.acquire("poll", 12)                           # batch A holds every poll line
    oversized = asyncio.create_task(b.acquire("poll", 12))   # batch B queued for the old full size
    behind = asyncio.create_task(b.acquire("poll", 2))       # a small batch queued behind it
    await asyncio.sleep(0)
    b.observe_limit(10)                                   # stream is already 0: poll shrinks to 2
    assert b.capacity("poll") == 2
    with pytest.raises(ValueError):
        await asyncio.wait_for(oversized, timeout=0.5)    # fails promptly instead of blocking the queue
    b.release("poll", 12)                                 # batch A finishes
    await asyncio.wait_for(behind, timeout=0.5)
    assert b.used("poll") == 2
    b.release("poll", 2)
    await asyncio.wait_for(b.acquire("poll", 2), timeout=0.5)   # a later batch is not stranded
    assert b.used("poll") == 2


@pytest.mark.asyncio
async def test_a_waiter_cancelled_after_it_was_failed_does_not_release_lines_it_never_held():
    b = LineBudget({"fixed": 4, "order": 4, "poll": 12, "stream": 0})
    await b.acquire("poll", 12)
    oversized = asyncio.create_task(b.acquire("poll", 12))
    await asyncio.sleep(0)
    b.observe_limit(10)                                   # fails the queued waiter...
    oversized.cancel()                                    # ...which is cancelled before it resumes
    with pytest.raises(asyncio.CancelledError):
        await oversized
    assert b.used("poll") == 12                           # batch A's lines are still its own
