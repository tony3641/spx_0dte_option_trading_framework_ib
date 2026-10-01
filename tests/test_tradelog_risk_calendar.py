"""Focused tests for the ported risk metrics and the extracted calendar module.

The calendar test that matters most is the subprocess isolation check: the whole
point of moving this function out of `src/ui/tab_calendar.py` is that importing
it must not pull in streamlit or plotly.
"""
from __future__ import annotations

import subprocess
import sys

import pandas as pd
import pytest

from spx_trade_desk.tradelog.domain.calendar import _build_calendar_matrix, _fmt_signed
from spx_trade_desk.tradelog.domain.risk_metrics import calculate_risk_metrics


def _daily(rows):
    return pd.DataFrame(
        rows,
        columns=["activity_date", "realized_pnl", "commission_spent",
                 "option_contracts_traded", "trade_count"],
    )


def test_calendar_import_pulls_in_neither_streamlit_nor_plotly():
    code = (
        "import sys;"
        "import spx_trade_desk.tradelog.domain.calendar;"
        "assert 'streamlit' not in sys.modules, 'streamlit was imported';"
        "assert 'plotly' not in sys.modules, 'plotly was imported'"
    )
    completed = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_fmt_signed_formats_with_an_explicit_sign():
    # Non-tie values on purpose: 1234.5 would hit Python's round-half-to-even.
    assert _fmt_signed(1234.6, 0) == "+1235"
    assert _fmt_signed(-1234.6, 0) == "-1235"
    assert _fmt_signed(0.0, 0) == "0"
    assert _fmt_signed(float("nan")) == ""


def test_calendar_matrix_returns_six_frames_and_week_labels():
    daily = _daily([
        {"activity_date": pd.Timestamp("2026-01-15").date(), "realized_pnl": 250.0,
         "commission_spent": 2.0, "option_contracts_traded": 2, "trade_count": 1},
        {"activity_date": pd.Timestamp("2026-01-16").date(), "realized_pnl": -100.0,
         "commission_spent": 2.0, "option_contracts_traded": 2, "trade_count": 1},
    ])

    # The adapter consumes only five of the six returns, so the fourth frame is
    # unnamed here on purpose — assert its shape, not its meaning.
    pnl_matrix, commission_matrix, date_matrix, fourth_matrix, week_labels, weekly_text = (
        _build_calendar_matrix(daily)
    )

    assert len(week_labels) == 1
    assert pnl_matrix.shape == (1, 7)
    assert commission_matrix.shape == (1, 7)
    assert date_matrix.shape == (1, 7)
    assert fourth_matrix.shape == (1, 7)
    assert weekly_text.shape[0] == 1
    # Thursday of that week is day index 3 (Mon=0)
    assert pnl_matrix.iloc[0, 3] == pytest.approx(250.0)
    assert pnl_matrix.iloc[0, 4] == pytest.approx(-100.0)


def test_calendar_matrix_on_empty_input_is_empty():
    pnl_matrix, _c, _d, _k, week_labels, _w = _build_calendar_matrix(_daily([]))
    assert pnl_matrix.empty
    assert week_labels == []


def test_risk_metrics_returns_empty_dict_for_an_empty_view():
    assert calculate_risk_metrics(_daily([]), 50000.0, 0.04) == {}


def test_risk_metrics_reports_extremes_and_the_expected_keys():
    daily = _daily([
        {"activity_date": pd.Timestamp("2026-01-15").date(), "realized_pnl": 100.0,
         "commission_spent": 1.0, "option_contracts_traded": 2, "trade_count": 1},
        {"activity_date": pd.Timestamp("2026-01-16").date(), "realized_pnl": -50.0,
         "commission_spent": 1.0, "option_contracts_traded": 2, "trade_count": 1},
    ])

    metrics = calculate_risk_metrics(daily, 50000.0, 0.04)

    for key in ("period_return", "sharpe", "sortino", "max_gain", "max_loss",
                "net_ev", "commission_drag"):
        assert key in metrics
    assert metrics["max_gain"] == pytest.approx(100.0)
    assert metrics["max_loss"] == pytest.approx(-50.0)
