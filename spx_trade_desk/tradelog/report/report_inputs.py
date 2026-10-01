"""Inputs bundle and source loaders for the monthly report.

``build_report`` is source-agnostic: it asks ``load_report_inputs`` for a ``ReportInputs`` and renders it.
A QFX statement carries intraday timestamps, so it yields reconstructed positions and spreads. A trade-log
workbook carries dates only, so position direction cannot be recovered and ``positions``/``spreads`` are
``None``; the report then shows notices for the sections that need them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from spx_trade_desk.tradelog.analysis import strategy_analysis as sa
from spx_trade_desk.tradelog.domain.pnl_engine import build_realized_pnl
from spx_trade_desk.tradelog.domain.strategy_filter import filter_strategy_rows
from spx_trade_desk.tradelog.io.load_qfx import load_transactions_qfx
from spx_trade_desk.tradelog.io.load_xlsx import load_transactions_xlsx

QFX_EXTS = (".qfx",)
LEDGER_EXTS = (".xlsx", ".xlsm")
DEFAULT_LEDGER_CAPITAL = 100_000.0
_EMPTY_DAILY_COLUMNS = [
    "activity_date", "realized_pnl", "commission_spent", "option_contracts_traded", "trade_count",
    "expire_inferred_count", "expire_inferred_contract_count", "expire_inferred_pnl", "cumulative_pnl",
]


@dataclass
class ReportInputs:
    daily: pd.DataFrame
    initial_capital: float
    account_label: str
    positions: pd.DataFrame | None = None
    spreads: pd.DataFrame | None = None
    prior_daily: pd.DataFrame | None = None
    prior_capital: float | None = None
    # A workbook holds its own earlier months: build_report derives the prior months from ``daily``.
    prior_from_window: bool = False
    # Provenance for the HTML box and report_data; ``None`` for a QFX statement.
    source: dict | None = None

    @property
    def has_positions(self) -> bool:
        return self.positions is not None


def report_source_kind(path: str, display_name: str | None = None) -> str:
    """Return ``"qfx"`` or ``"ledger"`` from the file extension, else raise ``ValueError``."""
    ext = Path(str(path)).suffix.lower()
    if ext in QFX_EXTS:
        return "qfx"
    if ext in LEDGER_EXTS:
        return "ledger"
    name = display_name or Path(str(path)).name
    shown = ext if ext else "a file with no extension"
    raise ValueError(
        "generate_monthly_report accepts a QFX statement or an .xlsx trade-log workbook; "
        f"{name} is {shown}"
    )


def load_qfx_inputs(args) -> ReportInputs:
    """A QFX statement: daily P&L, reconstructed positions and spreads, balance-anchored capital."""
    monthly = str(Path(args.monthly))
    daily = sa.load_daily(monthly)
    positions = sa.load_positions(monthly)
    spreads = sa.reconstruct_spreads(monthly)
    df_raw, bal = load_transactions_qfx(monthly)
    full_initial = bal.total - df_raw["net_amount"].fillna(0.0).sum()
    account_id = str(df_raw["account_id"].iloc[0]) if ("account_id" in df_raw and len(df_raw)) else "—"

    prior_daily = None
    prior_capital = None
    if getattr(args, "ytd", None):
        prior_daily = sa.load_daily(args.ytd)
        ytd_raw, ytd_bal = load_transactions_qfx(args.ytd)
        prior_capital = ytd_bal.total - ytd_raw["net_amount"].fillna(0.0).sum()

    return ReportInputs(
        daily=daily,
        initial_capital=full_initial,
        account_label=f"Account {account_id}",
        positions=positions,
        spreads=spreads,
        prior_daily=prior_daily,
        prior_capital=prior_capital,
    )


def _coverage(frame: pd.DataFrame) -> tuple[int, int]:
    """(contract-days, closed-intraday) of the option rows.

    A contract-day is a distinct (account, contract, date); it is closed intraday when the quantity bought
    equals the quantity sold, which is the case whose direction a date-only ledger cannot recover.
    """
    traded = frame[frame["quantity"].fillna(0.0) != 0.0] if not frame.empty else frame
    if traded.empty:
        return 0, 0
    qty = traded["quantity"].astype(float)
    grouped = (
        pd.DataFrame({
            "account_id": traded["account_id"], "symbol": traded["symbol"],
            "day": traded["activity_date"], "bought": qty.clip(lower=0.0), "sold": (-qty).clip(lower=0.0),
        })
        .groupby(["account_id", "symbol", "day"])[["bought", "sold"]].sum()
    )
    closed = int(((grouped["bought"] > 0) & (grouped["bought"] == grouped["sold"])).sum())
    return int(len(grouped)), closed


def load_ledger_inputs(args) -> ReportInputs:
    """A trade-log workbook: SPX/SPXW option rows, optional account filter, no positions."""
    capital_arg = getattr(args, "initial_capital", None)
    if capital_arg is not None and not capital_arg > 0:
        raise ValueError("initial_capital must be a positive number")
    account = getattr(args, "account", None) or "All"

    loaded = load_transactions_xlsx(args.monthly)
    skipped_rows = int(loaded.attrs.get("skipped_rows", 0))
    frame = filter_strategy_rows(loaded)
    accounts = sorted(frame["account_id"].dropna().astype(str).unique()) if not frame.empty else []
    if account != "All":
        if account not in accounts:
            available = accounts if accounts else "none (the workbook has no SPX/SPXW option rows)"
            raise ValueError(f"Unknown account {account!r}; available: {available}")
        frame = frame[frame["account_id"].astype(str) == account].reset_index(drop=True)
        accounts = [account]

    # build_realized_pnl raises KeyError on an empty frame; report "no trades" instead.
    daily = pd.DataFrame(columns=_EMPTY_DAILY_COLUMNS) if frame.empty else build_realized_pnl(frame).daily
    capital = float(capital_arg) if capital_arg is not None else DEFAULT_LEDGER_CAPITAL
    contract_days, closed_intraday = _coverage(frame)
    prefix = "Account " if len(accounts) == 1 else "Accounts "
    return ReportInputs(
        daily=daily,
        initial_capital=capital,
        account_label=prefix + (", ".join(accounts) if accounts else "—"),
        prior_from_window=True,
        source={
            "kind": "ledger",
            "description": "trade-log workbook (dates only, no intraday timestamps)",
            "accounts": accounts,
            "scope": "SPX/SPXW options",
            "capital_at_start": capital,
            "capital_assumed": capital_arg is None,
            "contract_days": contract_days,
            "closed_intraday": closed_intraday,
            "skipped_rows": skipped_rows,
        },
    )


def load_report_inputs(args) -> ReportInputs:
    """Dispatch on the file extension of ``args.monthly``."""
    if report_source_kind(args.monthly) == "ledger":
        if getattr(args, "ytd", None):
            raise ValueError(
                "ytd is not supported with a workbook: earlier months are read from the workbook itself"
            )
        return load_ledger_inputs(args)
    return load_qfx_inputs(args)
