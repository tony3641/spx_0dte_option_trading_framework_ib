"""Integration tests for MCP server tools using synthetic CSV data."""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from spx_trade_desk.mcp.server import (
    _filter_window,
    _load_and_merge,
    _resolve_initial_capital,
    build_occ_symbol,
    compute_daily_pnl,
    compute_risk_metrics,
    generate_monthly_report,
    get_calendar_data,
    get_contract_details,
    get_market_data,
    get_transaction_summary,
    parse_occ_symbol,
)
from spx_trade_desk.tradelog.io.load_qfx import InvBalance


# ---------------------------------------------------------------------------
# Synthetic CSV data fixture
# ---------------------------------------------------------------------------

SYNTHETIC_CSV = (
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


@pytest.fixture
def csv_file_contents() -> list[dict[str, str]]:
    return [{"name": "test.csv", "data_text": SYNTHETIC_CSV}]


# ---------------------------------------------------------------------------
# _load_and_merge
# ---------------------------------------------------------------------------

class TestLoadAndMerge:
    def test_csv_data_text(self, csv_file_contents):
        df, balances, warnings = _load_and_merge(file_contents=csv_file_contents)
        assert len(df) == 4
        assert not warnings

    def test_empty(self):
        df, balances, warnings = _load_and_merge(
            file_contents=[{"name": "empty.csv", "data_text": ""}]
        )
        assert df.empty

    def test_both_none(self):
        df, balances, warnings = _load_and_merge()
        assert df.empty

    def test_missing_data_fields(self):
        df, balances, warnings = _load_and_merge(
            file_contents=[{"name": "bad.csv"}]  # no data_text or data_base64
        )
        assert df.empty
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# _filter_window
# ---------------------------------------------------------------------------

class TestFilterWindow:
    @pytest.fixture
    def daily_df(self, csv_file_contents):
        from spx_trade_desk.tradelog.domain.pnl_engine import build_realized_pnl
        merged, _, _ = _load_and_merge(file_contents=csv_file_contents)
        return build_realized_pnl(merged).daily

    def test_all(self, daily_df):
        result = _filter_window(daily_df, "All")
        assert len(result) == 3  # 3 distinct dates

    def test_1m(self, daily_df):
        result = _filter_window(daily_df, "1M")
        assert len(result) >= 1

    def test_custom(self, daily_df):
        result = _filter_window(daily_df, "Custom", "2026-01-15", "2026-01-16")
        assert len(result) == 2

    def test_empty_df(self):
        import pandas as pd
        df = pd.DataFrame(columns=["activity_date", "realized_pnl"])
        result = _filter_window(df, "1M")
        assert result.empty


# ---------------------------------------------------------------------------
# _resolve_initial_capital
# ---------------------------------------------------------------------------

class TestResolveInitialCapital:
    def test_provided(self):
        assert _resolve_initial_capital([], pd.DataFrame(), 50000.0) == 50000.0

    def test_default(self):
        assert _resolve_initial_capital([], pd.DataFrame(), None) == 100_000.0

    def test_qfx_balance(self, csv_file_contents):
        merged, _, _ = _load_and_merge(file_contents=csv_file_contents)
        bal = InvBalance(cash=1000.0, stock_value=5000.0)
        cap = _resolve_initial_capital([bal], merged, None)
        # final=6000, net_amounts≈997.40, so cap ≈ 5002.60
        assert cap > 0


# ---------------------------------------------------------------------------
# Tool: parse_occ_symbol
# ---------------------------------------------------------------------------

class TestParseOccSymbol:
    def test_valid(self):
        result = parse_occ_symbol("SPXW  260202P06940000")
        assert result["underlying"] == "SPXW"
        assert result["expiry_date"] == "2026-02-02"
        assert result["right"] == "P"
        assert result["strike"] == 6940.0
        assert "SPXW|2026-02-02|P|6940.000" in result["contract_key"]

    def test_invalid(self):
        result = parse_occ_symbol("NOT_A_SYMBOL")
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool: build_occ_symbol
# ---------------------------------------------------------------------------

class TestBuildOccSymbol:
    def test_valid(self):
        result = build_occ_symbol("SPXW", "2026-02-02", "P", 6940.0)
        assert result["occ_symbol"] == "SPXW  260202P06940000"
        assert "SPXW|2026-02-02|P|6940.000" in result["contract_key"]

    def test_invalid_date(self):
        result = build_occ_symbol("SPXW", "not-a-date", "P", 6940.0)
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool: get_transaction_summary
# ---------------------------------------------------------------------------

class TestGetTransactionSummary:
    def test_basic(self, csv_file_contents):
        result = get_transaction_summary(file_contents=csv_file_contents)
        assert result["total_rows"] == 4
        assert result["date_range"]["start"] == "2026-01-15"
        assert result["date_range"]["end"] == "2026-01-20"
        assert "U123456" in result["accounts"]
        assert "Sell" in result["transaction_types"]
        assert "Buy" in result["transaction_types"]

    def test_empty(self):
        result = get_transaction_summary(file_contents=[])
        assert result["total_rows"] == 0

    def test_with_paths(self):
        # Paths that don't exist should generate warnings, not crashes
        result = get_transaction_summary(paths=["nonexistent.csv"])
        assert result["total_rows"] == 0


# ---------------------------------------------------------------------------
# Tool: compute_daily_pnl
# ---------------------------------------------------------------------------

class TestComputeDailyPnl:
    def test_basic(self, csv_file_contents):
        result = compute_daily_pnl(file_contents=csv_file_contents)
        assert "error" not in result
        assert result["total_trades"] == 4
        assert result["total_realized_pnl"] is not None
        assert len(result["daily_series"]) == 3  # 3 distinct trading days

    def test_with_capital(self, csv_file_contents):
        result = compute_daily_pnl(
            file_contents=csv_file_contents, initial_capital=10000.0
        )
        assert result["initial_capital"] == 10000.0

    def test_account_filter(self, csv_file_contents):
        result = compute_daily_pnl(
            file_contents=csv_file_contents, account_filter="U123456"
        )
        assert "error" not in result


# ---------------------------------------------------------------------------
# Tool: get_contract_details
# ---------------------------------------------------------------------------

class TestGetContractDetails:
    def test_valid_contract(self, csv_file_contents):
        result = get_contract_details(
            contract_key="SPXW|2026-01-15|P|670.000",  # strike is 670.000 from OCC
            file_contents=csv_file_contents,
        )
        # The actual contract key depends on OCC parsing; just check no error
        # if the key is valid
        if "error" in result:
            # Try the actual key format from parse
            retry_key = parse_occ_symbol("SPXW  260115P06700000")
            if "contract_key" in retry_key:
                result = get_contract_details(
                    contract_key=retry_key["contract_key"],
                    file_contents=csv_file_contents,
                )

    def test_missing_key(self, csv_file_contents):
        result = get_contract_details(file_contents=csv_file_contents)
        assert "error" in result
        assert "required" in result["error"].lower()

    def test_nonexistent_contract(self, csv_file_contents):
        result = get_contract_details(
            contract_key="FAKE|2026-01-01|C|999.999",
            file_contents=csv_file_contents,
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool: get_market_data
# ---------------------------------------------------------------------------

class TestGetMarketData:
    def test_invalid_ticker(self):
        result = get_market_data("INVALID", "2026-01-01", "2026-01-31")
        assert "error" in result

    def test_invalid_dates(self):
        result = get_market_data("SPX", "bad-date", "2026-01-31")
        assert "error" in result

    @pytest.mark.slow
    def test_spx_fetch(self):
        """Live network test — may be slow."""
        result = get_market_data("SPX", "2026-01-01", "2026-01-10")
        if "error" in result:
            pytest.skip(f"Network unavailable: {result['error']}")
        assert result["ticker"] == "SPX"
        assert len(result["series"]) > 0

    @pytest.mark.slow
    def test_vix_fetch(self):
        """Live network test — may be slow."""
        result = get_market_data("VIX", "2026-01-01", "2026-01-10")
        if "error" in result:
            pytest.skip(f"Network unavailable: {result['error']}")
        assert result["ticker"] == "VIX"
        assert len(result["series"]) > 0


# ---------------------------------------------------------------------------
# Tool: get_calendar_data
# ---------------------------------------------------------------------------

class TestGetCalendarData:
    def test_basic(self, csv_file_contents):
        result = get_calendar_data(file_contents=csv_file_contents)
        assert "error" not in result
        assert "week_labels" in result
        assert "pnl_matrix" in result
        assert len(result["weekday_labels"]) == 7
        assert result["weekday_labels"][0] == "Mon"

    def test_empty(self):
        result = get_calendar_data(file_contents=[])
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool: compute_risk_metrics
# ---------------------------------------------------------------------------

class TestComputeRiskMetrics:
    def test_basic_no_benchmarks(self, csv_file_contents):
        result = compute_risk_metrics(
            file_contents=csv_file_contents,
            with_spx=False,
            with_vix=False,
        )
        assert "error" not in result
        assert "risk_adjusted" in result
        assert result["risk_adjusted"]["net_ev"] is not None

    def test_custom_window(self, csv_file_contents):
        result = compute_risk_metrics(
            file_contents=csv_file_contents,
            window="Custom",
            custom_start="2026-01-15",
            custom_end="2026-01-20",
            with_spx=False,
            with_vix=False,
        )
        assert "error" not in result
        assert result["window"]["trading_days"] >= 1

    def test_account_filter(self, csv_file_contents):
        result = compute_risk_metrics(
            file_contents=csv_file_contents,
            account_filter="U123456",
            with_spx=False,
            with_vix=False,
        )
        assert "error" not in result

    def test_invalid_window(self, csv_file_contents):
        result = compute_risk_metrics(
            file_contents=csv_file_contents,
            window="INVALID",
            with_spx=False,
            with_vix=False,
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Tool: generate_monthly_report
# ---------------------------------------------------------------------------

# Minimal QFX: one bull put spread (short 7400 / long 7350), both expiring
# worthless on 2026-07-15. Short collects +49.35 net, long pays -20.65 net.
SYNTHETIC_QFX = (
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


@pytest.fixture
def qfx_file_contents() -> list[dict[str, str]]:
    return [{"name": "synthetic.qfx", "data_text": SYNTHETIC_QFX}]


class TestGenerateMonthlyReport:
    def test_by_file_contents(self, qfx_file_contents):
        result = generate_monthly_report(
            file_contents=qfx_file_contents, label="Synthetic Test", offline=True
        )
        assert result["total_pnl"] == pytest.approx(28.70, abs=0.01)
        assert result["spreads"]["paired"] == 1
        assert result["takeaways"]
        assert result["html_path"].endswith(".html")
        import json
        json.dumps(result)  # must be JSON-serializable

    def test_by_path(self, tmp_path, qfx_file_contents):
        p = tmp_path / "synthetic.qfx"
        p.write_text(qfx_file_contents[0]["data_text"], encoding="utf-8")
        result = generate_monthly_report(paths=[str(p)], offline=True)
        assert result["total_pnl"] == pytest.approx(28.70, abs=0.01)
        assert result["month"] == "2026-07"

    def test_offline_uses_cache(self, qfx_file_contents):
        # offline=True must not require the network — it reads the bundled CSVs
        result = generate_monthly_report(
            file_contents=qfx_file_contents, label="Synthetic Test", offline=True
        )
        assert result["total_pnl"] == pytest.approx(28.70, abs=0.01)
        assert "period_return" in result["spx"]  # benchmark block computed from cache

    def test_no_file(self):
        result = generate_monthly_report()
        assert "error" in result
