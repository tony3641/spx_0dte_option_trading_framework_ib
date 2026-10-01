from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf


def load_spx_daily(start_date: date, end_date: date) -> pd.DataFrame:
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date.")

    fetch_start = start_date - timedelta(days=10)
    fetch_end = end_date + timedelta(days=2)

    # Use Ticker.history() — more cloud-deployment friendly than yf.download().
    ticker = yf.Ticker("^GSPC")
    data = ticker.history(
        start=fetch_start.isoformat(),
        end=fetch_end.isoformat(),
        interval="1d",
        auto_adjust=True,
        timeout=20,
    )

    if data is None or data.empty:
        return pd.DataFrame(columns=["activity_date", "spx_close", "spx_return"])

    # Ticker.history() always returns a flat column index; "Close" is the adjusted close.
    close = data["Close"].copy()

    frame = close.rename("spx_close").to_frame().reset_index()
    date_col = "Date" if "Date" in frame.columns else frame.columns[0]
    frame["activity_date"] = pd.to_datetime(frame[date_col]).dt.date
    frame["spx_close"] = pd.to_numeric(frame["spx_close"], errors="coerce")

    out = frame[["activity_date", "spx_close"]].dropna().drop_duplicates(subset=["activity_date"])
    out = out.sort_values("activity_date").reset_index(drop=True)
    out["spx_return"] = out["spx_close"].pct_change()

    return out
