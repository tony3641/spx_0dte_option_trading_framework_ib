"""ITM cash settlement of expired SPXW legs that a QFX leaves out (invented data only)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from spx_trade_desk.tradelog.analysis import strategy_analysis as sa
from spx_trade_desk.tradelog.domain.pnl_engine import build_realized_pnl
from spx_trade_desk.tradelog.domain.settlement import (
    describe_settlements, find_settlements, infer_expiry_settlements, load_settle_prices,
)
from spx_trade_desk.tradelog.io.load_qfx import load_transactions_qfx

AS_OF = date(2026, 8, 1)
EXPIRY = date(2026, 7, 15)
SHORT_SYM, LONG_SYM = "SPXW  260715P07400000", "SPXW  260715P07350000"


def _trade(tag, uid, memo, hhmmss, units, price, commission, total):
    inner = "INVBUY" if tag == "BUYOPT" else "INVSELL"
    return (
        f"<{tag}><{inner}><INVTRAN><DTTRADE>20260715{hhmmss}.000[-5:EST]</DTTRADE>"
        f"<DTSTAMP>20260715{hhmmss}.000[-5:EST]</DTSTAMP><MEMO>{memo}</MEMO></INVTRAN>"
        f"<SECID><UNIQUEID>{uid}</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>"
        f"<UNITS>{units}</UNITS><UNITPRICE>{price}</UNITPRICE><COMMISSION>{commission}</COMMISSION>"
        f"<TOTAL>{total}</TOTAL></{inner}></{tag}>"
    )


def _qfx(trades: str) -> str:
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
        "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
        "<OFX><INVSTMTMSGSRSV1><INVSTMTTRNRS><TRNUID>1</TRNUID><INVSTMTRS>"
        "<DTASOF>20260731120000.000[-5:EST]</DTASOF><CURDEF>USD</CURDEF><INVACCTFROM>"
        "<BROKERID>BROKER</BROKERID><ACCTID>U000111</ACCTID></INVACCTFROM><INVTRANLIST>"
        "<DTSTART>20260701</DTSTART><DTEND>20260731</DTEND>" + trades + "</INVTRANLIST><SECLIST>"
        "<OPTINFO><SECINFO><SECID><UNIQUEID>00001</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>"
        f"<TICKER>{SHORT_SYM}</TICKER><SECNAME>SPXW 15JUL26 7400 P</SECNAME></SECINFO>"
        "<OPTYPE>PUT</OPTYPE><STRIKEPRICE>7400</STRIKEPRICE><DTEXPIRE>20260715</DTEXPIRE>"
        "<SHPERCTRCT>100</SHPERCTRCT></OPTINFO>"
        "<OPTINFO><SECINFO><SECID><UNIQUEID>00002</UNIQUEID><UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>"
        f"<TICKER>{LONG_SYM}</TICKER><SECNAME>SPXW 15JUL26 7350 P</SECNAME></SECINFO>"
        "<OPTYPE>PUT</OPTYPE><STRIKEPRICE>7350</STRIKEPRICE><DTEXPIRE>20260715</DTEXPIRE>"
        "<SHPERCTRCT>100</SHPERCTRCT></OPTINFO></SECLIST>"
        "<INVBAL><AVAILCASH>50000.00</AVAILCASH><BAL><NAME>stock</NAME><VALUE>0.00</VALUE></BAL></INVBAL>"
        "</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1></OFX>"
    )


# A 1-lot 7400/7350 put spread opened and never closed: the QFX shows only the two opening trades.
OPEN_ONLY = _qfx(
    _trade("SELLOPT", "00001", "SPXW 15JUL26 7400 P", "093000", -1, "0.50", "0.65", "49.35")
    + _trade("BUYOPT", "00002", "SPXW 15JUL26 7350 P", "093001", 1, "0.20", "0.65", "-20.65")
)


@pytest.fixture
def open_only_path(tmp_path):
    p = tmp_path / "open_only.qfx"
    p.write_text(OPEN_ONLY, encoding="latin-1")
    return str(p)


def test_itm_short_is_settled_at_the_official_close(open_only_path):
    # SPX 7370: the 7400 put is 30 points ITM, the 7350 put is OTM
    df, _ = load_transactions_qfx(open_only_path, settle_prices={EXPIRY: 7370.0}, as_of=AS_OF)
    settle = df[df["transaction_type"] == "Cash Settlement"]
    assert len(settle) == 1
    row = settle.iloc[0]
    assert row["symbol"] == SHORT_SYM
    assert row["net_amount"] == pytest.approx(-3000.0)
    assert row["activity_date"] == EXPIRY
    daily = build_realized_pnl(df).daily
    assert daily["realized_pnl"].sum() == pytest.approx(49.35 - 20.65 - 3000.0)


def test_both_legs_itm_settle_to_the_spread_max_loss(open_only_path):
    df, _ = load_transactions_qfx(open_only_path, settle_prices={EXPIRY: 7300.0}, as_of=AS_OF)
    cash = df.loc[df["transaction_type"] == "Cash Settlement", "net_amount"].sum()
    assert cash == pytest.approx(-(100 * 100) + 50 * 100)   # short -10000, long +5000


def test_loader_is_unchanged_without_prices(open_only_path):
    df, _ = load_transactions_qfx(open_only_path)
    assert (df["transaction_type"] == "Cash Settlement").sum() == 0
    assert len(df) == 2


def test_unpriced_expiry_is_reported_not_guessed(open_only_path):
    df, _ = load_transactions_qfx(open_only_path, settle_prices={}, as_of=AS_OF)
    assert (df["transaction_type"] == "Cash Settlement").sum() == 0
    assert df.attrs["unpriced_expiries"] == [EXPIRY]
    notes = describe_settlements(df, df.attrs["unpriced_expiries"])
    assert any("2026-07-15" in n and "NOT included" in n for n in notes)


def test_worthless_expiry_close_means_nothing_to_settle(tmp_path):
    closed = _qfx(
        _trade("SELLOPT", "00001", "x", "093000", -1, "0.50", "0.65", "49.35")
        + _trade("BUYOPT", "00001", "x", "160000", 1, "0.00", "0.00", "0.00")
    )
    p = tmp_path / "closed.qfx"
    p.write_text(closed, encoding="latin-1")
    df, _ = load_transactions_qfx(str(p), settle_prices={EXPIRY: 7000.0}, as_of=AS_OF)
    assert (df["transaction_type"] == "Cash Settlement").sum() == 0


def test_not_yet_expired_and_older_positions_are_left_alone():
    legs = pd.DataFrame({
        "account_id": ["A", "A"], "symbol": [SHORT_SYM, LONG_SYM],
        "trade_date": [date(2026, 7, 14), date(2026, 7, 15)],   # first leg opened the day before
        "signed_qty": [-1.0, 1.0],
    })
    found, _ = find_settlements(legs, {EXPIRY: 7300.0}, as_of=AS_OF)
    assert [s.symbol for s in found] == [LONG_SYM]              # the multi-day contract is not trusted
    assert find_settlements(legs, {EXPIRY: 7300.0}, as_of=date(2026, 7, 14))[0] == []   # not expired yet


def test_a_real_settlement_row_wins_over_the_inferred_one(open_only_path):
    df, _ = load_transactions_qfx(open_only_path)
    real = pd.DataFrame([{
        "activity_date": EXPIRY, "account_id": "U000111", "description": "real", "transaction_type": "Cash Settlement",
        "symbol": SHORT_SYM, "quantity": None, "price": None, "gross_amount": -2900.0, "commission": 0.0,
        "net_amount": -2900.0, "source_row": 99,
    }])
    merged = pd.concat([df, real], ignore_index=True)
    out, _ = infer_expiry_settlements(merged, {EXPIRY: 7370.0}, as_of=AS_OF)
    assert out["net_amount"].sum() == pytest.approx(merged["net_amount"].sum())


def test_positions_and_spreads_include_the_settlement(open_only_path):
    prices = {EXPIRY: 7370.0}
    pos = sa.load_positions(open_only_path, prices)
    short = pos[pos["direction"] == "short"].iloc[0]
    assert short["settlement"] == pytest.approx(-3000.0)
    assert short["total_pnl"] == pytest.approx(49.35 - 3000.0)
    assert short["trade_pnl"] == pytest.approx(49.35)
    long_ = pos[pos["direction"] == "long"].iloc[0]
    assert long_["settlement"] == 0.0 and long_["debit"] == pytest.approx(20.65)

    spreads = sa.reconstruct_spreads(open_only_path, prices)
    assert len(spreads) == 1 and bool(spreads.iloc[0]["paired"])
    assert spreads.iloc[0]["credit_ct"] == pytest.approx(28.70, abs=0.01)
    assert spreads.iloc[0]["short_pnl"] == pytest.approx(49.35 - 3000.0)
    # an ITM expiry is not a stop: nothing was closed
    assert sa.stop_events(pos, stop_multiple=5.0)["is_stop"].sum() == 0


def test_long_cost_is_what_was_paid_even_when_sold_back(tmp_path):
    sold_back = _qfx(
        _trade("SELLOPT", "00001", "x", "093000", -1, "0.50", "0.65", "49.35")
        + _trade("BUYOPT", "00002", "x", "093001", 1, "0.20", "0.65", "-20.65")
        + _trade("BUYOPT", "00001", "x", "110000", 1, "1.00", "0.65", "-100.65")
        + _trade("SELLOPT", "00002", "x", "110001", -1, "0.30", "0.65", "29.35")
    )
    p = tmp_path / "sold_back.qfx"
    p.write_text(sold_back, encoding="latin-1")
    spreads = sa.reconstruct_spreads(str(p), {})
    assert spreads.iloc[0]["credit_ct"] == pytest.approx(49.35 - 20.65, abs=0.01)   # not 49.35 - |8.70|


def test_price_cache_ignores_the_unfinished_day(tmp_path):
    csv = tmp_path / "spx.csv"
    csv.write_text("activity_date,spx_close,spx_return\n2026-07-14,7400.5,0.0\n2026-07-15,7380.25,0.0\n")
    assert load_settle_prices(csv, as_of=date(2026, 7, 15)) == {date(2026, 7, 14): 7400.5}
    assert load_settle_prices(csv, as_of=date(2026, 7, 16))[date(2026, 7, 15)] == 7380.25
    assert load_settle_prices(tmp_path / "missing.csv") == {}
