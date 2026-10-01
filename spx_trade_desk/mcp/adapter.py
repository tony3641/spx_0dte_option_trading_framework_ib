"""DataFrame and domain-object serializers for MCP JSON output.

Every function in this module converts internal objects (DataFrames,
dataclasses, numeric dicts) into plain Python dicts/lists/primitives
that are safe for JSON serialisation.

NaN and Inf are always replaced with ``None``.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from spx_trade_desk.tradelog.domain.parse_option_symbol import ParsedOption
from spx_trade_desk.tradelog.domain.pnl_engine import PnlResult
from spx_trade_desk.tradelog.io.load_etrade_pdf import EtradeBalance
from spx_trade_desk.tradelog.io.load_qfx import InvBalance


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def serialize_float(value: Any) -> float | None:
    """Convert a value to float, returning ``None`` for NaN, Inf, or None."""
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def friendly_date(d: date | str | None) -> str | None:
    """Convert a date to ISO-8601 string, or return ``None`` for None."""
    if d is None:
        return None
    if isinstance(d, str):
        return d
    if isinstance(d, (datetime, pd.Timestamp)):
        return d.strftime("%Y-%m-%d")
    return d.isoformat()


def _safe_value(val: Any) -> Any:
    """Recursively replace NaN / NaT / Inf with None in any scalar or dict."""
    if val is None:
        return None
    if isinstance(val, (int, str, bool)):
        return val
    if isinstance(val, float):
        return serialize_float(val)
    if isinstance(val, (datetime, date, pd.Timestamp)):
        return friendly_date(val)
    if isinstance(val, (np.floating,)):
        return serialize_float(float(val))
    if isinstance(val, dict):
        return {k: _safe_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_safe_value(v) for v in val]
    return val


# ---------------------------------------------------------------------------
# DataFrame helpers
# ---------------------------------------------------------------------------

def df_to_records(
    df: pd.DataFrame,
    date_cols: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert a DataFrame to a list of dicts suitable for JSON.

    Parameters
    ----------
    df:
        DataFrame to convert.
    date_cols:
        Column names whose values should be formatted as ISO-8601 strings.
        If ``None``, any column named ``*date*`` or ``*Date*`` is treated
        as a date column automatically.
    """
    if df.empty:
        return []

    if date_cols is None:
        date_cols = [c for c in df.columns if "date" in c.lower()]

    records: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        record: dict[str, Any] = {}
        for col in df.columns:
            val = row[col]
            if col in date_cols:
                record[col] = friendly_date(val)
            elif isinstance(val, (np.floating,)):
                record[col] = serialize_float(float(val))
            elif isinstance(val, float):
                record[col] = serialize_float(val)
            elif isinstance(val, (np.integer,)):
                record[col] = int(val)  # type: ignore[arg-type]
            elif isinstance(val, (datetime, pd.Timestamp)):
                record[col] = friendly_date(val)
            elif pd.isna(val):
                record[col] = None
            else:
                record[col] = val
        records.append(record)

    return records


# ---------------------------------------------------------------------------
# PnL result serializers
# ---------------------------------------------------------------------------

def pnl_result_to_summary(result: PnlResult, initial_capital: float | None = None) -> dict[str, Any]:
    """Extract top-level summary from a PnlResult."""
    daily = result.daily
    enriched = result.enriched_rows

    if daily.empty:
        return {
            "total_rows": 0,
            "total_realized_pnl": 0.0,
            "total_commission": 0.0,
            "total_trades": 0,
            "total_option_contracts": 0,
            "expire_inferred_count": 0,
            "expire_inferred_pnl": 0.0,
            "cumulative_pnl_today": 0.0,
            "date_range": None,
        }

    pnl_total = serialize_float(daily["realized_pnl"].sum()) or 0.0
    commission_total = serialize_float(daily["commission_spent"].sum()) or 0.0
    trades_total = int(daily["trade_count"].sum())
    contracts_total = int(daily["option_contracts_traded"].sum())
    expire_count = int(daily["expire_inferred_count"].sum())
    expire_pnl = serialize_float(daily["expire_inferred_pnl"].sum()) or 0.0
    cumulative = serialize_float(daily["cumulative_pnl"].iloc[-1]) or 0.0

    summary: dict[str, Any] = {
        "total_rows": int(len(enriched)),
        "total_realized_pnl": pnl_total,
        "total_commission": commission_total,
        "total_trades": trades_total,
        "total_option_contracts": contracts_total,
        "expire_inferred_count": expire_count,
        "expire_inferred_pnl": expire_pnl,
        "cumulative_pnl_today": cumulative,
        "date_range": {
            "start": friendly_date(daily["activity_date"].min()),
            "end": friendly_date(daily["activity_date"].max()),
        },
    }

    if initial_capital is not None:
        total_return = pnl_total / initial_capital if initial_capital > 0 else 0.0
        summary["total_return_pct"] = total_return

    return summary


def pnl_result_to_daily_series(result: PnlResult) -> list[dict[str, Any]]:
    """Extract daily time series from a PnlResult."""
    return df_to_records(result.daily, date_cols=["activity_date"])


def pnl_result_to_top_contracts(result: PnlResult, top_n: int = 10) -> list[dict[str, Any]]:
    """Return the top-N option contracts by total PnL."""
    enriched = result.enriched_rows
    if enriched.empty or "contract_key" not in enriched.columns:
        return []

    option_rows = enriched[enriched["is_option"] & enriched["in_pnl"]]
    if option_rows.empty:
        return []

    contract_pnl = (
        option_rows.groupby("contract_key")
        .agg(
            pnl=("net_amount", "sum"),
            trade_count=("source_row", "count"),
            underlying=("underlying", "first"),
            expiry_date=("expiry_date", "first"),
            right=("right", "first"),
            strike=("strike", "first"),
        )
        .sort_values("pnl", key=abs, ascending=False)
        .head(top_n)
        .reset_index()
    )

    return df_to_records(contract_pnl, date_cols=["expiry_date"])


# ---------------------------------------------------------------------------
# Calendar serializers
# ---------------------------------------------------------------------------

def calendar_to_dict(
    pnl_matrix: pd.DataFrame,
    commission_matrix: pd.DataFrame,
    date_matrix: pd.DataFrame,
    week_labels: list[str],
    weekly_text: pd.DataFrame,
) -> dict[str, Any]:
    """Serialize calendar matrix data for JSON output.

    Parameters match the return values of
    ``spx_trade_desk.tradelog.domain.calendar._build_calendar_matrix``.
    """
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def _matrix_to_rows(mat: pd.DataFrame) -> list[list[Any]]:
        """Convert a DataFrame matrix to a list of rows, each row a list."""
        result: list[list[Any]] = []
        for _week_idx, row in mat.iterrows():
            row_vals: list[Any] = []
            for col_idx in range(7):
                if col_idx in mat.columns:
                    val = row[col_idx]
                    row_vals.append(_safe_value(val))
                else:
                    row_vals.append(None)
            result.append(row_vals)
        return result

    weekly_summary: list[dict[str, Any]] = []
    for _, row in weekly_text.iterrows():
        weekly_summary.append(_safe_value({
            "week_pnl": row.get("weekly_pnl"),
            "week_commission": row.get("weekly_commission"),
            "week_options": row.get("weekly_options"),
            "week_start": row.get("week_start_date"),
            "week_end": row.get("week_end_date"),
        }))

    return {
        "week_labels": week_labels,
        "weekday_labels": weekday_labels,
        "pnl_matrix": _matrix_to_rows(pnl_matrix),
        "commission_matrix": _matrix_to_rows(commission_matrix),
        "date_matrix": _matrix_to_rows(date_matrix),
        "weekly_summary": weekly_summary,
    }


# ---------------------------------------------------------------------------
# Domain-object serializers
# ---------------------------------------------------------------------------

def parsed_option_to_dict(po: ParsedOption) -> dict[str, Any]:
    """Serialize a ParsedOption to a plain dict."""
    return {
        "underlying": po.underlying,
        "expiry_date": friendly_date(po.expiry_date),
        "right": po.right,
        "strike": po.strike,
        "contract_key": po.contract_key,
    }


def balance_to_dict(bal: InvBalance | EtradeBalance) -> dict[str, Any]:
    """Serialize a balance object (InvBalance or EtradeBalance) to a dict."""
    if isinstance(bal, InvBalance):
        return {
            "type": "inv_balance",
            "account_id": bal.account_id,
            "as_of": friendly_date(bal.as_of),
            "dt_start": friendly_date(bal.dt_start),
            "dt_end": friendly_date(bal.dt_end),
            "cash": serialize_float(bal.cash),
            "stock_value": serialize_float(bal.stock_value),
            "total": serialize_float(bal.total),
        }
    else:  # EtradeBalance
        return {
            "type": "etrade_balance",
            "account_id": bal.account_id,
            "period_start": friendly_date(bal.period_start),
            "period_end": friendly_date(bal.period_end),
            "beginning_value": serialize_float(bal.beginning_value),
            "ending_value": serialize_float(bal.ending_value),
            "cash": serialize_float(bal.cash),
            "stock_value": serialize_float(bal.stock_value),
        }


# ---------------------------------------------------------------------------
# Metrics serialization
# ---------------------------------------------------------------------------

def metrics_to_dict(metrics: dict[str, Any]) -> dict[str, Any]:
    """Clean up a metrics dict for JSON: replace NaN with None, flatten DataFrames.

    Expects the dict returned by
    ``src.domain.risk_metrics.calculate_risk_metrics``.
    """
    if not metrics:
        return {}

    result: dict[str, Any] = {}

    # --- period_overview ---
    result["period_overview"] = _safe_value({
        "period_return_pct": metrics.get("period_return"),
        "positive_cycles": metrics.get("positive_cycles"),
        "negative_cycles": metrics.get("negative_cycles"),
        "max_recovery_days": metrics.get("max_recovery_days"),
        "ongoing_recovery_days": metrics.get("ongoing_recovery_days"),
    })

    # --- daily_extremes ---
    result["daily_extremes"] = _safe_value({
        "max_gain": metrics.get("max_gain"),
        "max_loss": metrics.get("max_loss"),
        "commission_drag_pct": metrics.get("commission_drag"),
    })

    # --- risk_adjusted ---
    result["risk_adjusted"] = _safe_value({
        "sharpe_ratio": metrics.get("sharpe"),
        "sortino_ratio": metrics.get("sortino"),
        "daily_std": metrics.get("std_daily"),
        "net_ev": metrics.get("net_ev"),
    })

    # --- spx_benchmark ---
    result["spx_benchmark"] = _safe_value({
        "spx_correlation": metrics.get("spx_corr"),
        "spx_beta": metrics.get("spx_beta"),
        "spx_alpha_annualized_pct": metrics.get("spx_alpha"),
        "spx_period_return_pct": metrics.get("spx_period_return"),
        "return_delta_vs_spx_pct": metrics.get("return_delta_vs_spx"),
        "overlap_days": metrics.get("spx_overlap_days"),
    })

    # --- vix_analysis ---
    regime_table = metrics.get("vix_regime_table")
    regime_list: list[dict[str, Any]] = []
    if regime_table is not None and not regime_table.empty:
        regime_list = df_to_records(regime_table, date_cols=[])

    result["vix_analysis"] = _safe_value({
        "vix_correlation": metrics.get("vix_corr"),
        "vix_beta": metrics.get("vix_beta"),
        "overlap_days": metrics.get("vix_overlap_days"),
        "regime_table": regime_list,
    })

    return result
