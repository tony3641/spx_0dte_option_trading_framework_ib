"""Standalone capture: idles while the dashboard records, takes over when it stops."""
import os
import time
from datetime import datetime, timedelta

import pytest

from spx_trade_desk.market import capture as cap
from spx_trade_desk.market import chain_recorder as rec_mod
from spx_trade_desk.market.chain_recorder import ChainRecorder
from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.hours import ET
from spx_trade_desk.market.quote_book import QuoteBook


@pytest.fixture(autouse=True)
def full_day(monkeypatch):
    monkeypatch.setattr(rec_mod, "is_short_trading_day", lambda d=None: False)
    monkeypatch.setattr(cap, "update_spx_es_prices", _noop)


async def _noop(*a, **k):
    return None


class FakeClock:
    """ET wall clock + epoch + monotonic that only move when ``sleep`` is awaited."""

    def __init__(self, start: datetime):
        self.t = start

    def now(self):
        return self.t

    def epoch(self):
        return self.t.timestamp()

    def mono(self):
        return self.t.timestamp()

    async def sleep(self, s):
        self.t += timedelta(seconds=s)


def _state(app_state):
    st = app_state
    st.expiration, st.spx_price, st.strikes = "20261005", 7700.0, [7700.0]
    st.quote_book = QuoteBook()
    return st


async def _fetch(*a, **k):
    return [OptionData(7700.0, "P", bid=1.0)]


def test_heartbeat_fresh(tmp_path):
    hb = tmp_path / ".heartbeat-dashboard"
    assert not cap.heartbeat_fresh(hb, time.time())          # missing file
    hb.touch()
    assert cap.heartbeat_fresh(hb, time.time())
    assert not cap.heartbeat_fresh(hb, time.time() + 181)


def test_standalone_heartbeat_is_separate(tmp_path):
    assert ChainRecorder(tmp_path, source="standalone").heartbeat != tmp_path / ".heartbeat-dashboard"


@pytest.mark.asyncio
async def test_capture_records_on_schedule_and_stops_at_close(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 50, tzinfo=ET))
    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, tmp_path / ".heartbeat-dashboard",
                                  clock=clock.now, epoch=clock.epoch, mono=clock.mono,
                                  sleep=clock.sleep, fetch=_fetch)
    assert n == 10                                  # 15:50 .. 15:59 at 60 s
    assert clock.t.time() >= datetime(2026, 10, 5, 16, 0).time()


@pytest.mark.asyncio
async def test_capture_idles_while_dashboard_heartbeat_is_fresh(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 57, tzinfo=ET))
    hb = tmp_path / ".heartbeat-dashboard"
    hb.touch()
    os.utime(hb, (clock.epoch() + 10_000, clock.epoch() + 10_000))   # stays fresh all session
    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, hb, clock=clock.now,
                                  epoch=clock.epoch, mono=clock.mono, sleep=clock.sleep,
                                  fetch=_fetch)
    assert n == 0


@pytest.mark.asyncio
async def test_capture_takes_over_when_dashboard_heartbeat_goes_stale(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 50, tzinfo=ET))
    hb = tmp_path / ".heartbeat-dashboard"
    hb.touch()
    os.utime(hb, (clock.epoch(), clock.epoch()))     # dashboard's last write: 15:50
    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, hb, clock=clock.now,
                                  epoch=clock.epoch, mono=clock.mono, sleep=clock.sleep,
                                  fetch=_fetch)
    # idle until the heartbeat is 180 s old (15:53, next check 15:53/15:54), then 60 s cadence
    assert 6 <= n <= 7
