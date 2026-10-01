"""
E*Trade trades-download CSV parser.

Parses the CSV produced by E*Trade ("Downloads -> Trades") and returns a
DataFrame with the same column schema as the other loaders:

    activity_date, account_id, description, transaction_type, symbol,
    quantity, price, gross_amount, commission, net_amount, source_row

The CSV carries no account number, so rows are tagged with a virtual
``account_id`` (default ``"E*Trade"``).  Callers that load an E*Trade PDF
statement alongside the CSV should pass the real account id so the merge
layer dedups the overlapping option trades.

Key facts about the file (verified against a real export):

* Header: ``Trade Date,Order Type,Security,Cusip,Transaction Description,
  Quantity,Executed Price,Commission,Net Amount``; dates are ``M/D/YYYY``.
* **Every ``Net Amount`` is an unsigned magnitude** — a ``Buy Open`` shows
  ``+8.05`` (a debit).  The loader signs by order type: ``Buy*`` rows are
  negative, ``Sell*`` rows positive.  ``Option Expire`` rows are always net
  zero and are skipped entirely (including them would double the visible
  contract volume and break contract net-quantity heuristics; the P&L is
  fully realized at open: credit received minus debit paid).
* The ``Commission`` column is *per contract* for options (2 contracts x
  $1.03) but *total* for stocks, so the actual commission is derived as
  ``abs(abs(gross) - abs(net))`` — the same convention as
  ``load_etrade_pdf``.  ``Net Amount`` is taken verbatim and never
  recomputed.
* Stock trades (TSLA, MSFT, ...) are retained in the full ledger so the
  money-weighted-return / reconciliation calculations see all cash
  movements; strategy views filter them out (see
  ``src/domain/strategy_filter.py``).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO, Optional, Union

import pandas as pd

from spx_trade_desk.tradelog.domain.parse_option_symbol import build_occ_symbol

log = logging.getLogger(__name__)

# Default virtual account id used when no E*Trade statement supplies a real one.
ETRADE_ACCOUNT_ID = "E*Trade"

# Required header cells for an E*Trade trades CSV (normalized for matching).
ETRADE_HEADER = [
    "trade date",
    "order type",
    "security",
    "cusip",
    "transaction description",
    "quantity",
    "executed price",
    "commission",
    "net amount",
]

# OCC-like symbol embedded in Transaction Description, e.g.
#   "PUT  SPXW   01/14/26  6865.000"   -> right=PUT, underlying=SPXW,
#                                        expiry=01/14/26, strike=6865.000
_OPT_PATTERN = re.compile(
    r"^\s*(PUT|CALL)\s+([A-Z.\-]+)\s+(\d{1,2})/(\d{1,2})/(\d{2})\s+([\d.]+)\s*$",
    re.IGNORECASE,
)

# Order Type -> (transaction_type, direction).  direction == +1 for a buy
# (debit), -1 for a sell (credit), matching the E*Trade PDF loader.
_ORDER_TYPE_MAP = {
    "Sell To Open": ("Sell", -1),
    "Sell To Close": ("Sell", -1),
    "Sell": ("Sell", -1),
    "Buy Open": ("Buy", +1),
    "Buy To Open": ("Buy", +1),
    "Buy to Open": ("Buy", +1),
    "Buy": ("Buy", +1),
}

# Order types that represent expiry realizations.  Net is always 0; skipped.
_EXPIRE_TYPES = {"Option Expire", "Expire", "Option Expired"}


def _to_float(raw: Optional[str]) -> Optional[float]:
    """Parse a CSV cell to float, handling '', 'N/A', '$', ',', and parens."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "N/A", "n/a", "nan", "None"):
        return None
    s = s.replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_mdy_full(s: str) -> Optional[date]:
    """Parse 'M/D/YYYY' (Trade Date)."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _parse_mdyy(s: str) -> Optional[date]:
    """Parse 'MM/DD/YY' (option expiry)."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%y").date()
    except ValueError:
        return None


def _norm_cell(cell: str) -> str:
    return str(cell).lstrip("﻿").strip()


def _extract_header_and_rows(raw_text: str) -> tuple[list[str], list[list[str]]]:
    """Locate the header row and return (normalized header, data rows)."""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(raw_text)))
    required = {c.lower() for c in ETRADE_HEADER}

    for idx, row in enumerate(rows):
        normalized = [_norm_cell(c).lower() for c in row]
        if required.issubset(set(normalized)):
            header = [_norm_cell(c) for c in row]
            return header, rows[idx + 1 :]

    raise ValueError("Could not find an E*Trade trades header in the CSV.")


def load_transactions_etrade_csv(
    file_or_path: Union[BinaryIO, str, Path],
    account_id: str = ETRADE_ACCOUNT_ID,
) -> pd.DataFrame:
    """
    Parse an E*Trade trades-download CSV.

    Parameters
    ----------
    file_or_path:
        A path (str / Path), raw bytes, or a file-like object (e.g. a
        Streamlit UploadedFile).
    account_id:
        Account id to tag the rows with.  Defaults to the virtual
        ``"E*Trade"`` account; pass the real E*Trade statement account id
        (e.g. ``"999-999999-999"``) when PDFs are loaded alongside so the
        merge layer dedups overlapping option trades.

    Returns
    -------
    pd.DataFrame
        Standard 11-column transaction schema.  All rows are kept (options
        and stocks); ``Option Expire`` rows are dropped.
    """
    if isinstance(file_or_path, (str, Path)):
        raw_text = Path(file_or_path).read_text(encoding="utf-8-sig", errors="replace")
    elif isinstance(file_or_path, bytes):
        raw_text = file_or_path.decode("utf-8-sig", errors="replace")
    else:
        raw = file_or_path.read()
        if isinstance(raw, bytes):
            raw_text = raw.decode("utf-8-sig", errors="replace")
        else:
            raw_text = raw

    header, data_rows = _extract_header_and_rows(raw_text)

    # Rebuild a clean table and let pandas handle CSV quoting / types.
    import csv
    import io

    cleaned = io.StringIO()
    writer = csv.writer(cleaned, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(data_rows)
    cleaned.seek(0)

    df = pd.read_csv(cleaned, dtype=str)

    # ---- activity date -----------------------------------------------------
    parsed = pd.to_datetime(df["Trade Date"], format="%m/%d/%Y", errors="coerce")
    if parsed.isna().any():
        parsed = parsed.fillna(pd.to_datetime(df["Trade Date"], errors="coerce"))
    df["activity_date"] = parsed.dt.date
    df = df.dropna(subset=["activity_date"]).reset_index(drop=True)

    rows: list[dict] = []
    for _, row in df.iterrows():
        order_type = _norm_cell(row.get("Order Type") or "")
        tx_type, direction = _ORDER_TYPE_MAP.get(order_type, (None, None))

        # Skip expiry rows entirely (net always 0).
        if order_type in _EXPIRE_TYPES:
            continue

        quantity = abs(_to_float(row.get("Quantity")) or 0.0)
        price = _to_float(row.get("Executed Price")) or 0.0
        net_mag = abs(_to_float(row.get("Net Amount")) or 0.0)
        desc = _norm_cell(row.get("Transaction Description") or "")

        # Option or stock?
        m = _OPT_PATTERN.match(desc)
        if m:
            right_raw = m.group(1)
            underlying = m.group(2).strip()
            expiry = _parse_mdyy(f"{m.group(3)}/{m.group(4)}/{m.group(5)}")
            strike = float(m.group(6))
            mult = 100.0
            symbol = build_occ_symbol(underlying, expiry, right_raw, strike)
        else:
            underlying = None
            expiry = None
            mult = 1.0
            symbol = _norm_cell(row.get("Security") or "")

        if direction is None:
            # Unknown order type — keep the row visible but neutral.
            log.warning("E*Trade CSV: unrecognized Order Type %r kept with zero PnL", order_type)
            rows.append({
                "activity_date": row["activity_date"],
                "account_id": account_id,
                "description": desc,
                "transaction_type": order_type or "Unknown",
                "symbol": symbol,
                "quantity": 0.0,
                "price": price,
                "gross_amount": 0.0,
                "commission": 0.0,
                "net_amount": 0.0,
            })
            continue

        signed_qty = direction * quantity
        # NOTE: quantity sign and net sign use opposite conventions — a Buy
        # adds quantity (positive) but is a debit (negative net), a Sell
        # removes quantity (negative) but is a credit (positive net).
        gross = -direction * quantity * price * mult
        signed_net = -direction * net_mag
        commission = round(abs(abs(gross) - abs(signed_net)), 2)

        rows.append({
            "activity_date": row["activity_date"],
            "account_id": account_id,
            "description": desc,
            "transaction_type": tx_type,
            "symbol": symbol,
            "quantity": signed_qty,
            "price": price,
            "gross_amount": gross,
            "commission": commission,
            "net_amount": signed_net,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        out = pd.DataFrame(columns=[
            "activity_date", "account_id", "description", "transaction_type",
            "symbol", "quantity", "price", "gross_amount", "commission",
            "net_amount",
        ])
    out["source_row"] = range(1, len(out) + 1)
    out = out.sort_values("activity_date").reset_index(drop=True)
    return out
