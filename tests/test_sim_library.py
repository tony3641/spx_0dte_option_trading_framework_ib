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
    rows += [_row(6000.0, "C", None), _row(6010.0, "C", 0.11)]    # IV missing at the nearest strike
    assert record_atm(rows, 5998.0) is None                       # next IV is 15 points away


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
