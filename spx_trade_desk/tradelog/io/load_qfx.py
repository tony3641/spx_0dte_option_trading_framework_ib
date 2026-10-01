"""
QFX / OFX investment-statement parser for Interactive Brokers exports.

Produces a DataFrame with the same column schema as load_transactions_csv() so
the rest of the pipeline (pnl_engine, UI tabs) works without any changes.

Supports:
  - BUYOPT / SELLOPT, BUYSTOCK / SELLSTOCK → transaction_type "Buy" / "Sell"
  - INCOME (dividends) → transaction_type "Dividend"  (included in PnL)
  - INVBANKTRAN (fees) → transaction_type "Other Fee"  (excluded from PnL)
  - INVBAL              → final cash + stock balance for initial-capital inference
    (InvBalance also carries the account id and the <DTASOF>/<DTSTART>/<DTEND>
    dates so it can contribute a TWR statement period)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional, Union

import pandas as pd


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SecurityInfo:
    unique_id: str
    ticker: str            # OCC format for options, e.g. "SPXW  260102P06730000"
    secname: str
    expiry_date: Optional[date]   # set for options only
    sh_per_contract: int


@dataclass
class InvBalance:
    cash: float
    stock_value: float
    account_id: str = "Unknown"   # <ACCTID> of the exporting IBKR account
    as_of: Optional[date] = None  # <DTASOF> — balance snapshot date (period end)
    dt_start: Optional[date] = None  # <DTSTART> — transaction-window start
    dt_end: Optional[date] = None    # <DTEND> — transaction-window end

    @property
    def total(self) -> float:
        return self.cash + self.stock_value

    def to_statement_period(self) -> "TwrPeriod":
        """Convert this QFX balance snapshot to a TWR statement period.

        A QFX file supplies an **ending** value only (no beginning value), so
        ``beginning_value`` is a 0.0 placeholder: the statement-based consumers
        (``compute_strategy_twr``, ``statement_external_flows``, the capital
        builders) never read it, and ``compute_twr`` would skip a ``<= 0``
        beginning if one were ever misrouted there.
        """
        from spx_trade_desk.tradelog.domain.return_metrics import TwrPeriod

        return TwrPeriod(
            period_start=self.dt_start or self.as_of,
            beginning_value=0.0,
            ending_value=self.total,
            period_end=self.as_of,
        )


def resolve_qfx_account_id(bal: InvBalance, df: pd.DataFrame) -> str:
    """Return the account id a QFX balance belongs to.

    Prefers the balance's own account id (from ``<ACCTID>``); falls back to
    the single ``U*`` (IBKR) account present in the merged ledger.
    """
    if bal.account_id and bal.account_id != "Unknown":
        return bal.account_id
    u_accounts = df[df["account_id"].astype(str).str.startswith("U", na=False)]["account_id"].unique()
    return str(u_accounts[0]) if len(u_accounts) == 1 else bal.account_id


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _tag_val(block: str, tag: str) -> Optional[str]:
    """Return the scalar value of an OFX SGML leaf tag, e.g. <TOTAL>42.5 → '42.5'."""
    match = re.search(rf"<{tag}>([^<\n]+)", block)
    return match.group(1).strip() if match else None


def _parse_ofx_date(dtstr: Optional[str]) -> Optional[date]:
    """Parse OFX datetime '20260225141412.000[-5:EST]' → date(2026, 2, 25)."""
    if not dtstr:
        return None
    try:
        return datetime.strptime(dtstr[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _to_float(val: Optional[str]) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val.replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# SECLIST → security map   (UNIQUEID/CONID → SecurityInfo)
# ---------------------------------------------------------------------------

def _build_security_map(text: str) -> Dict[str, SecurityInfo]:
    """Parse <SECLIST> and return UNIQUEID → SecurityInfo lookup."""
    sec_map: Dict[str, SecurityInfo] = {}

    seclist_match = re.search(r"<SECLIST>(.*?)</SECLIST>", text, re.DOTALL)
    if not seclist_match:
        return sec_map
    seclist_text = seclist_match.group(1)

    # Options
    for block in re.findall(r"<OPTINFO>(.*?)</OPTINFO>", seclist_text, re.DOTALL):
        uid = _tag_val(block, "UNIQUEID")
        ticker = _tag_val(block, "TICKER")
        secname = _tag_val(block, "SECNAME")
        expiry_raw = _tag_val(block, "DTEXPIRE")
        shperctrct = _tag_val(block, "SHPERCTRCT")
        if uid is None or ticker is None:
            continue
        sec_map[uid] = SecurityInfo(
            unique_id=uid,
            ticker=ticker,
            secname=secname or ticker,
            expiry_date=_parse_ofx_date(expiry_raw),
            sh_per_contract=int(shperctrct) if shperctrct else 100,
        )

    # Stocks / ETFs (e.g. BIL)
    for block in re.findall(r"<STOCKINFO>(.*?)</STOCKINFO>", seclist_text, re.DOTALL):
        uid = _tag_val(block, "UNIQUEID")
        ticker = _tag_val(block, "TICKER")
        secname = _tag_val(block, "SECNAME")
        if uid is None or ticker is None:
            continue
        sec_map[uid] = SecurityInfo(
            unique_id=uid,
            ticker=ticker,
            secname=secname or ticker,
            expiry_date=None,
            sh_per_contract=1,
        )

    return sec_map


# ---------------------------------------------------------------------------
# INVBAL parser
# ---------------------------------------------------------------------------

def _parse_invbal(text: str) -> InvBalance:
    invbal_match = re.search(r"<INVBAL>(.*?)</INVBAL>", text, re.DOTALL)
    if not invbal_match:
        return InvBalance(cash=0.0, stock_value=0.0)
    invbal_text = invbal_match.group(1)

    cash = _to_float(_tag_val(invbal_text, "AVAILCASH")) or 0.0

    stock_value = 0.0
    for bal_block in re.findall(r"<BAL>(.*?)</BAL>", invbal_text, re.DOTALL):
        name = _tag_val(bal_block, "NAME")
        if name and name.strip().lower() == "stock":
            v = _to_float(_tag_val(bal_block, "VALUE"))
            if v is not None:
                stock_value = v
            break

    return InvBalance(cash=cash, stock_value=stock_value)


# ---------------------------------------------------------------------------
# Per-block transaction parsers
# ---------------------------------------------------------------------------

def _invtran_fields(block: str):
    """Extract (trade_date, memo) from an <INVTRAN> sub-block."""
    invtran_match = re.search(r"<INVTRAN>(.*?)</INVTRAN>", block, re.DOTALL)
    if not invtran_match:
        return None, None
    invtran = invtran_match.group(1)
    dttrade = _tag_val(invtran, "DTTRADE")
    memo = _tag_val(invtran, "MEMO")
    return _parse_ofx_date(dttrade), memo


def _parse_invbuy(block: str, sec_map: Dict[str, SecurityInfo],
                  account_id: str, row_num: int) -> Optional[dict]:
    """Parse an ``<INVBUY>`` block (inside ``<BUYOPT>`` or ``<BUYSTOCK>``)."""
    trade_date, memo = _invtran_fields(block)
    if trade_date is None:
        return None

    invbuy = re.search(r"<INVBUY>(.*?)</INVBUY>", block, re.DOTALL)
    if not invbuy:
        return None
    ib = invbuy.group(1)

    uid = _tag_val(ib, "UNIQUEID")
    units = _to_float(_tag_val(ib, "UNITS")) or 0.0
    unitprice = _to_float(_tag_val(ib, "UNITPRICE")) or 0.0
    commission = _to_float(_tag_val(ib, "COMMISSION")) or 0.0
    total = _to_float(_tag_val(ib, "TOTAL")) or 0.0

    sec = sec_map.get(uid or "")
    ticker = sec.ticker if sec else (uid or "")
    secname = sec.secname if sec else ticker
    sh_per_contract = sec.sh_per_contract if sec else 100

    gross_amount = -(abs(units) * unitprice * sh_per_contract)  # outflow

    return {
        "activity_date": trade_date,
        "account_id": account_id,
        "description": secname,
        "transaction_type": "Buy",
        "symbol": ticker,
        "quantity": units,
        "price": unitprice,
        "gross_amount": gross_amount,
        "commission": commission,
        "net_amount": total,
        "source_row": row_num,
    }


def _parse_invsell(block: str, sec_map: Dict[str, SecurityInfo],
                   account_id: str, row_num: int) -> Optional[dict]:
    """Parse an ``<INVSELL>`` block (inside ``<SELLOPT>`` or ``<SELLSTOCK>``)."""
    trade_date, memo = _invtran_fields(block)
    if trade_date is None:
        return None

    invsell = re.search(r"<INVSELL>(.*?)</INVSELL>", block, re.DOTALL)
    if not invsell:
        return None
    ib = invsell.group(1)

    uid = _tag_val(ib, "UNIQUEID")
    units = _to_float(_tag_val(ib, "UNITS")) or 0.0
    unitprice = _to_float(_tag_val(ib, "UNITPRICE")) or 0.0
    commission = _to_float(_tag_val(ib, "COMMISSION")) or 0.0
    total = _to_float(_tag_val(ib, "TOTAL")) or 0.0

    sec = sec_map.get(uid or "")
    ticker = sec.ticker if sec else (uid or "")
    secname = sec.secname if sec else ticker
    sh_per_contract = sec.sh_per_contract if sec else 100

    gross_amount = abs(units) * unitprice * sh_per_contract  # inflow

    return {
        "activity_date": trade_date,
        "account_id": account_id,
        "description": secname,
        "transaction_type": "Sell",
        "symbol": ticker,
        "quantity": units,
        "price": unitprice,
        "gross_amount": gross_amount,
        "commission": commission,
        "net_amount": total,
        "source_row": row_num,
    }


def _parse_income(block: str, sec_map: Dict[str, SecurityInfo],
                  account_id: str, row_num: int) -> Optional[dict]:
    trade_date, memo = _invtran_fields(block)
    if trade_date is None:
        return None

    uid = _tag_val(block, "UNIQUEID")
    total = _to_float(_tag_val(block, "TOTAL")) or 0.0

    sec = sec_map.get(uid or "")
    ticker = sec.ticker if sec else (uid or "")
    description = memo or (sec.secname if sec else ticker) or "Dividend"

    return {
        "activity_date": trade_date,
        "account_id": account_id,
        "description": description,
        "transaction_type": "Dividend",
        "symbol": ticker,
        "quantity": None,
        "price": None,
        "gross_amount": total,
        "commission": 0.0,
        "net_amount": total,
        "source_row": row_num,
    }


def _parse_invbanktran(block: str, account_id: str, row_num: int) -> Optional[dict]:
    stmttrn = re.search(r"<STMTTRN>(.*?)</STMTTRN>", block, re.DOTALL)
    if not stmttrn:
        return None
    st = stmttrn.group(1)

    dtposted = _tag_val(st, "DTPOSTED")
    trade_date = _parse_ofx_date(dtposted)
    if trade_date is None:
        return None

    trnamt = _to_float(_tag_val(st, "TRNAMT")) or 0.0
    memo = _tag_val(st, "MEMO") or "Bank Transaction"

    return {
        "activity_date": trade_date,
        "account_id": account_id,
        "description": memo,
        "transaction_type": "Other Fee",
        "symbol": "",
        "quantity": None,
        "price": None,
        "gross_amount": trnamt,
        "commission": 0.0,
        "net_amount": trnamt,
        "source_row": row_num,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_EMPTY_COLUMNS = [
    "activity_date", "account_id", "description", "transaction_type",
    "symbol", "quantity", "price", "gross_amount", "commission",
    "net_amount", "source_row",
]


def load_transactions_qfx(
    file_or_path: Union[bytes, str, Path, object],
) -> tuple[pd.DataFrame, InvBalance]:
    """
    Parse a QFX/OFX investment statement.

    Parameters
    ----------
    file_or_path:
        Raw bytes, a file-like object (e.g. Streamlit UploadedFile), or a
        path (str / Path) to the .qfx file.

    Returns
    -------
    (DataFrame, InvBalance)
        DataFrame has the same column schema as load_transactions_csv().
        InvBalance carries final cash and equity values from <INVBAL>.
    """
    if isinstance(file_or_path, (str, Path)):
        text = Path(file_or_path).read_text(encoding="latin-1", errors="replace")
    elif isinstance(file_or_path, bytes):
        text = file_or_path.decode("latin-1", errors="replace")
    else:
        raw = file_or_path.read()
        text = raw.decode("latin-1", errors="replace")

    # ---- account ID --------------------------------------------------------
    acctid_match = re.search(r"<ACCTID>([^<\n]+)", text)
    account_id = acctid_match.group(1).strip() if acctid_match else "Unknown"

    # ---- security lookup table (SECLIST at end of file) --------------------
    sec_map = _build_security_map(text)

    # ---- transactions (limit search to INVTRANLIST to avoid SECLIST noise) -
    tl_match = re.search(r"<INVTRANLIST>(.*?)</INVTRANLIST>", text, re.DOTALL)
    tran_text = tl_match.group(1) if tl_match else text

    rows: list[dict] = []
    row_num = 1

    for block in re.findall(r"<BUYOPT>(.*?)</BUYOPT>", tran_text, re.DOTALL):
        row = _parse_invbuy(block, sec_map, account_id, row_num)
        if row:
            rows.append(row)
            row_num += 1

    for block in re.findall(r"<BUYSTOCK>(.*?)</BUYSTOCK>", tran_text, re.DOTALL):
        row = _parse_invbuy(block, sec_map, account_id, row_num)
        if row:
            rows.append(row)
            row_num += 1

    for block in re.findall(r"<SELLOPT>(.*?)</SELLOPT>", tran_text, re.DOTALL):
        row = _parse_invsell(block, sec_map, account_id, row_num)
        if row:
            rows.append(row)
            row_num += 1

    for block in re.findall(r"<SELLSTOCK>(.*?)</SELLSTOCK>", tran_text, re.DOTALL):
        row = _parse_invsell(block, sec_map, account_id, row_num)
        if row:
            rows.append(row)
            row_num += 1

    for block in re.findall(r"<INCOME>(.*?)</INCOME>", tran_text, re.DOTALL):
        row = _parse_income(block, sec_map, account_id, row_num)
        if row:
            rows.append(row)
            row_num += 1

    for block in re.findall(r"<INVBANKTRAN>(.*?)</INVBANKTRAN>", tran_text, re.DOTALL):
        row = _parse_invbanktran(block, account_id, row_num)
        if row:
            rows.append(row)
            row_num += 1

    if not rows:
        df = pd.DataFrame(columns=_EMPTY_COLUMNS)
    else:
        df = pd.DataFrame(rows)
        df = df.sort_values("activity_date").reset_index(drop=True)

    invbal = _parse_invbal(text)
    invbal.account_id = account_id
    invbal.as_of = _parse_ofx_date(_tag_val(text, "DTASOF"))
    invbal.dt_start = _parse_ofx_date(_tag_val(text, "DTSTART"))
    invbal.dt_end = _parse_ofx_date(_tag_val(text, "DTEND"))
    return df, invbal
