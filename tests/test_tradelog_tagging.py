"""Tests for strategy-compliance tagging.

The delta solver is verified by round-trip: price a spread at a known vol, then
assert the solver recovers that vol and the matching delta. Everything else
pins the pass / fail / unverifiable trichotomy — in particular that a missing
input is never reported as a compliance failure.
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
import pytest

from spx_trade_desk.resources import REPORT_DATA_DIR
from spx_trade_desk.sim.pricing import bsm_put
from spx_trade_desk.strategy.models import Condition, ExitRules, StopLoss, Strategy
from spx_trade_desk.tradelog.analysis import tagging


def _bull_put_strategy(name="Test", delta=(0.03, 0.06), width=(40.0, 60.0),
                       credit=(0.20, 0.60), run_days=None, **kwargs):
    conditions = [
        Condition(kind="short_delta", params={"min": delta[0], "max": delta[1]}),
        Condition(kind="spread_width", params={"min": width[0], "max": width[1]}),
        Condition(kind="credit", params={"min": credit[0], "max": credit[1]}),
    ]
    return Strategy(
        name=name,
        direction="bull_put",
        conditions=conditions,
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)),
        run_days=run_days if run_days is not None else [0, 1, 2, 3, 4],
        **kwargs,
    )


def _spread(**overrides):
    row = {
        "date": date(2026, 7, 15),
        "expiry": "2026-07-15",
        "short_strike": 7400.0,
        "qty": 1,
        "short_credit_ct": 49.35,
        "paired": True,
        "long_strike": 7350.0,
        "width": 50.0,
        "credit_ct": 28.70,
        "max_loss_ct": 4971.30,
        "short_pnl": 28.70,
        "open_ts": datetime(2026, 7, 15, 9, 30, 0),
    }
    row.update(overrides)
    return pd.DataFrame([row])


@pytest.fixture
def qfx_losing_positions():
    positions = pd.DataFrame([{
        "contract_key": "SPXW|2026-07-15|P|7400.000",
        "date": date(2026, 7, 15),
        "open_ts": datetime(2026, 7, 15, 9, 30),
        "direction": "short",
        "contracts": 1.0,
        "total_pnl": -400.0,
        "credit": 49.35,
        "expiry": "2026-07-15",
        "strike": 7400.0,
    }])
    spreads = pd.DataFrame([{
        "date": date(2026, 7, 15), "expiry": "2026-07-15", "short_strike": 7400.0,
        "qty": 1, "short_credit_ct": 49.35, "paired": False, "long_strike": None,
        "width": None, "credit_ct": 49.35, "max_loss_ct": None, "short_pnl": -400.0,
    }])
    return positions, spreads


# --- the solver ---------------------------------------------------------

def test_implied_vol_round_trips():
    spot, k_short, k_long, t, r, sigma = 7405.0, 7400.0, 7350.0, 0.0024, 0.04, 0.35
    credit = float(bsm_put(spot, k_short, t, r, sigma)) - float(bsm_put(spot, k_long, t, r, sigma))

    recovered = tagging.implied_vol_from_credit(spot, k_short, k_long, t, credit, r)

    assert recovered == pytest.approx(sigma, abs=1e-4)


def test_short_delta_is_a_bounded_probability():
    spot, k_short, k_long, t, r, sigma = 7405.0, 7400.0, 7350.0, 0.0024, 0.04, 0.35
    credit = float(bsm_put(spot, k_short, t, r, sigma)) - float(bsm_put(spot, k_long, t, r, sigma))

    delta = tagging.short_delta_estimate(spot, k_short, k_long, t, credit, r)

    assert 0.0 < delta < 0.5


@pytest.mark.parametrize("credit", [0.0, -1.0, 50.0, 999.0, None, float("nan")])
def test_solver_returns_none_instead_of_raising_on_impossible_credits(credit):
    assert tagging.implied_vol_from_credit(7405.0, 7400.0, 7350.0, 0.0024, credit, 0.04) is None


def test_solver_returns_none_when_the_spread_is_deep_in_the_money():
    """An ITM spread's credit does not pin a vol — it must be unverifiable, not invented."""
    assert tagging.implied_vol_from_credit(6000.0, 7400.0, 7350.0, 0.0024, 20.0, 0.04) is None


# --- the condition matrix ----------------------------------------------

def test_matrix_reports_pass_fail_and_unverifiable_per_strategy():
    """The `passes` band is derived from the solver, so this tests the wiring
    (delta -> band comparison -> match), not a hand-computed delta that would
    drift with any change to the fixture or the time convention.
    """
    spread = _spread()
    implied = tagging.short_delta_estimate(
        spot=7405.0,
        k_short=spread.iloc[0]["short_strike"],
        k_long=spread.iloc[0]["long_strike"],
        t_years=tagging._year_fraction(datetime(2026, 7, 15, 9, 30), date(2026, 7, 15)),
        credit_per_share=spread.iloc[0]["credit_ct"] / 100.0,
        r=0.04,
    )
    assert implied is not None, "the fixture must be solvable or this test proves nothing"

    strategies = {
        "passes": _bull_put_strategy(
            "passes", delta=(implied - 0.01, implied + 0.01),
            width=(40.0, 60.0), credit=(0.20, 0.60)),
        "fails_width": _bull_put_strategy("fails_width", width=(5.0, 10.0)),
    }
    market = pd.DataFrame([{"activity_date": date(2026, 7, 15),
                            "spx_close": 7405.0, "vix_close": 16.0}])

    matrix = tagging.spread_condition_matrix(spread, strategies, market=market)
    by_strategy = matrix.set_index("strategy")

    # `==` rather than `is`: pandas stores a None-free column as numpy bool, and
    # `==` still distinguishes True from None while `is` would not.
    assert by_strategy.loc["passes", "match"] == True      # noqa: E712
    assert by_strategy.loc["passes", "failed"] == []
    assert by_strategy.loc["fails_width", "match"] == False  # noqa: E712
    assert "spread_width" in by_strategy.loc["fails_width", "failed"]


def test_a_strategy_narrower_than_the_implied_delta_fails_the_delta_check():
    spread = _spread()
    # A band far below any plausible 0-delta-adjacent short must fail.
    strategies = {"too_low": _bull_put_strategy("too_low", delta=(0.90, 0.99))}
    market = pd.DataFrame([{"activity_date": date(2026, 7, 15), "spx_close": 7405.0}])

    matrix = tagging.spread_condition_matrix(spread, strategies, market=market)

    assert "short_delta" in matrix.iloc[0]["failed"]


def test_unpaired_short_is_unverifiable_not_failed():
    """A single-leg premium is NOT a spread credit. Comparing it against the
    strategy's spread-credit band reports a false compliance failure on a row
    that was never a spread — the failure mode this test exists to prevent.
    """
    strategies = {"main_like": _bull_put_strategy(
        "main_like", credit=(0.30, 0.45))}   # the real Main band
    spread = _spread(paired=False, long_strike=None, width=None,
                     credit_ct=500.0, short_credit_ct=500.0, max_loss_ct=None)

    matrix = tagging.spread_condition_matrix(spread, strategies)

    row = matrix.iloc[0]
    assert row["failed"] == []
    assert "credit" in row["unverifiable"]
    assert "spread_width" in row["unverifiable"]
    assert row["match"] is None


def test_nan_market_value_is_unverifiable_not_failed():
    """A missing VIX close (yfinance gap, half day) must not read as a violation."""
    strategy = _bull_put_strategy("vix_gated")
    strategy.conditions.append(Condition(kind="volatility", params={
        "vix_enabled": True, "vix_op": "above", "vix_value": 13}))

    def _market(vix):
        return pd.DataFrame([{"activity_date": date(2026, 7, 15),
                              "spx_close": 7405.0, "vix_close": vix}])

    nan_row = tagging.spread_condition_matrix(
        _spread(), {"s": strategy}, market=_market(float("nan"))).iloc[0]
    real_row = tagging.spread_condition_matrix(
        _spread(), {"s": strategy}, market=_market(16.0)).iloc[0]
    low_row = tagging.spread_condition_matrix(
        _spread(), {"s": strategy}, market=_market(10.0)).iloc[0]

    assert "volatility" in nan_row["unverifiable"]
    assert "volatility" not in nan_row["failed"]
    assert "volatility" not in real_row["failed"]     # 16 > 13 -> passes
    assert "volatility" in low_row["failed"]          # 10 < 13 -> fails


def test_matrix_carries_the_source_column_and_summary_groups_by_it():
    """spread_id restarts at 0 per file, so a multi-file run needs the file name
    carried through or every spread is identified as '0'."""
    strategies = {"s": _bull_put_strategy("s", delta=(0.0, 0.5))}
    jan = _spread()
    jan["source"] = "jan.qfx"
    feb = _spread()
    feb["source"] = "feb.qfx"
    both = pd.concat([jan, feb], ignore_index=True)
    market = pd.DataFrame([{"activity_date": date(2026, 7, 15), "spx_close": 7405.0}])

    matrix = tagging.spread_condition_matrix(both, strategies, market=market)
    summary = tagging.strategy_compliance_summary(matrix, strategies)

    assert set(matrix["source"]) == {"jan.qfx", "feb.qfx"}
    assert len(summary) == 2                       # one row per (source, strategy)
    assert set(summary["source"]) == {"jan.qfx", "feb.qfx"}


def test_exit_audit_measures_a_spread_against_the_spread_credit():
    """The live engine stops on the SPREAD credit (strategy/engine.py:
    stop_mag = abs(candidate.credit_mid) * multiplier). Dividing the short leg's
    loss by its OWN premium understates the ratio whenever the long leg cost
    anything — here 3.5x instead of the true 6x — so a fired stop reads as clean.
    """
    positions = pd.DataFrame([
        {"contract_key": "S", "date": date(2026, 7, 15),
         "open_ts": datetime(2026, 7, 15, 9, 30), "direction": "short",
         "contracts": 1.0, "total_pnl": -350.0, "credit": 100.0,
         "expiry": "2026-07-15", "strike": 7400.0},
        {"contract_key": "L", "date": date(2026, 7, 15),
         "open_ts": datetime(2026, 7, 15, 9, 30), "direction": "long",
         "contracts": 1.0, "total_pnl": 50.0, "credit": None,
         "expiry": "2026-07-15", "strike": 7350.0},
    ])
    spreads = pd.DataFrame([{
        "date": date(2026, 7, 15), "expiry": "2026-07-15", "short_strike": 7400.0,
        "qty": 1, "short_credit_ct": 100.0, "paired": True, "long_strike": 7350.0,
        "width": 50.0, "credit_ct": 50.0, "max_loss_ct": 4950.0, "short_pnl": -350.0,
    }])

    audit = tagging.exit_audit(spreads, positions, {"s": _bull_put_strategy()})
    row = audit.iloc[0]

    assert row["basis"] == "spread"
    assert row["loss_ratio"] == pytest.approx(6.0)   # 300 / 50, not 350 / 100
    assert row["stop_hit"] == True                   # noqa: E712 — a 6x stop fires here


def test_exit_audit_falls_back_to_the_short_leg_for_an_unpaired_trade():
    positions = pd.DataFrame([
        {"contract_key": "S", "date": date(2026, 7, 15),
         "open_ts": datetime(2026, 7, 15, 9, 30), "direction": "short",
         "contracts": 1.0, "total_pnl": -400.0, "credit": 49.35,
         "expiry": "2026-07-15", "strike": 7400.0},
    ])
    spreads = pd.DataFrame([{
        "date": date(2026, 7, 15), "expiry": "2026-07-15", "short_strike": 7400.0,
        "qty": 1, "short_credit_ct": 49.35, "paired": False, "long_strike": None,
        "width": None, "credit_ct": 49.35, "max_loss_ct": None, "short_pnl": -400.0,
    }])

    audit = tagging.exit_audit(spreads, positions, {"s": _bull_put_strategy()})
    row = audit.iloc[0]

    assert row["basis"] == "short_leg"
    assert row["loss_ratio"] == pytest.approx(8.105, abs=0.01)


def test_tool_evaluates_delta_from_market_data(tmp_path):
    """Finding 1 regression: the tool never passed a market frame, so the delta
    was always unverifiable and NO strategy could ever match through the tool —
    every strategy reported 'matched 0 real fills'.

    The band is derived from the seed market data the tool itself loads: the
    fixture's strikes sit far OTM relative to the real 2026-07-15 close, so a
    hand-picked band would be a guess about the market rather than a test of the
    plumbing. The solver's own correctness is pinned by the round-trip test.
    """
    seed = pd.read_csv(REPORT_DATA_DIR / "spx_closes.csv")
    spot = float(seed[pd.to_datetime(seed["activity_date"]).dt.date
                      == date(2026, 7, 15)]["spx_close"].iloc[0])
    t_years = tagging._year_fraction(datetime(2026, 7, 15, 9, 30), date(2026, 7, 15))
    expected_delta = tagging.short_delta_estimate(spot, 7400.0, 7350.0, t_years, 0.287, 0.04)
    assert expected_delta is not None, "the seed data must admit a solution"

    cfg = tmp_path / "strategies.json"
    cfg.write_text(json.dumps({"T": {
        "name": "T", "direction": "bull_put",
        "conditions": [
            {"kind": "short_delta", "enabled": True,
             "params": {"min": expected_delta - 0.005, "max": expected_delta + 0.005}},
            {"kind": "spread_width", "enabled": True, "params": {"min": 40, "max": 60}},
            {"kind": "credit", "enabled": True, "params": {"min": 0.20, "max": 0.60}},
        ],
        "run_days": [0, 1, 2, 3, 4],
    }}), encoding="utf-8")
    qfx = tmp_path / "min.qfx"
    qfx.write_text(MINIMAL_QFX, encoding="latin-1")

    from spx_trade_desk.mcp.server import analyze_strategy_compliance
    result = analyze_strategy_compliance(
        paths=[str(qfx)], strategies_path=str(cfg), offline=True)

    assert "error" not in result, result.get("traceback")
    row = result["condition_matrix"][0]
    assert "short_delta" not in row["unverifiable"]     # the delta was evaluated
    assert result["compliance"][0]["spreads_matching"] == 1


def test_tool_labels_each_spread_with_its_source_file(tmp_path):
    """Two statements in one call must stay attributable."""
    cfg = tmp_path / "strategies.json"
    cfg.write_text(json.dumps({"T": {
        "name": "T", "direction": "bull_put",
        "conditions": [{"kind": "spread_width", "enabled": True,
                        "params": {"min": 40, "max": 60}}],
        "run_days": [0, 1, 2, 3, 4],
    }}), encoding="utf-8")
    first = tmp_path / "jan.qfx"
    second = tmp_path / "feb.qfx"
    for path in (first, second):
        path.write_text(MINIMAL_QFX, encoding="latin-1")

    from spx_trade_desk.mcp.server import analyze_strategy_compliance
    result = analyze_strategy_compliance(
        paths=[str(first), str(second)], strategies_path=str(cfg), offline=True)

    assert "error" not in result, result.get("traceback")
    sources = {row["source"] for row in result["condition_matrix"]}
    assert sources == {"jan.qfx", "feb.qfx"}
    ids = [row["spread_id"] for row in result["condition_matrix"]]
    assert len(ids) == len(set(ids))                    # not all "0"
    assert len(result["compliance"]) == 2               # one row per (source, strategy)


def test_missing_market_data_makes_delta_unverifiable_not_failed():
    strategies = {"s": _bull_put_strategy()}

    matrix = tagging.spread_condition_matrix(_spread(), strategies, market=None)

    row = matrix.iloc[0]
    assert "short_delta" in row["unverifiable"]
    assert row["match"] is None


def test_bear_call_strategy_can_never_match():
    strategy = _bull_put_strategy("bear")
    strategy.direction = "bear_call"

    matrix = tagging.spread_condition_matrix(_spread(), {"bear": strategy})

    assert matrix.iloc[0]["match"] is None
    assert "direction" in matrix.iloc[0]["unverifiable"]


def test_entry_window_is_unverifiable_without_a_timestamp():
    strategy = _bull_put_strategy("windowed")
    strategy.conditions.append(
        Condition(kind="entry_window", params={"start": "10:45", "end": "12:30"}))

    matrix = tagging.spread_condition_matrix(
        _spread(open_ts=None), {"windowed": strategy})

    assert "entry_window" in matrix.iloc[0]["unverifiable"]


def test_entry_window_passes_inside_and_fails_outside_the_band():
    """With no market the delta check is unverifiable, so these assert on the
    failed list — which is where a real violation shows up — not on `match`.
    """
    strategy = _bull_put_strategy("windowed")
    strategy.conditions.append(
        Condition(kind="entry_window", params={"start": "10:45", "end": "12:30"}))

    inside = tagging.spread_condition_matrix(
        _spread(open_ts=datetime(2026, 7, 15, 11, 30)), {"windowed": strategy})
    outside = tagging.spread_condition_matrix(
        _spread(open_ts=datetime(2026, 7, 15, 15, 0)), {"windowed": strategy})

    assert "entry_window" not in inside.iloc[0]["failed"]
    assert "entry_window" in outside.iloc[0]["failed"]


def test_run_days_and_fomc_filters_are_evaluated():
    monday_only = _bull_put_strategy("monday", run_days=[0])
    # 2026-07-15 is a Wednesday
    matrix = tagging.spread_condition_matrix(_spread(), {"monday": monday_only})
    assert "run_days" in matrix.iloc[0]["failed"]


def test_compliance_summary_counts_matches():
    # A wide delta band keeps this test about the summary arithmetic rather than
    # about whatever delta the credit solver happens to return.
    strategies = {"passes": _bull_put_strategy("passes", delta=(0.0, 0.5))}
    market = pd.DataFrame([{"activity_date": date(2026, 7, 15),
                            "spx_close": 7405.0, "vix_close": 16.0}])
    matrix = tagging.spread_condition_matrix(_spread(), strategies, market=market)

    summary = tagging.strategy_compliance_summary(matrix, strategies)

    row = summary.set_index("strategy").loc["passes"]
    assert row["spreads_considered"] == 1
    assert row["spreads_matching"] == 1
    assert row["spreads_failing"] == 0
    assert row["failure_counts"] == {}


def test_config_fingerprint_is_stable_and_describes_the_file(tmp_path):
    path = tmp_path / "strategies.json"
    path.write_text(json.dumps({"A": {"name": "A", "direction": "bull_put"}}), encoding="utf-8")

    first = tagging.strategy_config_fingerprint(path)
    second = tagging.strategy_config_fingerprint(path)

    assert first["sha256"] == second["sha256"]
    assert first["path"].endswith("strategies.json")
    assert "mtime" in first


def test_config_fingerprint_on_a_missing_file_is_empty():
    assert tagging.strategy_config_fingerprint("does-not-exist.json")["sha256"] is None


def test_attach_entry_times_restores_the_intraday_timestamp():
    """reconstruct_spreads drops open_ts, which would leave entry_window
    permanently unverifiable even for a QFX that carries the timestamp. The
    timestamp is joined back from load_positions on (date, expiry, strike).
    """
    spreads = _spread().drop(columns=["open_ts"])
    positions = pd.DataFrame([{
        "date": date(2026, 7, 15), "expiry": "2026-07-15", "strike": 7400.0,
        "direction": "short", "open_ts": datetime(2026, 7, 15, 9, 30),
    }])

    enriched = tagging.attach_entry_times(spreads, positions)
    assert enriched.iloc[0]["open_ts"] == datetime(2026, 7, 15, 9, 30)

    strategy = _bull_put_strategy("windowed")
    strategy.conditions.append(
        Condition(kind="entry_window", params={"start": "10:45", "end": "12:30"}))
    matrix = tagging.spread_condition_matrix(enriched, {"windowed": strategy})

    # 09:30 is before the window, so the condition is now decidable — and fails.
    assert "entry_window" in matrix.iloc[0]["failed"]


def test_attach_entry_times_is_a_no_op_without_positions():
    spreads = _spread().drop(columns=["open_ts"])
    assert tagging.attach_entry_times(spreads, pd.DataFrame()).equals(spreads)


def test_exit_audit_flags_a_trade_that_lost_more_than_its_stop(qfx_losing_positions):
    positions, spreads = qfx_losing_positions
    audit = tagging.exit_audit(spreads, positions, {"s": _bull_put_strategy()})

    assert "loss_ratio" in audit.columns
    assert "stop_multiple" in audit.columns
    assert "stop_hit" in audit.columns
    assert "basis" in audit.columns
    # unpaired here, so the ratio is measured against the short leg's own premium:
    # -400 on a 49.35 credit is 8.1x, which trips a 6x stop
    assert audit["loss_ratio"].iloc[0] == pytest.approx(8.105, abs=0.01)
    assert audit["stop_hit"].all()


# --- config fail-safety (Review Focus item 4) ---------------------------

@pytest.fixture
def corrupted_config(tmp_path):
    path = tmp_path / "strategies.json"
    path.write_text("{not valid json", encoding="utf-8")
    return str(path)


def test_load_strategies_is_fail_safe_on_a_corrupt_config(corrupted_config):
    from spx_trade_desk.strategy.store import load_strategies
    assert load_strategies(corrupted_config) == {}


def test_compliance_on_a_corrupt_config_reports_an_error_not_a_traceback(corrupted_config):
    from spx_trade_desk.mcp.server import analyze_strategy_compliance
    result = analyze_strategy_compliance(
        file_contents=[{"name": "x.qfx", "data_text": "<OFX></OFX>"}],
        strategies_path=corrupted_config,
    )
    assert "error" in result


# --- tool serialization (regression) ------------------------------------

MINIMAL_QFX = (
    "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
    "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
    "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY>"
    "</STATUS><DTSOFDT>20260731120000.000[-5:EST]</DTSOFDT><LANGUAGE>ENG</LANGUAGE>"
    "<FI><ORG>BROKER</ORG><FID>123</FID></FI></SONRS></SIGNONMSGSRSV1>"
    "<INVSTMTMSGSRSV1><INVSTMTTRNRS><TRNUID>1</TRNUID><STATUS><CODE>0</CODE>"
    "<SEVERITY>INFO</SEVERITY></STATUS><INVSTMTRS><DTASOF>20260731120000.000[-5:EST]"
    "</DTASOF><CURDEF>USD</CURDEF><INVACCTFROM><BROKERID>BROKER</BROKERID>"
    "<ACCTID>U123456</ACCTID></INVACCTFROM><INVTRANLIST><DTSTART>20260701</DTSTART>"
    "<DTEND>20260731</DTEND>"
    "<SELLOPT><INVSELL><INVTRAN><DTTRADE>20260715093000.000[-5:EST]</DTTRADE>"
    "<DTSTAMP>20260715093500.000[-5:EST]</DTSTAMP><MEMO>SPXW 15JUL26 7400 P</MEMO>"
    "</INVTRAN><SECID><UNIQUEID>00001</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE>"
    "</SECID><UNITS>-1</UNITS><UNITPRICE>0.50</UNITPRICE><COMMISSION>0.65</COMMISSION>"
    "<TOTAL>49.35</TOTAL></INVSELL></SELLOPT>"
    "<BUYOPT><INVBUY><INVTRAN><DTTRADE>20260715093001.000[-5:EST]</DTTRADE>"
    "<DTSTAMP>20260715093501.000[-5:EST]</DTSTAMP><MEMO>SPXW 15JUL26 7350 P</MEMO>"
    "</INVTRAN><SECID><UNIQUEID>00002</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE>"
    "</SECID><UNITS>1</UNITS><UNITPRICE>0.20</UNITPRICE><COMMISSION>0.65</COMMISSION>"
    "<TOTAL>-20.65</TOTAL></INVBUY></BUYOPT>"
    "<BUYOPT><INVBUY><INVTRAN><DTTRADE>20260715160000.000[-5:EST]</DTTRADE>"
    "<DTSTAMP>20260715160500.000[-5:EST]</DTSTAMP><MEMO>SPXW 15JUL26 7400 P expiry</MEMO>"
    "</INVTRAN><SECID><UNIQUEID>00001</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE>"
    "</SECID><UNITS>1</UNITS><UNITPRICE>0.00</UNITPRICE><COMMISSION>0.00</COMMISSION>"
    "<TOTAL>0.00</TOTAL></INVBUY></BUYOPT>"
    "<SELLOPT><INVSELL><INVTRAN><DTTRADE>20260715160001.000[-5:EST]</DTTRADE>"
    "<DTSTAMP>20260715160501.000[-5:EST]</DTSTAMP><MEMO>SPXW 15JUL26 7350 P expiry</MEMO>"
    "</INVTRAN><SECID><UNIQUEID>00002</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE>"
    "</SECID><UNITS>-1</UNITS><UNITPRICE>0.00</UNITPRICE><COMMISSION>0.00</COMMISSION>"
    "<TOTAL>0.00</TOTAL></INVSELL></SELLOPT>"
    "</INVTRANLIST>"
    "<SECLIST>"
    "<OPTINFO><SECINFO><SECID><UNIQUEID>00001</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE>"
    "</SECID><TICKER>SPXW  260715P07400000</TICKER><SECNAME>SPXW 15JUL26 7400 P</SECNAME>"
    "</SECINFO><OPTYPE>PUT</OPTYPE><STRIKEPRICE>7400</STRIKEPRICE><DTEXPIRE>20260715"
    "</DTEXPIRE><SHPERCTRCT>100</SHPERCTRCT></OPTINFO>"
    "<OPTINFO><SECINFO><SECID><UNIQUEID>00002</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE>"
    "</SECID><TICKER>SPXW  260715P07350000</TICKER><SECNAME>SPXW 15JUL26 7350 P</SECNAME>"
    "</SECINFO><OPTYPE>PUT</OPTYPE><STRIKEPRICE>7350</STRIKEPRICE><DTEXPIRE>20260715"
    "</DTEXPIRE><SHPERCTRCT>100</SHPERCTRCT></OPTINFO>"
    "</SECLIST>"
    "<INVBAL><AVAILCASH>50400.00</AVAILCASH><BAL><NAME>stock</NAME><VALUE>0.00</VALUE>"
    "</BAL></INVBAL></INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1></OFX>"
)


def test_tool_serializes_list_valued_matrix_columns(tmp_path):
    """Regression: df_to_records cleans each cell with pd.isna, and pd.isna on a
    list returns an ARRAY — so list-valued columns (`failed`, `unverifiable`)
    must be flattened to scalars before serialization, or the tool errors out
    with "truth value of an array ... is ambiguous" instead of returning data.
    """
    cfg = tmp_path / "strategies.json"
    cfg.write_text(json.dumps({
        "T": {
            "name": "T", "direction": "bull_put",
            "conditions": [{"kind": "spread_width", "enabled": True,
                            "params": {"min": 40, "max": 60}}],
            "run_days": [0, 1, 2, 3, 4],
        },
    }), encoding="utf-8")
    qfx = tmp_path / "min.qfx"
    qfx.write_text(MINIMAL_QFX, encoding="latin-1")

    from spx_trade_desk.mcp.server import analyze_strategy_compliance
    result = analyze_strategy_compliance(
        paths=[str(qfx)], strategies_path=str(cfg), offline=True)

    assert "error" not in result, result.get("traceback")
    row = result["condition_matrix"][0]
    assert row["strategy"] == "T"
    assert isinstance(row["failed"], str)
    assert isinstance(row["unverifiable"], str)
    assert isinstance(result["compliance"][0]["failure_counts"], dict)
