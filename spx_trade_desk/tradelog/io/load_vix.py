from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf


def load_vix_daily(start_date: date, end_date: date) -> pd.DataFrame:
    """Fetch CBOE VIX daily OHLC from Yahoo Finance.

    Returns a DataFrame with columns:
      activity_date  – calendar date (no time component)
      vix_open       – VIX open
      vix_high       – VIX high of day (used for regime classification)
      vix_low        – VIX low of day
      vix_close      – VIX close
      vix_change     – day-over-day point change in VIX close
    """
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date.")

    fetch_start = start_date - timedelta(days=10)
    fetch_end = end_date + timedelta(days=2)

    # Use Ticker.history() — more cloud-deployment friendly than yf.download().
    ticker = yf.Ticker("^VIX")
    data = ticker.history(
        start=fetch_start.isoformat(),
        end=fetch_end.isoformat(),
        interval="1d",
        auto_adjust=True,
        timeout=20,
    )

    if data is None or data.empty:
        return pd.DataFrame(columns=[
            "activity_date", "vix_open", "vix_high",
            "vix_low", "vix_close", "vix_change",
        ])

    # Extract OHLC columns.
    close = data["Close"].copy()
    vix_open = data["Open"].copy()
    vix_high = data["High"].copy()
    vix_low = data["Low"].copy()

    frame = pd.DataFrame({
        "vix_close": close,
        "vix_open":  vix_open,
        "vix_high":  vix_high,
        "vix_low":   vix_low,
    }).reset_index()

    date_col = "Date" if "Date" in frame.columns else frame.columns[0]
    frame["activity_date"] = pd.to_datetime(frame[date_col]).dt.date

    for col in ["vix_open", "vix_high", "vix_low", "vix_close"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    out = frame[[
        "activity_date", "vix_open", "vix_high", "vix_low", "vix_close",
    ]].dropna().drop_duplicates(subset=["activity_date"])

    out = out.sort_values("activity_date").reset_index(drop=True)
    out["vix_change"] = out["vix_close"].diff()

    return out
