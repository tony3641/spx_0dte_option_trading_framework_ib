"""Tests for E*Trade CSV loading, format detection, strategy filtering, and
SPX/SPXW-only TWR / MWR return metrics."""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
import pytest

from spx_trade_desk.mcp.server import (
    _build_account_capital,
    _load_and_merge,
    compute_account_return,
    compute_daily_pnl,
    get_transaction_summary,
)
from spx_trade_desk.tradelog.domain.return_metrics import (
    TwrPeriod,
    annualize,
    compute_mwr,
    compute_strategy_twr,
    compute_twr,
    statement_external_flows,
)
from spx_trade_desk.tradelog.domain.strategy_filter import (
    filter_strategy_rows,
    is_etrade_account,
    is_strategy_symbol,
)
from spx_trade_desk.tradelog.io.format_detect import detect_csv_format
from spx_trade_desk.tradelog.io.load_etrade_csv import load_transactions_etrade_csv
from spx_trade_desk.tradelog.io.load_etrade_pdf import EtradeBalance

# ---------------------------------------------------------------------------
# Synthetic E*Trade trades CSV fixture
# ---------------------------------------------------------------------------

ETRADE_CSV = (
    "Trade Date,Order Type,Security,Cusip,Transaction Description,Quantity,"
    "Executed Price,Commission,Net Amount\n"
    "1/14/2026,Sell To Open,SPXW JAN 14 '26 $6865 PUT,,"
    "PUT  SPXW   01/14/26  6865.000,2,0.33,1.03,63.95\n"
    "1/14/2026,Buy Open,SPXW JAN 14 '26 $6815 PUT,,"
    "PUT  SPXW   01/14/26  6815.000,2,0.03,1.03,8.05\n"
    "1/15/2026,Option Expire,SPXW JAN 14 '26 $6865 PUT,,"
    "PUT  SPXW   01/14/26  6865.000,2,0,N/A,0\n"
    "1/22/2026,Buy Open,SPXW JAN 22 '26 $6820 CALL,,"
    "CALL  SPXW   01/22/26  6820.000,1,0.2,0.51,21.02\n"
    "1/20/2026,Buy,TSLA,88160R101,TESLA INC UNSOLICITED TRADE,2,420,0.0000,840\n"
    "2/4/2026,Sell,QCOM,747525103,QUALCOMM INC UNSOLICITED TRADE,20,145,4.95,2895.05\n"
)

IBKR_CSV = (
    "Date,Account,Description,Transaction Type,Symbol,Quantity,Price,"
    "Price Currency, Gross Amount,Commission,Net Amount\n"
    "2026-01-15,U123456,SPXW 01/15/26 P6700,Sell,SPXW  260115P06700000,"
    "1,5.00,USD,500.00,0.65,499.35\n"
)


@pytest.fixture
def etrade_file_contents() -> list[dict[str, str]]:
    return [{"name": "tradesdownload.csv", "data_text": ETRADE_CSV}]


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

class TestDetectCsvFormat:
    def test_etrade(self):
        assert detect_csv_format(ETRADE_CSV) == "etrade"

    def test_ibkr(self):
        assert detect_csv_format(IBKR_CSV) == "ibkr"

    def test_unknown(self):
        assert detect_csv_format("foo,bar\n1,2\n") == "unknown"

    def test_ibkr_with_preamble(self):
        # IBKR exports often have boilerplate rows above the real header.
        with_bom_and_preamble = (
            "﻿Some preamble line\nAnother line\n\n" + IBKR_CSV
        )
        assert detect_csv_format(with_bom_and_preamble) == "ibkr"

    def test_etrade_leading_spaces(self):
        spaced = "\n".join(
            "  " + line if line else line for line in ETRADE_CSV.splitlines()
        )
        assert detect_csv_format(spaced) == "etrade"


# ---------------------------------------------------------------------------
# E*Trade CSV loader
# ---------------------------------------------------------------------------

class TestLoadEtradeCsv:
    def test_parse(self):
        df = load_transactions_etrade_csv(io.StringIO(ETRADE_CSV))
        assert len(df) == 5  # Option Expire row skipped
        assert set(df["account_id"]) == {"E*Trade"}

        sell = df[df["symbol"] == "SPXW  260114P06865000"].iloc[0]
        assert sell["transaction_type"] == "Sell"
        assert sell["quantity"] == -2.0
        assert sell["net_amount"] == 63.95
        assert sell["commission"] == pytest.approx(2.05)

        buy = df[df["symbol"] == "SPXW  260114P06815000"].iloc[0]
        assert buy["transaction_type"] == "Buy"
        assert buy["quantity"] == 2.0
        assert buy["net_amount"] == -8.05

        call = df[df["symbol"] == "SPXW  260122C06820000"].iloc[0]
        assert call["transaction_type"] == "Buy"
        assert call["net_amount"] == -21.02
        assert call["activity_date"] == date(2026, 1, 22)

    def test_stock_rows_kept(self):
        df = load_transactions_etrade_csv(io.StringIO(ETRADE_CSV))
        tsla = df[df["symbol"] == "TSLA"].iloc[0]
        assert tsla["transaction_type"] == "Buy"
        assert tsla["quantity"] == 2.0
        assert tsla["net_amount"] == -840.0
        assert tsla["commission"] == 0.0
        qcom = df[df["symbol"] == "QCOM"].iloc[0]
        assert qcom["transaction_type"] == "Sell"
        assert qcom["quantity"] == -20.0
        assert qcom["net_amount"] == 2895.05
        assert qcom["commission"] == pytest.approx(4.95)

    def test_account_override(self):
        df = load_transactions_etrade_csv(
            io.StringIO(ETRADE_CSV), account_id="999-999999-999"
        )
        assert set(df["account_id"]) == {"999-999999-999"}

    def test_from_path(self, tmp_path):
        p = tmp_path / "trades.csv"
        p.write_text(ETRADE_CSV, encoding="utf-8")
        df = load_transactions_etrade_csv(str(p))
        assert len(df) == 5

    def test_from_bytes(self):
        df = load_transactions_etrade_csv(ETRADE_CSV.encode("utf-8"))
        assert len(df) == 5

    def test_unknown_order_type_zero_pnl(self):
        csv_text = (
            "Trade Date,Order Type,Security,Cusip,Transaction Description,Quantity,"
            "Executed Price,Commission,Net Amount\n"
            "1/14/2026,Mystery Order,SPXW JAN 14 '26 $6865 PUT,,"
            "PUT  SPXW   01/14/26  6865.000,2,0.33,1.03,63.95\n"
        )
        df = load_transactions_etrade_csv(io.StringIO(csv_text))
        assert len(df) == 1
        assert df.iloc[0]["transaction_type"] == "Mystery Order"
        assert df.iloc[0]["net_amount"] == 0.0


# ---------------------------------------------------------------------------
# Strategy filter
# ---------------------------------------------------------------------------

class TestStrategyFilter:
    def test_is_etrade_account(self):
        assert is_etrade_account("E*Trade")
        assert is_etrade_account("999-999999-999")
        assert not is_etrade_account("U123456")
        assert not is_etrade_account(None)

    def test_is_strategy_symbol(self):
        assert is_strategy_symbol("SPXW  260114P06865000")
        assert is_strategy_symbol("SPX  260114C06865000")
        assert not is_strategy_symbol("TSLA")
        assert not is_strategy_symbol(None)

    def test_filter(self):
        df = pd.DataFrame({
            "account_id": ["E*Trade", "E*Trade", "999-999999-999", "U123456"],
            "symbol": ["SPXW  260114P06865000", "TSLA",
                       "SPXW  260115P06700000", "TSLA"],
        })
        out = filter_strategy_rows(df)
        # SPX rows kept for every account (E*Trade, real E*Trade, IBKR);
        # TSLA stock rows dropped for every account.
        assert len(out) == 2
        assert set(out["account_id"]) == {"E*Trade", "999-999999-999"}
        assert (out["symbol"].map(is_strategy_symbol)).all()


# ---------------------------------------------------------------------------
# Return metrics
# ---------------------------------------------------------------------------

class TestReturnMetrics:
    def test_compute_strategy_twr(self):
        res = compute_strategy_twr(
            100.0,
            [(date(2026, 1, 15), 10.0)],
            [(date(2026, 1, 20), -30.0)],
        )
        assert res["twr"] == pytest.approx(0.10)
        assert res["period_count"] == 1
        assert res["periods"][0]["external_flows"] == -30.0

    def test_compute_strategy_twr_with_statements(self):
        # With statements, the monthly value tracks the account's actual value
        # (beginning/ending), and the return is SPX/SPXW PnL ÷ value at start.
        periods = [
            TwrPeriod(date(2026, 1, 1), 100000.0, 105000.0, date(2026, 1, 31)),
            TwrPeriod(date(2026, 2, 1), 105000.0, 112000.0, date(2026, 2, 28)),
        ]
        res = compute_strategy_twr(
            100000.0,
            [(date(2026, 1, 15), 5000.0)],
            [],
            statement_periods=periods,
        )
        assert res["periods"][0]["beginning_value"] == 100000.0  # initial
        assert res["periods"][0]["ending_value"] == 105000.0     # statement ending
        assert res["periods"][0]["monthly_return"] == pytest.approx(0.05)
        # February rolls from January's ending (deposits carry forward).
        assert res["periods"][1]["beginning_value"] == 105000.0
        assert res["periods"][1]["ending_value"] == 112000.0
        assert res["periods"][1]["monthly_return"] == pytest.approx(0.0)
        assert res["twr"] == pytest.approx(0.05)

    def test_statement_external_flows(self):
        periods = [
            TwrPeriod(date(2026, 1, 1), 100000.0, 105000.0, date(2026, 1, 31)),
        ]
        flows = statement_external_flows(
            100000.0, [(date(2026, 1, 15), 5000.0)], periods
        )
        # ending − start − SPX PnL = 105000 − 100000 − 5000 = 0
        assert flows == [(date(2026, 1, 31), 0.0)]

        # A deposit makes the flow positive.
        periods2 = [
            TwrPeriod(date(2026, 1, 1), 100000.0, 120000.0, date(2026, 1, 31)),
        ]
        flows2 = statement_external_flows(
            100000.0, [(date(2026, 1, 15), 5000.0)], periods2
        )
        assert flows2[0][1] == pytest.approx(15000.0)

    def test_compute_twr_single(self):
        res = compute_twr([TwrPeriod(date(2026, 1, 1), 100, 105, date(2026, 2, 1))])
        assert res["twr"] == pytest.approx(0.05)

    def test_compute_twr_chained(self):
        res = compute_twr([
            TwrPeriod(date(2026, 1, 1), 100, 105, date(2026, 2, 1)),
            TwrPeriod(date(2026, 2, 1), 105, 110, date(2026, 3, 1)),
        ])
        assert res["twr"] == pytest.approx(0.10)

    def test_compute_twr_invalid_period(self):
        res = compute_twr([TwrPeriod(date(2026, 1, 1), 0, 105, date(2026, 2, 1))])
        assert res["twr"] is None
        assert res["warnings"]

    def test_compute_mwr_no_flows(self):
        res = compute_mwr(100, 110, date(2026, 1, 1), date(2027, 1, 1))
        assert res["annualized"] == pytest.approx(0.10)
        assert res["mwr"] == pytest.approx(0.10)
        assert res["converged"]

    def test_compute_mwr_with_deposit(self):
        res = compute_mwr(
            100, 168, date(2026, 1, 1), date(2027, 1, 1),
            cash_flows=[(date(2026, 7, 1), 50.0)],
        )
        assert res["converged"]
        assert res["annualized"] == pytest.approx(0.145, abs=0.005)

    def test_compute_mwr_bad_capital(self):
        res = compute_mwr(100, 0, date(2026, 1, 1), date(2027, 1, 1))
        assert not res["converged"]
        assert res["mwr"] is None
        assert res["warning"]

    def test_annualize(self):
        assert annualize(0.10, 365) == pytest.approx(0.10)
        assert annualize(None, 30) is None
        assert annualize(0.10, 0) is None


# ---------------------------------------------------------------------------
# MCP routing + tools with E*Trade data
# ---------------------------------------------------------------------------

class TestMcpEtrade:
    def test_load_and_merge_etrade_csv(self, etrade_file_contents):
        merged, balances, warnings = _load_and_merge(file_contents=etrade_file_contents)
        assert len(merged) == 5
        assert set(merged["account_id"]) == {"E*Trade"}
        assert not warnings

    def test_get_transaction_summary_full_ledger(self, etrade_file_contents):
        result = get_transaction_summary(file_contents=etrade_file_contents)
        assert result["total_rows"] == 5  # stocks included in the summary
        assert "E*Trade" in result["accounts"]
        assert result["transaction_types"]["Buy"] == 3  # 2 options + TSLA
        assert result["transaction_types"]["Sell"] == 2  # 1 option + QCOM

    def test_compute_daily_pnl_strategy_filter(self, etrade_file_contents):
        result = compute_daily_pnl(
            file_contents=etrade_file_contents, initial_capital=10000.0
        )
        assert "error" not in result
        # Stocks (TSLA / QCOM) are excluded from the strategy PnL.
        assert result["total_rows"] == 3
        assert result["total_realized_pnl"] == pytest.approx(63.95 - 8.05 - 21.02)

    def test_compute_account_return_etrade(self, etrade_file_contents):
        result = compute_account_return(
            file_contents=etrade_file_contents, method="TWR"
        )
        assert "error" not in result
        accts = result["accounts"]
        assert accts and accts[0]["account"] == "E*Trade"
        twr = accts[0]["twr"]
        assert twr["period_count"] == 2  # Jan + Feb
        # Strategy PnL = 34.88 on initial 100k default.
        assert twr["twr"] == pytest.approx(34.88 / 100000.0, abs=1e-9)

    def test_compute_account_return_mwr_uses_strategy_ending(self, etrade_file_contents):
        # The strategy-only MWR uses the strategy's derived ending value, so a
        # profitable SPX/SPXW strategy yields a positive MWR even when the
        # whole-account value (incl. non-SPX activity) differs.
        result = compute_account_return(
            file_contents=etrade_file_contents, method="MWR"
        )
        acct = result["accounts"][0]
        # SPX/SPXW PnL = +34.88; external flows = -840 + 2895.05 = +2055.05.
        assert acct["strategy_ending"] == pytest.approx(
            100000.0 + 34.88 + 2055.05, abs=1e-6
        )
        assert acct["mwr"]["mwr"] is not None
        assert acct["mwr"]["mwr"] > 0

    def test_build_account_capital_chained_twr(self):
        merged = pd.DataFrame({"account_id": ["999-999999-999"]})
        balances = [
            EtradeBalance("999-999999-999", date(2026, 6, 1), 98232.53, 96703.19,
                          period_end=date(2026, 6, 30)),
            EtradeBalance("999-999999-999", date(2026, 7, 1), 96703.19, 96372.06,
                          period_end=date(2026, 7, 31)),
        ]
        cap = _build_account_capital(merged, balances)
        entry = cap["999-999999-999"]
        assert entry["initial"] == 98232.53
        assert entry["ending"] == 96372.06
        periods = entry["periods"]
        # June ending == July beginning → the chained monthly TWR:
        twr = compute_twr([TwrPeriod(p.period_start, p.beginning_value, p.ending_value,
                                     p.period_end) for p in periods])
        expected = (96703.19 / 98232.53) * (96372.06 / 96703.19) - 1.0
        assert twr["twr"] == pytest.approx(expected)
