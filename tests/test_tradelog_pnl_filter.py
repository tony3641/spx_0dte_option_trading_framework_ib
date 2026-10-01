"""Focused tests for the ported P&L engine and SPX/SPXW strategy filter.

The IBKR fixture (four rows) closes two SPXW short puts: 6700 collected 499.35
and bought back for -200.65 (+298.70), and 6800 collected 698.70 and bought
back at 0.00 (+698.70). Total realized: 997.40 net of commission.
"""
from __future__ import annotations

import pandas as pd
import pytest

from spx_trade_desk.tradelog.domain.pnl_engine import build_realized_pnl
from spx_trade_desk.tradelog.domain.strategy_filter import (
    filter_strategy_rows,
    is_etrade_account,
    is_strategy_symbol,
)
from spx_trade_desk.tradelog.io.load_csv import load_transactions_csv
from spx_trade_desk.tradelog.io.load_etrade_csv import load_transactions_etrade_csv

IBKR_CSV = (
    "Date,Account,Description,Transaction Type,Symbol,Quantity,Price,"
    "Price Currency, Gross Amount,Commission,Net Amount\n"
    "2026-01-15,U123456,SPXW 01/15/26 P6700,Sell,SPXW  260115P06700000,"
    "1,5.00,USD,500.00,0.65,499.35\n"
    "2026-01-15,U123456,SPXW 01/15/26 P6700,Buy,SPXW  260115P06700000,"
    "-1,2.00,USD,-200.00,0.65,-200.65\n"
    "2026-01-16,U123456,SPXW 01/20/26 P6800,Sell,SPXW  260120P06800000,"
    "2,3.50,USD,700.00,1.30,698.70\n"
    "2026-01-20,U123456,SPXW 01/20/26 P6800 expiry,Buy,SPXW  260120P06800000,"
    "-2,0.00,USD,0.00,0.00,0.00\n"
)

ETRADE_CSV = (
    "Trade Date,Order Type,Security,Cusip,Transaction Description,Quantity,"
    "Executed Price,Commission,Net Amount\n"
    "1/14/2026,Sell To Open,SPXW JAN 14 '26 $6865 PUT,,"
    "PUT  SPXW   01/14/26  6865.000,2,0.33,1.03,63.95\n"
    "1/14/2026,Buy Open,SPXW JAN 14 '26 $6815 PUT,,"
    "PUT  SPXW   01/14/26  6815.000,2,0.03,1.03,8.05\n"
    "1/22/2026,Buy Open,SPXW JAN 22 '26 $6820 CALL,,"
    "CALL  SPXW   01/22/26  6820.000,1,0.2,0.51,21.02\n"
    "1/20/2026,Buy,TSLA,88160R101,TESLA INC UNSOLICITED TRADE,2,420,0.0000,840\n"
    "2/4/2026,Sell,QCOM,747525103,QUALCOMM INC UNSOLICITED TRADE,20,145,4.95,2895.05\n"
)


@pytest.fixture
def ibkr_df(tmp_path):
    path = tmp_path / "ibkr.csv"
    path.write_text(IBKR_CSV, encoding="utf-8")
    return load_transactions_csv(path)


@pytest.fixture
def etrade_df(tmp_path):
    path = tmp_path / "trades.csv"
    path.write_text(ETRADE_CSV, encoding="utf-8")
    return load_transactions_etrade_csv(path)


def test_enriched_rows_derive_option_fields(ibkr_df):
    result = build_realized_pnl(ibkr_df)
    rows = result.enriched_rows

    for column in ("contract_key", "underlying", "expiry_date", "right", "strike",
                   "is_option", "is_expire_inferred", "in_pnl",
                   "option_contracts_traded", "realization_reason"):
        assert column in rows.columns

    assert set(rows["underlying"]) == {"SPXW"}
    assert rows["is_option"].all()


def test_daily_realized_pnl_sums_both_closed_spreads(ibkr_df):
    daily = build_realized_pnl(ibkr_df).daily
    assert daily["realized_pnl"].sum() == pytest.approx(997.40)


def test_build_realized_pnl_is_idempotent(ibkr_df):
    once = build_realized_pnl(ibkr_df).enriched_rows
    twice = build_realized_pnl(once).enriched_rows
    assert list(once.columns) == list(twice.columns)
    assert len(once) == len(twice)


def test_strategy_symbol_accepts_spx_and_spxw_options_only():
    assert is_strategy_symbol("SPXW  260115P06700000")
    assert is_strategy_symbol("SPX   260115P06700000")
    assert not is_strategy_symbol("TSLA")
    assert not is_strategy_symbol("SPY   260115P06700000")


def test_etrade_account_detection():
    assert is_etrade_account("E*Trade")
    assert is_etrade_account("123-456789-012")
    assert not is_etrade_account("U123456")


def test_filter_strategy_rows_keeps_only_spx_options(etrade_df):
    filtered = filter_strategy_rows(etrade_df)

    # 5 loaded rows, minus the TSLA and QCOM stock trades = 3 SPXW option rows.
    # The CALL survives: the filter is underlying-based, not right-based.
    assert len(filtered) == 3
    assert set(filtered["symbol"]) == {
        "SPXW  260114P06865000",
        "SPXW  260114P06815000",
        "SPXW  260122C06820000",
    }


def test_filter_strategy_rows_on_empty_frame_is_safe():
    assert filter_strategy_rows(pd.DataFrame()).empty
