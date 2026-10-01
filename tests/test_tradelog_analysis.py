"""Focused tests for the ported edge analysis and the HTML report wiring.

Spread reconstruction is tested against the same synthetic QFX the loader tests
use: short 7400 / long 7350, both expiring worthless, so the spread is paired,
50 points wide, and keeps its credit.
"""
from __future__ import annotations

import json

import pytest

from spx_trade_desk.resources import REPORT_DATA_DIR, REPORT_OUTPUT_DIR
from spx_trade_desk.tradelog.analysis import strategy_analysis as sa

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
def qfx_path(tmp_path):
    path = tmp_path / "synthetic.qfx"
    path.write_text(SYNTHETIC_QFX, encoding="latin-1")
    return str(path)


def test_reconstruct_spreads_pairs_the_vertical(qfx_path):
    spreads = sa.reconstruct_spreads(qfx_path)

    assert len(spreads) == 1
    row = spreads.iloc[0]
    assert bool(row["paired"]) is True
    assert row["short_strike"] == 7400.0
    assert row["long_strike"] == 7350.0
    assert row["width"] == pytest.approx(50.0)
    # credit_ct is dollars per contract: short collected 49.35, long cost 20.65
    assert row["credit_ct"] == pytest.approx(28.70, abs=0.01)


def test_load_positions_preserves_intraday_timestamps(qfx_path):
    positions = sa.load_positions(qfx_path)
    assert len(positions) == 2
    assert positions["open_ts"].notna().all()
    assert set(positions["direction"]) == {"short", "long"}


def test_stop_events_flags_nothing_when_no_trade_lost_multiple_of_credit(qfx_path):
    positions = sa.load_positions(qfx_path)
    stops = sa.stop_events(positions, stop_multiple=5.0)
    assert stops["is_stop"].sum() == 0


def test_bootstrap_ci_brackets_the_mean():
    lo, mid, hi = sa.bootstrap_ci([10.0, 20.0, 30.0, 40.0], n_boot=2000, seed=7)
    assert lo < mid < hi
    assert mid == pytest.approx(25.0, abs=2.0)


def test_kelly_binary_and_breakeven():
    fraction, half = sa.kelly_binary(p_win=0.8, b=2.0)
    assert fraction == pytest.approx(0.7)
    assert half == pytest.approx(0.35)
    assert sa.breakeven_win_rate(100.0, 100.0) == pytest.approx(0.5)


def test_report_writes_into_the_repo_reports_directory():
    assert REPORT_OUTPUT_DIR.name == "output"
    assert REPORT_OUTPUT_DIR.parent.name == "reports"
    assert REPORT_DATA_DIR.name == "data"
    assert (REPORT_DATA_DIR / "spx_closes.csv").exists()
    assert (REPORT_DATA_DIR / "vix_closes.csv").exists()


def test_build_report_returns_html_and_json_without_writing_files(qfx_path):
    """build_report is pure output generation: it returns the document and the
    data, and writes nothing. The CLI's main() and the MCP tool own the writing,
    so a write added here would fight their path handling.
    """
    from types import SimpleNamespace

    from spx_trade_desk.tradelog.report.generate_report import build_report

    before = set(REPORT_OUTPUT_DIR.glob("*.html"))
    html_doc, report_data = build_report(SimpleNamespace(
        monthly=qfx_path, month=None, ytd=None, rf=0.04,
        label="Synthetic Test", offline=True,
    ))

    assert "<html" in html_doc.lower()
    assert report_data["total_pnl"] == pytest.approx(28.70, abs=0.01)
    json.dumps(report_data)          # must stay JSON-serializable
    assert set(REPORT_OUTPUT_DIR.glob("*.html")) == before
