"""Calendar matrix construction for the daily realized-PnL heatmap.

Extracted from the source repo's ``src/ui/tab_calendar.py`` so the MCP server can
build calendar matrices without importing streamlit or plotly. The two functions
below are byte-identical to their originals; only the imports differ.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd


def _fmt_signed(value: float, decimals: int = 0) -> str:
    if pd.isna(value):
        return ""
    if decimals == 0:
        rounded = int(round(float(value)))
        return f"+{rounded}" if rounded > 0 else f"{rounded}"
    number = float(value)
    return f"+{number:.{decimals}f}" if number > 0 else f"{number:.{decimals}f}"


def _build_calendar_matrix(
    daily_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str], pd.DataFrame]:
    if daily_df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [], pd.DataFrame()

    start = pd.to_datetime(daily_df["activity_date"].min())
    end = pd.to_datetime(daily_df["activity_date"].max())

    start_week = start - timedelta(days=int(start.weekday()))
    end_week = end + timedelta(days=int(6 - end.weekday()))

    all_days = pd.date_range(start=start_week, end=end_week, freq="D")
    date_df = pd.DataFrame({"activity_date": all_days.date})
    merged = date_df.merge(daily_df, on="activity_date", how="left")

    # Only zero-fill days within the actual data range; days outside stay NaN
    # so they render transparent on the heatmap.
    in_range_mask = (
        (pd.to_datetime(merged["activity_date"]) >= start) &
        (pd.to_datetime(merged["activity_date"]) <= end)
    )
    fill_cols = [c for c in merged.columns if c != "activity_date"]
    merged.loc[in_range_mask, fill_cols] = merged.loc[in_range_mask, fill_cols].fillna(0)

    merged["date_ts"] = pd.to_datetime(merged["activity_date"])
    merged["date_str"] = merged["date_ts"].dt.strftime("%Y-%m-%d")
    merged["weekday"] = merged["date_ts"].dt.weekday
    merged["week_start"] = merged["date_ts"] - pd.to_timedelta(merged["weekday"], unit="D")

    unique_week_starts = sorted(merged["week_start"].unique())
    week_map = {week_start: idx + 1 for idx, week_start in enumerate(unique_week_starts)}
    merged["week_seq"] = merged["week_start"].map(week_map).astype(int)

    pivot_pnl = (
        merged.pivot(index="week_seq", columns="weekday", values="realized_pnl")
        .reindex(columns=range(7), fill_value=0.0)
        .sort_index()
    )
    pivot_commission = (
        merged.pivot(index="week_seq", columns="weekday", values="commission_spent")
        .reindex(columns=range(7), fill_value=0.0)
        .sort_index()
    )
    pivot_date = (
        merged.pivot(index="week_seq", columns="weekday", values="date_str")
        .reindex(columns=range(7), fill_value="")
        .sort_index()
    )

    weekly_summary = (
        merged.groupby("week_seq", as_index=True)
        .agg(
            weekly_pnl=("realized_pnl", "sum"),
            weekly_commission=("commission_spent", "sum"),
            weekly_options=("option_contracts_traded", "sum"),
            week_start_date=("date_str", "first"),
            week_end_date=("date_str", "last"),
        )
        .sort_index()
    )

    weekday_pnl = pivot_pnl.copy()
    weekday_commission = pivot_commission.copy()

    for weekend_day in (5, 6):
        weekday_pnl[weekend_day] = np.nan
        weekday_commission[weekend_day] = np.nan

    text_cells = weekday_pnl.copy().astype(object)
    for week in text_cells.index:
        for weekday in range(7):
            value = weekday_pnl.at[week, weekday]
            text_cells.at[week, weekday] = _fmt_signed(value, 1)

    week_labels = [f"Week {week}" for week in weekday_pnl.index]

    weekly_text = weekly_summary.copy().astype(object)
    weekly_text["label"] = weekly_text.apply(
        lambda row: f"{_fmt_signed(row['weekly_pnl'], 1)} Opt:{int(round(row['weekly_options']))}",
        axis=1,
    )

    return weekday_pnl, weekday_commission, pivot_date, text_cells, week_labels, weekly_text
