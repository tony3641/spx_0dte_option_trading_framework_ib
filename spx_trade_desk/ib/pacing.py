"""Token-bucket pacing for the data lane of the TWS connection.

The TWS API connection is a FIFO queue: a burst of qualification or market-data requests sent
ahead of an order delays the order (a 300-request burst measured about 6 s). Data-lane call
sites ``await pacer.acquire()`` before sending; the order lane never touches the pacer. It
imports only ``core.perf``.
"""
import asyncio
import time
from typing import Callable

from spx_trade_desk.core.perf import perf

# Token comparison slack. After sleeping exactly the computed deficit, floating-point round-off
# in ``(now - stamp) * rate`` can leave the bucket a hair under the request; the residual sleep
# is then below one clock ulp, the clock never advances and the wait loop would spin forever.
_EPS = 1e-9


class RequestPacer:
    def __init__(self, rate: float, burst: int,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self.rate = float(rate)
        self.burst = max(1, int(burst))
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self.burst)       # may go negative after ``debit``
        self._stamp = clock()
        self._lock = None                      # created lazily: needs a running loop on py3.10

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(float(self.burst), self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, n: int = 1) -> float:
        """Wait until ``n`` messages may be sent (FIFO); return the seconds waited.

        A request larger than the burst is taken in burst-sized chunks. Tokens are taken only
        after the wait, so a cancelled waiter leaves nothing behind (chunks already taken stay
        spent, which only matters for requests larger than the burst).
        """
        if self.rate <= 0 or n <= 0:
            return 0.0
        if self._lock is None:
            self._lock = asyncio.Lock()
        waited = 0.0
        async with self._lock:
            remaining = n
            while remaining > 0:
                chunk = min(remaining, self.burst)
                while True:
                    self._refill()
                    if self._tokens >= chunk - _EPS:
                        break
                    delay = (chunk - self._tokens) / self.rate
                    await self._sleep(delay)
                    waited += delay
                self._tokens -= chunk
                remaining -= chunk
        if waited > 0:
            perf.record("pacer.wait", waited * 1000.0)
        return waited

    def debit(self, n: int = 1) -> None:
        """Count messages already sent (cancels) without waiting for them."""
        if self.rate <= 0 or n <= 0:
            return
        self._refill()
        self._tokens -= n
