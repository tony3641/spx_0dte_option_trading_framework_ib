"""MCP server for Trade PnL Dashboard analysis tools.

Run with::

    python -m spx_trade_desk.mcp.server

Or via FastMCP CLI::

    fastmcp run spx_trade_desk.mcp.server
"""

from __future__ import annotations

import base64
import io
import re
import shutil
import tempfile
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
from mcp.server.fastmcp import FastMCP

from spx_trade_desk.tradelog.report.generate_report import build_report
from spx_trade_desk.tradelog.report.report_inputs import report_source_kind

from spx_trade_desk.resources import REPORT_OUTPUT_DIR
from spx_trade_desk.tradelog.domain.merge import merge_transaction_frames
from spx_trade_desk.tradelog.domain.parse_option_symbol import (
    ParsedOption,
    build_occ_symbol as _domain_build_occ_symbol,
    parse_occ_option_symbol,
)
from spx_trade_desk.tradelog.domain.pnl_engine import PnlResult, build_realized_pnl
from spx_trade_desk.tradelog.domain.return_metrics import (
    compute_mwr,
    compute_portfolio_mwr,
    compute_portfolio_twr,
    compute_strategy_twr,
    statement_external_flows,
)
from spx_trade_desk.tradelog.domain.risk_metrics import calculate_risk_metrics
from spx_trade_desk.tradelog.domain.strategy_filter import (
    filter_strategy_rows,
    is_etrade_account,
    is_strategy_symbol,
)
from spx_trade_desk.tradelog.io.format_detect import load_csv_by_format
from spx_trade_desk.tradelog.io.load_etrade_pdf import EtradeBalance, load_transactions_etrade_pdf
from spx_trade_desk.tradelog.io.load_qfx import InvBalance, load_transactions_qfx, resolve_qfx_account_id
from spx_trade_desk.tradelog.io.load_spx import load_spx_daily
from spx_trade_desk.tradelog.io.load_vix import load_vix_daily
from spx_trade_desk.tradelog.io.load_xlsx import load_transactions_xlsx
from spx_trade_desk.tradelog.domain.calendar import _build_calendar_matrix

from spx_trade_desk.mcp.adapter import (
    _safe_value,
    balance_to_dict,
    calendar_to_dict,
    df_to_records,
    friendly_date,
    metrics_to_dict,
    parsed_option_to_dict,
    pnl_result_to_daily_series,
    pnl_result_to_summary,
    pnl_result_to_top_contracts,
    serialize_float,
)

# ---------------------------------------------------------------------------
# FastMCP application
# ---------------------------------------------------------------------------

mcp = FastMCP("trade-pnl-dashboard")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BalanceInfo = InvBalance | EtradeBalance

# Consolidated trade-log workbooks; routed to the xlsx loader, not the CSV one.
_XLSX_EXTS = (".xlsx", ".xlsm")


def _load_file_from_path(
    path_str: str,
    csv_account_id: str = "E*Trade",
) -> tuple[pd.DataFrame, _BalanceInfo | None, str | None]:
    """Load a single file from a local path. Returns (df, balance, warning)."""
    p = Path(path_str)
    ext = p.suffix.lower()
    try:
        if ext == ".qfx":
            df, bal = load_transactions_qfx(p)
            return df, bal, None
        elif ext == ".pdf":
            df, bal = load_transactions_etrade_pdf(p)
            return df, bal, None
        elif ext in _XLSX_EXTS:
            df = load_transactions_xlsx(p, account_id=csv_account_id)
            return df, None, df.attrs.get("warning")
        else:
            df = load_csv_by_format(p, account_id=csv_account_id)
            return df, None, None
    except (ValueError, ImportError, FileNotFoundError, OSError) as exc:
        return pd.DataFrame(), None, f"{p.name}: {exc}"


def _load_file_from_bytes(
    name: str,
    data: bytes,
    csv_account_id: str = "E*Trade",
) -> tuple[pd.DataFrame, _BalanceInfo | None, str | None]:
    """Load a single file from in-memory bytes. Returns (df, balance, warning)."""
    ext = Path(name).suffix.lower()
    buf = io.BytesIO(data)
    try:
        if ext == ".qfx":
            df, bal = load_transactions_qfx(buf)
            return df, bal, None
        elif ext == ".pdf":
            df, bal = load_transactions_etrade_pdf(buf)
            return df, bal, None
        elif ext in _XLSX_EXTS:
            df = load_transactions_xlsx(buf, account_id=csv_account_id)
            return df, None, df.attrs.get("warning")
        else:
            df = load_csv_by_format(buf, account_id=csv_account_id)
            return df, None, None
    except (ValueError, ImportError) as exc:
        return pd.DataFrame(), None, f"{name}: {exc}"


def _materialize_content(fc: dict[str, str]) -> tuple[str, bytes | None, str | None]:
    """Return (name, raw_bytes, warning_or_None) for an in-memory file content."""
    name = fc.get("name", "unknown.csv")
    data_text = fc.get("data_text")
    data_b64 = fc.get("data_base64")
    if data_b64 is not None:
        try:
            return name, base64.b64decode(data_b64), None
        except Exception as exc:
            return name, None, f"{name}: base64 decode failed ({exc})"
    if data_text is not None:
        return name, data_text.encode("utf-8"), None
    return name, None, f"{name}: neither data_text nor data_base64 provided — skipped"


def _load_and_merge(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
) -> tuple[pd.DataFrame, list[_BalanceInfo], list[str]]:
    """Load, merge, and deduplicate transaction files.

    Two-pass loading so an E*Trade trades CSV aligns its account id to a
    loaded E*Trade PDF statement (their option trades overlap and must dedup):
    pass 1 loads QFX/PDF (balances + E*Trade PDF account ids); pass 2 loads
    CSVs and ``.xlsx`` trade-log workbooks using the resolved account id.

    Returns ``(merged_df, balances, warnings)``.
    """
    sources: list[tuple[str, object]] = []
    for path_str in (paths or []):
        sources.append(("path", path_str))
    for fc in (file_contents or []):
        sources.append(("content", fc))

    def _ext(kind: str, payload) -> str:
        if kind == "path":
            return Path(str(payload)).suffix.lower()
        return Path(str(payload.get("name", "unknown.csv"))).suffix.lower()

    frames: list[pd.DataFrame] = []
    balances: list[_BalanceInfo] = []
    warnings: list[str] = []
    etrade_pdf_accounts: list[str] = []
    qfx_loaded = False
    xlsx_loaded = False

    # ---- Pass 1: QFX + PDF (balance-bearing) --------------------------------
    for kind, payload in sources:
        ext = _ext(kind, payload)
        if ext not in (".qfx", ".pdf"):
            continue
        df, bal, warn = _load_single_source(kind, payload)
        if not df.empty:
            frames.append(df)
            qfx_loaded = qfx_loaded or ext == ".qfx"
        if bal is not None:
            balances.append(bal)
            if isinstance(bal, EtradeBalance):
                etrade_pdf_accounts.append(bal.account_id)
        if warn:
            warnings.append(warn)

    csv_account_id = etrade_pdf_accounts[0] if etrade_pdf_accounts else "E*Trade"

    # ---- Pass 2: CSVs + xlsx workbooks (aligned to E*Trade PDF account) ----
    for kind, payload in sources:
        ext = _ext(kind, payload)
        if ext != ".csv" and ext not in _XLSX_EXTS:
            continue
        df, bal, warn = _load_single_source(kind, payload, csv_account_id=csv_account_id)
        if not df.empty:
            frames.append(df)
            xlsx_loaded = xlsx_loaded or ext in _XLSX_EXTS
        if bal is not None:
            balances.append(bal)
        if warn:
            warnings.append(warn)

    if qfx_loaded and xlsx_loaded:
        warnings.append(
            "An .xlsx trade-log workbook was loaded together with a QFX statement: "
            "the workbook's IBKR account id is masked and cannot be matched to the "
            "QFX account, so trades present in both files are not deduplicated and "
            "would be double counted. Load the workbook on its own."
        )

    if not frames:
        return pd.DataFrame(), balances, warnings

    try:
        merged = merge_transaction_frames(frames)
    except Exception as exc:
        warnings.append(f"Merge failed: {exc}")
        # Fall back to simple concatenation
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.sort_values("activity_date").reset_index(drop=True)

    return merged, balances, warnings


def _load_single_source(
    kind: str,
    payload,
    csv_account_id: str = "E*Trade",
) -> tuple[pd.DataFrame, _BalanceInfo | None, str | None]:
    """Load one source (a local path or an in-memory file content)."""
    if kind == "path":
        return _load_file_from_path(str(payload), csv_account_id=csv_account_id)
    name, raw, warn = _materialize_content(payload)
    if warn:
        return pd.DataFrame(), None, warn
    return _load_file_from_bytes(name, raw, csv_account_id=csv_account_id)


def _filter_window(
    df: pd.DataFrame,
    window: str = "All",
    custom_start: str | None = None,
    custom_end: str | None = None,
) -> pd.DataFrame:
    """Apply time-window filter to a daily-PnL DataFrame."""
    if df.empty or window == "All":
        return df.sort_values("activity_date").reset_index(drop=True)

    last_date = df["activity_date"].max()

    if window == "Custom":
        c_start = pd.to_datetime(custom_start).date() if custom_start else df["activity_date"].min()
        c_end = pd.to_datetime(custom_end).date() if custom_end else last_date
        return (
            df[(df["activity_date"] >= c_start) & (df["activity_date"] <= c_end)]
            .copy()
            .sort_values("activity_date")
            .reset_index(drop=True)
        )

    if window == "1M":
        start = last_date - timedelta(days=29)
    elif window == "3M":
        start = last_date - timedelta(days=89)
    elif window == "1Y":
        start = last_date - timedelta(days=364)
    else:  # YTD
        start = pd.Timestamp(last_date.year, 1, 1).date()

    return (
        df[df["activity_date"] >= start]
        .copy()
        .sort_values("activity_date")
        .reset_index(drop=True)
    )


def _resolve_initial_capital(
    balances: list[_BalanceInfo],
    merged_df: pd.DataFrame,
    provided: float | None,
) -> float:
    """Resolve initial capital: use provided value, or auto-infer from balances.

    For QFX balances the latest snapshot per account (by ``<DTASOF>``) anchors
    ``total − Σ that account's net amounts``; E*Trade PDFs use the earliest
    statement's beginning value.
    """
    if provided is not None:
        return float(provided)

    capital_parts: dict[str, float] = {}
    qfx_latest: dict[str, InvBalance] = {}

    for bal in balances:
        if isinstance(bal, InvBalance):
            acct = resolve_qfx_account_id(bal, merged_df)
            if acct not in qfx_latest or (
                bal.as_of and (qfx_latest[acct].as_of is None or bal.as_of > qfx_latest[acct].as_of)
            ):
                qfx_latest[acct] = bal
        elif isinstance(bal, EtradeBalance):
            capital_parts.setdefault(bal.account_id, bal.beginning_value)

    for acct, bal in qfx_latest.items():
        acct_net = float(
            merged_df[merged_df["account_id"] == acct]["net_amount"].fillna(0.0).sum()
        )
        capital_parts[acct] = float(bal.total - acct_net)

    if capital_parts:
        return sum(capital_parts.values())

    return 100_000.0  # sensible default


# ---------------------------------------------------------------------------
# Tool 6: parse_occ_symbol
# ---------------------------------------------------------------------------

@mcp.tool()
def parse_occ_symbol(symbol: str) -> dict[str, Any]:
    """Parse an OCC option symbol into its components.

    Args:
        symbol: OCC option symbol, e.g. "SPXW  260202P06940000"

    Returns:
        Dict with underlying, expiry_date, right, strike, and contract_key,
        or an error key if parsing fails.
    """
    result = parse_occ_option_symbol(symbol)
    if result is None:
        return {"error": f"Could not parse symbol: {symbol!r}"}
    return parsed_option_to_dict(result)


# ---------------------------------------------------------------------------
# Tool 7: build_occ_symbol
# ---------------------------------------------------------------------------

@mcp.tool()
def build_occ_symbol(
    underlying: str,
    expiry: str,
    right: str,
    strike: float,
) -> dict[str, Any]:
    """Build a padded OCC option symbol from components.

    Args:
        underlying: Underlying ticker, e.g. "SPXW"
        expiry: Expiration date as "YYYY-MM-DD"
        right: Option right, "P" for put or "C" for call
        strike: Strike price, e.g. 6940.0

    Returns:
        Dict with occ_symbol and contract_key.
    """
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        return {"error": f"Invalid expiry date format: {expiry!r}. Use YYYY-MM-DD."}

    symbol = _domain_build_occ_symbol(underlying, expiry_date, right, strike)
    # Build the contract key in the same format as parse_occ_option_symbol
    contract_key = f"{underlying.upper()}|{expiry_date.isoformat()}|{right.upper()}|{strike:.3f}"

    return {
        "occ_symbol": symbol,
        "contract_key": contract_key,
    }


# ---------------------------------------------------------------------------
# Tool 1: get_transaction_summary
# ---------------------------------------------------------------------------

@mcp.tool()
def get_transaction_summary(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Load transaction files and return a summary of their contents.

    Use this first to understand what data is available before running
    deeper analysis.

    Args:
        paths: Optional list of local file paths (CSV, QFX, PDF, or XLSX).
        file_contents: Optional list of dicts, each with:
            - name (required): filename with extension for format detection
            - data_text (optional): raw text content for CSV/QFX files
            - data_base64 (optional): base64-encoded bytes for PDFs, XLSX
              trade-log workbooks, or other binary files

    Returns:
        Dict with total_rows, date_range, accounts, transaction_types,
        balances, and any warnings.
    """
    try:
        merged, balances, warnings = _load_and_merge(paths, file_contents)
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}

    if merged.empty:
        return {
            "total_rows": 0,
            "date_range": None,
            "accounts": [],
            "transaction_types": {},
            "balances": [],
            "warnings": warnings or ["No transaction data found in provided files."],
        }

    type_counts = (
        merged["transaction_type"]
        .fillna("Unknown")
        .value_counts()
        .to_dict()
    )

    return {
        "total_rows": int(len(merged)),
        "date_range": {
            "start": friendly_date(merged["activity_date"].min()),
            "end": friendly_date(merged["activity_date"].max()),
        },
        "accounts": sorted(merged["account_id"].dropna().unique().tolist()),
        "transaction_types": {str(k): int(v) for k, v in type_counts.items()},
        "balances": [balance_to_dict(b) for b in balances],
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Tool 2: compute_daily_pnl
# ---------------------------------------------------------------------------

@mcp.tool()
def compute_daily_pnl(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
    account_filter: str = "All",
    initial_capital: float | None = None,
) -> dict[str, Any]:
    """Run the realized PnL pipeline and return daily performance series.

    Args:
        paths: Optional list of local file paths (CSV, QFX, PDF, or XLSX).
        file_contents: Optional list of dicts with name and data_text/data_base64.
        account_filter: Account ID to filter by, or "All" for all accounts.
        initial_capital: Starting capital in dollars. Auto-inferred from
            balance data if omitted (falls back to $100,000).

    Returns:
        Dict with initial_capital, summary totals, daily_series array,
        and top_contracts list.
    """
    try:
        merged, balances, warnings = _load_and_merge(paths, file_contents)
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}

    if merged.empty:
        return {"error": "No transaction data found in provided files.", "warnings": warnings}

    # Strategy views are SPX/SPXW-focused for E*Trade accounts.
    merged = filter_strategy_rows(merged)

    capital = _resolve_initial_capital(balances, merged, initial_capital)

    # Account filtering
    if account_filter != "All" and account_filter in merged["account_id"].values:
        merged = merged[merged["account_id"] == account_filter]

    result = build_realized_pnl(merged)

    return {
        "initial_capital": capital,
        **pnl_result_to_summary(result, initial_capital=capital),
        "daily_series": pnl_result_to_daily_series(result),
        "top_contracts": pnl_result_to_top_contracts(result, top_n=10),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Tool 8: get_contract_details
# ---------------------------------------------------------------------------

@mcp.tool()
def get_contract_details(
    contract_key: str = "",
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
    account_filter: str = "All",
) -> dict[str, Any]:
    """Get all trades and aggregate PnL for a specific option contract.

    Args:
        contract_key: Contract identifier, e.g. "SPXW|2026-02-02|P|6940.000".
            Use parse_occ_symbol or build_occ_symbol to obtain this key.
        paths: Optional list of local file paths.
        file_contents: Optional list of dicts with name and data_text/data_base64.
        account_filter: Account ID to filter by, or "All".

    Returns:
        Dict with contract metadata, total_pnl, net_quantity, trade list,
        and a human-readable summary.
    """
    if not contract_key:
        return {"error": "contract_key is required. Use parse_occ_symbol or build_occ_symbol to obtain one."}

    try:
        merged, _balances, warnings = _load_and_merge(paths, file_contents)
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}

    if merged.empty:
        return {"error": "No transaction data found.", "warnings": warnings}

    # Strategy views are SPX/SPXW-focused for E*Trade accounts.
    merged = filter_strategy_rows(merged)

    if account_filter != "All" and account_filter in merged["account_id"].values:
        merged = merged[merged["account_id"] == account_filter]

    result = build_realized_pnl(merged)
    enriched = result.enriched_rows

    # Filter by contract key
    if "contract_key" not in enriched.columns:
        return {"error": "No option contract data available.", "warnings": warnings}

    contract_rows = enriched[enriched["contract_key"] == contract_key]
    if contract_rows.empty:
        return {
            "error": f"No trades found for contract key: {contract_key!r}",
            "available_contracts_hint": (
                enriched.loc[enriched["is_option"], "contract_key"]
                .dropna().unique()[:20].tolist()
            ),
        }

    # Metadata from first row
    first = contract_rows.iloc[0]
    total_pnl = serialize_float(contract_rows["net_amount"].sum()) or 0.0
    total_commission = serialize_float(contract_rows["commission"].abs().sum()) or 0.0
    net_qty = serialize_float(contract_rows["quantity"].sum()) or 0.0

    trades = df_to_records(
        contract_rows[[
            "activity_date", "account_id", "transaction_type", "description",
            "quantity", "price", "net_amount", "commission",
            "is_expire_inferred", "is_option",
        ]].sort_values("activity_date"),
        date_cols=["activity_date"],
    )

    # Summary string
    if abs(net_qty) < 0.001:
        summary = f"Fully closed with ${total_pnl:,.2f} realized PnL"
    else:
        direction = "long" if net_qty > 0 else "short"
        summary = f"Still open with {abs(net_qty)} net {direction} contracts"

    return {
        "contract_key": contract_key,
        "underlying": first.get("underlying"),
        "expiry_date": friendly_date(first.get("expiry_date")),
        "right": first.get("right"),
        "strike": serialize_float(first.get("strike")),
        "total_pnl": total_pnl,
        "total_commission": total_commission,
        "net_quantity": net_qty,
        "trade_count": int(len(contract_rows)),
        "first_trade_date": friendly_date(contract_rows["activity_date"].min()),
        "last_trade_date": friendly_date(contract_rows["activity_date"].max()),
        "trades": trades,
        "summary": summary,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Tool 5: get_market_data
# ---------------------------------------------------------------------------

@mcp.tool()
def get_market_data(
    ticker: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Fetch daily SPX or VIX data from Yahoo Finance.

    Args:
        ticker: "SPX" for S&P 500 or "VIX" for CBOE Volatility Index.
        start_date: Start date as "YYYY-MM-DD".
        end_date: End date as "YYYY-MM-DD".

    Returns:
        Dict with ticker and series array of daily OHLC/returns.
    """
    ticker_upper = ticker.upper().strip()
    if ticker_upper not in ("SPX", "VIX"):
        return {"error": "ticker must be 'SPX' or 'VIX'"}

    try:
        s = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return {"error": "Dates must be in YYYY-MM-DD format."}

    try:
        if ticker_upper == "SPX":
            df = load_spx_daily(s, e)
            return {
                "ticker": "SPX",
                "series": df_to_records(df, date_cols=["activity_date"]),
            }
        else:
            df = load_vix_daily(s, e)
            return {
                "ticker": "VIX",
                "series": df_to_records(df, date_cols=["activity_date"]),
            }
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}


# ---------------------------------------------------------------------------
# Tool 4: get_calendar_data
# ---------------------------------------------------------------------------

@mcp.tool()
def get_calendar_data(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build weekly calendar matrix of daily PnL for heatmap visualization.

    Args:
        paths: Optional list of local file paths (CSV, QFX, PDF, or XLSX).
        file_contents: Optional list of dicts with name and data_text/data_base64.

    Returns:
        Dict with week_labels, weekday_labels, pnl_matrix, commission_matrix,
        date_matrix, and weekly_summary. Matrices are lists of rows (week_seq x 7).
        Weekend values (Sat/Sun) are null.
    """
    try:
        merged, _balances, warnings = _load_and_merge(paths, file_contents)
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}

    if merged.empty:
        return {"error": "No transaction data found.", "warnings": warnings}

    # Strategy views are SPX/SPXW-focused for E*Trade accounts.
    merged = filter_strategy_rows(merged)

    result = build_realized_pnl(merged)
    daily = result.daily

    if daily.empty:
        return {"error": "No daily PnL data.", "warnings": warnings}

    (
        pnl_matrix, commission_matrix, date_matrix,
        text_cells, week_labels, weekly_text,
    ) = _build_calendar_matrix(daily)

    return {
        **calendar_to_dict(
            pnl_matrix, commission_matrix, date_matrix,
            week_labels, weekly_text,
        ),
        "total_rows": int(len(daily)),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Tool 3: compute_risk_metrics
# ---------------------------------------------------------------------------

_WINDOW_OPTIONS = ["1M", "3M", "YTD", "1Y", "All", "Custom"]


@mcp.tool()
def compute_risk_metrics(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
    account_filter: str = "All",
    initial_capital: float | None = None,
    annual_rf_pct: float = 0.0,
    window: str = "All",
    custom_start: str | None = None,
    custom_end: str | None = None,
    with_spx: bool = True,
    with_vix: bool = True,
) -> dict[str, Any]:
    """Compute risk-adjusted performance metrics with optional SPX/VIX benchmarking.

    Args:
        paths: Optional list of local file paths (CSV, QFX, PDF, or XLSX).
        file_contents: Optional list of dicts with name and data_text/data_base64.
        account_filter: Account ID to filter by, or "All".
        initial_capital: Starting capital in dollars. Auto-inferred if omitted.
        annual_rf_pct: Annual risk-free rate as a percentage (e.g. 5.0 means 5%).
            Default 0.0.
        window: Time window — "1M", "3M", "YTD", "1Y", "All", or "Custom".
        custom_start: Start date for Custom window, as "YYYY-MM-DD".
        custom_end: End date for Custom window, as "YYYY-MM-DD".
        with_spx: Whether to fetch SPX benchmark data (default True).
        with_vix: Whether to fetch VIX data for regime analysis (default True).

    Returns:
        Dict with window, period_overview, daily_extremes, risk_adjusted,
        spx_benchmark, vix_analysis, and warnings.
    """
    try:
        merged, balances, warnings = _load_and_merge(paths, file_contents)
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}

    if merged.empty:
        return {"error": "No transaction data found.", "warnings": warnings}

    # Strategy views are SPX/SPXW-focused for E*Trade accounts.
    merged = filter_strategy_rows(merged)

    capital = _resolve_initial_capital(balances, merged, initial_capital)

    if account_filter != "All" and account_filter in merged["account_id"].values:
        merged = merged[merged["account_id"] == account_filter]

    pnl_result = build_realized_pnl(merged)
    daily = pnl_result.daily

    if window not in _WINDOW_OPTIONS:
        return {"error": f"Invalid window: {window!r}. Must be one of {_WINDOW_OPTIONS}."}

    view = _filter_window(daily, window, custom_start, custom_end)
    if view.empty:
        return {"error": "No data in selected window.", "warnings": warnings}

    # Fetch benchmark data if requested
    spx_df: pd.DataFrame | None = None
    vix_df: pd.DataFrame | None = None

    if with_spx:
        try:
            spx_df = load_spx_daily(
                view["activity_date"].min(),
                view["activity_date"].max(),
            )
        except Exception as exc:
            warnings.append(f"SPX data fetch failed: {exc}")

    if with_vix:
        try:
            vix_df = load_vix_daily(
                view["activity_date"].min(),
                view["activity_date"].max(),
            )
        except Exception as exc:
            warnings.append(f"VIX data fetch failed: {exc}")

    # Compute metrics
    metrics = calculate_risk_metrics(
        view=view,
        initial_capital=float(capital),
        annual_rf=annual_rf_pct / 100.0,
        spx_df=spx_df,
        vix_df=vix_df,
    )

    window_start = friendly_date(view["activity_date"].min())
    window_end = friendly_date(view["activity_date"].max())

    return {
        "window": {
            "label": window,
            "start": window_start,
            "end": window_end,
            "trading_days": int(len(view)),
        },
        **metrics_to_dict(metrics),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Tool: compute_account_return (SPX/SPXW-only TWR / MWR)
# ---------------------------------------------------------------------------

def _build_account_capital(
    merged: pd.DataFrame,
    balances: list[_BalanceInfo],
) -> dict[str, dict]:
    """Per-account capital map seeded from balance data (mirrors app.py).

    QFX balances accumulate statement periods per IBKR account — each file is
    one balance snapshot (period_end = ``<DTASOF>``); the latest snapshot
    anchors ``ending = total`` and ``initial = total − Σ that account's net``.
    E*Trade PDF periods keep their statement beginning value as the initial.
    """
    cap: dict[str, dict] = {}
    qfx_latest: dict[str, InvBalance] = {}

    for bal in balances:
        if isinstance(bal, InvBalance):
            acct = resolve_qfx_account_id(bal, merged)
            entry = cap.setdefault(acct, {"initial": None, "ending": None, "periods": []})
            entry["periods"].append(bal.to_statement_period())
            if acct not in qfx_latest or (
                bal.as_of and (qfx_latest[acct].as_of is None or bal.as_of > qfx_latest[acct].as_of)
            ):
                qfx_latest[acct] = bal
        elif isinstance(bal, EtradeBalance):
            entry = cap.setdefault(
                bal.account_id, {"initial": None, "ending": None, "periods": []}
            )
            entry["periods"].append(bal)

    for acct, bal in qfx_latest.items():
        acct_net = float(
            merged[merged["account_id"] == acct]["net_amount"].fillna(0.0).sum()
        )
        cap[acct]["initial"] = float(bal.total - acct_net)
        cap[acct]["ending"] = float(bal.total)

    for acct in merged["account_id"].dropna().unique().tolist():
        if is_etrade_account(acct) and acct not in cap:
            cap[acct] = {"initial": 100000.0, "ending": None, "periods": []}

    for entry in cap.values():
        # Deduplicate by period (duplicate files for the same month may be passed).
        periods = sorted(
            {b.period_start: b for b in entry["periods"]}.values(),
            key=lambda b: b.period_start,
        )
        if periods:
            # E*Trade periods carry a real beginning value → their initial.
            # QFX periods have a 0.0 placeholder and their initial/ending were
            # set above from the latest snapshot.
            first_begin = getattr(periods[0], "beginning_value", None)
            if first_begin and first_begin > 0:
                entry["initial"] = first_begin
            if entry.get("ending") is None:
                entry["ending"] = periods[-1].ending_value
        else:
            entry.setdefault("initial", 100000.0)
            entry.setdefault("ending", None)
        entry["periods"] = periods
    return cap


def _period_range(acct_df: pd.DataFrame, periods: list) -> tuple[date, date]:
    """Analysis window: merge statement-period bounds with the ledger activity range."""
    lo = acct_df["activity_date"].min() if not acct_df.empty else None
    hi = acct_df["activity_date"].max() if not acct_df.empty else None
    for p in periods:
        ps = getattr(p, "period_start", None)
        pe = getattr(p, "period_end", None)
        if ps is not None:
            lo = ps if lo is None else min(lo, ps)
        end = pe or (ps + timedelta(days=30) if ps is not None else None)
        if end is not None:
            hi = end if hi is None else max(hi, end)
    if lo is not None and hi is not None:
        return lo, hi
    return date.today() - timedelta(days=30), date.today()


@mcp.tool()
def compute_account_return(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
    account_filter: str = "All",
    method: str = "Both",
    initial_capital: float | None = None,
    ending_capital: float | None = None,
    cash_flows: list[dict] | None = None,
) -> dict[str, Any]:
    """Compute SPX/SPXW-only time-weighted (TWR) and money-weighted (MWR) returns.

    The SPX/SPXW strategy is the only return driver; every non-SPX/SPXW
    transaction (stock trades, dividends, deposits/withdrawals, non-SPX
    options) is treated as an external cash flow to/from the strategy.

    Args:
        paths / file_contents: Transaction sources (CSV, QFX, PDF, or XLSX).
        account_filter: "All" or a specific account id.
        method: "TWR", "MWR", or "Both".
        initial_capital / ending_capital: Optional overrides.  Defaults are
            inferred from balance data (E*Trade PDF beginning/ending values,
            QFX INVBAL).
        cash_flows: Optional extra external flows as ``[{"date": "YYYY-MM-DD",
            "amount": float}]`` where positive = deposit into the account,
            negative = withdrawal.

    Returns:
        Dict with per-account ``twr`` / ``mwr`` results and warnings.
    """
    try:
        merged, balances, warnings = _load_and_merge(paths, file_contents)
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}

    if merged.empty:
        return {"error": "No transaction data found.", "warnings": warnings}

    account_capital = _build_account_capital(merged, balances)

    accounts = sorted(merged["account_id"].dropna().unique().tolist())
    targets = [account_filter] if account_filter != "All" and account_filter in accounts else accounts

    parsed_flows: list[tuple[date, float]] = []
    for cf in (cash_flows or []):
        d, amt = cf.get("date"), cf.get("amount")
        if d and amt is not None:
            try:
                parsed_flows.append((datetime.strptime(d, "%Y-%m-%d").date(), float(amt)))
            except ValueError:
                warnings.append(f"Ignoring cash flow with invalid date: {d!r}")

    results: list[dict[str, Any]] = []
    portfolio_accounts: list[dict[str, Any]] = []
    for acct in targets:
        cap = account_capital.setdefault(
            acct, {"initial": initial_capital or 100000.0, "ending": ending_capital, "periods": []}
        )
        if initial_capital is not None:
            cap["initial"] = initial_capital
        if ending_capital is not None:
            cap["ending"] = ending_capital

        acct_df_all = merged[merged["account_id"] == acct]
        start, end = _period_range(acct_df_all, cap.get("periods") or [])
        if not acct_df_all.empty:
            acct_df = acct_df_all[
                (acct_df_all["activity_date"] >= start) & (acct_df_all["activity_date"] <= end)
            ]
        else:
            acct_df = acct_df_all

        strat_mask = acct_df["symbol"].map(is_strategy_symbol).fillna(False)
        strategy_df = acct_df[strat_mask]
        flow_df = acct_df[~strat_mask]
        spx_pnl = [(r["activity_date"], r["net_amount"]) for _, r in strategy_df.iterrows()]
        ext_flows = [(r["activity_date"], r["net_amount"]) for _, r in flow_df.iterrows()]

        entry: dict[str, Any] = {
            "account": acct,
            "initial_capital": cap["initial"],
            "ending_capital": cap["ending"],
            "start": friendly_date(start),
            "end": friendly_date(end),
            "strategy_trades": int(len(strategy_df)),
            "external_flow_rows": int(len(flow_df)),
        }
        # Statement-derived external flows (incl. deposits) when statements are
        # loaded — this makes the strategy value track the account.
        periods = cap.get("periods") or []
        derived_flows = (
            statement_external_flows(cap["initial"], spx_pnl, periods) if periods else []
        )
        strategy_pnl = float(strategy_df["net_amount"].sum()) if not strategy_df.empty else 0.0
        ext_flows_total = (
            sum(a for _, a in derived_flows)
            if periods
            else (float(flow_df["net_amount"].sum()) if not flow_df.empty else 0.0)
        )
        strategy_ending = cap["initial"] + strategy_pnl + ext_flows_total
        entry["strategy_ending"] = strategy_ending
        entry["statement_periods"] = len(periods)
        if method in ("Both", "TWR"):
            entry["twr"] = _safe_value(
                compute_strategy_twr(
                    cap["initial"], spx_pnl, ext_flows, statement_periods=periods or None
                )
            )
        if method in ("Both", "MWR"):
            if periods:
                mwr = compute_mwr(
                    cap["initial"], cap["ending"], start, end,
                    cash_flows=derived_flows + parsed_flows,
                )
            else:
                mwr = compute_mwr(
                    cap["initial"], strategy_ending, start, end,
                    cash_flows=ext_flows + parsed_flows,
                )
            entry["mwr"] = _safe_value(mwr)
        portfolio_accounts.append({
            "initial": cap["initial"],
            "ending": cap["ending"] if cap["ending"] is not None else strategy_ending,
            "spx_pnl_by_date": spx_pnl,
            "external_flows_by_date": ext_flows,
            "statement_periods": periods,
            "mwr_flows": (derived_flows + parsed_flows) if periods else (ext_flows + parsed_flows),
        })
        results.append(entry)

    result: dict[str, Any] = {"method": method, "accounts": results, "warnings": warnings}
    if account_filter == "All" and len(targets) > 1:
        combined: dict[str, Any] = {}
        if method in ("Both", "TWR"):
            combined["twr"] = _safe_value(compute_portfolio_twr(portfolio_accounts))
        if method in ("Both", "MWR"):
            all_periods = [p for a in portfolio_accounts for p in (a["statement_periods"] or [])]
            c_start, c_end = _period_range(merged, all_periods)
            combined["mwr"] = _safe_value(
                compute_portfolio_mwr(portfolio_accounts, c_start, c_end)
            )
            combined["start"] = friendly_date(c_start)
            combined["end"] = friendly_date(c_end)
        combined["initial_capital"] = serialize_float(
            sum(float(a["initial"] or 0.0) for a in portfolio_accounts)
        )
        combined["ending_capital"] = serialize_float(
            sum(float(a["ending"] or 0.0) for a in portfolio_accounts)
        )
        result["combined"] = combined
    return result


# ---------------------------------------------------------------------------
# Tool: generate_monthly_report
# ---------------------------------------------------------------------------

def _materialize_file_contents(fc: dict[str, str], tmpdir: str, index: int) -> str | None:
    """Write an in-memory file_content to a temp file, returning its path."""
    name = fc.get("name", "upload.qfx")
    data_text = fc.get("data_text")
    data_b64 = fc.get("data_base64")
    if data_b64 is not None:
        try:
            raw = base64.b64decode(data_b64)
        except Exception:
            return None
    elif data_text is not None:
        raw = data_text.encode("utf-8")
    else:
        return None
    ext = Path(name).suffix.lower() or ".qfx"
    p = Path(tmpdir) / f"upload_{index}{ext}"
    p.write_bytes(raw)
    return str(p)


@mcp.tool()
def generate_monthly_report(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
    month: str | None = None,
    annual_rf_pct: float = 4.0,
    label: str | None = None,
    ytd_paths: list[str] | None = None,
    ytd_file_contents: list[dict[str, str]] | None = None,
    offline: bool = False,
    include_html: bool = False,
    account_filter: str = "All",
    initial_capital: float | None = None,
) -> dict[str, Any]:
    """Generate the monthly trading report from a QFX statement or a trade-log workbook.

    Combines the monthly performance report (daily PnL, weekly breakdown,
    risk-adjusted metrics at the risk-free rate, VIX regimes, SPX benchmark)
    with the strategy-level edge & risk analysis and optional cross-month
    context. Returns the structured report data and writes a self-contained
    HTML report to ``reports/output/``.

    A QFX statement carries intraday timestamps, so the report also includes
    the bull-put-credit-spread structure, per-leg win rates, stops & re-entry,
    spread-capped tail stress and Kelly. An ``.xlsx`` trade-log workbook has
    dates only, so position direction cannot be recovered: the report covers
    SPX/SPXW option rows at the day level (summary, daily/weekly PnL, risk,
    VIX regimes, SPX benchmark, bootstrap, Monte Carlo) and shows a notice in
    place of structure, stops/re-entry and Kelly (``unavailable_sections`` in
    the result lists them).

    Args:
        paths: Optional local file path(s): a QFX statement or an
            ``.xlsx`` / ``.xlsm`` trade-log workbook. Other types are rejected.
        file_contents: Optional in-memory files as dicts with ``name`` plus
            ``data_text`` or ``data_base64`` (a workbook must use ``data_base64``).
        month: Optional calendar-month filter, e.g. ``"2026-06"``. For a
            workbook the earlier months in the same file feed the cross-month
            table and pooled significance automatically.
        annual_rf_pct: Annual risk-free rate as a percentage (e.g. 5.0 = 5%).
        label: Report title, e.g. ``"July 2026"``.
        ytd_paths / ytd_file_contents: Optional prior-month / YTD QFX for
            cross-month context and pooled significance. Not supported with a
            workbook.
        offline: When True, use the bundled SPX/VIX CSVs without a network
            fetch. Default False -> fetches fresh SPX/VIX from Yahoo Finance
            (keeping the benchmark and VIX regimes up to date) and caches the
            result back into the CSVs. The bundled cache ends 2026-07-31.
        include_html: When True, also return the full HTML report string.
        account_filter: Workbook only: ``"All"`` (the combined book) or one
            account id. Ignored, with a warning, for a QFX statement.
        initial_capital: Workbook only: capital at the start of the workbook
            (default 100,000, shown in the report as assumed). Ignored, with a
            warning, for a QFX statement, which anchors capital on its balance.

    Returns:
        Dict with the structured report data (total_pnl, return_pct, risk,
        spreads, edge, stops, tail, takeaways, cross_month, ...) plus
        ``html_path`` and ``warnings``; a workbook report adds ``source`` and
        ``unavailable_sections``. On bad input: ``{"error": ..., "warnings": ...}``.
    """
    warnings: list[str] = []
    monthly_src: str | None = None
    ytd_src: str | None = None
    display_names: dict[str, str] = {}
    tmpdir = tempfile.mkdtemp(prefix="report_")
    try:
        monthly_candidates = list(paths or [])
        for i, fc in enumerate(file_contents or []):
            p = _materialize_file_contents(fc, tmpdir, i)
            if p:
                monthly_candidates.append(p)
                display_names[p] = fc.get("name", "?")
            else:
                warnings.append(f"{fc.get('name', '?')}: could not materialize — skipped")
        if monthly_candidates:
            monthly_src = monthly_candidates[0]
            if len(monthly_candidates) > 1:
                warnings.append("Multiple monthly files provided — using the first; others ignored.")

        ytd_candidates = list(ytd_paths or [])
        for i, fc in enumerate(ytd_file_contents or []):
            p = _materialize_file_contents(fc, tmpdir, 100 + i)
            if p:
                ytd_candidates.append(p)
                display_names[p] = fc.get("name", "?")
        if ytd_candidates:
            ytd_src = ytd_candidates[0]
            if len(ytd_candidates) > 1:
                warnings.append("Multiple YTD files provided — using the first; others ignored.")

        if not monthly_src:
            return {"error": "No monthly file provided (pass paths or file_contents).",
                    "warnings": warnings}

        try:
            kind = report_source_kind(monthly_src, display_names.get(monthly_src))
            if ytd_src:
                if report_source_kind(ytd_src, display_names.get(ytd_src)) != "qfx":
                    raise ValueError("ytd_paths / ytd_file_contents must be a QFX statement")
                if kind == "ledger":
                    raise ValueError(
                        "ytd_paths / ytd_file_contents are not supported with a workbook: "
                        "earlier months are read from the workbook itself")
        except ValueError as exc:
            return {"error": str(exc), "warnings": warnings}

        if kind == "qfx":
            if initial_capital is not None:
                warnings.append(
                    "initial_capital is ignored for a QFX statement (capital is anchored on its balance).")
            if account_filter != "All":
                warnings.append(
                    "account_filter is ignored for a QFX statement (a QFX is a single account).")

        ns = SimpleNamespace(
            monthly=monthly_src, month=month, ytd=ytd_src, rf=annual_rf_pct / 100.0,
            label=label, offline=bool(offline),
            account=None if account_filter == "All" else account_filter,
            initial_capital=initial_capital if kind == "ledger" else None,
        )
        try:
            html_doc, report_data = build_report(ns)
        except ValueError as exc:
            return {"error": str(exc), "warnings": warnings}

        if report_data is None:
            return {"error": "No trades found in the provided monthly file.", "warnings": warnings}
        skipped = (report_data.get("source") or {}).get("skipped_rows")
        if skipped:
            warnings.append(f"{skipped} workbook row(s) with a missing or invalid Date were skipped.")

        out_dir = REPORT_OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w\-]+", "_", (label or report_data.get("month") or "report")).strip("_") or "report"
        out_path = out_dir / f"{slug}_report.html"
        out_path.write_text(html_doc, encoding="utf-8")

        result = dict(report_data)
        result["html_path"] = str(out_path.resolve())
        if include_html:
            result["html"] = html_doc
        result["warnings"] = warnings
        return _safe_value(result)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tool: analyze_strategy_compliance
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_strategy_compliance(
    paths: list[str] | None = None,
    file_contents: list[dict[str, str]] | None = None,
    account_filter: str = "All",
    strategies_path: str | None = None,
    annual_rf_pct: float = 4.0,
    offline: bool = False,
) -> dict[str, Any]:
    """Score realized spreads against the conditions in config/strategies.json.

    Reports, per strategy, which enabled entry conditions the real fills satisfy,
    which they violate, and which cannot be checked from a statement at all.
    Read-only: the strategy config is never modified.

    Only QFX statements are accepted — they are the one format that carries the
    intraday timestamps the entry-window check needs. SPX and VIX closes for the
    statement's date range are loaded (fresh by default; ``offline=True`` uses
    the cached CSVs) so the credit-implied delta and the VIX gate are decidable.
    """
    try:
        from spx_trade_desk.tradelog.analysis import strategy_analysis as sa
        from spx_trade_desk.tradelog.analysis import tagging as tg

        strategies = tg.load_strategy_specs(strategies_path)
        if not strategies:
            return {
                "error": "no strategies configured",
                "config": tg.strategy_config_fingerprint(strategies_path),
            }

        loaded: list[tuple[str, pd.DataFrame, pd.DataFrame]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            qfx_paths = [str(p) for p in (paths or [])
                         if Path(p).suffix.lower() == ".qfx"]
            for index, fc in enumerate(file_contents or []):
                if Path(fc.get("name", "")).suffix.lower() != ".qfx":
                    continue
                materialized = _materialize_file_contents(fc, tmpdir, index)
                if materialized is not None:
                    qfx_paths.append(materialized)

            if not qfx_paths:
                return {"error": "analyze_strategy_compliance requires a QFX statement; "
                                 "no other format carries intraday entry timestamps"}

            for qfx_path in qfx_paths:
                if account_filter != "All":
                    frame, _balance, _warning = _load_file_from_path(qfx_path)
                    accounts = set(frame["account_id"].dropna().astype(str)) if not frame.empty else set()
                    if account_filter not in accounts:
                        continue

                spreads = sa.reconstruct_spreads(qfx_path)
                if spreads.empty:
                    continue
                positions = sa.load_positions(qfx_path)
                spreads = tg.attach_entry_times(spreads, positions)
                spreads["source"] = Path(qfx_path).name
                spreads["spread_id"] = [
                    f"{Path(qfx_path).name}:{index}" for index in spreads.index
                ]
                loaded.append((qfx_path, spreads, positions))

            if not loaded:
                return {"error": "no spreads found for the requested statement or account",
                        "config": tg.strategy_config_fingerprint(strategies_path)}

            # Market data spanning every statement, so the delta solver and the
            # VIX gate have a spot/vol per entry date. Without it every
            # short_delta check is unverifiable and no strategy can ever match.
            first_day = min(pd.to_datetime(spreads["date"]).min() for _, spreads, _ in loaded)
            last_day = max(pd.to_datetime(spreads["date"]).max() for _, spreads, _ in loaded)
            market = _load_market_frame(first_day, last_day, offline=offline)

            matrices, summaries, audits = [], [], []
            for _qfx_path, spreads, positions in loaded:
                matrix = tg.spread_condition_matrix(
                    spreads, strategies, market=market, r=annual_rf_pct / 100.0)
                matrices.append(matrix)
                summaries.append(tg.strategy_compliance_summary(matrix, strategies))
                audits.append(tg.exit_audit(spreads, positions, strategies))

        # df_to_records cleans each cell with pd.isna, which returns an ARRAY for
        # a list cell (and raises on an empty one), so the list-valued columns are
        # flattened to scalars before serialization.
        matrix_frame = pd.concat(matrices, ignore_index=True)
        for column in ("failed", "unverifiable"):
            matrix_frame[column] = matrix_frame[column].apply(
                lambda value: ", ".join(value) if isinstance(value, list) else value)

        return {
            "config": tg.strategy_config_fingerprint(strategies_path),
            "compliance": df_to_records(pd.concat(summaries, ignore_index=True)),
            "condition_matrix": df_to_records(matrix_frame),
            "exit_audit": df_to_records(pd.concat(audits, ignore_index=True)),
            "market_days": int(len(market)) if market is not None else 0,
            "note": ("Compliance compares the CURRENT config against historical fills; the "
                     "config armed when a trade was placed is not recoverable from a statement. "
                     "A null match means a condition could not be checked — not that it failed. "
                     "Rows are grouped by source because spread ids restart in every file. "
                     "exit_audit's basis column says whether loss_ratio was measured against "
                     "the spread credit (the live engine's rule) or a single short leg."),
        }
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}


def _load_market_frame(first_day, last_day, offline: bool = False):
    """SPX + VIX closes spanning [first_day, last_day], or None when unavailable.

    Failure to load is not fatal: a missing market frame leaves spot-dependent
    conditions unverifiable, which is the honest answer, rather than failing them.
    """
    try:
        from spx_trade_desk.resources import REPORT_DATA_DIR
        from spx_trade_desk.tradelog.analysis import strategy_analysis as sa

        start_year = pd.Timestamp(first_day).year
        end_date = pd.Timestamp(last_day)
        spx, vix = sa.load_market_data(
            str(REPORT_DATA_DIR / "spx_closes.csv"),
            str(REPORT_DATA_DIR / "vix_closes.csv"),
            start_year, end_date, offline=offline,
        )
        frame = spx[["activity_date", "spx_close"]].copy()
        if vix is not None and not vix.empty and "vix_close" in vix.columns:
            frame = frame.merge(vix[["activity_date", "vix_close"]],
                                on="activity_date", how="outer")
        return frame if not frame.empty else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
