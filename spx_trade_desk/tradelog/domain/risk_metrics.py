"""Pure risk-metric computations. No Streamlit dependency.

Extracted from ``src/ui/tab_risk.py`` so both the Streamlit UI and the MCP
server can import the same function.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def calculate_risk_metrics(
    view: pd.DataFrame,
    initial_capital: float,
    annual_rf: float,
    spx_df: pd.DataFrame | None = None,
    vix_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Compute risk-adjusted performance metrics for a daily-PnL view.

    Parameters
    ----------
    view:
        Daily PnL DataFrame.  Must contain at least ``activity_date`` and
        ``realized_pnl``.  Should already be filtered to the desired time
        window.
    initial_capital:
        Account starting capital in dollars.
    annual_rf:
        Annual risk-free rate expressed as a decimal (e.g. 0.05 → 5 %).
    spx_df:
        Optional SPX benchmark DataFrame with columns ``activity_date``,
        ``spx_close``, and ``spx_return``.
    vix_df:
        Optional VIX DataFrame with columns ``activity_date``,
        ``vix_open``, ``vix_high``, ``vix_low``, ``vix_close``, and
        ``vix_change``.

    Returns
    -------
    dict
        Keys include ``period_return``, ``sharpe``, ``sortino``,
        ``std_daily``, ``positive_cycles``, ``negative_cycles``,
        ``max_gain``, ``max_loss``, ``commission_drag``,
        ``max_recovery_days``, ``ongoing_recovery_days``, ``net_ev``,
        and SPX / VIX benchmark fields.

        NaN values are represented as ``float('nan')`` — callers should
        serialise them appropriately for their output format.
    """
    if view.empty:
        return {}

    # ------------------------------------------------------------------
    # Fill ALL business days between first and last date so that
    # non-trading days (zero PnL, zero return) are properly included in
    # Sharpe / Sortino and benchmark alignment.
    # ------------------------------------------------------------------
    all_bdays = pd.bdate_range(
        start=view["activity_date"].min(),
        end=view["activity_date"].max(),
    )
    full_cal = pd.DataFrame({"activity_date": all_bdays.date})
    view = (
        full_cal
        .merge(view, on="activity_date", how="left")
        .fillna({"realized_pnl": 0.0, "commission_spent": 0.0,
                 "option_contracts_traded": 0, "trade_count": 0})
        .sort_values("activity_date")
        .reset_index(drop=True)
    )

    # ------------------------------------------------------------------
    # Core return series
    # ------------------------------------------------------------------
    rf_daily = annual_rf / 252.0
    pnl = view["realized_pnl"].astype(float)
    daily_returns = pnl / initial_capital
    valid_returns = daily_returns.replace([np.inf, -np.inf], np.nan).dropna()

    period_return = pnl.sum() / initial_capital

    # -- Sharpe & Sortino -------------------------------------------------
    std_daily = valid_returns.std(ddof=1) if len(valid_returns) > 1 else np.nan
    excess_returns = valid_returns - rf_daily
    sharpe = np.nan
    if len(valid_returns) > 1 and std_daily and not np.isclose(std_daily, 0.0):
        sharpe = (excess_returns.mean() / std_daily) * math.sqrt(252)

    downside = np.minimum(excess_returns, 0.0)
    downside_std = downside.std(ddof=1) if len(downside) > 1 else np.nan
    sortino = np.nan
    if len(downside) > 1 and downside_std and not np.isclose(downside_std, 0.0):
        sortino = (excess_returns.mean() / downside_std) * math.sqrt(252)

    # -- Cycles & extremes -------------------------------------------------
    positive_cycles = int((pnl > 0).sum())
    negative_cycles = int((pnl < 0).sum())
    max_gain = float(pnl.max()) if not pnl.empty else np.nan
    max_loss = float(pnl.min()) if not pnl.empty else np.nan
    gross_gains = float(pnl[pnl > 0].sum())
    total_commission = float(view["commission_spent"].astype(float).sum())
    commission_drag = np.nan
    if gross_gains > 0:
        commission_drag = total_commission / gross_gains

    # -- Drawdown recovery -------------------------------------------------
    equity = pnl.cumsum().reset_index(drop=True)
    dates = pd.to_datetime(view["activity_date"]).reset_index(drop=True)

    peak_equity = float(equity.iloc[0])
    peak_date = dates.iloc[0]
    drawdown_peak_date = None
    max_recovery_days = 0

    for idx in range(1, len(equity)):
        current_equity = float(equity.iloc[idx])
        current_date = dates.iloc[idx]

        if current_equity >= peak_equity:
            if drawdown_peak_date is not None:
                recovery_days = int((current_date - drawdown_peak_date).days)
                max_recovery_days = max(max_recovery_days, recovery_days)
                drawdown_peak_date = None
            peak_equity = current_equity
            peak_date = current_date
        elif drawdown_peak_date is None:
            drawdown_peak_date = peak_date

    ongoing_recovery_days = None
    if drawdown_peak_date is not None:
        ongoing_recovery_days = int((dates.iloc[-1] - drawdown_peak_date).days)

    # -- Net EV (day-level expectancy) -------------------------------------
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    net_ev = np.nan
    cycle_total = len(wins) + len(losses)
    if cycle_total > 0 and len(wins) > 0 and len(losses) > 0:
        p_win = len(wins) / cycle_total
        p_loss = len(losses) / cycle_total
        avg_win = float(wins.mean())
        avg_loss = abs(float(losses.mean()))
        net_ev = p_win * avg_win - p_loss * avg_loss
    elif cycle_total > 0 and len(wins) > 0:
        net_ev = float(wins.mean())
    elif cycle_total > 0 and len(losses) > 0:
        net_ev = float(losses.mean())

    # ------------------------------------------------------------------
    # SPX benchmark
    # ------------------------------------------------------------------
    spx_corr = np.nan
    spx_beta = np.nan
    spx_alpha = np.nan
    spx_period_return = np.nan
    return_delta_vs_spx = np.nan
    spx_overlap_days = 0
    if spx_df is not None and not spx_df.empty:
        strategy = view[["activity_date"]].copy()
        strategy["strategy_return"] = daily_returns.values

        benchmark = spx_df[["activity_date", "spx_return"]].copy()
        aligned = strategy.merge(benchmark, on="activity_date", how="inner").dropna()
        spx_overlap_days = int(len(aligned))

        period_start = pd.to_datetime(view["activity_date"].min())
        period_end = pd.to_datetime(view["activity_date"].max())
        benchmark_close = spx_df[["activity_date", "spx_close"]].copy()
        benchmark_close["activity_date"] = pd.to_datetime(benchmark_close["activity_date"])
        benchmark_close["spx_close"] = pd.to_numeric(benchmark_close["spx_close"], errors="coerce")
        benchmark_close = benchmark_close.dropna(subset=["spx_close"]).sort_values("activity_date")

        in_window = benchmark_close[
            (benchmark_close["activity_date"] >= period_start)
            & (benchmark_close["activity_date"] <= period_end)
        ]
        if len(in_window) >= 2:
            start_close = float(in_window.iloc[0]["spx_close"])
            end_close = float(in_window.iloc[-1]["spx_close"])
            if start_close > 0:
                spx_period_return = (end_close / start_close) - 1.0
                return_delta_vs_spx = period_return - spx_period_return

        if spx_overlap_days >= 2:
            strategy_series = aligned["strategy_return"].astype(float)
            benchmark_series = aligned["spx_return"].astype(float)
            benchmark_var = benchmark_series.var(ddof=1)

            spx_corr = strategy_series.corr(benchmark_series)

            if not pd.isna(benchmark_var) and not np.isclose(benchmark_var, 0.0):
                covariance = strategy_series.cov(benchmark_series)
                spx_beta = covariance / benchmark_var
                alpha_daily = ((strategy_series - rf_daily) - spx_beta * (benchmark_series - rf_daily)).mean()
                spx_alpha = alpha_daily * 252.0

    # ------------------------------------------------------------------
    # VIX metrics
    # ------------------------------------------------------------------
    vix_corr = np.nan
    vix_beta = np.nan
    vix_regime_table: pd.DataFrame = pd.DataFrame()
    vix_overlap_days = 0

    if vix_df is not None and not vix_df.empty:
        # VIX correlation & beta (vs vix_change, i.e. daily point moves)
        strategy = view[["activity_date"]].copy()
        strategy["strategy_return"] = daily_returns.values

        benchmark = vix_df[["activity_date", "vix_change"]].copy()
        aligned = strategy.merge(benchmark, on="activity_date", how="inner").dropna()
        vix_overlap_days = int(len(aligned))

        if vix_overlap_days >= 2:
            strategy_series = aligned["strategy_return"].astype(float)
            benchmark_series = aligned["vix_change"].astype(float)
            benchmark_var = benchmark_series.var(ddof=1)

            vix_corr = strategy_series.corr(benchmark_series)

            if not pd.isna(benchmark_var) and not np.isclose(benchmark_var, 0.0):
                covariance = strategy_series.cov(benchmark_series)
                vix_beta = covariance / benchmark_var

        # Regime breakdown — classify by VIX *high* (captures intraday spikes)
        regime_merge = view[["activity_date", "realized_pnl"]].copy()
        regime_merge = regime_merge.merge(
            vix_df[["activity_date", "vix_high", "vix_close"]],
            on="activity_date",
            how="inner",
        ).dropna(subset=["vix_high"])

        if not regime_merge.empty:
            def _classify_vix_regime(vix_high_val: float) -> str:
                if vix_high_val < 15.0:
                    return "Calm (High<15)"
                elif vix_high_val < 20.0:
                    return "Normal (High 15-20)"
                elif vix_high_val < 25.0:
                    return "Elevated (High 20-25)"
                else:
                    return "Stress (High>25)"

            regime_merge["regime"] = regime_merge["vix_high"].apply(_classify_vix_regime)

            regime_order = [
                "Calm (High<15)", "Normal (High 15-20)",
                "Elevated (High 20-25)", "Stress (High>25)",
            ]
            regime_merge["regime"] = pd.Categorical(
                regime_merge["regime"], categories=regime_order, ordered=True,
            )

            vix_regime_table = (
                regime_merge.groupby("regime", observed=False)
                .agg(
                    days=("activity_date", "count"),
                    net_pnl=("realized_pnl", "sum"),
                    win_rate=("realized_pnl", lambda s: (s > 0).mean()),
                    avg_win=("realized_pnl", lambda s: s[s > 0].mean()),
                    avg_loss=("realized_pnl", lambda s: abs(s[s < 0].mean())),
                    avg_vix_close=("vix_close", "mean"),
                )
                .reset_index()
            )
            # Compute Net EV per regime: P(win)×AvgWin − P(loss)×AvgLoss
            vix_regime_table["net_ev"] = vix_regime_table.apply(
                lambda r: (
                    r["win_rate"] * r["avg_win"]
                    - (1.0 - r["win_rate"]) * r["avg_loss"]
                    if (not pd.isna(r["win_rate"]) and not pd.isna(r["avg_win"])
                        and not pd.isna(r["avg_loss"]))
                    else (r["avg_win"] if not pd.isna(r["avg_win"])
                          else (-r["avg_loss"] if not pd.isna(r["avg_loss"]) else np.nan))
                ),
                axis=1,
            )
            vix_regime_table = vix_regime_table.drop(columns=["avg_win", "avg_loss"])

    return {
        "period_return": float(period_return),
        "sharpe": float(sharpe) if not pd.isna(sharpe) else np.nan,
        "sortino": float(sortino) if not pd.isna(sortino) else np.nan,
        "std_daily": float(std_daily) if not pd.isna(std_daily) else np.nan,
        "positive_cycles": positive_cycles,
        "negative_cycles": negative_cycles,
        "max_gain": float(max_gain) if not pd.isna(max_gain) else np.nan,
        "max_loss": float(max_loss) if not pd.isna(max_loss) else np.nan,
        "commission_drag": float(commission_drag) if not pd.isna(commission_drag) else np.nan,
        "max_recovery_days": max_recovery_days,
        "ongoing_recovery_days": ongoing_recovery_days,
        "net_ev": float(net_ev) if not pd.isna(net_ev) else np.nan,
        "spx_corr": float(spx_corr) if not pd.isna(spx_corr) else np.nan,
        "spx_beta": float(spx_beta) if not pd.isna(spx_beta) else np.nan,
        "spx_alpha": float(spx_alpha) if not pd.isna(spx_alpha) else np.nan,
        "spx_period_return": float(spx_period_return) if not pd.isna(spx_period_return) else np.nan,
        "return_delta_vs_spx": float(return_delta_vs_spx) if not pd.isna(return_delta_vs_spx) else np.nan,
        "spx_overlap_days": spx_overlap_days,
        "vix_corr": float(vix_corr) if not pd.isna(vix_corr) else np.nan,
        "vix_beta": float(vix_beta) if not pd.isna(vix_beta) else np.nan,
        "vix_overlap_days": vix_overlap_days,
        "vix_regime_table": vix_regime_table,
    }
