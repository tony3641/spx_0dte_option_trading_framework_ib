"""SPX 1-minute bars for the price chart.

IB maintains the bars (``reqHistoricalData`` with ``keepUpToDate``): completed bars come only
from IB. The forming bar is also merged every PRICE_PUSH_INTERVAL from the live SPX *last*
(never bid/ask); an IB update for that minute overwrites it. During RTH the series is today's
session; outside RTH it is the last session plus an ES-derived SPX point per minute. At the first
RTH pass of a new day the request is re-issued and the overnight line cleared.
``PRICE_BARS_KEEP_UP_TO_DATE = False`` swaps the source for a one-shot backfill (same interfaces).
Loop thread only.
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, Optional

from spx_trade_desk.core.config import PRICE_BARS_KEEP_UP_TO_DATE, PRICE_BARS_STALL_S, PRICE_PUSH_INTERVAL
from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib.connection import update_spx_es_prices
from spx_trade_desk.market.hours import ET, is_within_rth, now_et

logger = logging.getLogger(__name__)

RETRY_BACKOFF_S = (5.0, 15.0, 60.0)
LIVE_FAILURES_BEFORE_FALLBACK = 3


def _as_et(dt: datetime) -> datetime:
    return dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)


def bar_to_dict(bar) -> dict:
    dt = _as_et(bar.date)
    return {"time": dt.isoformat(), "time_short": dt.strftime("%H:%M"),
            "open": round(bar.open, 2), "high": round(bar.high, 2),
            "low": round(bar.low, 2), "close": round(bar.close, 2)}


def snapshot_payload(state, rth: bool, today: Optional[str] = None) -> dict:
    today = today or now_et().date().isoformat()
    live = rth and state.price_session_date == today
    return {"session_date": state.price_session_date, "mode": "live" if live else "historical",
            "bars": list(state.price_history), "overnight": list(state.price_overnight)}


def _apply_bar(history, d: dict) -> bool:
    """Insert or replace ``d`` by its minute; return True when the series changed."""
    if history and history[-1]["time"] == d["time"]:
        if history[-1] == d:
            return False
        history[-1] = d
        return True
    if not history or d["time"] > history[-1]["time"]:
        history.append(d)
        return True
    for i in range(len(history) - 1, max(-1, len(history) - 30), -1):
        if history[i]["time"] == d["time"]:
            history[i] = d
            return True
    return False


def _spx_last(state) -> Optional[float]:
    s = getattr(state, "spx_stream", None)
    if s is None:
        return None
    for v in (getattr(s, "last", None), getattr(s, "close", None)):
        if v is not None and v > 0:
            return float(v)
    return None


class PriceBarFeed:
    def __init__(self, ib, state, broadcast_fn=None, *, clock: Callable[[], float] = time.monotonic,
                 now_fn: Callable[[], datetime] = now_et, rth_fn: Callable[[], bool] = is_within_rth,
                 keep_up_to_date: bool = PRICE_BARS_KEEP_UP_TO_DATE):
        self.ib = ib
        self.state = state
        self.broadcast_fn = broadcast_fn
        self._clock = clock
        self._now = now_fn
        self._rth = rth_fn
        self._keep = keep_up_to_date
        self.req_id: Optional[int] = None
        self._last_update_mono: Optional[float] = None
        self._failed = False
        self._attempt = 0
        self._live_failures = 0                  # consecutive failed keepUpToDate requests
        self._next_retry = 0.0
        self._was_rth: Optional[bool] = None
        self._open_reset_day = ""
        self._tasks: set = set()                 # in-flight broadcasts started from IB callbacks

    # -- request lifecycle ----------------------------------------------------

    async def start(self) -> bool:
        """(Re)request the bars; replace the series; broadcast a snapshot.

        True when the series is usable: bars arrived, or RTH has just opened and there is no bar yet
        (IB updates will fill it). False when the request failed or came back empty outside RTH; a
        retry is then scheduled.
        """
        self.stop()
        st = self.state
        if st.spx_contract is None:
            return False
        # Reset before the request: an error reported during the initial load must survive it.
        self._failed = False
        self._next_retry = 0.0
        rth = self._rth()
        errored = False
        was_keep = self._keep
        try:
            if self._keep:
                self.req_id, bars = await self.ib.req_historical_bars_live(
                    st.spx_contract, self._on_update, self._on_error)
            else:
                bars = await self.ib.req_historical_bars(st.spx_contract, end_date_time="", duration="1 D",
                                                         bar_size="1 min", what_to_show="TRADES", use_rth=True)
        except Exception as e:
            logger.warning(f"Price bars request failed: {e}")
            bars, errored = [], True
        perf.count("price_bars.restart")
        self._last_update_mono = self._clock()
        today = self._now().date().isoformat()
        rows = [bar_to_dict(b) for b in bars]
        target = today if rth else (rows[-1]["time"][:10] if rows else st.price_session_date)
        rows = [r for r in rows if r["time"][:10] == target]
        if self._failed or errored:
            if was_keep:
                self._note_live_failure()
            if was_keep and not self._keep:      # the third failure in a row just switched the source
                self.stop()
                self._next_retry = 0.0
                return await self.start()        # one-shot backfill now, no backoff wait
            return self._retry_later()           # keep the series; the failed request is cancelled
        if not rows and (not rth or (st.price_history and st.price_session_date == target)):
            return self._retry_later()           # an empty answer never wipes a series we have
        self._live_failures = 0                  # a start that gave a usable series ends the failure streak
        st.price_history.clear()
        st.price_history.extend(rows)
        st.price_session_date = target
        if rows:
            last_close = rows[-1]["close"]
            st.spx_last_close = last_close
            if st.spx_price <= 0 or not rth:
                st.spx_price = last_close
            st.historical_date = target
        if st.data_mode != "live":
            st.data_mode = "historical"
        if not self._keep:
            self._attempt = 0               # no IB updates to wait for: the backfill is the success
        logger.info(f"Price bars: {len(rows)} bars for {target} "
                    f"({'keepUpToDate' if self._keep else 'one-shot backfill'})")
        await self.broadcast_snapshot()
        return bool(rows) or rth

    def stop(self) -> None:
        if self.req_id is not None:
            try:
                self.ib.cancel_historical_bars(self.req_id)
            except Exception:
                pass
            self.req_id = None

    def _retry_later(self) -> bool:
        """Cancel the request that gave nothing usable and schedule the next attempt (no-op if one is set)."""
        self.stop()
        if self._next_retry == 0.0:
            self._schedule_retry()
        return False

    def _note_live_failure(self) -> None:
        """Count a failed keepUpToDate start; after LIVE_FAILURES_BEFORE_FALLBACK in a row stop using it.

        A failed start is a request that raised or was reported dead by IB before it returned a usable
        series, so no bars show. The streak ends with a start that gives a series or with an IB update.
        The fallback lasts for the process: it is the existing PRICE_BARS_KEEP_UP_TO_DATE=false path
        (one-shot backfill, ``last`` merge).
        """
        self._live_failures += 1
        if self._live_failures >= LIVE_FAILURES_BEFORE_FALLBACK:
            self._keep = False
            self._attempt = 0
            logger.warning(f"Price bars: keepUpToDate failed {self._live_failures} times in a row; falling back "
                           f"to a one-shot backfill plus the live SPX last (restart to retry keepUpToDate)")

    def _schedule_retry(self) -> None:
        delay = RETRY_BACKOFF_S[min(self._attempt, len(RETRY_BACKOFF_S) - 1)]
        self._attempt += 1
        self._next_retry = self._clock() + delay

    # -- IB callbacks (loop thread) ----------------------------------------------

    def _on_update(self, bar) -> None:
        self._attempt = 0                        # the request works: the next failure retries after 5 s again
        self._live_failures = 0
        now = self._clock()
        if self._last_update_mono is not None:
            perf.record("price_bars.update_gap", (now - self._last_update_mono) * 1000.0)
        self._last_update_mono = now
        perf.count("price_bars.update")
        d = bar_to_dict(bar)
        if d["time"][:10] != self.state.price_session_date:
            return
        st = self.state
        if _apply_bar(st.price_history, d):
            if not self._rth() and st.price_history[-1]["time"] == d["time"]:
                # IB's final update of the session can land after the close: keep the ES baseline base current.
                st.spx_last_close = d["close"]
                if not st.es_derived:
                    st.spx_price = d["close"]
            self._emit({"type": "price_bar", "data": {"session_date": st.price_session_date, "bar": d}})

    def _on_error(self, code: int, message: str) -> None:
        logger.warning(f"Price bars request error {code}: {message}; re-requesting")
        self._failed = True
        self._retry_later()      # start() cancels a request still being loaded once it returns

    # -- one pass of the loop ----------------------------------------------------

    async def tick(self) -> None:
        st = self.state
        now = self._now()
        rth = self._rth()
        today = now.date().isoformat()
        mono = self._clock()

        if self._was_rth and not rth:                 # the session just closed
            self._was_rth = rth
            if st.price_history:
                st.spx_last_close = st.price_history[-1]["close"]
            if st.es_price > 0:
                st.es_at_spx_close = st.es_price
            if st.spx_last_close > 0:
                # The ES refresh may already have derived a price from the previous baseline: re-base it.
                st.spx_price = st.live_price = st.spx_last_close
            return                                    # the overnight line starts on the next pass
        self._was_rth = rth

        if rth and st.price_session_date != today and self._open_reset_day != today:
            self._open_reset_day = today
            st.price_overnight.clear()
            logger.info("Price bars: regular session opened, switching to today's bars")
            await self.start()
            return

        stalled = (self._keep and rth and self.req_id is not None and self._last_update_mono is not None
                   and mono - self._last_update_mono > PRICE_BARS_STALL_S)
        if stalled and self._next_retry == 0.0:
            logger.warning(f"Price bars: no IB update for {PRICE_BARS_STALL_S:.0f}s; re-requesting")
            self._retry_later()
        if self._next_retry and mono >= self._next_retry:
            await self.start()
            return

        message = None
        if rth and st.price_session_date == today:
            message = self._merge_live(now)
        elif not rth:
            message = self._sample_overnight(now)
        if message is not None:
            await self._send(message)

    def _merge_live(self, now: datetime) -> Optional[dict]:
        price = _spx_last(self.state)
        if price is None:
            return None
        price = round(price, 2)
        minute = now.replace(second=0, microsecond=0)
        key = minute.isoformat()
        hist = self.state.price_history
        if hist and hist[-1]["time"] == key:
            b = dict(hist[-1])
            b["high"], b["low"], b["close"] = max(b["high"], price), min(b["low"], price), price
        elif not hist or key > hist[-1]["time"]:
            b = {"time": key, "time_short": minute.strftime("%H:%M"),
                 "open": price, "high": price, "low": price, "close": price}
        else:
            return None
        if _apply_bar(hist, b):
            return {"type": "price_bar", "data": {"session_date": self.state.price_session_date, "bar": b}}
        return None

    def _sample_overnight(self, now: datetime) -> Optional[dict]:
        st = self.state
        minute = now.replace(second=0, microsecond=0).isoformat()
        if not st.es_derived or st.spx_price <= 0:
            return None
        if st.price_overnight and st.price_overnight[-1]["time"] >= minute:
            return None                              # one point per minute, also across a feed restart
        point = {"time": minute, "value": round(st.spx_price, 2)}
        st.price_overnight.append(point)
        return {"type": "price_overnight", "data": {"point": point}}

    # -- output ----------------------------------------------------------------

    async def _send(self, message: dict) -> None:
        if self.broadcast_fn is not None:
            await self.broadcast_fn(message)

    def _emit(self, message: dict) -> None:
        """Broadcast from a sync IB callback: schedule it and keep a reference until it is done."""
        if self.broadcast_fn is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.broadcast_fn(message))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def broadcast_snapshot(self) -> None:
        if self.broadcast_fn is not None:
            await self.broadcast_fn({"type": "price_snapshot", "data": snapshot_payload(
                self.state, self._rth(), self._now().date().isoformat())})


async def seed_price_bars(ib, state) -> None:
    """First-boot step: request the bars and keep the request open (the loop takes it over)."""
    feed = PriceBarFeed(ib, state)
    state.price_feed = feed             # registered first: a failed first request is retried by the loop
    await feed.start()


async def _refresh_prices(state) -> None:
    """SPX/ES price refresh every PRICE_PUSH_INTERVAL, independent of the bar feed's IB waits."""
    while True:
        try:
            await asyncio.sleep(PRICE_PUSH_INTERVAL)
            await update_spx_es_prices(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Price refresh error: {e}")
            await asyncio.sleep(5)


async def price_bars_loop(ib, state, broadcast_fn):
    """Session loop: live merge, session transitions, overnight line; the price refresh runs beside it.

    The refresh is its own task because ``feed.tick`` can wait on IB (a re-request at the 09:30 reset
    takes the pacer plus up to 30 s) and spx_price / data_mode / the ES derivation must not stall.
    """
    feed = state.price_feed
    fresh = feed is None or feed.ib is not ib
    if fresh:
        feed = PriceBarFeed(ib, state)
        state.price_feed = feed
    feed.broadcast_fn = broadcast_fn
    refresh = asyncio.create_task(_refresh_prices(state))
    try:
        try:
            if fresh:
                await feed.start()                  # reconnect: re-request off the boot path
            else:
                await feed.broadcast_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Price bars start error: {e}")     # keep looping; a retry is scheduled
        while True:
            try:
                await asyncio.sleep(PRICE_PUSH_INTERVAL)
                await feed.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Price bars loop error: {e}")
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass
    finally:
        feed.stop()
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)
