"""Score reconstructed statement spreads against configured strategy conditions.

Reads ``config/strategies.json`` and never writes it. The tagger compares
*today's* config against historical fills: it cannot know which config was armed
when a trade was placed, so every result carries a fingerprint of the config
revision it was computed from.

Band and bucket semantics are taken from the live strategy engine's own private
helpers rather than re-derived, so "compliant" means exactly what the engine
enforces at entry.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from spx_trade_desk.core.config import DEFAULT_RISK_FREE_RATE
from spx_trade_desk.market import hours
from spx_trade_desk.sim.pricing import bsm_put, bsm_put_delta
from spx_trade_desk.strategy.engine import _bucket_params, _hi, _lo, _passes_bucket
from spx_trade_desk.strategy.models import Strategy
from spx_trade_desk.strategy.store import load_strategies

PASS = "pass"
FAIL = "fail"
UNVERIFIABLE = "unverifiable"

SPX_CLOSE_ET = time(16, 0)
ASSUMED_ENTRY_ET = time(12, 0)
# Mirrors sim/pricing.bar_year_frac: one trading year = 252 sessions x 6.5h.
SECONDS_PER_TRADING_YEAR = 252.0 * 6.5 * 3600.0
DOLLARS_PER_CONTRACT = 100.0
# Same defaults the engine uses when a condition is absent entirely.
DEFAULT_DELTA_BAND = (0.05, 0.35)
DEFAULT_WIDTH_BAND = (5.0, 50.0)
DEFAULT_CREDIT_BAND = (0.0, 1e6)


def _finite(*values) -> bool:
    for value in values:
        if value is None:
            return False
        try:
            if not np.isfinite(float(value)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _year_fraction(entry_dt: datetime, expiry: date) -> float:
    close = datetime.combine(expiry, SPX_CLOSE_ET)
    return max((close - entry_dt).total_seconds(), 0.0) / SECONDS_PER_TRADING_YEAR


def implied_vol_from_credit(spot, k_short, k_long, t_years, credit_per_share,
                            r: float = DEFAULT_RISK_FREE_RATE) -> Optional[float]:
    """BSM vol that prices a bull put spread at ``credit_per_share``.

    ``credit_per_share`` is a per-share option price (0.287), not the dollar
    amount per contract (28.70). Returns None when the inputs cannot pin a vol —
    an unpaired short, a non-positive time to expiry, a credit outside the
    no-arbitrage band, or a spread too deep in the money for its credit to be
    informative.
    """
    if not _finite(spot, k_short, k_long, t_years, credit_per_share):
        return None
    if spot <= 0 or t_years <= 0 or k_short <= k_long:
        return None
    if credit_per_share <= 0 or credit_per_share >= (k_short - k_long):
        return None

    def excess(sigma: float) -> float:
        short_leg = float(bsm_put(spot, k_short, t_years, r, sigma))
        long_leg = float(bsm_put(spot, k_long, t_years, r, sigma))
        return short_leg - long_leg - credit_per_share

    lo, hi = 1e-4, 5.0
    try:
        if excess(lo) * excess(hi) > 0:
            return None
        return float(brentq(excess, lo, hi, maxiter=200, xtol=1e-10))
    except (ValueError, RuntimeError):
        return None


def short_delta_estimate(spot, k_short, k_long, t_years, credit_per_share,
                         r: float = DEFAULT_RISK_FREE_RATE) -> Optional[float]:
    """Absolute BSM delta of the short put, implied by the spread's credit."""
    sigma = implied_vol_from_credit(spot, k_short, k_long, t_years, credit_per_share, r)
    if sigma is None:
        return None
    return abs(float(bsm_put_delta(spot, k_short, t_years, r, sigma)))


def _credit_per_share(row: pd.Series) -> Optional[float]:
    """Statement credits are dollars per contract; strategy bands are per share."""
    raw = row.get("credit_ct") if bool(row.get("paired")) else row.get("short_credit_ct")
    if not _finite(raw):
        return None
    return float(raw) / DOLLARS_PER_CONTRACT


def _entry_datetime(row: pd.Series) -> tuple[Optional[datetime], bool]:
    """Return (entry datetime, whether the time was assumed)."""
    raw = row.get("open_ts")
    try:
        if raw is not None and not pd.isna(raw):
            return pd.Timestamp(raw).to_pydatetime(), False
    except (TypeError, ValueError):
        pass
    day = row.get("date")
    if day is None or (isinstance(day, float) and np.isnan(day)):
        return None, True
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if isinstance(day, pd.Timestamp):
        day = day.date()
    return datetime.combine(day, ASSUMED_ENTRY_ET), True


def load_strategy_specs(path=None) -> dict[str, Strategy]:
    """Configured strategies, keyed by name. Empty when the config is unusable."""
    return load_strategies(path)


def _market_row(market: Optional[pd.DataFrame], day) -> Optional[pd.Series]:
    if market is None or market.empty or day is None:
        return None
    key = pd.Timestamp(day).date()
    dates = pd.to_datetime(market["activity_date"]).dt.date
    match = market[dates == key]
    return None if match.empty else match.iloc[0]


def _range_state(value, cond, default_band) -> str:
    if cond is None:
        lo, hi = default_band
    else:
        lo, hi = _lo(cond.params, "min"), _hi(cond.params, "max")
    if not _finite(value):
        return UNVERIFIABLE
    return PASS if lo <= float(value) <= hi else FAIL


def _bucket_state(value, params, base) -> str:
    if value is None:
        return UNVERIFIABLE
    op, lo, hi = _bucket_params(params, base)
    if lo is None and hi is None:
        return UNVERIFIABLE
    return PASS if _passes_bucket(float(value), op, lo, hi) else FAIL


def _evaluate(row, strategy: Strategy, market_row, assumed_entry: bool,
              entry_dt: Optional[datetime], r: float) -> dict:
    checks: dict[str, str] = {}
    conds = {c.kind: c for c in strategy.conditions if c.enabled}

    if strategy.direction != "bull_put":
        checks["direction"] = UNVERIFIABLE

    checks["spread_width"] = _range_state(row.get("width"), conds.get("spread_width"), DEFAULT_WIDTH_BAND)
    checks["credit"] = _range_state(_credit_per_share(row), conds.get("credit"), DEFAULT_CREDIT_BAND)

    delta = None
    if market_row is not None and entry_dt is not None and _finite(row.get("width")):
        expiry = row.get("expiry")
        try:
            expiry_date = date.fromisoformat(str(expiry)[:10])
        except ValueError:
            expiry_date = None
        if expiry_date is not None:
            delta = short_delta_estimate(
                spot=float(market_row.get("spx_close")),
                k_short=float(row.get("short_strike")),
                k_long=float(row.get("long_strike")),
                t_years=_year_fraction(entry_dt, expiry_date),
                credit_per_share=_credit_per_share(row),
                r=r,
            )
    checks["short_delta"] = _range_state(delta, conds.get("short_delta"), DEFAULT_DELTA_BAND)

    if "entry_window" in conds:
        if entry_dt is None or assumed_entry:
            checks["entry_window"] = UNVERIFIABLE
        else:
            params = conds["entry_window"].params
            start = params.get("start", "09:30")
            end = params.get("end", "15:30")
            hhmm = entry_dt.strftime("%H:%M")
            checks["entry_window"] = PASS if start <= hhmm <= end else FAIL

    if "volatility" in conds:
        params = conds["volatility"].params
        if params.get("vix_enabled"):
            vix = market_row.get("vix_close") if market_row is not None else None
            checks["volatility"] = _bucket_state(vix, params, "vix")
        if params.get("atm_iv_enabled"):
            checks["volatility_atm_iv"] = UNVERIFIABLE

    if "trend" in conds:
        checks["trend"] = UNVERIFIABLE

    day = row.get("date")
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if isinstance(day, pd.Timestamp):
        day = day.date()

    if day is not None and hasattr(day, "weekday"):
        checks["run_days"] = PASS if day.weekday() in strategy.run_days else FAIL
        if hours.is_fomc_day(day) and not strategy.run_on_fomc:
            checks["run_on_fomc"] = FAIL
        if hours.is_nfp_day(day) and not strategy.run_on_nfp:
            checks["run_on_nfp"] = FAIL
        if hours.is_short_trading_day(day) and not strategy.short_day_enabled:
            checks["short_day_enabled"] = FAIL

    return checks


def spread_condition_matrix(spreads: pd.DataFrame, strategies: dict[str, Strategy],
                            market: Optional[pd.DataFrame] = None,
                            r: float = DEFAULT_RISK_FREE_RATE) -> pd.DataFrame:
    """One row per (spread × strategy) with pass / fail / unverifiable detail.

    ``market`` is an optional frame with ``activity_date`` plus ``spx_close``
    and/or ``vix_close``. Without it, spot-dependent checks come back
    unverifiable rather than failed.
    """
    rows = []
    for spread_id, row in spreads.iterrows():
        entry_dt, assumed_entry = _entry_datetime(row)
        market_row = _market_row(market, row.get("date"))
        for name, strategy in strategies.items():
            checks = _evaluate(row, strategy, market_row, assumed_entry, entry_dt, r)
            failed = sorted(k for k, v in checks.items() if v == FAIL)
            unverifiable = sorted(k for k, v in checks.items() if v == UNVERIFIABLE)
            if failed:
                match: Any = False
            elif unverifiable:
                match = None
            else:
                match = True
            rows.append({
                "spread_id": spread_id,
                "strategy": name,
                "match": match,
                "failed": failed,
                "unverifiable": unverifiable,
                "entry_time_assumed": assumed_entry,
            })
    return pd.DataFrame(
        rows,
        columns=["spread_id", "strategy", "match", "failed", "unverifiable", "entry_time_assumed"],
    )


def strategy_compliance_summary(matrix: pd.DataFrame, strategies: dict[str, Strategy]) -> pd.DataFrame:
    """Per-strategy tallies over the condition matrix."""
    rows = []
    for name in strategies:
        sub = matrix[matrix["strategy"] == name]
        failed_kinds: dict[str, int] = {}
        for failed in sub["failed"]:
            for kind in failed:
                failed_kinds[kind] = failed_kinds.get(kind, 0) + 1
        rows.append({
            "strategy": name,
            "spreads_considered": int(len(sub)),
            "spreads_matching": int((sub["match"] == True).sum()),        # noqa: E712
            "spreads_failing": int((sub["match"] == False).sum()),        # noqa: E712
            "spreads_unverifiable": int(sub["match"].isna().sum()),
            "failure_counts": failed_kinds,
        })
    return pd.DataFrame(rows)


def exit_audit(positions: pd.DataFrame, strategies: dict[str, Strategy]) -> pd.DataFrame:
    """Realized loss ratio per short position against each strategy's stop multiple."""
    rows = []
    for _, position in positions.iterrows():
        if position.get("direction") != "short":
            continue
        credit = position.get("credit")
        if not _finite(credit) or float(credit) <= 0:
            continue
        pnl = float(position.get("total_pnl") or 0.0)
        loss_ratio = (-pnl / float(credit)) if pnl < 0 else 0.0
        for name, strategy in strategies.items():
            stop = strategy.exit_rules.stop_loss
            multiple = float(stop.multiplier) if stop is not None else None
            rows.append({
                "date": position.get("date"),
                "open_ts": position.get("open_ts"),
                "strike": position.get("strike"),
                "strategy": name,
                "total_pnl": pnl,
                "credit": float(credit),
                "loss_ratio": loss_ratio,
                "stop_multiple": multiple,
                "stop_hit": bool(multiple is not None and loss_ratio >= multiple),
            })
    return pd.DataFrame(rows, columns=[
        "date", "open_ts", "strike", "strategy", "total_pnl", "credit",
        "loss_ratio", "stop_multiple", "stop_hit",
    ])


def strategy_config_fingerprint(path=None) -> dict:
    """Identify the config revision a result was computed from."""
    resolved = Path(path) if path is not None else None
    if resolved is None:
        from spx_trade_desk.resources import CONFIG_DIR
        resolved = CONFIG_DIR / "strategies.json"
    if not resolved.exists():
        return {"path": str(resolved), "mtime": None, "sha256": None}
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return {
        "path": str(resolved),
        "mtime": datetime.fromtimestamp(resolved.stat().st_mtime).isoformat(),
        "sha256": digest,
    }
