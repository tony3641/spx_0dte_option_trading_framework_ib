"""Transaction DataFrame merge with cross-file deduplication.

Extracted from ``app.py`` so both the Streamlit entry-point and the MCP
server can import the same merge logic.
"""

from __future__ import annotations

import pandas as pd

# Columns used to identify duplicate rows when merging multiple source files.
DEDUP_KEY = ["activity_date", "account_id", "symbol", "quantity", "net_amount"]


def merge_transaction_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate DataFrames and drop cross-file duplicates.

    Each frame is tagged with a ``_src`` index so that we only dedup rows that
    appear in **different** source files.  Rows within the same file are always
    kept — they represent distinct broker executions that may happen to share
    the same (date, account, symbol, qty, net_amount) tuple (e.g. multiple
    partial fills at the same price from IBKR).
    """
    tagged: list[pd.DataFrame] = []
    for idx, df in enumerate(frames):
        tmp = df.copy()
        tmp["_src"] = idx
        tagged.append(tmp)

    merged = pd.concat(tagged, ignore_index=True)

    dup_cols = [c for c in DEDUP_KEY if c in merged.columns]
    if dup_cols and len(frames) > 1:
        # For each dedup-key group, keep ALL rows from the first source that
        # has them, and drop rows from later sources whose key already appeared
        # in an earlier source.
        keep_mask = pd.Series(True, index=merged.index)
        seen: dict[tuple, int] = {}  # dedup_key_tuple → _src value

        for i, row in merged.iterrows():
            key = tuple(row[c] for c in dup_cols)
            src = row["_src"]
            if key not in seen:
                seen[key] = src
            elif seen[key] != src:
                # This key already contributed by a different source file —
                # mark this row for removal.
                keep_mask.at[i] = False

        merged = merged[keep_mask]

    merged = merged.drop(columns=["_src"], errors="ignore")
    merged = merged.sort_values("activity_date").reset_index(drop=True)
    # Re-number source_row after merge
    merged["source_row"] = range(1, len(merged) + 1)
    return merged
