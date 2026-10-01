"""
E*Trade / Morgan Stanley monthly PDF statement parser.

Extracts option trades from the "CASH FLOW ACTIVITY BY DATE" section and
produces a DataFrame with the same column schema as load_transactions_csv()
and load_transactions_qfx() so the rest of the pipeline works unchanged.

Supports:
  - Option trades (Bought/Sold PUT/CALL)  → transaction_type "Buy" / "Sell"
  - Interest Income                        → transaction_type "Dividend"
  - Multiple monthly PDFs (dedup handled by the merge layer in app.py)
  - Cross-month positions (open in month A, close/expire in month B)

Stock trades are intentionally ignored — this account is options-focused
and stock activity (purchases, transfers, RSU vests) is excluded.

Requires: pdfplumber
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from spx_trade_desk.tradelog.domain.parse_option_symbol import build_occ_symbol

try:
    import pdfplumber
except ImportError:
    pdfplumber = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Balance data class
# ---------------------------------------------------------------------------

@dataclass
class EtradeBalance:
    """Beginning / ending account value extracted from an E*Trade statement."""
    account_id: str
    period_start: date          # e.g. 2026-02-01
    beginning_value: float      # "Beginning Total Value"
    ending_value: float         # "Ending Total Value"
    cash: float = 0.0           # Cash allocation from page-3 asset table
    stock_value: float = 0.0    # Equities allocation from page-3 asset table
    period_end: Optional[date] = None  # "Ending Total Value (as of …)" date


# ---------------------------------------------------------------------------
# Column schema (same as CSV / QFX loaders)
# ---------------------------------------------------------------------------

_EMPTY_COLUMNS = [
    "activity_date", "account_id", "description", "transaction_type",
    "symbol", "quantity", "price", "gross_amount", "commission",
    "net_amount", "source_row",
]


# ---------------------------------------------------------------------------
# Amount / date helpers
# ---------------------------------------------------------------------------

def _parse_amount(raw: str) -> float:
    """Parse dollar amounts: '$71.95', '(16.05)', '(2,076.65)' → float."""
    s = raw.replace("$", "").replace(",", "").strip()
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    return float(s)


def _parse_date_md(md_str: str, year: int) -> date:
    """Parse 'M/D' with a known statement year → date."""
    month, day = md_str.split("/")
    return date(year, int(month), int(day))


# ---------------------------------------------------------------------------
# Line-matching patterns
# ---------------------------------------------------------------------------

# Option trade line:
#   2/2 2/3 Sold PUT SPXW 02/02/26 6940.000 ACTED AS AGENT 2.000 $0.3700 $71.95
_OPT_PATTERN = re.compile(
    r"^(\d{1,2}/\d{1,2})\s+(\d{1,2}/\d{1,2})\s+"   # activity date, settle date
    r"(Sold|Bought)\s+"                               # action
    r"(PUT|CALL)\s+"                                   # right
    r"(\S+)\s+"                                        # underlying (e.g. SPXW)
    r"(\d{2}/\d{2}/\d{2})\s+"                         # expiry MM/DD/YY
    r"([\d,]+(?:\.\d+)?)\s+"                           # strike
    r"ACTED\s+AS\s+AGENT\s+"                           # broker tag
    r"([\d,]+(?:\.\d+)?)\s+"                           # quantity
    r"\$?([\d,.]+)\s+"                                 # price per contract
    r"(.+)$"                                           # net amount (with $ or parens)
)

# Interest / income line:
#   2/27 Interest Income MORGAN STANLEY BANK N.A. (Period 02/01-02/28) 0.11
_INTEREST_PATTERN = re.compile(
    r"^(\d{1,2}/\d{1,2})\s+"
    r"Interest\s+Income\s+"
    r"(.+?)\s+"
    r"([\d,.]+)$"
)

# Dividend income line (e.g. stock dividends):
#   2/15 Dividend Received QUALCOMM INC ... 12.50
_DIVIDEND_PATTERN = re.compile(
    r"^(\d{1,2}/\d{1,2})\s+"
    r"(?:Dividend\s+Received|Dividend)\s+"
    r"(.+?)\s+"
    r"([\d,.]+)$"
)

# Lines to skip inside the activity section
_SKIP_PREFIXES = (
    "CLIENT STATEMENT",
    "Morgan Stanley",
    "Account Detail",
    "Activity",
    "Date",
    "CASH FLOW",
    "Purchase and Sale",
)
_SKIP_CONTAINS = ("UNSOLICITED", "INDEX OPTION", "OPENING")


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def _extract_account_id(all_text: str) -> str:
    """Extract the E*Trade account number (e.g. '999-999999-999')."""
    m = re.search(r"(\d{3}-\d{6}-\d{3})", all_text)
    return m.group(1) if m else "Unknown"


def _extract_statement_year(all_text: str) -> int:
    """Extract the statement year from the header period line."""
    m = re.search(r"For the Period.*?\b(20\d{2})\b", all_text)
    return int(m.group(1)) if m else datetime.now().year


_BALANCE_RE = re.compile(
    r"(Beginning|Ending)\s+Total\s+Value\s+"
    r"\(as\s+of\s+(\d{1,2}/\d{1,2}/\d{2,4})\)\s+"
    r"\$?([\d,]+\.\d{2})",
)

# Page-3 asset-allocation table rows:
#   "Cash $14,746.86 37.28"  and  "Equities 24,805.63 62.71 ..."
_CASH_ALLOC_RE = re.compile(
    r"^Cash\s+\$?([\d,]+\.\d{2})\s+[\d.]+"
)
_EQUITY_ALLOC_RE = re.compile(
    r"^Equities\s+([\d,]+\.\d{2})\s+[\d.]+"
)


def _extract_balance(all_text: str, account_id: str) -> Optional[EtradeBalance]:
    """Parse Beginning / Ending Total Value and cash/equity allocation."""
    beginning_value: Optional[float] = None
    ending_value: Optional[float] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    cash: float = 0.0
    stock_value: float = 0.0

    for m in _BALANCE_RE.finditer(all_text):
        label = m.group(1)          # "Beginning" or "Ending"
        date_str = m.group(2)       # e.g. "2/1/26"
        amount = float(m.group(3).replace(",", ""))

        # Parse date — handle both 2-digit and 4-digit year
        parts = date_str.split("/")
        month, day = int(parts[0]), int(parts[1])
        yr = int(parts[2])
        if yr < 100:
            yr += 2000
        dt = date(yr, month, day)

        if label == "Beginning":
            beginning_value = amount
            period_start = dt
        else:
            ending_value = amount
            period_end = dt

    # Extract cash / equity allocation from page-3 asset table
    for line in all_text.split("\n"):
        line = line.strip()
        mc = _CASH_ALLOC_RE.match(line)
        if mc:
            cash = float(mc.group(1).replace(",", ""))
        me = _EQUITY_ALLOC_RE.match(line)
        if me:
            stock_value = float(me.group(1).replace(",", ""))

    if beginning_value is not None and period_start is not None:
        return EtradeBalance(
            account_id=account_id,
            period_start=period_start,
            beginning_value=beginning_value,
            ending_value=ending_value or 0.0,
            cash=cash,
            stock_value=stock_value,
            period_end=period_end,
        )
    return None


# ---------------------------------------------------------------------------
# Core parsing
# ---------------------------------------------------------------------------

def _parse_activity_lines(
    lines: list[str],
    account_id: str,
    statement_year: int,
) -> list[dict]:
    """
    Walk through extracted text lines and pull out option trades and
    interest/dividend income from the CASH FLOW ACTIVITY BY DATE section.

    Stock trades (Bought/Sold <STOCK NAME>) are intentionally ignored.
    """
    in_activity = False
    rows: list[dict] = []

    for line in lines:
        line = line.strip()

        # Section boundaries
        if "CASH FLOW ACTIVITY BY DATE" in line:
            in_activity = True
            continue
        if in_activity and "NET CREDITS" in line:
            in_activity = False
            continue
        if not in_activity:
            continue

        # Skip boilerplate
        if any(line.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if any(s in line for s in _SKIP_CONTAINS):
            continue
        if not line:
            continue

        # --- Option trade ---
        m = _OPT_PATTERN.match(line)
        if m:
            act_date = _parse_date_md(m.group(1), statement_year)
            action = m.group(3)           # "Sold" or "Bought"
            right = m.group(4)            # "PUT" or "CALL"
            underlying = m.group(5)       # e.g. "SPXW"
            expiry = datetime.strptime(m.group(6), "%m/%d/%y").date()
            strike = float(m.group(7).replace(",", ""))
            qty = float(m.group(8).replace(",", ""))
            price = float(m.group(9).replace(",", ""))
            net_amount = _parse_amount(m.group(10))

            occ = build_occ_symbol(underlying, expiry, right, strike)
            desc = f"{right} {underlying} {expiry.strftime('%m/%d/%y')} {strike:.3f}"

            signed_qty = qty if action == "Bought" else -qty
            if action == "Bought":
                gross = -(qty * price * 100)
            else:
                gross = qty * price * 100

            commission = round(abs(abs(gross) - abs(net_amount)), 2)

            rows.append({
                "activity_date": act_date,
                "account_id": account_id,
                "description": desc,
                "transaction_type": "Buy" if action == "Bought" else "Sell",
                "symbol": occ,
                "quantity": signed_qty,
                "price": price,
                "gross_amount": gross,
                "commission": commission,
                "net_amount": net_amount,
            })
            continue

        # --- Interest income ---
        m2 = _INTEREST_PATTERN.match(line)
        if m2:
            act_date = _parse_date_md(m2.group(1), statement_year)
            rows.append({
                "activity_date": act_date,
                "account_id": account_id,
                "description": m2.group(2),
                "transaction_type": "Dividend",
                "symbol": "",
                "quantity": None,
                "price": None,
                "gross_amount": float(m2.group(3).replace(",", "")),
                "commission": 0.0,
                "net_amount": float(m2.group(3).replace(",", "")),
            })
            continue

        # --- Dividend income ---
        m3 = _DIVIDEND_PATTERN.match(line)
        if m3:
            act_date = _parse_date_md(m3.group(1), statement_year)
            rows.append({
                "activity_date": act_date,
                "account_id": account_id,
                "description": m3.group(2),
                "transaction_type": "Dividend",
                "symbol": "",
                "quantity": None,
                "price": None,
                "gross_amount": float(m3.group(3).replace(",", "")),
                "commission": 0.0,
                "net_amount": float(m3.group(3).replace(",", "")),
            })
            continue

        # Stock trades and other lines are silently skipped.

    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_transactions_etrade_pdf(
    file_or_path: Union[bytes, str, Path, object],
) -> tuple[pd.DataFrame, Optional[EtradeBalance]]:
    """
    Parse an E*Trade / Morgan Stanley monthly PDF statement.

    Parameters
    ----------
    file_or_path :
        A file path (str / Path), raw bytes, or a file-like object
        (e.g. Streamlit UploadedFile).

    Returns
    -------
    (pd.DataFrame, Optional[EtradeBalance])
        DataFrame with the same column schema as the other loaders.
        EtradeBalance holds the beginning/ending account value and
        the period start date (used for initial-capital inference).

    Raises
    ------
    ImportError
        If pdfplumber is not installed.
    ValueError
        If the PDF does not contain a recognisable activity section.

    Notes
    -----
    Multiple monthly PDFs can be loaded and merged upstream (app.py).
    Cross-month positions (opened in one statement, closed/expired in a later
    one) are handled naturally: each statement contributes its rows, and the
    existing dedup + PnL engine resolves them.
    """
    if pdfplumber is None:
        raise ImportError(
            "pdfplumber is required to parse E*Trade PDF statements. "
            "Install it with: pip install pdfplumber"
        )

    # ---- open the PDF -----------------------------------------------------
    if isinstance(file_or_path, (str, Path)):
        pdf = pdfplumber.open(str(file_or_path))
    elif isinstance(file_or_path, bytes):
        import io
        pdf = pdfplumber.open(io.BytesIO(file_or_path))
    else:
        # file-like (e.g. Streamlit UploadedFile) — read bytes then wrap
        import io
        raw = file_or_path.read()
        if isinstance(raw, str):
            raw = raw.encode("latin-1")
        pdf = pdfplumber.open(io.BytesIO(raw))

    # ---- extract all text --------------------------------------------------
    all_text_parts: list[str] = []
    for page in pdf.pages:
        t = page.extract_text()
        if t:
            all_text_parts.append(t)

    all_text = "\n".join(all_text_parts)
    lines = all_text.split("\n")

    # ---- metadata ----------------------------------------------------------
    account_id = _extract_account_id(all_text)
    statement_year = _extract_statement_year(all_text)
    balance = _extract_balance(all_text, account_id)

    # ---- parse activity rows -----------------------------------------------
    rows = _parse_activity_lines(lines, account_id, statement_year)

    if not rows:
        return pd.DataFrame(columns=_EMPTY_COLUMNS), balance

    df = pd.DataFrame(rows)
    df = df.sort_values("activity_date").reset_index(drop=True)
    df["source_row"] = range(1, len(df) + 1)
    df = df.dropna(subset=["activity_date"]).reset_index(drop=True)
    return df, balance
