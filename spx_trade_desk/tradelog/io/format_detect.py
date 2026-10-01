"""
Shared CSV format detection + routing for the transaction loaders.

Both the Streamlit app and the MCP server route ``.csv`` files through
``load_csv_by_format`` so an IBKR export and an E*Trade trades export
(different headers, same extension) are parsed by the right loader.

Detection is by exact header cells (normalized lower-case), scanning the
first few rows so IBKR files with a preamble above the real header still
classify correctly.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import BinaryIO, Union

from spx_trade_desk.tradelog.io.load_csv import load_transactions_csv
from spx_trade_desk.tradelog.io.load_etrade_csv import ETRADE_ACCOUNT_ID, load_transactions_etrade_csv

# Header signatures — exact cells, normalized (strip, lower, drop BOM).
_ETRADE_HEADER_SIG = {
    "trade date", "order type", "security", "cusip",
    "transaction description", "quantity", "executed price",
    "commission", "net amount",
}

_IBKR_HEADER_SIG = {
    "date", "account", "description", "transaction type", "symbol",
    "quantity", "price", "price currency", "gross amount", "commission",
    "net amount",
}

_MAX_SCAN_ROWS = 30


def _norm_cell(cell) -> str:
    return str(cell).lstrip("﻿").strip().lower()


def detect_csv_format(text: str) -> str:
    """Return ``"etrade"``, ``"ibkr"``, or ``"unknown"`` from CSV text."""
    reader = csv.reader(io.StringIO(text))
    for idx, row in enumerate(reader):
        if idx >= _MAX_SCAN_ROWS:
            break
        if not row or not any(str(c).strip() for c in row):
            continue
        cells = {_norm_cell(c) for c in row}
        if _ETRADE_HEADER_SIG.issubset(cells):
            return "etrade"
        if _IBKR_HEADER_SIG.issubset(cells):
            return "ibkr"
    return "unknown"


def load_csv_by_format(
    file_or_path: Union[BinaryIO, str, Path],
    account_id: str = ETRADE_ACCOUNT_ID,
) -> "object":
    """
    Detect the CSV flavor and dispatch to the matching loader.

    Parameters
    ----------
    file_or_path:
        A path (str / Path), raw bytes, or a file-like object.  File-like
        objects are rewound to position 0 before being handed to the loader.
    account_id:
        Account id used for E*Trade CSVs (see
        ``load_transactions_etrade_csv``).

    Returns
    -------
    pd.DataFrame
        A standard transaction DataFrame from the E*Trade or IBKR loader.
        If the format cannot be detected it falls back to the IBKR loader,
        which raises ``ValueError`` when the file has no IBKR header.
    """
    if isinstance(file_or_path, (str, Path)):
        text = Path(file_or_path).read_text(encoding="utf-8-sig", errors="replace")
        fmt = detect_csv_format(text)
        if fmt == "etrade":
            return load_transactions_etrade_csv(file_or_path, account_id=account_id)
        return load_transactions_csv(file_or_path)

    if isinstance(file_or_path, bytes):
        text = file_or_path.decode("utf-8-sig", errors="replace")
        if detect_csv_format(text) == "etrade":
            return load_transactions_etrade_csv(file_or_path, account_id=account_id)
        return load_transactions_csv(file_or_path)

    # File-like object: read for detection, rewind, then load.
    raw = file_or_path.read()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    fmt = detect_csv_format(text)
    file_or_path.seek(0)
    if fmt == "etrade":
        return load_transactions_etrade_csv(file_or_path, account_id=account_id)
    return load_transactions_csv(file_or_path)
