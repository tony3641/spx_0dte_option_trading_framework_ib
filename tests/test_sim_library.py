# tests/test_sim_library.py
"""Chain library reader, extraction and cache (SP2 spec section 6). Invented data only."""
import gzip

import numpy as np
import pytest

from spx_trade_desk.sim import library
from spx_trade_desk.sim.library import (extract_day, extract_record, iter_records, load_day,
                                        record_atm)
from tests.sim_chain_fixture import ATM_OPEN, g_true, r_true, write_day, write_record


def _row(k, r, iv, **kw):
    row = {"k": k, "r": r, "iv": iv, "bid": 1.0, "ask": 1.1, "delta": -0.3, "age_s": 0.0}
    row.update(kw)
    return row


def test_iter_records_stops_at_truncated_member(tmp_path):
    p = tmp_path / "20300304.jsonl.gz"
    for i in range(3):
        write_record(p, {"v": 1, "i": i})
    tail = gzip.compress(b'{"v":1,"i":3}\n')
    with open(p, "ab") as fh:              # a member the recorder is still writing
        fh.write(tail[: len(tail) // 2])
    assert [r["i"] for r in iter_records(p)] == [0, 1, 2]


def test_iter_records_skips_bad_lines_and_other_versions(tmp_path):
    p = tmp_path / "20300304.jsonl.gz"
    write_record(p, {"v": 1, "i": 0})
    with gzip.open(p, "at", encoding="utf-8") as fh:
        fh.write("not json\n" + '{"v": 2, "i": 1}\n')
    write_record(p, {"v": 1, "i": 2})
    assert [r["i"] for r in iter_records(p)] == [0, 2]


def test_record_atm_interpolates_between_bracketing_strikes():
    rows = [_row(5995.0, "P", 0.10), _row(5995.0, "C", 0.12),
            _row(6000.0, "P", 0.14), _row(6000.0, "C", 0.14)]
    assert record_atm(rows, 5997.5) == pytest.approx(0.5 * 0.11 + 0.5 * 0.14)


def test_record_atm_one_sided_book_is_none():
    rows = [_row(5990.0, "P", 0.12), _row(5995.0, "P", 0.12)]
    assert record_atm(rows, 5998.0) is None                       # nothing quoted above spot
    rows += [_row(6000.0, "C", None), _row(6010.0, "C", 0.11)]    # nearest IV above is 15 points away
    assert record_atm(rows, 5998.0) is None                       # bracket gap 15 > ATM_MAX_GAP


def test_record_atm_ignores_rows_without_a_usable_iv_on_one_side():
    # The strikes just above spot (gap 5, inside ATM_MAX_GAP) carry a missing and a zero IV,
    # so they must not count as a bracket side: with them filtered nothing is quoted above.
    rows = [_row(5995.0, "P", 0.12), _row(6000.0, "C", None), _row(6003.0, "P", 0.0)]
    assert record_atm(rows, 5998.0) is None


def test_record_atm_skips_ivless_nearest_strike_when_gap_stays_inside_limit():
    # 6000 has no usable IV (None put, zero call); the next IV above is 6005, so the bracket is
    # 5995..6005 (gap 10 = ATM_MAX_GAP) and the value interpolates between 0.10 and 0.14 only.
    rows = [_row(5995.0, "P", 0.10), _row(6000.0, "P", None), _row(6000.0, "C", 0.0),
            _row(6005.0, "C", 0.14)]
    atm = record_atm(rows, 5998.0)
    assert atm is not None and np.isfinite(atm)
    assert atm == pytest.approx(0.10 + 0.3 * 0.04)                # 0.112; 0.06 if 6000 were used


def test_record_atm_exact_strike_ignores_stale_rows():
    rows = [_row(6000.0, "P", 0.13), _row(6000.0, "C", 0.50, age_s=999.0)]
    assert record_atm(rows, 6000.0) == pytest.approx(0.13)


def test_extract_record_applies_capture_hygiene():
    rows = [_row(6000.0, "P", 0.12), _row(6000.0, "C", 0.12),            # ATM
            _row(5950.0, "P", 0.15, bid=0.50, delta=-0.10),               # kept
            _row(5940.0, "P", 0.16, bid=0.05, delta=-0.08),               # bid below 0.10
            _row(5930.0, "P", 0.17, bid=0.20, delta=-0.002),              # |delta| below 0.005
            _row(5920.0, "P", 0.18, bid=0.20, delta=-0.05, age_s=999.0),  # stale
            _row(6050.0, "P", 0.11, bid=40.0, ask=40.5, delta=-0.80),     # ITM put: not a shape point
            _row(6050.0, "C", 0.11, bid=0.30, ask=0.35, delta=0.10)]      # kept (OTM call)
    rec = {"v": 1, "ts": "2030-03-04T14:00:00-05:00", "expiry": "20300304", "spot": 6000.0,
           "vix1d": 13.0, "rows": rows}
    tau, atm, v1, shape, spreads = extract_record(rec, "20300304")
    assert tau == 120.0 and atm == pytest.approx(0.12) and v1 == 13.0
    assert sorted(round(r, 6) for _, r in shape) == sorted([round(0.11 / 0.12, 6), round(0.15 / 0.12, 6)])
    assert len(spreads) == 5                              # every fresh put with a two-sided quote
    assert extract_record(dict(rec, expiry="20300305"), "20300304") is None     # not 0DTE
    assert extract_record(dict(rec, ts="2030-03-04T16:05:00-05:00"), "20300304") is None


def test_extract_day_recovers_truth(tmp_path):
    d = extract_day(write_day(tmp_path, "20300304", seed=1))
    assert d.day == "20300304" and d.session_min == 390.0
    assert len(d.rec_tau) == 78 and d.n_skipped == 0 and d.rec_tau[0] == pytest.approx(389.0)
    truth = np.array([ATM_OPEN * g_true(t) for t in d.rec_tau])
    # Linear-in-strike interpolation between 5-point strikes is convex-biased in the last
    # minutes (one strike step spans ~3 z there), so the tolerance widens near the close.
    rtol = np.where(d.rec_tau >= 15.0, 0.01, 0.03)
    assert np.all(np.abs(d.rec_atm - truth) <= rtol * truth)
    want = np.array([float(r_true(z, d.rec_tau[i])) for z, i in zip(d.pt_z, d.pt_rec)])
    ok = d.rec_tau[d.pt_rec] >= 15.0                  # the biased ATM also shifts z and the ratio
    assert ok.sum() > 1000 and np.allclose(d.pt_ratio[ok], want[ok], rtol=0.02)
    assert np.isnan(d.rec_vix1d).sum() == 0


def test_day_without_0dte_expiry_is_skipped(tmp_path):
    assert extract_day(write_day(tmp_path, "20300304", seed=1, step_min=30, expiry="20300305")) is None


def test_half_day_uses_the_real_close(tmp_path, monkeypatch):
    from spx_trade_desk.sim import clock
    monkeypatch.setattr(clock, "is_short_trading_day", lambda d=None: True)
    d = extract_day(write_day(tmp_path, "20301129", seed=1))      # written to 16:00
    assert d.session_min == 210.0 and d.rec_tau[0] == pytest.approx(209.0)
    assert len(d.rec_tau) == 42 and d.n_skipped == 36             # records after 13:00 dropped


def test_load_day_uses_cache_until_the_file_changes(tmp_path, monkeypatch):
    p = write_day(tmp_path, "20300304", seed=1, step_min=30)
    first = load_day(p)
    calls = []
    real = library.extract_day
    monkeypatch.setattr(library, "extract_day", lambda path: calls.append(path) or real(path))
    again = load_day(p)
    assert calls == [] and np.array_equal(again.pt_z, first.pt_z)
    write_record(p, {"v": 1, "ts": "2030-03-04T15:58:00-05:00", "expiry": "20300304",
                     "spot": 6000.0, "rows": []})               # appended member: size + mtime change
    load_day(p)
    assert calls == [p]


def test_load_days_lists_day_files_only(tmp_path):
    write_day(tmp_path, "20300304", seed=1, step_min=60)
    write_day(tmp_path, "20300305", seed=2, step_min=60)
    (tmp_path / "pricing_model.json").write_text("{}")
    (tmp_path / "notes.jsonl.gz").write_bytes(b"")
    assert [d.day for d in library.load_days(tmp_path)] == ["20300304", "20300305"]
    assert [d.day for d in library.load_days(tmp_path, exclude=("20300304",))] == ["20300305"]


# ---- builder, Cold default, VIX1D (spec sections 5.2, 5.4, 6.1) ------------------------
import json
import sys
from datetime import datetime

import pandas as pd

from spx_trade_desk.market.hours import ET
from spx_trade_desk.sim.data import load_vix1d_daily as real_load_vix1d_daily
from spx_trade_desk.sim.data import run_vix1d_prev
from spx_trade_desk.sim.pricing_tables import COLD_PATH, N_TAU, PricingTables, load_cold

TABLE_KEYS = {"v", "z_grid", "tau_edges", "mid_edges", "g_tau", "f", "f_sweeps", "g", "hs",
              "atm_vix1d_ratio", "n_days", "provisional"}


def test_prior_closes_order(tmp_path):
    days = [extract_day(write_day(tmp_path, "20300304", seed=1)),             # last record tau 4
            extract_day(write_day(tmp_path, "20300305", seed=2, step_min=30)),  # last record tau 29
            extract_day(write_day(tmp_path, "20300307", seed=3, step_min=30))]
    prev = library.prior_closes(days, {}, {"20300307": 14.0})
    assert prev == {"20300305": 13.0, "20300307": 14.0}       # 0305 from 0304's closing VIX1D
    prev = library.prior_closes(days, {"20300301": 11.0}, None)
    assert prev == {"20300304": 11.0, "20300305": 11.0, "20300307": 11.0}


def test_build_and_write_keeps_scores(tmp_path):
    for i, d in enumerate(("20300304", "20300305")):
        write_day(tmp_path, d, seed=i, step_min=15)
    path = tmp_path / "pricing_model.json"
    path.write_text(json.dumps({"scores": {"thin": {"passed": 1, "scored": 2}}}))
    s = library.build_and_write(tmp_path, overrides={"20300304": 13.0, "20300305": 13.0})
    m = json.loads(path.read_text())
    assert s["days"] == m["days"] == 2 and m["last_capture"] == "20300305"
    assert m["scores"] == {"thin": {"passed": 1, "scored": 2}}
    assert m["regime_days"] == {"12-18": 2} and set(m["regimes"]) == {"12-18"}
    assert PricingTables.from_dict(m["pooled"]).n_days == 2
    assert s["sweeps"]["<15"] == m["pooled"]["f_sweeps"][6]


def test_write_cold_default_from_a_partial_day(tmp_path):
    write_day(tmp_path, "20300304", seed=1, start="13:25")
    out = tmp_path / "cold.json"
    library.write_cold_default(tmp_path, {"20300304": 13.0}, path=out)
    back = load_cold(out)
    assert back.provisional and all(back.row_usable(b) for b in range(N_TAU))
    assert np.array_equal(back.f[0], back.f[2])     # earlier buckets copy the first captured one
    assert set(json.loads(out.read_text())) == TABLE_KEYS


def test_write_cold_default_needs_a_vix1d_prior_close(tmp_path):
    write_day(tmp_path, "20300304", seed=1, start="13:25")
    with pytest.raises(ValueError, match="VIX1D"):
        library.write_cold_default(tmp_path, {}, path=tmp_path / "cold.json")


def test_committed_cold_default_is_complete():
    t = load_cold()
    assert all(t.row_usable(b) for b in range(N_TAU)) and t.level_usable()
    assert set(json.loads(COLD_PATH.read_text())) == TABLE_KEYS     # aggregates only: no dates


def test_cli_build(tmp_path, capsys):
    write_day(tmp_path, "20300304", seed=1, step_min=60)
    assert library.main(["build", "--root", str(tmp_path), "--vix1d-prev", "20300304=13"]) == 0
    assert (tmp_path / "pricing_model.json").exists()
    assert '"days": 1' in capsys.readouterr().out


class _FakeYF:
    def __init__(self, df=None, exc=None):
        self.df, self.exc = df, exc

    def download(self, *a, **k):
        if self.exc:
            raise self.exc
        return self.df


def test_load_vix1d_daily_parses_and_fails_soft(monkeypatch):
    df = pd.DataFrame({"Close": [11.0, float("nan"), 12.5]},
                      index=pd.to_datetime(["2030-03-01", "2030-03-04", "2030-03-05"]))
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYF(df))
    assert real_load_vix1d_daily() == {"20300301": 11.0, "20300305": 12.5}
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYF(exc=RuntimeError("offline")))
    assert real_load_vix1d_daily() == {}


def test_run_vix1d_prev():
    daily = {"20300301": 11.0, "20300304": 12.0}
    assert run_vix1d_prev(daily, datetime(2030, 3, 4, 10, 0, tzinfo=ET)) == 11.0
    assert run_vix1d_prev(daily, datetime(2030, 3, 4, 16, 30, tzinfo=ET)) == 12.0
    assert run_vix1d_prev({}, datetime(2030, 3, 4, 10, 0, tzinfo=ET)) is None


def test_pricing_summary_without_a_library_is_cold():
    s = library.pricing_summary()
    assert s["tier"] == "cold" and s["library"] is None and s["vix1d_prev"] is None
    assert any(w.startswith("pricing: tier cold") for w in s["warnings"])


def test_rebuild_warns_when_vix1d_is_unavailable(tmp_path, caplog, monkeypatch):
    from spx_trade_desk.sim import data as sim_data
    write_day(tmp_path, "20300304", seed=1, step_min=30)
    with caplog.at_level("WARNING"):
        s = library.build_and_write(tmp_path, overrides={"20300304": 13.0})
    assert any("consecutive-day" in w for w in s["warnings"])
    assert any("consecutive-day" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(sim_data, "load_vix1d_daily", lambda period="2y": {"20300301": 12.0})
    assert library.build_and_write(tmp_path)["warnings"] == []
