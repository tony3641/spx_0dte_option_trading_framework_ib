"""IBKR QFX support in the Account Return (TWR/MWR) + E*Trade×IBKR aggregation.

These tests use a compact synthetic spanning QFX whose monthly SPX/flow/BIL
aggregates reproduce the real file's numbers (see the Plan file / README):
initial $48,668.98 + SPX PnL $6,712.22 − non-SPX flows $5,452.36 = $49,928.84.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from spx_trade_desk.mcp.server import (
    _build_account_capital,
    _period_range,
    compute_account_return,
)
from spx_trade_desk.tradelog.domain.return_metrics import (
    TwrPeriod,
    compute_portfolio_twr,
    compute_strategy_twr,
    statement_external_flows,
)
from spx_trade_desk.tradelog.io.load_qfx import InvBalance, load_transactions_qfx

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

# Monthly SPX/SPXW PnL and non-SPX external-flow totals from the real
# U99999999_20260101_20260727.qfx (dates within each month are cosmetic — the
# engine buckets by calendar month).
SPX_PNL_BY_MONTH = [
    (date(2026, 1, 15), -790.790000),
    (date(2026, 2, 15), 1555.612500),
    (date(2026, 3, 15), 1650.681400),
    (date(2026, 4, 15), 272.708550),
    (date(2026, 5, 15), 1406.473500),
    (date(2026, 6, 15), 1916.689500),
    (date(2026, 7, 15), 700.845520),
]

FLOWS_BY_MONTH = [
    (date(2026, 2, 20), 172.483680),
    (date(2026, 3, 20), 104.540000),
    (date(2026, 4, 20), 113.370000),
    (date(2026, 5, 20), 174.953047),
    (date(2026, 6, 25), -6151.524832),  # BIL buy + other June non-SPX movement
    (date(2026, 7, 20), 133.810000),
]

QFX_INITIAL = 48668.98
QFX_TOTAL = 49928.84
SPX_TOTAL = sum(a for _, a in SPX_PNL_BY_MONTH)          # 6712.22097
FLOW_TOTAL = sum(a for _, a in FLOWS_BY_MONTH)           # -5452.3613

MINIMAL_QFX = """<OFX>
<SIGNONMSGSRSV1><SONRS><DTSERVER>20260626202000[-4:EDT]</DTSERVER></SONRS></SIGNONMSGSRSV1>
<INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>
<DTASOF>20260626202000.000[-4:EDT]</DTASOF>
<CURDEF>USD</CURDEF>
<INVACCTFROM><BROKERID>4705</BROKERID><ACCTID>U99999999</ACCTID></INVACCTFROM>
<INVTRANLIST>
<DTSTART>20260601202000.000[-4:EDT]</DTSTART>
<DTEND>20260630202000.000[-4:EDT]</DTEND>
<BUYSTOCK>
<INVBUY>
<INVTRAN><FITID>x</FITID><DTTRADE>20260625143918.000[-4:EDT]</DTTRADE></INVTRAN>
<SECID><UNIQUEID>78468R663</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>
<UNITS>70</UNITS>
<UNITPRICE>91.61</UNITPRICE>
<COMMISSION>0.36446725</COMMISSION>
<TAXES>0</TAXES>
<TOTAL>-6413.06446725</TOTAL>
<SUBACCTSEC>CASH</SUBACCTSEC>
<SUBACCTFUND>CASH</SUBACCTFUND>
</INVBUY>
</BUYSTOCK>
</INVTRANLIST>
<INVBAL>
<AVAILCASH>1000</AVAILCASH>
<MARGINBALANCE>0</MARGINBALANCE>
<SHORTBALANCE>0</SHORTBALANCE>
<BALLIST>
<BAL><NAME>Cash</NAME><DESC>Cash Balance</DESC><BALTYPE>NUMBER</BALTYPE><VALUE>1000</VALUE></BAL>
<BAL><NAME>Stock</NAME><DESC>Total Stocks</DESC><BALTYPE>NUMBER</BALTYPE><VALUE>2000</VALUE></BAL>
</BALLIST>
</INVBAL>
</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>
<SECLISTMSGSRSV1><SECLIST>
<STOCKINFO><SECINFO>
<SECID><UNIQUEID>78468R663</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>
<SECNAME>BIL SS SPDR BB 1-3M T-BILL ETF</SECNAME>
<TICKER>BIL</TICKER>
</SECINFO></STOCKINFO>
</SECLIST></SECLISTMSGSRSV1>
</OFX>
"""

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

IBKR_CSV = (
    "Date,Account,Description,Transaction Type,Symbol,Quantity,Price,"
    "Price Currency, Gross Amount,Commission,Net Amount\n"
    "2026-01-15,U123456,SPXW 01/15/26 P6700,Sell,SPXW  260115P06700000,"
    "1,5.00,USD,500.00,0.65,499.35\n"
)


def _spanning_period() -> TwrPeriod:
    return TwrPeriod(
        period_start=date(2025, 12, 31),
        beginning_value=0.0,  # placeholder — QFX has no beginning value
        ending_value=QFX_TOTAL,
        period_end=date(2026, 7, 27),
    )


# ---------------------------------------------------------------------------
# QFX loader
# ---------------------------------------------------------------------------

class TestLoadQfx:
    def test_buystock_parsed(self):
        df, bal = load_transactions_qfx(MINIMAL_QFX.encode("utf-8"))
        assert len(df) == 1
        row = df.iloc[0]
        assert row["transaction_type"] == "Buy"
        assert row["symbol"] == "BIL"
        assert row["quantity"] == 70.0
        assert row["price"] == pytest.approx(91.61)
        assert row["net_amount"] == pytest.approx(-6413.06446725)
        assert row["commission"] == pytest.approx(0.36446725)
        assert row["activity_date"] == date(2026, 6, 25)

    def test_invbalance_metadata(self):
        _, bal = load_transactions_qfx(MINIMAL_QFX.encode("utf-8"))
        assert bal.account_id == "U99999999"
        assert bal.as_of == date(2026, 6, 26)
        assert bal.dt_start == date(2026, 6, 1)
        assert bal.dt_end == date(2026, 6, 30)
        assert bal.total == pytest.approx(3000.0)

    def test_to_statement_period(self):
        bal = InvBalance(
            cash=4118.84, stock_value=45810.00,
            account_id="U99999999", as_of=date(2026, 7, 27),
            dt_start=date(2025, 12, 31), dt_end=date(2026, 7, 27),
        )
        assert bal.total == pytest.approx(49928.84)
        p = bal.to_statement_period()
        assert p.period_start == date(2025, 12, 31)
        assert p.period_end == date(2026, 7, 27)
        assert p.ending_value == pytest.approx(49928.84)


# ---------------------------------------------------------------------------
# Spanning-period TWR + statement flows
# ---------------------------------------------------------------------------

class TestSpanningPeriodReturns:
    def test_compute_strategy_twr_spanning_qfx(self):
        res = compute_strategy_twr(
            QFX_INITIAL, SPX_PNL_BY_MONTH, FLOWS_BY_MONTH,
            statement_periods=[_spanning_period()],
        )
        assert res["period_count"] == 7
        assert res["twr"] == pytest.approx(0.138741, rel=1e-3)

        rows = res["periods"]
        # Jan: ledger-built, no anchor.
        assert rows[0]["period_start"] == date(2026, 1, 1)
        assert rows[0]["beginning_value"] == pytest.approx(48668.98, abs=0.01)
        assert rows[0]["spx_pnl"] == pytest.approx(-790.79, abs=0.01)
        assert rows[0]["ending_value"] == pytest.approx(47878.19, abs=0.01)
        # June carries the big BIL outflow.
        assert rows[5]["beginning_value"] == pytest.approx(53329.02, abs=0.01)
        assert rows[5]["spx_pnl"] == pytest.approx(1916.69, abs=0.01)
        assert rows[5]["external_flows"] == pytest.approx(-6151.52, abs=0.01)
        # July: anchored to the real balance snapshot.
        assert rows[6]["period_start"] == date(2026, 7, 1)
        assert rows[6]["period_end"] == date(2026, 7, 27)
        assert rows[6]["ending_value"] == pytest.approx(49928.84, abs=0.01)
        assert rows[6]["monthly_return"] == pytest.approx(700.84552 / 49094.18, abs=1e-4)

    def test_statement_external_flows_spanning(self):
        flows = statement_external_flows(
            QFX_INITIAL, SPX_PNL_BY_MONTH, [_spanning_period()]
        )
        # ending − initial − full-window SPX PnL = 49928.84 − 48668.98 − 6712.22
        assert flows == [(date(2026, 7, 27), pytest.approx(-5452.36, abs=0.05))]

    def test_compute_portfolio_twr(self):
        accounts = [
            {  # E*Trade CSV semantics: SPX options + stock external flows
                "initial": 100000.0,
                "spx_pnl_by_date": [
                    (date(2026, 1, 14), 63.95),
                    (date(2026, 1, 14), -8.05),
                    (date(2026, 1, 22), -21.02),
                ],
                "external_flows_by_date": [
                    (date(2026, 1, 20), -840.0),
                    (date(2026, 2, 4), 2895.05),
                ],
                "statement_periods": None,
            },
            {  # IBKR CSV semantics: one SPX sell, no flows
                "initial": 100000.0,
                "spx_pnl_by_date": [(date(2026, 1, 15), 499.35)],
                "external_flows_by_date": [],
                "statement_periods": None,
            },
        ]
        res = compute_portfolio_twr(accounts)
        assert res["period_count"] == 2

        jan, feb = res["periods"]
        # Jan: both accounts active → values and PnL sum.
        assert jan["beginning_value"] == pytest.approx(200000.0)
        assert jan["spx_pnl"] == pytest.approx(534.23, abs=1e-6)
        assert jan["monthly_return"] == pytest.approx(534.23 / 200000.0)
        # Feb: account 1 has the QCOM flow; account 2 is forward-filled.
        assert feb["beginning_value"] == pytest.approx(99194.88 + 100499.35, abs=0.01)
        assert feb["spx_pnl"] == 0.0
        assert feb["monthly_return"] == 0.0
        assert res["twr"] == pytest.approx(534.23 / 200000.0)


# ---------------------------------------------------------------------------
# MCP capital map + tool
# ---------------------------------------------------------------------------

class TestMcpQfx:
    def test_build_account_capital_qfx(self):
        merged = pd.DataFrame({
            "account_id": ["U99999999", "U99999999"],
            "net_amount": [50.0, 50.0],   # +100 realized → initial = 3000 − 100
        })
        balances = [InvBalance(
            cash=1000.0, stock_value=2000.0, account_id="U99999999",
            as_of=date(2026, 7, 27), dt_start=date(2026, 7, 1), dt_end=date(2026, 7, 27),
        )]
        cap = _build_account_capital(merged, balances)
        entry = cap["U99999999"]
        assert entry["initial"] == pytest.approx(2900.0)
        assert entry["ending"] == pytest.approx(3000.0)
        assert len(entry["periods"]) == 1
        assert entry["periods"][0].period_end == date(2026, 7, 27)
        assert entry["periods"][0].ending_value == pytest.approx(3000.0)

    def test_period_range_merges_ledger(self):
        df = pd.DataFrame({
            "account_id": ["U99999999", "U99999999"],
            "activity_date": [date(2026, 1, 2), date(2026, 7, 27)],
        })
        july_period = TwrPeriod(
            period_start=date(2026, 7, 1), beginning_value=0.0,
            ending_value=3000.0, period_end=date(2026, 7, 27),
        )
        lo, hi = _period_range(df, [july_period])
        assert lo == date(2026, 1, 2)
        assert hi == date(2026, 7, 27)

    def test_compute_account_return_combined(self):
        result = compute_account_return(
            file_contents=[
                {"name": "etrade.csv", "data_text": ETRADE_CSV},
                {"name": "ibkr.csv", "data_text": IBKR_CSV},
            ],
            method="TWR",
            account_filter="All",
        )
        assert "error" not in result
        accts = [a["account"] for a in result["accounts"]]
        assert len(accts) == 2
        assert "combined" in result
        combined = result["combined"]["twr"]
        assert combined["period_count"] == 2
        assert combined["twr"] == pytest.approx(534.23 / 200000.0, abs=1e-9)
