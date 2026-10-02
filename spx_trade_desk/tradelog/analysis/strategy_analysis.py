"""Reusable strategy analysis for 0DTE SPX bull-put-credit-spread books.

Consolidates the edge / risk analysis built during the July 2026 session into a
single importable module, with the spread-pairing bug fixed:

  * Position direction is determined from the OPENING leg in execution order
    (DTTRADE timestamp), not from ``price`` or ``source_row`` — this correctly
    excludes $0.00 expiry liquidations and closing legs that were previously
    misread as "naked shorts".
  * Short positions are paired with same-day, same-expiry, lower-strike long
    positions into bull put spreads (greedy closest-lower, quantity matched).

Everything returns plain pandas DataFrames so ``generate_report.py`` can render
them.
"""
from __future__ import annotations

import re as _re
from datetime import date as _date, datetime as _dt, time as _time
from pathlib import Path
from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from spx_trade_desk.tradelog.domain.parse_option_symbol import parse_occ_option_symbol
from spx_trade_desk.tradelog.domain.settlement import find_settlements, load_settle_prices
from spx_trade_desk.tradelog.io.load_qfx import _build_security_map, load_transactions_qfx
from spx_trade_desk.tradelog.domain.pnl_engine import build_realized_pnl

SETTLE_TRANS_TYPE = "Settle"          # trans_type of a cash-settlement leg in load_options_with_time
SETTLE_TIME = _time(16, 0)            # SPXW settles at the 16:00 ET close


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _settle_prices(settle_prices: Optional[Mapping[_date, float]]) -> Mapping[_date, float]:
    """``None`` -> official closes from the repo's market-data cache; pass ``{}`` to switch inference off."""
    return load_settle_prices() if settle_prices is None else settle_prices


def load_enriched(path: str, settle_prices: Optional[Mapping[_date, float]] = None) -> pd.DataFrame:
    """Load a QFX file and return the pnl_engine enriched rows.

    ITM expiries the QFX leaves out are settled at the official close (``settle_prices``, default
    the repo's cached SPX closes; ``{}`` turns the inference off).
    """
    df, _ = load_transactions_qfx(path, settle_prices=_settle_prices(settle_prices))
    return build_realized_pnl(df).enriched_rows


def load_daily(path: str, settle_prices: Optional[Mapping[_date, float]] = None) -> pd.DataFrame:
    """Return the daily realized-PnL series for a QFX file (ITM expiries settled, see ``load_enriched``)."""
    df, _ = load_transactions_qfx(path, settle_prices=_settle_prices(settle_prices))
    return build_realized_pnl(df).daily


def load_options_with_time(path: str, settle_prices: Optional[Mapping[_date, float]] = None) -> pd.DataFrame:
    """Re-parse QFX option transactions preserving true intra-day order.

    The project loader truncates DTTRADE to the date; here we keep the full
    timestamp so opening vs closing legs are correctly ordered.

    A QFX omits the cash settlement of an in-the-money expiry, so each unclosed SPXW contract that
    expired ITM gets a ``Settle`` leg at the 16:00 close carrying the settlement cash (see
    ``domain.settlement``); ``settle_prices`` is as in ``load_enriched``.
    """
    text = Path(path).read_text(encoding="latin-1", errors="replace")
    sec_map = _build_security_map(text)
    tl = _re.search(r"<INVTRANLIST>(.*?)</INVTRANLIST>", text, _re.DOTALL)
    tran = tl.group(1) if tl else text

    rows = []
    pat = _re.compile(r"<(BUYOPT|SELLOPT)>(.*?)</(?:BUYOPT|SELLOPT)>", _re.DOTALL)
    for m in pat.finditer(tran):
        tag, block = m.group(1), m.group(2)
        it = _re.search(r"<INVTRAN>(.*?)</INVTRAN>", block, _re.DOTALL)
        dt_raw = _re.search(r"<DTTRADE>([^<\n]+)", it.group(1)) if it else None
        ts = None
        if dt_raw:
            try:
                ts = _dt.strptime(dt_raw.group(1).strip()[:14], "%Y%m%d%H%M%S")
            except ValueError:
                ts = None
        inv = _re.search(r"<INV(?:BUY|SELL)>(.*?)</INV(?:BUY|SELL)>", block, _re.DOTALL)
        if not inv:
            continue
        ib = inv.group(1)
        uid = _re.search(r"<UNIQUEID>([^<\n]+)", ib)
        if not uid:
            continue
        sec = sec_map.get(uid.group(1).strip())
        if sec is None:
            continue
        po = parse_occ_option_symbol(sec.ticker)
        if po is None:
            continue
        units = float(_re.search(r"<UNITS>([^<\n]+)", ib).group(1).strip())
        price = float(_re.search(r"<UNITPRICE>([^<\n]+)", ib).group(1).strip())
        total = float(_re.search(r"<TOTAL>([^<\n]+)", ib).group(1).strip())
        qty = units if tag == "BUYOPT" else -units
        rows.append({
            "ts": ts,
            "symbol": sec.ticker,
            "contract_key": po.contract_key,
            "expiry": po.expiry_date.isoformat(),
            "strike": float(po.strike),
            "qty": qty,
            "net_amount": total,
            "trans_type": "Buy" if tag == "BUYOPT" else "Sell",
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    legs = pd.DataFrame({
        "account_id": "",
        "symbol": frame["symbol"],
        "trade_date": [t.date() if (t is not None and not pd.isna(t)) else pd.NaT for t in frame["ts"]],
        "signed_qty": np.where(frame["trans_type"] == "Buy", frame["qty"], -frame["qty"]),
    })
    found, _ = find_settlements(legs, _settle_prices(settle_prices), include_worthless=True)
    if found:
        extra = []
        for s in found:
            po = parse_occ_option_symbol(s.symbol)
            extra.append({
                "ts": _dt.combine(s.expiry, SETTLE_TIME),
                "symbol": s.symbol,
                "contract_key": po.contract_key,
                "expiry": s.expiry.isoformat(),
                "strike": float(s.strike),
                "qty": abs(s.net_qty),
                "net_amount": s.cash,
                "trans_type": SETTLE_TRANS_TYPE,
            })
        frame = pd.concat([frame, pd.DataFrame(extra)], ignore_index=True)
    return frame


def load_positions(path: str, settle_prices: Optional[Mapping[_date, float]] = None) -> pd.DataFrame:
    """One row per closed position (contract_key), direction from opening leg.

    Columns: contract_key, date, open_ts, direction (short/long), contracts,
    total_pnl (net of commission, including any cash settlement), trade_pnl (the same without the
    settlement), settlement (cash settlement at expiry, 0 when none), credit (short side: total premium
    received), debit (long side: total premium paid), expiry, strike.
    """
    legs = load_options_with_time(path, settle_prices)
    out = []
    for key, g in legs.groupby("contract_key"):
        g = g.sort_values("ts", na_position="last")
        trades = g[g["trans_type"] != SETTLE_TRANS_TYPE]
        settles = g[g["trans_type"] == SETTLE_TRANS_TYPE]
        first = trades.iloc[0]
        opening = first["trans_type"]
        out.append({
            "contract_key": key,
            "date": pd.Timestamp(first["ts"].date()) if first["ts"] else None,
            "open_ts": first["ts"],
            "direction": "short" if opening == "Sell" else "long",
            "contracts": g["qty"].abs().sum() / 2.0,
            "total_pnl": g["net_amount"].sum(),
            "trade_pnl": trades["net_amount"].sum(),
            "settlement": settles["net_amount"].sum(),
            "credit": trades.loc[trades["trans_type"] == "Sell", "net_amount"].sum()
                      if opening == "Sell" else None,
            "debit": -trades.loc[trades["trans_type"] == "Buy", "net_amount"].sum()
                     if opening == "Buy" else None,
            "expiry": first["expiry"],
            "strike": float(first["strike"]),
        })
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Spread reconstruction
# ---------------------------------------------------------------------------

def reconstruct_spreads(path: str, settle_prices: Optional[Mapping[_date, float]] = None) -> pd.DataFrame:
    """Pair short positions with lower-strike longs into bull put spreads.

    Returns one row per short position: date, expiry, short_strike, qty,
    short_credit_ct (net premium received per contract), paired (bool),
    long_strike, width, credit_ct (net spread credit = short - long cost),
    max_loss_ct (width*100 - credit_ct).  Unpaired shorts are flagged.
    """
    pos = load_positions(path, settle_prices)
    rows = []
    for (d, exp), g in pos.groupby(["date", "expiry"]):
        shorts = g[g["direction"] == "short"].copy()
        longs = g[g["direction"] == "long"].copy()
        # cost of the long as bought: its P&L is not the cost once it was sold back or settled ITM
        longs["cost_ct"] = longs["debit"] / longs["contracts"]
        for _, s in shorts.iterrows():
            s_qty = int(s["contracts"])
            s_prem_ct = (s["credit"] or 0.0) / s_qty
            avail = longs[longs["strike"] < s["strike"]].sort_values(
                "strike", ascending=False)
            matched_qty = 0
            width = None
            long_cost_ct = None
            for _, lo in avail.iterrows():
                if s_qty <= 0:
                    break
                lo_qty = int(lo["contracts"])
                use = min(lo_qty, s_qty)
                if use > 0:
                    matched_qty += use
                    width = float(s["strike"] - lo["strike"])
                    long_cost_ct = float(lo["cost_ct"])
                    longs.loc[lo.name, "contracts"] -= use
                    s_qty -= use
            credit_ct = (s_prem_ct - long_cost_ct) if (matched_qty and long_cost_ct is not None) else s_prem_ct
            max_loss_ct = (width * 100.0 - credit_ct) if (matched_qty and width is not None) else None
            rows.append({
                "date": d,
                "expiry": exp,
                "short_strike": float(s["strike"]),
                "qty": int(s["contracts"]),
                "short_credit_ct": s_prem_ct,
                "paired": matched_qty > 0,
                "long_strike": float(s["strike"] - width) if (matched_qty and width is not None) else None,
                "width": width,
                "credit_ct": credit_ct,
                "max_loss_ct": max_loss_ct,
                "short_pnl": float(s["total_pnl"]),
            })
    return pd.DataFrame(rows)


def per_leg_stats(pos: pd.DataFrame) -> pd.DataFrame:
    """Win-rate / avg-win / avg-loss / EV for the short and long legs."""
    rows = []
    for label in ["short", "long"]:
        sub = pos[pos["direction"] == label]
        w = sub[sub["total_pnl"] > 0]["total_pnl"]
        l = sub[sub["total_pnl"] < 0]["total_pnl"]
        n = len(sub)
        rows.append({
            "leg": label,
            "n": n,
            "win_rate": (sub["total_pnl"] > 0).mean() if n else np.nan,
            "avg_win": w.mean() if len(w) else np.nan,
            "avg_loss": abs(l.mean()) if len(l) else np.nan,
            "ev": sub["total_pnl"].mean() if n else np.nan,
            "total_pnl": sub["total_pnl"].sum() if n else 0.0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def bootstrap_ci(x: Iterable[float], n_boot: int = 100_000,
                 seed: int = 7) -> tuple[float, float, float]:
    """Percentile bootstrap 95% CI of the mean -> (lo, median, hi)."""
    r = np.random.default_rng(seed)
    x = np.asarray(list(x), dtype=float)
    if len(x) == 0:
        return (np.nan, np.nan, np.nan)
    idx = r.integers(0, len(x), size=(n_boot, len(x)))
    means = x[idx].mean(axis=1)
    return tuple(np.percentile(means, [2.5, 50, 97.5]))


def bootstrap_pval_gt0(x: Iterable[float], n_boot: int = 100_000,
                       seed: int = 11) -> float:
    """Fraction of bootstrap means <= 0 -> p(H0: mean <= 0)."""
    r = np.random.default_rng(seed)
    x = np.asarray(list(x), dtype=float)
    if len(x) == 0:
        return np.nan
    idx = r.integers(0, len(x), size=(n_boot, len(x)))
    means = x[idx].mean(axis=1)
    return float((means <= 0).mean())


def breakeven_win_rate(avg_win: float, avg_loss: float) -> float:
    """Win rate required for EV=0 given avg win/loss."""
    if avg_loss and avg_win and (avg_win + avg_loss) > 0:
        return avg_loss / (avg_win + avg_loss)
    return np.nan


def kelly_binary(p_win: float, b: float) -> tuple[float, float]:
    """Kelly fraction for a binary bet winning b units per 1 lost."""
    if b <= 0:
        return 0.0, 0.0
    f = p_win - (1 - p_win) / b
    return f, max(0.0, f / 2.0)


def monte_carlo_days(days: np.ndarray, n_months: int = 100_000,
                     days_in_month: int = 22, seed: int = 23) -> dict:
    """Resample days to simulate months and years (iid day bootstrap)."""
    r = np.random.default_rng(seed)
    n_days = len(days)
    if n_days == 0:
        return {}
    idx = r.integers(0, n_days, size=(n_months, days_in_month))
    month_totals = days[idx].sum(axis=1)
    idx12 = r.integers(0, n_days, size=(n_months, 12, days_in_month))
    year_totals = days[idx12].sum(axis=2).sum(axis=1)
    return {
        "month_mean": float(month_totals.mean()),
        "month_p5": float(np.percentile(month_totals, 5)),
        "month_p25": float(np.percentile(month_totals, 25)),
        "month_p50": float(np.percentile(month_totals, 50)),
        "month_p95": float(np.percentile(month_totals, 95)),
        "month_p_losing": float((month_totals < 0).mean()),
        "year_mean": float(year_totals.mean()),
        "year_p5": float(np.percentile(year_totals, 5)),
        "year_p_losing": float((year_totals < 0).mean()),
        "prob_day_neg1000": float((days < -1000).mean()),
        "prob_day_neg1500": float((days < -1500).mean()),
        "days_per_year_neg1000": float((days < -1000).mean()) * 252,
        "days_per_year_neg1500": float((days < -1500).mean()) * 252,
        "worst_day": float(days.min()),
    }


# ---------------------------------------------------------------------------
# Tail stress (spread-capped)
# ---------------------------------------------------------------------------

def gap_stress(spreads: pd.DataFrame, spx_map: dict,
               gaps: Iterable[float]) -> pd.DataFrame:
    """Per-day book loss for each gap, using spread max-loss caps.

    For a paired spread held to expiry at S_end = spot*(1-gap):
      S_end >= short_strike -> keep full credit (profit)
      between strikes       -> lose (short_strike - S_end)*100 - credit
      below long strike     -> lose max_loss = width*100 - credit
    Unpaired shorts use the full notional (no cap).
    """
    gaps = list(gaps)
    rows = []
    for g in gaps:
        per_day: dict[str, float] = {}
        for _, r in spreads.iterrows():
            spot = spx_map.get(pd.Timestamp(r["date"]).date())
            if spot is None:
                continue
            s_end = spot * (1.0 - g)
            qty = int(r["qty"])
            credit = float(r["credit_ct"]) if r["paired"] else float(r["short_credit_ct"])
            if s_end >= r["short_strike"]:
                pnl = credit * qty
            else:
                intrinsic = (r["short_strike"] - s_end) * 100.0
                if r["paired"] and r["max_loss_ct"] is not None:
                    pnl = max(credit - intrinsic, -float(r["max_loss_ct"])) * qty
                else:
                    pnl = (credit - intrinsic) * qty
            per_day.setdefault(str(r["date"]), 0.0)
            per_day[str(r["date"])] += pnl
        for d, v in per_day.items():
            rows.append({"gap": g, "date": d, "pnl": v})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Stops & re-entry
# ---------------------------------------------------------------------------

def stop_events(positions: pd.DataFrame, stop_multiple: float = 5.0) -> pd.DataFrame:
    """Flag short positions closed at >= stop_multiple x credit (default 5x).

    Uses the trade P&L only: an ITM expiry settled in cash is not a stop (nothing was closed), so
    its settlement is left out here and shows up in the daily and per-leg figures instead.
    """
    stops = []
    for _, r in positions.iterrows():
        if r["direction"] != "short":
            continue
        credit = r["credit"]
        if credit is None or credit <= 0:
            continue
        pnl = r["trade_pnl"] if "trade_pnl" in r.index else r["total_pnl"]
        loss_ratio = (-pnl) / credit if pnl < 0 else 0.0
        stops.append({
            "date": r["date"],
            "open_ts": r["open_ts"],
            "strike": float(r["strike"]),
            "total_pnl": float(pnl),
            "credit": float(credit),
            "loss_ratio": float(loss_ratio),
            "is_stop": bool(loss_ratio >= stop_multiple),
        })
    return pd.DataFrame(stops)


def reentry_test(positions: pd.DataFrame, stops: pd.DataFrame) -> pd.DataFrame:
    """For each stop, sum PnL of positions opened after the stop that day."""
    rows = []
    for _, st in stops.iterrows():
        day = st["date"]
        later = positions[(positions["date"] == day) & (positions["open_ts"].notna())]
        if st["open_ts"] is not None:
            later = later[pd.to_datetime(later["open_ts"]) > pd.to_datetime(st["open_ts"])]
        rows.append({
            "stop_date": day,
            "stop_loss": st["total_pnl"],
            "n_after": int(len(later)),
            "pnl_after": float(later["total_pnl"].sum()) if len(later) else 0.0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def _update_market_cache(csv_path: str, fetched: pd.DataFrame) -> None:
    """Merge freshly fetched market data into the persisted CSV cache.

    The fetched rows win on date collisions; derived return/change columns are
    recomputed so the cache stays internally consistent. Keeps the offline
    fallback current even after the bundled CSVs were first written.
    """
    path = Path(csv_path)
    if path.exists():
        old = pd.read_csv(path)
        merged = pd.concat([old, fetched], ignore_index=True)
    else:
        merged = fetched.copy()
    # normalize dates to ISO strings FIRST so string + date rows dedupe correctly
    merged["activity_date"] = pd.to_datetime(merged["activity_date"]).dt.strftime("%Y-%m-%d")
    merged = merged.drop_duplicates(subset=["activity_date"], keep="last")
    merged = merged.sort_values("activity_date").reset_index(drop=True)
    if "spx_close" in merged.columns and "spx_return" in merged.columns:
        merged["spx_return"] = pd.to_numeric(merged["spx_close"], errors="coerce").pct_change()
    if "vix_close" in merged.columns and "vix_change" in merged.columns:
        merged["vix_change"] = pd.to_numeric(merged["vix_close"], errors="coerce").diff()
    merged.to_csv(path, index=False)


def load_market_data(spx_csv: str, vix_csv: str, start_year: int,
                     end_date: pd.Timestamp,
                     offline: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load SPX/VIX daily data, fetching fresh data by default.

    When ``offline`` is False (the default), fetches the latest SPX/VIX from
    Yahoo Finance and merges it into the persisted CSV cache so future offline
    runs stay current. When ``offline`` is True (or the network fetch fails),
    falls back to the cached CSVs. Returns (spx_df, vix_df) in the format
    risk_metrics expects.
    """
    from datetime import date
    start = date(start_year, 1, 1)
    end = end_date.date()
    spx = None
    vix = None
    if not offline:
        try:
            from spx_trade_desk.tradelog.io.load_spx import load_spx_daily
            spx = load_spx_daily(start, end)
            if spx is None or spx.empty:
                spx = None
        except Exception:
            spx = None
        try:
            from spx_trade_desk.tradelog.io.load_vix import load_vix_daily
            vix = load_vix_daily(start, end)
            if vix is None or vix.empty:
                vix = None
        except Exception:
            vix = None
        if spx is not None:
            _update_market_cache(spx_csv, spx)
        if vix is not None:
            _update_market_cache(vix_csv, vix)
    if spx is None:
        spx = pd.read_csv(spx_csv)
        spx["spx_return"] = pd.to_numeric(spx["spx_close"], errors="coerce").pct_change()
    if vix is None:
        vix = pd.read_csv(vix_csv)
    return spx, vix
