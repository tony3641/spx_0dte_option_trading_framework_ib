"""Market-data line budget.

IB caps concurrent market-data lines per account (100 by default) across every API
client. The dashboard splits that allowance into fixed shares at startup so the chain
stream, the wing poller and order entry can never starve one another, and so this
process never sends a request IB would reject with error 101 (max number of tickers).
"""
import asyncio
from collections import deque
from typing import Deque, Dict, Tuple

FIXED_LINES = 4     # SPX, ES, VIX, VIX1D underlyings
ORDER_LINES = 4     # order-entry mid lookups; never lent to anyone else
POLL_LINES = 12     # wing poller + monthly GEX fetch
SPARE_LINES = 2     # head-room for IB-side accounting lag


class LineBudgetExceeded(RuntimeError):
    """A synchronous subscribe found no free line in its share."""


def split_lines(total: int, stream_cap: int = 160) -> Dict[str, int]:
    """Split ``total`` lines into shares. Lines past ``stream_cap`` go to the poller."""
    base = FIXED_LINES + ORDER_LINES + POLL_LINES + SPARE_LINES
    if total < base + 2:
        raise ValueError(f"MARKET_DATA_LINES={total} is below the minimum of {base + 2}")
    stream = min(total - base, max(0, int(stream_cap)))
    stream -= stream % 2                      # calls and puts are subscribed in pairs
    poll = POLL_LINES + (total - base - stream)
    return {"fixed": FIXED_LINES, "order": ORDER_LINES, "poll": poll, "stream": stream}


class LineBudget:
    """Per-share line counters. Sync ``try_acquire`` for subscriptions, FIFO async
    ``acquire`` for snapshot batches. All calls must run on the event-loop thread."""

    def __init__(self, shares: Dict[str, int]):
        self._cap = {k: int(v) for k, v in shares.items()}
        self._used = {k: 0 for k in self._cap}
        self._waiters: Dict[str, Deque[Tuple[int, asyncio.Future]]] = {
            k: deque() for k in self._cap}

    def capacity(self, share: str) -> int:
        return self._cap[share]

    def used(self, share: str) -> int:
        return self._used[share]

    def free(self, share: str) -> int:
        return self._cap[share] - self._used[share]

    def shares(self) -> Dict[str, int]:
        return dict(self._cap)

    def try_acquire(self, share: str, n: int = 1) -> bool:
        if self.free(share) < n:
            return False
        self._used[share] += n
        return True

    async def acquire(self, share: str, n: int = 1) -> None:
        if n > self._cap[share]:
            raise ValueError(f"cannot take {n} '{share}' lines: capacity is {self._cap[share]}")
        if not self._waiters[share] and self.try_acquire(share, n):
            return
        fut = asyncio.get_running_loop().create_future()
        entry = (n, fut)
        self._waiters[share].append(entry)
        try:
            await fut
        except asyncio.CancelledError:
            try:
                self._waiters[share].remove(entry)
            except ValueError:
                pass
            if fut.done() and not fut.cancelled():   # granted, then cancelled before resuming
                self.release(share, n)               # frees the lines and wakes the queue
            else:
                self._wake(share)                    # a cancelled head may have been blocking
            raise

    def release(self, share: str, n: int = 1) -> None:
        self._used[share] = max(0, self._used[share] - n)
        self._wake(share)

    def _wake(self, share: str) -> None:
        q = self._waiters[share]
        while q:
            need, fut = q[0]
            if fut.done():                  # cancelled waiter
                q.popleft()
                continue
            if self.free(share) < need:     # FIFO: nobody overtakes the head
                break
            q.popleft()
            self._used[share] += need
            fut.set_result(None)
