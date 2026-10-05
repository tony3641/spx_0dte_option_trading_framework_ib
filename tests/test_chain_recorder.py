"""Chain recorder: schedule, schema, gzip append, heartbeat, error isolation."""
import gzip
import json
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
