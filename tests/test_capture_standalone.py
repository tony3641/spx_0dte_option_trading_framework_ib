"""Standalone capture: idles while the dashboard records, takes over when it stops."""
import gzip
import json
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


def _records(root, day="20261005"):
    with gzip.open(root / f"{day}.jsonl.gz", "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


@pytest.mark.asyncio
async def test_capture_survives_one_failed_sweep(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 50, tzinfo=ET))
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pacing violation")
        return [OptionData(7700.0, "P", bid=1.0)]

    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, tmp_path / ".heartbeat-dashboard",
                                  clock=clock.now, epoch=clock.epoch, mono=clock.mono,
                                  sleep=clock.sleep, fetch=flaky)
    assert n > 0


@pytest.mark.asyncio
async def test_capture_gives_up_after_five_consecutive_failures(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 50, tzinfo=ET))
    calls = {"n": 0}

    async def broken(*a, **k):
        calls["n"] += 1
        raise RuntimeError("no data")

    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, tmp_path / ".heartbeat-dashboard",
                                  clock=clock.now, epoch=clock.epoch, mono=clock.mono,
                                  sleep=clock.sleep, fetch=broken)
    assert n == 0
    assert calls["n"] == 5
    assert clock.t.time() < datetime(2026, 10, 5, 16, 0).time()      # returned early
    # failed sweeps wait a record interval (60 s here) before the next attempt
    assert clock.t >= datetime(2026, 10, 5, 15, 54, tzinfo=ET)


@pytest.mark.asyncio
async def test_a_failed_sweep_is_not_retried_before_the_record_interval(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 50, tzinfo=ET))
    stamps = []

    async def broken(*a, **k):
        stamps.append(clock.t)
        raise RuntimeError("pacing violation")

    rec = ChainRecorder(tmp_path, source="standalone")
    await cap.capture_session(None, _state(app_state), rec, tmp_path / ".heartbeat-dashboard",
                              clock=clock.now, epoch=clock.epoch, mono=clock.mono,
                              sleep=clock.sleep, fetch=broken)
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    assert gaps and all(g >= 60 for g in gaps)


@pytest.mark.asyncio
async def test_the_failure_count_resets_after_an_idle_spell(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 20, tzinfo=ET))
    hb = tmp_path / ".heartbeat-dashboard"
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 4:                       # dashboard comes back for ~3 minutes
            hb.touch()
            os.utime(hb, (clock.epoch() + 100, clock.epoch() + 100))
        if calls["n"] <= 8:
            raise RuntimeError("pacing violation")
        return [OptionData(7700.0, "P", bid=1.0)]

    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, hb, clock=clock.now,
                                  epoch=clock.epoch, mono=clock.mono, sleep=clock.sleep,
                                  fetch=flaky)
    assert calls["n"] > 8 and n > 0               # 4 + 4 failures are not 5 in a row


@pytest.mark.asyncio
async def test_capture_returns_to_idle_when_dashboard_heartbeat_returns(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 40, tzinfo=ET))
    hb = tmp_path / ".heartbeat-dashboard"
    handover = datetime(2026, 10, 5, 15, 50, tzinfo=ET)
    sleep_calls = {"fresh": False}

    async def sleep(s):
        await clock.sleep(s)
        if clock.t >= handover and not sleep_calls["fresh"]:
            sleep_calls["fresh"] = True
            hb.touch()
            os.utime(hb, (clock.epoch() + 10_000, clock.epoch() + 10_000))

    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, hb, clock=clock.now,
                                  epoch=clock.epoch, mono=clock.mono, sleep=sleep, fetch=_fetch)
    stamps = [datetime.fromisoformat(r["ts"]) for r in _records(tmp_path)]
    assert n == len(stamps) > 0
    assert sum(1 for ts in stamps if ts > handover) <= 1      # at most one in-flight


@pytest.mark.asyncio
async def test_capture_records_are_tagged_standalone(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 57, tzinfo=ET))
    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, tmp_path / ".heartbeat-dashboard",
                                  clock=clock.now, epoch=clock.epoch, mono=clock.mono,
                                  sleep=clock.sleep, fetch=_fetch)
    records = _records(tmp_path)
    assert n == len(records) > 0
    assert {r["source"] for r in records} == {"standalone"}


@pytest.mark.asyncio
async def test_capture_waits_for_a_price_before_sweeping(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 58, tzinfo=ET))
    st = _state(app_state)
    st.spx_price = 0.0
    calls = {"n": 0}

    async def fetch(*a, **k):
        calls["n"] += 1
        return [OptionData(7700.0, "P", bid=1.0)]

    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, st, rec, tmp_path / ".heartbeat-dashboard",
                                  clock=clock.now, epoch=clock.epoch, mono=clock.mono,
                                  sleep=clock.sleep, fetch=fetch)
    assert n == 0 and calls["n"] == 0


@pytest.mark.asyncio
async def test_capture_backs_off_when_a_sweep_records_nothing(tmp_path, app_state):
    clock = FakeClock(datetime(2026, 10, 5, 15, 50, tzinfo=ET))
    calls = {"n": 0}

    async def empty(*a, **k):
        calls["n"] += 1
        return []                       # empty book -> maybe_record returns False

    rec = ChainRecorder(tmp_path, source="standalone")
    n = await cap.capture_session(None, _state(app_state), rec, tmp_path / ".heartbeat-dashboard",
                                  clock=clock.now, epoch=clock.epoch, mono=clock.mono,
                                  sleep=clock.sleep, fetch=empty)
    assert n == 0
    assert calls["n"] <= 10             # one sweep per 60 s interval, not one per 5 s step


def test_rebuild_pricing_library_runs_the_builder(monkeypatch):
    from spx_trade_desk.sim import library
    calls = []
    monkeypatch.setattr(library, "build_and_write", lambda *a, **k: calls.append(1) or {"days": 3})
    cap.rebuild_pricing_library()
    assert calls == [1]


def test_rebuild_pricing_library_never_raises(monkeypatch, caplog):
    from spx_trade_desk.sim import library

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(library, "build_and_write", boom)
    with caplog.at_level("WARNING"):
        cap.rebuild_pricing_library()
    assert "Pricing library rebuild failed" in caplog.text
