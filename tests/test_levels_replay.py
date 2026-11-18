"""Levels replay: outcome scoring, per-day replay, flow correlations and the CLI. Invented data only (2099 dates)."""
import csv
import json

import pytest

from spx_trade_desk.market.levels_replay import main, replay_day, score_level, summarize
from tests.sim_chain_fixture import write_record

DAY = "20990105"
VALID = {"hold", "break", "touch_only", "untouched"}


def _b(close, high=None, low=None):
    return {"open": close, "high": high if high is not None else close,
            "low": low if low is not None else close, "close": close}


def _price(minute_of_day: int) -> float:
    """Invented SPX path: flat at 100 until 10:00, then up 0.5 a minute."""
    return 100.0 if minute_of_day < 600 else 100.0 + 0.5 * (minute_of_day - 600)


def _bars(first=570, last=690):
    out = []
    for m in range(first, last):
        c = _price(m)
        out.append({"time": f"{DAY[:4]}-{DAY[4:6]}-{DAY[6:]}T{m // 60:02d}:{m % 60:02d}",
                    "open": c, "high": c + 0.05, "low": c - 0.05, "close": c})
    return out


def _chain_rows():
    rows = []
    for k in range(90, 111):
        call_oi = 50 + 10 * (k - 90)
        put_oi = 250 - 10 * (k - 90)
        for right, oi in (("C", call_oi), ("P", put_oi)):
            rows.append({"k": float(k), "r": right, "bid": 1.0, "ask": 1.2, "last": None, "iv": 0.20,
                         "delta": None, "gamma": None, "oi": oi, "vol": 10, "age_s": 0.0, "src": "poll"})
    return rows


def _record(hhmm: str) -> dict:
    return {"v": 1, "ts": f"{DAY[:4]}-{DAY[4:6]}-{DAY[6:]}T{hhmm}:00-05:00", "expiry": DAY, "spot": 100.0,
            "vix": 15.0, "vix1d": 14.0, "source": "test", "rows": _chain_rows()}


def test_score_level_outcomes():
    assert score_level(95.0, 100.0, [_b(98), _b(96.5, low=96.5), _b(100.5)]) == "hold"          # touched, then a close >= 100
    assert score_level(95.0, 100.0, [_b(96, low=96), _b(89.5)]) == "break"                      # a close <= 90 after the touch
    assert score_level(95.0, 100.0, [_b(96.5, low=96.5), _b(97), _b(96)]) == "touch_only"
    assert score_level(95.0, 100.0, [_b(99), _b(98)]) == "untouched"
    assert score_level(105.0, 100.0, [_b(103.5, high=103.5), _b(99.5)]) == "hold"               # resistance mirrors it


def test_replay_day_scores_levels_against_a_mirrored_baseline_and_dedupes(tmp_path):
    records = [_record("10:00"), _record("10:02"), _record("10:04")]
    day = replay_day(records, _bars(), 0.043)
    events = day["levels"]
    assert events, "the invented chain must produce level events"
    assert {e["outcome"] for e in events} <= VALID
    assert {e["baseline_outcome"] for e in events} <= VALID
    assert all(e["spot"] == 100.0 for e in events)
    # Same chain three times within DEDUPE_MIN: every (type, level) is evaluated once, at the first record.
    assert {e["time"] for e in events} == {"10:00"}
    keys = [(e["type"], round(e["level"])) for e in events]
    assert len(keys) == len(set(keys))
    assert {"em_high", "em_low"} <= {e["type"] for e in events}


def test_flow_correlations_need_enough_samples():
    def flow(i, x, y):
        return {"time": "10:00", "bucket": "open", "charm_flow_15m_usd": x, "charm_to_close_usd": None,
                "vanna_flow_est_usd": None, "ret_15": y, "ret_30": None, "ret_60": None}

    short = summarize([{"levels": [], "flows": [flow(i, float(i), float(i)) for i in range(3)]}])
    assert short["flows"]["charm_flow_15m_usd|15|open"] is None
    assert short["levels"] == {}

    linear = summarize([{"levels": [], "flows": [flow(i, float(i), 2.0 * i + 1.0) for i in range(12)]}])
    assert linear["flows"]["charm_flow_15m_usd|15|open"] == pytest.approx(1.0)
    assert linear["flows"]["charm_flow_15m_usd|15|mid"] is None


def test_main_writes_the_summary_files(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    for rec in (_record("10:00"), _record("10:02")):
        write_record(lib / f"{DAY}.jsonl.gz", rec)
    bars = tmp_path / "bars.csv"
    with open(bars, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close"])
        for b in _bars():
            w.writerow([b["time"] + ":00", b["open"], b["high"], b["low"], b["close"]])
    out = tmp_path / "out"
    assert main(["--date", DAY, "--bars", str(bars), "--library", str(lib), "--out", str(out)]) == 0
    assert (out / "summary.md").is_file()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert set(summary) >= {"levels", "flows"}


def test_main_returns_1_for_a_missing_bars_file(tmp_path, capsys):
    lib = tmp_path / "library"
    lib.mkdir()
    rc = main(["--date", DAY, "--bars", str(tmp_path / "nope.csv"), "--library", str(lib),
               "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "--bars" in capsys.readouterr().err


@pytest.mark.parametrize("body", [
    "ts,open,high,low,close\nnot-a-time,1,1,1,1\n",          # ValueError: bad timestamp
    "ts,open,high,low\n2099-01-05T10:00:00,1,1,1\n",          # KeyError: missing close column
])
def test_main_returns_1_for_a_malformed_bars_csv(tmp_path, capsys, body):
    lib = tmp_path / "library"
    lib.mkdir()
    write_record(lib / f"{DAY}.jsonl.gz", _record("10:00"))
    bars = tmp_path / "bars.csv"
    bars.write_text(body, encoding="utf-8")
    rc = main(["--date", DAY, "--bars", str(bars), "--library", str(lib), "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "--bars" in capsys.readouterr().err


def test_main_returns_1_for_an_empty_library_dir(tmp_path, capsys):
    lib = tmp_path / "library"
    lib.mkdir()
    bars = tmp_path / "bars.csv"
    bars.write_text("ts,open,high,low,close\n2099-01-05T10:00:00,1,1,1,1\n", encoding="utf-8")
    rc = main(["--date", DAY, "--bars", str(bars), "--library", str(lib), "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "nothing to replay" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
