"""Unit tests for adapter serializers."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from spx_trade_desk.mcp.adapter import (
    serialize_float,
    friendly_date,
    df_to_records,
    pnl_result_to_summary,
    pnl_result_to_daily_series,
    pnl_result_to_top_contracts,
    metrics_to_dict,
    parsed_option_to_dict,
    balance_to_dict,
    _safe_value,
)
from spx_trade_desk.tradelog.domain.parse_option_symbol import ParsedOption
from spx_trade_desk.tradelog.domain.pnl_engine import PnlResult, build_realized_pnl
from spx_trade_desk.tradelog.io.load_qfx import InvBalance
from spx_trade_desk.tradelog.io.load_etrade_pdf import EtradeBalance


# -----------------------------------------------------------------------
# serialize_float
# -----------------------------------------------------------------------

class TestSerializeFloat:
    def test_normal(self):
        assert serialize_float(3.14) == 3.14
        assert serialize_float(0.0) == 0.0
        assert serialize_float(-1.5) == -1.5

    def test_nan(self):
        assert serialize_float(float("nan")) is None

    def test_inf(self):
        assert serialize_float(float("inf")) is None
        assert serialize_float(float("-inf")) is None

    def test_none(self):
        assert serialize_float(None) is None

    def test_numpy_nan(self):
        assert serialize_float(np.nan) is None

    def test_string(self):
        assert serialize_float("3.14") == 3.14
        assert serialize_float("not a number") is None


# -----------------------------------------------------------------------
# friendly_date
# -----------------------------------------------------------------------

class TestFriendlyDate:
    def test_date(self):
        assert friendly_date(date(2026, 1, 15)) == "2026-01-15"

    def test_none(self):
        assert friendly_date(None) is None

    def test_string_passthrough(self):
        assert friendly_date("2026-01-15") == "2026-01-15"

    def test_pandas_timestamp(self):
        ts = pd.Timestamp("2026-01-15")
        assert friendly_date(ts) == "2026-01-15"


# -----------------------------------------------------------------------
# _safe_value
# -----------------------------------------------------------------------

class TestSafeValue:
    def test_scalars(self):
        assert _safe_value(42) == 42
        assert _safe_value("hello") == "hello"
        assert _safe_value(True) is True
        assert _safe_value(None) is None

    def test_float_nan(self):
        assert _safe_value(float("nan")) is None

    def test_dict(self):
        assert _safe_value({"a": float("nan"), "b": 1.5}) == {"a": None, "b": 1.5}

    def test_list(self):
        assert _safe_value([1.0, float("inf"), None]) == [1.0, None, None]


# -----------------------------------------------------------------------
# df_to_records
# -----------------------------------------------------------------------

class TestDfToRecords:
    def test_empty(self):
        df = pd.DataFrame()
        assert df_to_records(df) == []

    def test_basic(self):
        df = pd.DataFrame({
            "name": ["Alice", "Bob"],
            "value": [1.5, np.nan],
            "count": [3, 4],
        })
        records = df_to_records(df)
        assert len(records) == 2
        assert records[0]["name"] == "Alice"
        assert records[0]["value"] == 1.5
        assert records[1]["value"] is None  # NaN → None
        assert records[0]["count"] == 3

    def test_date_columns(self):
        df = pd.DataFrame({
            "activity_date": [date(2026, 1, 15), date(2026, 1, 16)],
            "value": [100.0, 200.0],
        })
        records = df_to_records(df)
        assert records[0]["activity_date"] == "2026-01-15"
        assert records[1]["activity_date"] == "2026-01-16"


# -----------------------------------------------------------------------
# pnl_result_to_summary
# -----------------------------------------------------------------------

class TestPnlResultToSummary:
    def _make_daily(self) -> pd.DataFrame:
        return pd.DataFrame({
            "activity_date": [date(2026, 1, 15), date(2026, 1, 16)],
            "realized_pnl": [100.0, -50.0],
            "commission_spent": [2.0, 1.5],
            "option_contracts_traded": [5, 3],
            "trade_count": [2, 1],
            "expire_inferred_count": [0, 1],
            "expire_inferred_pnl": [0.0, 50.0],
            "cumulative_pnl": [100.0, 50.0],
        })

    def test_basic(self):
        daily = self._make_daily()
        enriched = pd.DataFrame({"dummy": [1, 2, 3]})
        result = PnlResult(enriched_rows=enriched, daily=daily)
        summary = pnl_result_to_summary(result)
        assert summary["total_realized_pnl"] == 50.0
        assert summary["total_commission"] == 3.5
        assert summary["total_trades"] == 3
        assert summary["cumulative_pnl_today"] == 50.0
        assert summary["date_range"]["start"] == "2026-01-15"
        assert summary["date_range"]["end"] == "2026-01-16"

    def test_with_capital(self):
        daily = self._make_daily()
        enriched = pd.DataFrame({"dummy": [1, 2, 3]})
        result = PnlResult(enriched_rows=enriched, daily=daily)
        summary = pnl_result_to_summary(result, initial_capital=10000.0)
        assert summary["total_return_pct"] == 0.005  # 50 / 10000


# -----------------------------------------------------------------------
# pnl_result_to_daily_series
# -----------------------------------------------------------------------

class TestPnlResultToDailySeries:
    def test_basic(self):
        daily = pd.DataFrame({
            "activity_date": [date(2026, 1, 15)],
            "realized_pnl": [100.0],
            "commission_spent": [2.0],
            "option_contracts_traded": [5],
            "trade_count": [2],
            "expire_inferred_count": [0],
            "expire_inferred_pnl": [0.0],
            "cumulative_pnl": [100.0],
        })
        enriched = pd.DataFrame({"dummy": [1]})
        result = PnlResult(enriched_rows=enriched, daily=daily)
        series = pnl_result_to_daily_series(result)
        assert len(series) == 1
        assert series[0]["activity_date"] == "2026-01-15"
        assert series[0]["realized_pnl"] == 100.0


# -----------------------------------------------------------------------
# metrics_to_dict
# -----------------------------------------------------------------------

class TestMetricsToDict:
    def test_empty(self):
        assert metrics_to_dict({}) == {}

    def test_full(self):
        metrics = {
            "period_return": 0.05,
            "sharpe": 1.5,
            "sortino": 2.0,
            "std_daily": 0.01,
            "positive_cycles": 20,
            "negative_cycles": 10,
            "max_gain": 500.0,
            "max_loss": -300.0,
            "commission_drag": 0.02,
            "max_recovery_days": 5,
            "ongoing_recovery_days": None,
            "net_ev": 25.0,
            "spx_corr": 0.3,
            "spx_beta": 0.5,
            "spx_alpha": 0.08,
            "spx_period_return": 0.03,
            "return_delta_vs_spx": 0.02,
            "spx_overlap_days": 30,
            "vix_corr": -0.2,
            "vix_beta": -0.001,
            "vix_overlap_days": 30,
            "vix_regime_table": pd.DataFrame(),
        }
        result = metrics_to_dict(metrics)
        assert result["risk_adjusted"]["sharpe_ratio"] == 1.5
        assert result["spx_benchmark"]["spx_correlation"] == 0.3
        assert result["vix_analysis"]["regime_table"] == []

    def test_nan_handling(self):
        metrics = {
            "period_return": np.nan,
            "sharpe": float("nan"),
            "sortino": None,
            "std_daily": np.nan,
            "positive_cycles": 0,
            "negative_cycles": 0,
            "max_gain": np.nan,
            "max_loss": np.nan,
            "commission_drag": np.nan,
            "max_recovery_days": 0,
            "ongoing_recovery_days": None,
            "net_ev": np.nan,
            "spx_corr": np.nan,
            "spx_beta": np.nan,
            "spx_alpha": np.nan,
            "spx_period_return": np.nan,
            "return_delta_vs_spx": np.nan,
            "spx_overlap_days": 0,
            "vix_corr": np.nan,
            "vix_beta": np.nan,
            "vix_overlap_days": 0,
            "vix_regime_table": pd.DataFrame(),
        }
        result = metrics_to_dict(metrics)
        # All NaN values should be None
        assert result["risk_adjusted"]["sharpe_ratio"] is None
        assert result["spx_benchmark"]["spx_correlation"] is None
        assert result["daily_extremes"]["max_gain"] is None


# -----------------------------------------------------------------------
# parsed_option_to_dict
# -----------------------------------------------------------------------

class TestParsedOptionToDict:
    def test_basic(self):
        po = ParsedOption(
            underlying="SPXW",
            expiry_date=date(2026, 2, 2),
            right="P",
            strike=6940.0,
            contract_key="SPXW|2026-02-02|P|6940.000",
        )
        d = parsed_option_to_dict(po)
        assert d["underlying"] == "SPXW"
        assert d["expiry_date"] == "2026-02-02"
        assert d["right"] == "P"
        assert d["strike"] == 6940.0


# -----------------------------------------------------------------------
# balance_to_dict
# -----------------------------------------------------------------------

class TestBalanceToDict:
    def test_inv_balance(self):
        bal = InvBalance(
            cash=5601.49, stock_value=39379.40,
            account_id="U99999999", as_of=date(2026, 7, 27),
            dt_start=date(2025, 12, 31), dt_end=date(2026, 7, 27),
        )
        d = balance_to_dict(bal)
        assert d["type"] == "inv_balance"
        assert d["cash"] == 5601.49
        assert d["stock_value"] == 39379.40
        assert d["total"] == 44980.89
        assert d["account_id"] == "U99999999"
        assert d["as_of"] == "2026-07-27"
        assert d["dt_start"] == "2025-12-31"
        assert d["dt_end"] == "2026-07-27"

    def test_etrade_balance(self):
        bal = EtradeBalance(
            account_id="999-999999-999",
            period_start=date(2026, 2, 1),
            beginning_value=25000.0,
            ending_value=26500.0,
            cash=14746.86,
            stock_value=24805.63,
        )
        d = balance_to_dict(bal)
        assert d["type"] == "etrade_balance"
        assert d["account_id"] == "999-999999-999"
        assert d["period_start"] == "2026-02-01"
        assert d["beginning_value"] == 25000.0
