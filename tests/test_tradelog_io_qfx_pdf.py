"""Focused tests for the ported QFX / PDF / market-data loaders.

The QFX fixture is the source repo's own synthetic statement, copied verbatim:
one bull put spread (short 7400 / long 7350) opened 2026-07-15 and expiring
worthless the same day, plus a closing leg per contract.

The E*Trade PDF parser's body is not exercised here — it needs a real statement
PDF. Only its year-extraction helper, which carries a documented clock
dependency, is tested directly.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from spx_trade_desk.tradelog.io.load_etrade_pdf import (
    EtradeBalance,
    _extract_statement_year,
)
from spx_trade_desk.tradelog.io.load_qfx import InvBalance, load_transactions_qfx

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
    return path


def test_qfx_parses_all_transactions_with_symbols(qfx_path):
    df, balance = load_transactions_qfx(qfx_path)

    assert len(df) == 4                      # two opens + two expiry closes
    assert set(df["transaction_type"]) == {"Buy", "Sell"}
    assert "SPXW  260715P07400000" in set(df["symbol"])
    assert isinstance(balance, InvBalance)


def test_pdf_statement_year_is_read_from_the_period_line():
    assert _extract_statement_year(
        "For the Period January 1 - January 31, 2025") == 2025


def test_pdf_statement_year_falls_back_to_the_current_year():
    """Review Focus item 1: a year-less statement silently uses the current year.

    This is a documented limitation that the port preserves rather than fixes —
    a prior-year statement with no year in its header parses with wrong dates,
    which is why the README lists it as a known limitation.
    """
    assert _extract_statement_year("Account statement, no period line") == datetime.now().year


def test_etrade_balance_dataclass_defaults():
    balance = EtradeBalance(
        account_id="123-456789-012",
        period_start="2026-01-01",
        beginning_value=50000.0,
        ending_value=51000.0,
    )
    assert balance.cash == 0.0
    assert balance.stock_value == 0.0
    assert balance.period_end is None
