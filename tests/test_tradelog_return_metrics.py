"""Focused tests for the ported TWR / MWR return metrics.

Pure arithmetic on hand-built periods, so the expected values are exact:
chaining +10% then +5% gives 15.5%, and a single 1000 -> 1100 year is 10%.
"""
from __future__ import annotations

from datetime import date

import pytest

from spx_trade_desk.tradelog.domain.return_metrics import (
    TwrPeriod,
    annualize,
    compute_mwr,
    compute_twr,
)


def test_annualize_over_one_year_is_identity():
    assert annualize(0.10, 365) == pytest.approx(0.10, abs=1e-9)


def test_annualize_returns_none_without_inputs():
    assert annualize(None, 365) is None
    assert annualize(0.10, 0) is None


def test_compute_twr_chains_monthly_returns():
    periods = [
        TwrPeriod(period_start=date(2026, 1, 1), beginning_value=1000.0,
                  ending_value=1100.0, period_end=date(2026, 1, 31)),
        TwrPeriod(period_start=date(2026, 2, 1), beginning_value=1100.0,
                  ending_value=1155.0, period_end=date(2026, 2, 28)),
    ]

    result = compute_twr(periods)

    assert result["twr"] == pytest.approx(0.155, abs=1e-9)   # 1.10 * 1.05 - 1
    assert result["period_count"] == 2
    assert result["periods"][0]["monthly_return"] == pytest.approx(0.10)
    assert result["periods"][1]["monthly_return"] == pytest.approx(0.05)
    assert result["periods"][1]["cumulative_return"] == pytest.approx(0.155)
    assert result["warnings"] == []


def test_compute_twr_skips_invalid_periods_with_a_warning():
    """Documented contract: a non-positive beginning or ending value makes the
    period invalid, so it is skipped with a warning rather than counted."""
    result = compute_twr([
        TwrPeriod(period_start=date(2026, 1, 1), beginning_value=0.0,
                  ending_value=100.0, period_end=date(2026, 1, 31)),
    ])

    assert result["periods"] == []
    assert result["twr"] is None
    assert result["warnings"]


def test_compute_mwr_on_a_single_period_without_flows():
    result = compute_mwr(
        initial=1000.0,
        ending=1100.0,
        start=date(2025, 1, 1),
        end=date(2026, 1, 1),
    )

    assert result["converged"] is True
    assert result["mwr"] == pytest.approx(0.10, abs=1e-6)
    assert result["annualized"] == pytest.approx(0.10, abs=1e-6)
    assert result["total_days"] == 365


def test_compute_mwr_with_an_external_flow():
    """+1000 at the start and +500 mid-year, ending at 1650, is a ~12.5% money-weighted year."""
    result = compute_mwr(
        initial=1000.0,
        ending=1650.0,
        start=date(2025, 1, 1),
        end=date(2026, 1, 1),
        cash_flows=[(date(2025, 7, 1), 500.0)],
    )

    assert result["converged"] is True
    assert 0.0 < result["mwr"] < 0.15
    assert len(result["cf_table"]) == 1
