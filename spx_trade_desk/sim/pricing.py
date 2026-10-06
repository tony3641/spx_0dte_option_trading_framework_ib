"""Vectorized option pricing for the simulated market.

BSM family (mirrors gex_calculator's math, vectorized via scipy.special.ndtr) and the
repo's fill tick rules. The smile and half-spread live in pricing_model. Pure functions — no state.
"""
import numpy as np
from scipy.special import ndtr

from spx_trade_desk.sim.clock import bar_year_frac  # noqa: F401  (re-export; defined in clock)

RISK_FREE_RATE = 0.043   # mirrors config.DEFAULT_RISK_FREE_RATE fallback


def bsm_put(S, K, T, r, sigma):
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-4)
    T = np.asarray(T, dtype=float)
    if T.ndim == 0 and float(T) <= 0.0:
        return np.maximum(K - S, 0.0)
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return K * np.exp(-r * T) * ndtr(-d2) - S * ndtr(-d1)


def bsm_put_delta(S, K, T, r, sigma):
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-4)
    T = np.asarray(T, dtype=float)
    if T.ndim == 0 and float(T) <= 0.0:
        return np.where(K > S, -1.0, 0.0)
    sqrtT = np.sqrt(T)
    d1 = (np.log(np.asarray(S) / np.asarray(K)) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    return ndtr(d1) - 1.0


def build_ladder(s0: float, range_pct: float, step: float = 5.0) -> np.ndarray:
    lo = int(np.floor(s0 * (1 - range_pct) / step) * step)
    hi = int(np.ceil(s0 * (1 + range_pct) / step) * step)
    return np.arange(lo, hi + step / 2, step, dtype=float)


def tick_floor(x, tick: float = 0.05):
    a = np.asarray(x, dtype=float)
    out = np.floor((a + 1e-9) / tick) * tick
    # round off binary-FP drift (e.g. 3 * 0.05 -> 0.15000000000000002) so results sit
    # cleanly on the tick grid in reports.
    out = np.round(out, 6)
    return float(out) if out.ndim == 0 else out


def combo_fill_credit(mid_credit: float, cons_credit: float, tick: float) -> float:
    """Entry fill: never better than the natural, rounded down to the tick grid.

    Spec: fill = min(tick-floor(mid), conservative side), floored at one tick.
    The RESULT is itself on the tick grid too — the conservative side
    (bid_short - ask_long) is an arbitrary float, so it is floored as well.
    """
    raw = min(tick_floor(mid_credit, tick), cons_credit)
    return float(max(tick, tick_floor(raw, tick)))
