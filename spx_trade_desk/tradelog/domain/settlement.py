"""Cash settlement of expired SPXW options that a broker export leaves out.

An IBKR QFX statement records a worthless 0DTE expiry as a ``$0`` closing trade, but it says
nothing about an *in-the-money* expiry: the short (and long) legs just stay open, because the
cash settlement is booked on the transactions report only. P&L built from the QFX therefore
overstates every month with an ITM expiry.

SPXW is PM-settled in cash at the official SPX close, so the missing cash flow is exactly
``net signed quantity x intrinsic value x 100``. This module rebuilds it from a caller-supplied
map of official closes. It is deliberately conservative -- it only touches contracts whose whole
trade history sits on their expiry day (so the opening trade is guaranteed to be in the file),
and it never second-guesses a contract the ledger already settled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Optional

import pandas as pd

from spx_trade_desk.tradelog.domain.parse_option_symbol import parse_occ_option_symbol

SETTLED_ROOTS = frozenset({"SPXW"})   # PM-settled roots: their settlement value is the official close
SETTLEMENT_TYPE = "Cash Settlement"   # same label the IBKR transactions report uses
INFERRED_PREFIX = "Cash settlement (inferred"   # marks rows this module added
CONTRACT_MULTIPLIER = 100.0


@dataclass(frozen=True)
class Settlement:
    account_id: str
    symbol: str
    expiry: date
    right: str
    strike: float
    net_qty: float          # signed: long positive
    settle_price: float
    cash: float             # signed cash flow to the account


def intrinsic_value(right: str, strike: float, settle_price: float) -> float:
    if right == "P":
        return max(strike - settle_price, 0.0)
    return max(settle_price - strike, 0.0)


def find_settlements(
    legs: pd.DataFrame,
    settle_prices: Mapping[date, float],
    as_of: Optional[date] = None,
    already_settled: Iterable[tuple[str, str]] = (),
    include_worthless: bool = False,
) -> tuple[list[Settlement], list[date]]:
    """Cash settlements missing from a set of option trades.

    ``legs`` has one row per trade with columns ``account_id``, ``symbol`` (OCC), ``trade_date``
    and ``signed_qty`` (buy positive, sell negative). ``already_settled`` lists
    ``(account_id, symbol)`` pairs the ledger already carries a settlement for.

    Returns ``(settlements, unpriced_expiries)``. By default only ITM expiries produce a settlement:
    a worthless one needs no cash flow (the ledger's expire-inferred logic already treats a short
    as such). ``include_worthless`` also returns zero-cash settlements for them, which a leg-level
    consumer needs to see the position as closed. An expiry
    with an open position but no entry in ``settle_prices`` is reported in ``unpriced_expiries``
    instead of being guessed. Expiries after ``as_of`` have not happened yet and are skipped.
    """
    as_of = as_of or date.today()
    skip = set(already_settled)
    settlements: list[Settlement] = []
    unpriced: set[date] = set()
    if legs.empty:
        return settlements, []

    for (account, symbol), grp in legs.groupby(["account_id", "symbol"], sort=True):
        parsed = parse_occ_option_symbol(symbol)
        if parsed is None or parsed.underlying not in SETTLED_ROOTS:
            continue
        if (str(account), str(symbol)) in skip:
            continue
        net = float(grp["signed_qty"].sum())
        if abs(net) < 1e-9:
            continue                                   # closed out by trades (incl. $0 expiry closes)
        if parsed.expiry_date > as_of:
            continue                                   # not expired yet
        if any(pd.Timestamp(d).date() != parsed.expiry_date for d in grp["trade_date"]):
            continue                                   # opened before the file's window: net is not trustworthy
        price = settle_prices.get(parsed.expiry_date)
        if price is None:
            unpriced.add(parsed.expiry_date)
            continue
        intrinsic = intrinsic_value(parsed.right, parsed.strike, float(price))
        if intrinsic <= 0.0 and not include_worthless:
            continue
        settlements.append(Settlement(
            account_id=str(account), symbol=str(symbol), expiry=parsed.expiry_date,
            right=parsed.right, strike=parsed.strike, net_qty=net, settle_price=float(price),
            cash=round(net * intrinsic * CONTRACT_MULTIPLIER, 2),
        ))
    return settlements, sorted(unpriced)


def infer_expiry_settlements(
    df: pd.DataFrame,
    settle_prices: Mapping[date, float],
    as_of: Optional[date] = None,
) -> tuple[pd.DataFrame, list[date]]:
    """Append the missing ITM ``Cash Settlement`` rows to a transaction ledger.

    Returns ``(ledger, unpriced_expiries)``. The ledger is returned unchanged when nothing is missing.
    A contract the ledger already settled (a ``Cash Settlement`` row on the same account and symbol)
    is left alone, so a real settlement from a transactions CSV always wins over the inferred one.
    """
    if df.empty or "symbol" not in df.columns:
        return df, []
    ttype = df["transaction_type"].fillna("").astype(str).str.lower()
    settled = df[ttype.str.contains(SETTLEMENT_TYPE.lower(), na=False)]
    already = {(str(a), str(s)) for a, s in zip(settled["account_id"], settled["symbol"])}

    trades = df[ttype.isin(["buy", "sell"]) & df["quantity"].notna()]
    legs = pd.DataFrame({
        "account_id": trades["account_id"].astype(str),
        "symbol": trades["symbol"].astype(str),
        "trade_date": trades["activity_date"],
        "signed_qty": trades["quantity"].astype(float),
    })
    found, unpriced = find_settlements(legs, settle_prices, as_of=as_of, already_settled=already)
    if not found:
        return df, unpriced

    if "source_row" in df.columns:
        next_row = int(pd.to_numeric(df["source_row"], errors="coerce").fillna(0).max()) + 1
    else:
        next_row = len(df) + 1
    # keep the ledger's own date type so the combined column still sorts
    first_date = df["activity_date"].dropna().iloc[0] if df["activity_date"].notna().any() else None
    as_timestamp = isinstance(first_date, pd.Timestamp)
    rows = []
    for i, s in enumerate(found):
        rows.append({
            "activity_date": pd.Timestamp(s.expiry) if as_timestamp else s.expiry,
            "account_id": s.account_id,
            "description": f"{INFERRED_PREFIX} at {s.settle_price:.2f}) {s.symbol}",
            "transaction_type": SETTLEMENT_TYPE,
            "symbol": s.symbol,
            "quantity": float("nan"),
            "price": float("nan"),
            "gross_amount": s.cash,
            "commission": 0.0,
            "net_amount": s.cash,
            "source_row": next_row + i,
        })
    out = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    return out.sort_values("activity_date", kind="stable").reset_index(drop=True), unpriced


def load_settle_prices(csv_path: Optional[str | Path] = None, as_of: Optional[date] = None) -> dict[date, float]:
    """Official SPX closes from the repo's market-data cache, as ``{date: close}``.

    ``as_of``'s own row (and any later one) is dropped: a daily bar fetched mid-session carries the
    last price, not the settlement value, so a close is trusted only once its day is over.
    """
    if csv_path is None:
        from spx_trade_desk.resources import REPORT_DATA_DIR
        csv_path = Path(REPORT_DATA_DIR) / "spx_closes.csv"
    path = Path(csv_path)
    if not path.exists():
        return {}
    frame = pd.read_csv(path, usecols=["activity_date", "spx_close"])
    frame["activity_date"] = pd.to_datetime(frame["activity_date"], errors="coerce").dt.date
    frame["spx_close"] = pd.to_numeric(frame["spx_close"], errors="coerce")
    frame = frame.dropna()
    cutoff = as_of or date.today()
    return {d: float(c) for d, c in zip(frame["activity_date"], frame["spx_close"]) if d < cutoff}


def describe_settlements(ledger: pd.DataFrame, unpriced: list[date]) -> list[str]:
    """Human-readable notes about what the inference did, for report/tool warnings.

    ``ledger`` is the frame returned by :func:`infer_expiry_settlements`; the rows it added are
    recognised by their description prefix.
    """
    notes: list[str] = []
    if ledger is not None and not ledger.empty and "description" in ledger.columns:
        added = ledger[ledger["description"].astype(str).str.startswith(INFERRED_PREFIX)]
    else:
        added = pd.DataFrame()
    if not added.empty:
        total = float(added["net_amount"].sum())
        days = sorted({pd.Timestamp(d).date().isoformat() for d in added["activity_date"]})
        notes.append(
            f"Inferred {len(added)} ITM cash settlement(s) missing from the QFX "
            f"(expiry day(s) {', '.join(days)}; net {total:+,.2f}) at the official SPX close."
        )
    if unpriced:
        shown = ", ".join(d.isoformat() for d in unpriced)
        notes.append(
            f"Open SPXW position(s) expiring {shown} have no official SPX close on file, so their cash "
            "settlement is NOT included: P&L may overstate a loss or a gain for those days."
        )
    return notes
