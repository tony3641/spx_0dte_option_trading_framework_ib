"""Focused tests for the ported CSV statement loaders.

Fixtures are the source repo's own synthetic statements, copied verbatim from
`trade_pnl_dashboard/mcp_server/tests/`. They pin the two semantics that carry
real money: E*Trade's unsigned Net Amount is signed by order type, and
`Option Expire` rows are dropped rather than counted as zero-PnL trades.
"""
from __future__ import annotations

import pytest

from spx_trade_desk.tradelog.io.format_detect import detect_csv_format
from spx_trade_desk.tradelog.io.load_csv import load_transactions_csv
from spx_trade_desk.tradelog.io.load_etrade_csv import load_transactions_etrade_csv

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


def test_detect_etrade_and_ibkr():
    assert detect_csv_format(ETRADE_CSV) == "etrade"
    assert detect_csv_format(IBKR_CSV) == "ibkr"
    assert detect_csv_format("a,b,c\n1,2,3\n") == "unknown"


def test_etrade_loader_signs_by_order_type_and_skips_expiry_rows(tmp_path):
    path = tmp_path / "trades.csv"
    path.write_text(ETRADE_CSV, encoding="utf-8")

    df = load_transactions_etrade_csv(path)

    # 6 data rows, minus the Option Expire row = 5
    assert len(df) == 5
    assert "Option Expire" not in set(df["transaction_type"])

    sell = df[df["description"].str.contains("6865", na=False)].iloc[0]
    assert sell["transaction_type"] == "Sell"
    assert sell["quantity"] == -2.0        # Sell To Open -> negative quantity
    assert sell["net_amount"] == pytest.approx(63.95)   # credit received, positive

    buy = df[df["description"].str.contains("6815", na=False)].iloc[0]
    assert buy["quantity"] == 2.0          # Buy Open -> positive quantity
    assert buy["net_amount"] == pytest.approx(-8.05)    # debit paid, negative


def test_etrade_loader_builds_occ_symbols(tmp_path):
    path = tmp_path / "trades.csv"
    path.write_text(ETRADE_CSV, encoding="utf-8")

    df = load_transactions_etrade_csv(path)
    symbols = set(df["symbol"])

    assert "SPXW  260114P06865000" in symbols
    assert "SPXW  260122C06820000" in symbols


def test_ibkr_csv_parses_transaction_history(tmp_path):
    path = tmp_path / "ibkr.csv"
    path.write_text(IBKR_CSV, encoding="utf-8")

    df = load_transactions_csv(path)

    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "SPXW  260115P06700000"
    assert df.iloc[0]["net_amount"] == pytest.approx(499.35)


def test_ibkr_csv_raises_without_a_transaction_table(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_transactions_csv(path)
