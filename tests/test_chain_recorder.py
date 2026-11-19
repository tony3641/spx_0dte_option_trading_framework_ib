"""Chain recorder: schedule, schema, gzip append, heartbeat, error isolation."""
import gzip
import json
import os
from datetime import datetime

import pytest

from spx_trade_desk.market import chain_recorder as rec_mod
from spx_trade_desk.market.chain_recorder import ChainRecorder, build_record, record_interval
from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.hours import ET
from spx_trade_desk.market.quote_book import QuoteBook


def at(h, m, day=5):
    return datetime(2026, 10, day, h, m, tzinfo=ET)


@pytest.fixture(autouse=True)
def full_day(monkeypatch):
    monkeypatch.setattr(rec_mod, "is_short_trading_day", lambda d=None: False)


@pytest.mark.parametrize("hm,expected", [
    ((9, 30), None), ((9, 31), 60), ((9, 59), 60), ((10, 0), 120), ((14, 59), 120),
    ((15, 0), 60), ((15, 59), 60), ((16, 0), None),
])
def test_record_interval_full_day(hm, expected):
    assert record_interval(at(*hm)) == expected


def test_record_interval_half_day(monkeypatch):
    monkeypatch.setattr(rec_mod, "is_short_trading_day", lambda d=None: True)
    assert record_interval(at(11, 59)) == 120
    assert record_interval(at(12, 0)) == 60
    assert record_interval(at(13, 0)) is None


def _book():
    b = QuoteBook()
    b.reset("20261005")
    b.update([OptionData(7700, "P", bid=1.0, ask=1.2, implied_vol=0.25, delta=-0.3,
                         open_interest=10, volume=3)], "poll", now=0.0)
    b.update([OptionData(7705, "C", bid=2.0)], "stream", now=9.0)
    return b


def test_build_record_schema():
    r = build_record(_book(), "20261005", 7701.5, 18.2, 12.4, "dashboard", at(11, 0), now_mono=10.0)
    assert r["v"] == 1 and r["expiry"] == "20261005" and r["source"] == "dashboard"
    assert r["ts"] == "2026-10-05T11:00:00-04:00"
    assert (r["spot"], r["vix"], r["vix1d"]) == (7701.5, 18.2, 12.4)
    put = next(x for x in r["rows"] if x["r"] == "P")
    assert put == {"k": 7700.0, "r": "P", "bid": 1.0, "ask": 1.2, "last": None, "iv": 0.25,
                   "delta": -0.3, "gamma": None, "oi": 10, "vol": 3, "age_s": 10.0, "src": "poll"}
    assert next(x for x in r["rows"] if x["r"] == "C")["src"] == "stream"


def test_write_appends_gzip_members_and_touches_heartbeat(tmp_path):
    rec = ChainRecorder(tmp_path, source="dashboard")
    rec.write({"n": 1}, at(10, 0))
    rec.write({"n": 2}, at(10, 2))
    with gzip.open(tmp_path / "20261005.jsonl.gz", "rt", encoding="utf-8") as f:
        assert [json.loads(line)["n"] for line in f] == [1, 2]
    assert (tmp_path / ".heartbeat-dashboard").exists()


def test_due_respects_cadence(tmp_path):
    rec = ChainRecorder(tmp_path)
    assert rec.due(at(10, 0))
    rec.write({}, at(10, 0))
    assert not rec.due(at(10, 1))
    assert rec.due(at(10, 2))


def _state(app_state, expiry="20261005"):
    st = app_state
    st.expiration, st.spx_price = expiry, 7700.0
    st.quote_book = _book()
    return st


def test_maybe_record_writes_only_for_todays_0dte(tmp_path, app_state):
    rec = ChainRecorder(tmp_path)
    assert not rec.maybe_record(_state(app_state, "20261006"), at(10, 0), now_mono=10.0)
    assert rec.maybe_record(_state(app_state), at(10, 0), now_mono=10.0)
    assert (tmp_path / "20261005.jsonl.gz").exists()


def test_maybe_record_swallows_write_errors(tmp_path, app_state):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    rec = ChainRecorder(blocker / "library")                # mkdir under a file fails
    assert rec.maybe_record(_state(app_state), at(10, 0), now_mono=10.0) is False


def test_touch_heartbeat_creates_and_refreshes_the_file(tmp_path):
    rec = ChainRecorder(tmp_path / "library")
    rec.touch_heartbeat()
    assert rec.heartbeat.exists()
    os.utime(rec.heartbeat, (1.0, 1.0))
    rec.touch_heartbeat()
    assert rec.heartbeat.stat().st_mtime > 1_000_000


def test_a_failed_heartbeat_touch_is_swallowed_and_does_not_duplicate_records(
        tmp_path, app_state, monkeypatch):
    rec = ChainRecorder(tmp_path)
    boom = {"n": 0}

    def broken_touch(*a, **k):
        boom["n"] += 1
        raise PermissionError("locked")

    monkeypatch.setattr(type(rec.heartbeat), "touch", broken_touch)
    st = _state(app_state)
    assert rec.maybe_record(st, at(10, 0), now_mono=10.0)           # record landed, touch failed
    assert boom["n"] >= 1
    assert not rec.maybe_record(st, at(10, 0), now_mono=10.0)       # not due again: no duplicate
    with gzip.open(tmp_path / "20261005.jsonl.gz", "rt", encoding="utf-8") as f:
        assert len(f.readlines()) == 1


def test_no_record_while_spot_is_unknown(tmp_path, app_state):
    rec = ChainRecorder(tmp_path)
    st = _state(app_state)
    st.spx_price = 0.0
    assert not rec.maybe_record(st, at(10, 0), now_mono=10.0)
    assert not (tmp_path / "20261005.jsonl.gz").exists()


def at_s(h, m, sec, day=5):
    return datetime(2026, 10, day, h, m, sec, tzinfo=ET)


def test_a_record_carries_cumulative_flow_and_only_completed_minutes(tmp_path, app_state):
    from spx_trade_desk.market.flow import FlowTracker
    from spx_trade_desk.sim.library import iter_records

    plain = build_record(_book(), "20261005", 7700.0, None, None, "dashboard", at(10, 0), now_mono=1.0)
    assert "flow" not in plain                                         # no tracker, no flow block

    st = _state(app_state)
    st.flow_tracker = FlowTracker()
    keys = {(7700.0, "P")}

    def tick(t, volume):
        st.flow_tracker.update([OptionData(7700, "P", volume=volume, bid=1.0, ask=1.2, last=1.2, delta=-0.3)],
                               keys, 7700.0, t, "20261005")

    rec = ChainRecorder(tmp_path)
    tick(at_s(9, 59, 0), 100)                                          # baseline, minute 9:59
    tick(at_s(10, 0, 10), 105)                                         # 5 bought at the ask, minute 10:00
    assert rec.maybe_record(st, at_s(10, 0, 30), now_mono=10.0)        # 10:00 still filling: not written
    tick(at_s(10, 0, 50), 108)                                         # 3 more, still minute 10:00
    tick(at_s(10, 1, 20), 108)                                         # minute 10:01 starts
    assert rec.maybe_record(st, at_s(10, 2, 30), now_mono=12.0)        # 10:00 and 10:01 are completed
    tick(at_s(10, 2, 40), 110)                                         # 2 more, minute 10:02
    tick(at_s(10, 3, 30), 110)                                         # minute 10:03 starts
    assert rec.maybe_record(st, at_s(10, 4, 30), now_mono=14.0)        # 10:02 and 10:03 are completed

    recs = list(iter_records(tmp_path / "20261005.jsonl.gz"))
    assert [r["v"] for r in recs] == [1, 1, 1]
    a, b, c = (r["flow"] for r in recs)
    assert a["cum"] == [[7700, "P", 5, 0, 0]]
    assert [m["time"] for m in a["minutes"]] == ["2026-10-05T09:59"]         # 10:00 is not partial
    assert b["cum"] == [[7700, "P", 8, 0, 0]]                                 # cum is current at write time
    put_minute = next(m for m in b["minutes"] if m["time"] == "2026-10-05T10:00")
    assert put_minute["put_delta_usd"] == round(8 * -0.3 * 100 * 7700)        # the full minute: 8 contracts
    assert [m["time"] for m in b["minutes"]] == ["2026-10-05T10:00", "2026-10-05T10:01"]
    assert [m["time"] for m in c["minutes"]] == ["2026-10-05T10:02", "2026-10-05T10:03"]   # no gap, no repeat


def test_a_failing_flow_block_still_writes_the_chain_record_without_flow(tmp_path, app_state, monkeypatch, caplog):
    from spx_trade_desk.market.flow import FlowTracker
    from spx_trade_desk.sim.library import iter_records

    st = _state(app_state)
    st.flow_tracker = FlowTracker()

    def boom():
        raise RuntimeError("invented flow failure")

    monkeypatch.setattr(st.flow_tracker, "minutes", boom)
    rec = ChainRecorder(tmp_path)
    assert rec.maybe_record(st, at(10, 0), now_mono=10.0)
    (only,) = list(iter_records(tmp_path / "20261005.jsonl.gz"))
    assert only["v"] == 1 and "flow" not in only and len(only["rows"]) == 2
    assert "flow block failed" in caplog.text
