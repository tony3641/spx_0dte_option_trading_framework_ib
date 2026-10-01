from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import BinaryIO, Union

import pandas as pd


REQUIRED_COLS = [
    "Date",
    "Account",
    "Description",
    "Transaction Type",
    "Symbol",
    "Quantity",
    "Price",
    "Price Currency",
    "Gross Amount",
    "Commission",
    "Net Amount",
]


def _to_float(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("$", "", regex=False)
        .str.replace("(", "-", regex=False)
        .str.replace(")", "", regex=False)
        .str.strip()
    )
    cleaned = cleaned.replace({"": None, "nan": None, "None": None})
    return pd.to_numeric(cleaned, errors="coerce")


def _extract_transaction_history_text(raw_text: str) -> str:
    required_normalized = {col.lower() for col in REQUIRED_COLS}

    def _norm_header(cell: str) -> str:
        return str(cell).lstrip("\ufeff").strip().lower()

    rows = list(csv.reader(io.StringIO(raw_text)))
    header_idx = None
    header = None

    for idx, row in enumerate(rows):
        normalized = [_norm_header(cell) for cell in row]
        if required_normalized.issubset(set(normalized)):
            header_idx = idx
            header = [str(cell).lstrip("\ufeff").strip() for cell in row]
            break

    if header_idx is None or header is None:
        raise ValueError("Could not find Transaction History table in CSV.")

    expected_len = len(header)
    table_rows = [header]

    for row in rows[header_idx + 1 :]:
        normalized = [cell.strip() for cell in row]

        if not any(normalized):
            if len(table_rows) > 1:
                break
            continue

        if len(normalized) < expected_len:
            break

        table_rows.append(normalized[:expected_len])

    if len(table_rows) == 1:
        raise ValueError("Transaction History appears empty.")

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(table_rows)
    return output.getvalue()


def load_transactions_csv(file_or_path: Union[BinaryIO, str, Path]) -> pd.DataFrame:
    if isinstance(file_or_path, (str, Path)):
        raw_text = Path(file_or_path).read_text(encoding="utf-8-sig", errors="replace")
    else:
        raw = file_or_path.read()
        raw_text = raw.decode("utf-8-sig", errors="replace")

    tx_text = _extract_transaction_history_text(raw_text)
    df = pd.read_csv(io.StringIO(tx_text), dtype=str)

    df.columns = [str(col).lstrip("\ufeff").strip() for col in df.columns]
    lookup = {col.lower(): col for col in df.columns}

    missing = [col for col in REQUIRED_COLS if col.lower() not in lookup]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    df = df.rename(columns={lookup[col.lower()]: col for col in REQUIRED_COLS})

    for col in REQUIRED_COLS:
        df[col] = df[col].fillna("").astype(str).str.strip()

    out = pd.DataFrame(
        {
            "activity_date": pd.to_datetime(df["Date"], errors="coerce").dt.date,
            "account_id": df["Account"],
            "description": df["Description"],
            "transaction_type": df["Transaction Type"],
            "symbol": df["Symbol"],
            "quantity": _to_float(df["Quantity"]),
            "price": _to_float(df["Price"]),
            "gross_amount": _to_float(df["Gross Amount"]),
            "commission": _to_float(df["Commission"]),
            "net_amount": _to_float(df["Net Amount"]),
        }
    )

    out["source_row"] = range(1, len(out) + 1)
    out = out.dropna(subset=["activity_date"]).reset_index(drop=True)
    return out
