"""market/price_bars.py: IB-maintained bars, live merge from `last`, session transitions, messages."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from spx_trade_desk.core.app_state import create_app_state
from spx_trade_desk.market import price_bars
from spx_trade_desk.market.hours import ET
from tests.conftest import MockIBClient

DAY_PREV, DAY = "2099-01-02", "2099-01-05"     # invented dates (a Friday and the next Monday)


def _bar(day, hhmm, close, high=None, low=None):
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return SimpleNamespace(date=datetime(y, mo, d, h, m, tzinfo=ET), open=close,
                           high=high if high is not None else close,
                           low=low if low is not None else close, close=close)


class Clock:
    def __init__(self, day=DAY, hhmm="10:00", sec=0):
        self.set(day, hhmm, sec)
        self.mono = 1000.0

    def set(self, day, hhmm, sec=0):
        h, m = map(int, hhmm.split(":"))
        y, mo, d = map(int, day.split("-"))
        self.now = datetime(y, mo, d, h, m, sec, tzinfo=ET)

    def rth(self):
        t = self.now.hour * 60 + self.now.minute
        return self.now.weekday() < 5 and 570 <= t < 960


def _feed(ib, state, clock, msgs, keep=True):
    async def bcast(m):
        msgs.append(m)
    return price_bars.PriceBarFeed(ib, state, bcast, clock=lambda: clock.mono, now_fn=lambda: clock.now,
                                   rth_fn=clock.rth, keep_up_to_date=keep)


def _state():
    st = create_app_state()
    st.spx_contract = SimpleNamespace(symbol="SPX")
    st.spx_stream = SimpleNamespace(last=None, close=None, bid=None, ask=None)
    return st


@pytest.mark.asyncio
async def test_start_in_rth_keeps_today_only_and_sets_prices():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "10:00"), []
    ib.live_bars_initial = [_bar(DAY_PREV, "15:59", 90.0), _bar(DAY, "09:30", 100.0), _bar(DAY, "09:31", 101.0)]
    feed = _feed(ib, st, clock, msgs)
    assert await feed.start()
    assert [b["time"][11:16] for b in st.price_history] == ["09:30", "09:31"]
    assert st.price_session_date == DAY and st.spx_price == 101.0 and st.spx_last_close == 101.0
    snap = [m for m in msgs if m["type"] == "price_snapshot"][-1]["data"]
    assert snap["mode"] == "live" and snap["session_date"] == DAY and len(snap["bars"]) == 2


@pytest.mark.asyncio
async def test_start_outside_rth_keeps_the_last_session():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "08:00"), []
    ib.live_bars_initial = [_bar(DAY_PREV, "09:30", 90.0), _bar(DAY_PREV, "15:59", 95.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    assert st.price_session_date == DAY_PREV and len(st.price_history) == 2
    assert st.data_mode == "historical" and st.historical_date == DAY_PREV
    assert [m for m in msgs if m["type"] == "price_snapshot"][-1]["data"]["mode"] == "historical"


@pytest.mark.asyncio
async def test_ib_update_appends_or_overwrites_and_broadcasts():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "09:31"), []
    ib.live_bars_initial = [_bar(DAY, "09:30", 100.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    rid = next(iter(ib.live_bar_subs))
    ib.push_live_bar(rid, _bar(DAY, "09:31", 101.0))
    ib.push_live_bar(rid, _bar(DAY, "09:31", 102.0, high=103.0))
    await asyncio.sleep(0)
    assert len(st.price_history) == 2 and st.price_history[-1]["high"] == 103.0
    bars = [m["data"] for m in msgs if m["type"] == "price_bar"]
    assert bars[-1]["session_date"] == DAY and bars[-1]["bar"]["close"] == 102.0


@pytest.mark.asyncio
async def test_update_for_an_older_minute_replaces_that_bar():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "09:33"), []
    ib.live_bars_initial = [_bar(DAY, "09:30", 100.0), _bar(DAY, "09:31", 101.0), _bar(DAY, "09:32", 102.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    ib.push_live_bar(next(iter(ib.live_bar_subs)), _bar(DAY, "09:31", 99.0))
    assert [b["close"] for b in st.price_history] == [100.0, 99.0, 102.0]


@pytest.mark.asyncio
async def test_live_merge_uses_last_never_bid():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "09:31", 20), []
    ib.live_bars_initial = [_bar(DAY, "09:30", 100.0), _bar(DAY, "09:31", 100.5)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    st.spx_stream.bid, st.spx_stream.last = 50.0, 104.0
    await feed.tick()
    last = st.price_history[-1]
    assert last["high"] == 104.0 and last["close"] == 104.0 and last["low"] == 100.5
    clock.set(DAY, "09:32", 1)
    st.spx_stream.last = 103.0
    await feed.tick()
    assert st.price_history[-1]["time"][11:16] == "09:32" and st.price_history[-1]["open"] == 103.0


@pytest.mark.asyncio
async def test_live_merge_falls_back_to_close_and_skips_outside_rth():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "09:30", 30), []
    ib.live_bars_initial = [_bar(DAY, "09:30", 100.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    st.spx_stream.close = 105.0
    await feed.tick()
    assert st.price_history[-1]["close"] == 105.0
    clock.set(DAY, "17:00")
    st.spx_stream.close = 200.0
    await feed.tick()
    assert all(b["close"] != 200.0 for b in st.price_history)


@pytest.mark.asyncio
async def test_open_reset_filters_to_today_and_clears_overnight():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "09:00"), []
    ib.live_bars_initial = [_bar(DAY_PREV, "15:59", 95.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    st.es_derived, st.spx_price = True, 96.0
    await feed.tick()
    assert len(st.price_overnight) == 1
    ib.live_bars_initial = [_bar(DAY_PREV, "15:59", 95.0), _bar(DAY, "09:30", 97.0)]
    clock.set(DAY, "09:30", 5)
    await feed.tick()
    assert st.price_session_date == DAY and [b["time"][:10] for b in st.price_history] == [DAY]
    assert len(st.price_overnight) == 0
    assert ib.count_calls("cancel_historical_bars") == 1


@pytest.mark.asyncio
async def test_open_reset_with_no_bar_for_today_does_not_loop():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "09:00"), []
    ib.live_bars_initial = [_bar(DAY_PREV, "15:59", 95.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    clock.set(DAY, "09:30", 1)
    for _ in range(5):
        await feed.tick()
        clock.mono += 1.0
    assert ib.count_calls("req_historical_bars_live") == 2          # boot + one open reset
    assert st.price_session_date == DAY and len(st.price_history) == 0


@pytest.mark.asyncio
async def test_overnight_point_once_per_minute():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "20:00", 1), []
    ib.live_bars_initial = [_bar(DAY, "15:59", 95.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    st.es_derived, st.spx_price = True, 96.0
    await feed.tick()
    clock.set(DAY, "20:00", 30)
    await feed.tick()
    clock.set(DAY, "20:01", 2)
    await feed.tick()
    assert [p["time"][11:16] for p in st.price_overnight] == ["20:00", "20:01"]
    assert len([m for m in msgs if m["type"] == "price_overnight"]) == 2


@pytest.mark.asyncio
async def test_close_transition_sets_last_close_and_es_baseline():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "15:59", 50), []
    ib.live_bars_initial = [_bar(DAY, "15:59", 101.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    await feed.tick()
    st.es_price = 5000.0
    clock.set(DAY, "16:00", 2)
    await feed.tick()
    assert st.spx_last_close == 101.0 and st.es_at_spx_close == 5000.0


@pytest.mark.asyncio
async def test_error_and_stall_re_request_with_backoff():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "10:00"), []
    ib.live_bars_initial = [_bar(DAY, "09:30", 100.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    ib.push_live_error(next(iter(ib.live_bar_subs)), 366, "query cancelled")
    assert ib.live_bar_subs == {}                     # the dead request is cancelled at once
    await feed.tick()                                 # first retry waits 5 s
    assert ib.count_calls("req_historical_bars_live") == 1
    clock.mono += 5.1
    await feed.tick()
    assert ib.count_calls("req_historical_bars_live") == 2
    clock.mono += price_bars.PRICE_BARS_STALL_S + 1   # silent for 3 minutes in RTH
    await feed.tick()
    clock.mono += 5.1
    await feed.tick()
    assert ib.count_calls("req_historical_bars_live") == 3


@pytest.mark.asyncio
async def test_error_during_the_initial_load_re_requests_after_the_backoff():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "10:00"), []
    ib.live_bars_initial = [_bar(DAY, "09:30", 100.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    real = ib.req_historical_bars_live

    async def failing(contract, on_update, on_error=None, **kw):
        rid, _ = await real(contract, on_update, on_error, **kw)
        on_error(162, "Historical Market Data Service error")   # reported before the return
        return rid, [_bar(DAY, "09:30", 555.0)]                  # a short, untrustworthy load

    ib.req_historical_bars_live = failing
    clock.mono += price_bars.PRICE_BARS_STALL_S + 1   # a stall triggers the re-request
    await feed.tick()
    clock.mono += 5.1
    await feed.tick()
    assert ib.count_calls("req_historical_bars_live") == 2
    assert ib.live_bar_subs == {}                     # the failed request is not left registered
    assert [b["close"] for b in st.price_history] == [100.0]      # the failed load is not applied
    ib.req_historical_bars_live = real
    await feed.tick()
    assert ib.count_calls("req_historical_bars_live") == 2        # not before the backoff
    clock.mono += 15.1                                # second attempt in a row: 15 s
    await feed.tick()
    assert ib.count_calls("req_historical_bars_live") == 3 and len(ib.live_bar_subs) == 1


@pytest.mark.asyncio
async def test_fallback_path_uses_one_shot_bars():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "10:00"), []

    async def one_shot(contract, end_date_time="", duration="1 D", bar_size="1 min",
                       what_to_show="TRADES", use_rth=True, format_date=2):
        return [_bar(DAY, "09:30", 100.0)]

    ib.req_historical_bars = one_shot
    feed = _feed(ib, st, clock, msgs, keep=False)
    assert await feed.start()
    assert len(st.price_history) == 1 and ib.count_calls("req_historical_bars_live") == 0
    clock.mono += price_bars.PRICE_BARS_STALL_S + 10
    await feed.tick()
    assert ib.count_calls("req_historical_bars_live") == 0


@pytest.mark.asyncio
async def test_stop_cancels_the_request():
    ib, st, clock, msgs = MockIBClient(), _state(), Clock(DAY, "10:00"), []
    ib.live_bars_initial = [_bar(DAY, "09:30", 100.0)]
    feed = _feed(ib, st, clock, msgs)
    await feed.start()
    feed.stop()
    assert ib.count_calls("cancel_historical_bars") == 1 and ib.live_bar_subs == {}


@pytest.mark.asyncio
async def test_seed_price_bars_creates_the_feed_without_broadcasting():
    ib, st = MockIBClient(), _state()
    ib.live_bars_initial = [_bar(DAY_PREV, "15:59", 95.0)]
    await price_bars.seed_price_bars(ib, st)
    assert st.price_feed is not None and st.price_feed.ib is ib and len(st.price_history) == 1
