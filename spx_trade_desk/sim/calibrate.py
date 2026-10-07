"""GJR-GARCH(1,1) with Student-t innovations, fitted by MLE (scipy).

Preset fallback on non-convergence keeps runs alive; warnings surface in the UI
calibration panel. All functions are pure — no IO.
"""
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln, ndtr

PRESET = dict(alpha=0.04, gamma=0.10, beta=0.85, nu=6.0)


@dataclass
class GarchParams:
    omega: float
    alpha: float
    gamma: float        # GJR leverage term (asymmetric response to negative shocks)
    beta: float
    nu: float           # Student-t dof
    converged: bool = True


def gjr_variance_path(p: GarchParams, eps: np.ndarray) -> np.ndarray:
    """sigma^2_t for t = 0..n-1; warm-up variance backcast as EWMA of the first 50 |eps|."""
    n = len(eps)
    back = float(np.mean(eps[: min(50, n)] ** 2)) if n else 1e-12
    s2 = np.empty(n)
    prev = max(back, 1e-12)
    prev_e2 = back
    for t in range(n):
        shock_neg = 1.0 if (t > 0 and eps[t - 1] < 0) else 0.0
        s2[t] = p.omega + p.alpha * prev_e2 + p.gamma * prev_e2 * shock_neg + p.beta * prev
        prev = max(s2[t], 1e-16)
        prev_e2 = eps[t] ** 2
    return s2


def _neg_loglik(theta: np.ndarray, eps: np.ndarray) -> float:
    omega, alpha, gamma, beta, nu = theta
    if omega <= 0 or alpha < 0 or gamma < 0 or beta < 0 or nu <= 2.05:
        return 1e12
    if alpha + gamma / 2 + beta >= 0.999:
        return 1e12
    p = GarchParams(omega=omega, alpha=alpha, gamma=gamma, beta=beta, nu=nu)
    s2 = gjr_variance_path(p, eps)
    if not np.isfinite(s2).all() or (s2 <= 0).any():
        return 1e12
    c = math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2) - 0.5 * math.log(math.pi * (nu - 2))
    ll = c - 0.5 * np.log(s2) - ((nu + 1) / 2) * np.log1p(eps ** 2 / (s2 * (nu - 2)))
    val = -float(np.sum(ll))
    return val if math.isfinite(val) else 1e12


def fit_gjr_t(returns: np.ndarray, nu_init: float = 6.0) -> Tuple[GarchParams, List[str]]:
    """Fit GJR-GARCH(1,1)-t; falls back to a variance-targeted preset when MLE fails."""
    warnings: List[str] = []
    eps = np.asarray(returns, dtype=float)
    eps = eps[np.isfinite(eps)]
    var0 = float(np.var(eps)) if len(eps) > 10 else 1e-10
    var0 = max(var0, 1e-16)
    # Zero-variance input (flat/empty returns): the Student-t likelihood is
    # degenerate and unbounded below, so Nelder-Mead can "converge" to a
    # nonsense interior point (negll ~ -1e3) instead of failing, and the
    # best_val >= 1e11 fallback below never fires. Short-circuit straight to
    # the variance-targeted preset so flat series get a flagged, sane fit.
    if len(eps) == 0 or float(np.var(eps)) < 1e-16:
        omega = var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"])
        warnings.append("GARCH MLE did not converge — preset parameters in use (flagged in UI)")
        return GarchParams(omega=omega, alpha=PRESET["alpha"], gamma=PRESET["gamma"],
                           beta=PRESET["beta"], nu=PRESET["nu"], converged=False), warnings
    # variance targeting: omega = var * (1 - alpha - gamma/2 - beta)
    x0 = np.array([var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"]),
                   PRESET["alpha"], PRESET["gamma"], PRESET["beta"], nu_init])
    best, best_val = None, np.inf
    for nu_start in (nu_init, 4.0, 10.0):
        x = x0.copy()
        x[4] = nu_start
        try:
            res = minimize(_neg_loglik, x, args=(eps,), method="Nelder-Mead",
                           options=dict(maxiter=2000, xatol=1e-6, fatol=1e-6))
            res2 = minimize(_neg_loglik, res.x, args=(eps,), method="Nelder-Mead",
                            options=dict(maxiter=1000, xatol=1e-7, fatol=1e-7))
            cand = res2 if res2.fun < res.fun else res
        except Exception:
            continue
        if cand.fun < best_val and np.isfinite(cand.fun):
            best, best_val = cand.x, float(cand.fun)
    if best is None or best_val >= 1e11:
        omega = var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"])
        warnings.append("GARCH MLE did not converge — preset parameters in use (flagged in UI)")
        return GarchParams(omega=omega, alpha=PRESET["alpha"], gamma=PRESET["gamma"],
                           beta=PRESET["beta"], nu=PRESET["nu"], converged=False), warnings
    omega, alpha, gamma, beta, nu = (float(v) for v in best)
    return GarchParams(omega=max(omega, 1e-16), alpha=alpha, gamma=gamma, beta=beta,
                       nu=max(nu, 2.2), converged=True), warnings


# ---------- U-shape, VIX mapping, full pipeline ----------
from dataclasses import dataclass, field

from spx_trade_desk.resources import CONFIG_DIR
from spx_trade_desk.sim.config import BAR_SECONDS, SimRunConfig
from spx_trade_desk.sim.data import BarSeries
from spx_trade_desk.sim import library as sim_library
from spx_trade_desk.sim.pricing_tables import PricingTables

# The pre-SP2 captured smile. Nothing reads it any more; a leftover file is only warned about.
LEGACY_SMILE_PATH = CONFIG_DIR / "sim_smile.json"
RTH_START_MIN = 570


@dataclass
class CalibratedModel:
    garch: GarchParams
    ushape: np.ndarray            # (steps_per_day,), mean ~ 1
    sigma0: float                 # mean per-bar conditional vol (decimal return units)
    pricing: Optional[PricingTables]   # z-model tables chosen by tier (None: paths-only use)
    vix0: float
    source: str
    warnings: List[str] = field(default_factory=list)
    pricing_info: dict = field(default_factory=dict)   # tier, regime, fallbacks, staleness, scores
    vix1d_prev: Optional[float] = None                 # VIX1D prior close for the run's session

    def sigma_annual(self, cfg: SimRunConfig) -> float:
        return float(self.sigma0 * np.sqrt(cfg.steps_per_day() * 252))


def fit_ushape(returns: np.ndarray, minute_of_day: np.ndarray, steps_per_day: int) -> np.ndarray:
    """Multiplicative per-bar vol profile from mean |return| by minute-of-day, smoothed."""
    # RTH day is 390 minutes (09:30-16:00). Derive the per-bar width in whole
    # minutes from steps_per_day so minute-of-day aligns to bar buckets;
    # sub-minute bars collapse to minute resolution (minute_of_day loses seconds).
    bar_min = max(1, int(round(390 / steps_per_day)))
    idx = np.clip(((minute_of_day - RTH_START_MIN) // bar_min - 1).astype(int), 0, steps_per_day - 1)
    sums = np.zeros(steps_per_day)
    counts = np.zeros(steps_per_day)
    np.add.at(sums, idx, np.abs(returns))
    np.add.at(counts, idx, 1.0)
    mean_abs = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    # fill empty buckets from neighbours, then normalize and smooth (15-bar moving mean)
    n = len(mean_abs)
    fill = np.where(np.isfinite(mean_abs), mean_abs, np.nanmean(mean_abs))
    fill = np.where(np.isfinite(fill), fill, 1.0)
    kernel = np.ones(15) / 15.0
    smooth = np.convolve(fill, kernel, mode="same")
    out = smooth / max(float(np.mean(smooth)), 1e-12)
    return np.clip(out, 0.25, 4.0)


def _drop_overnight_returns(closes: np.ndarray, minute_of_day: np.ndarray):
    """Exclude cross-day diffs from an intraday return series.

    ``rets[i]`` spans bar ``i`` -> bar ``i+1``; a new RTH day is signalled by a
    ``minute_of_day`` decrease at the transition (intraday times ascend within a day,
    then drop back to the 09:30 open). The prior-16:00 -> next-09:3x close diff is an
    overnight gap that is NOT part of 0DTE intraday dynamics — folding it into the
    return series would inflate the opening U-shape bucket and skew sigma0 — so it is
    dropped before GJR fitting and U-shape bucketing.
    """
    closes = np.asarray(closes, dtype=float)
    mods = np.asarray(minute_of_day, dtype=float)
    rets = np.diff(np.log(closes))
    if len(closes) < 2 or len(mods) != len(closes):
        # Degenerate/unexpected bar metadata: keep the historical un-filtered series.
        return rets, mods[1:]
    same_day = mods[1:] > mods[:-1]          # True where the closing bar is later in the day
    return rets[same_day], mods[1:][same_day]


def calibrate(bars: BarSeries, cfg: SimRunConfig) -> CalibratedModel:
    """Full calibration: returns -> GJR-t MLE -> U-shape -> pricing tables by tier -> VIX mapping."""
    warnings: List[str] = list(bars.warnings)
    rets, mods = _drop_overnight_returns(bars.closes, bars.minute_of_day)
    garch, w = fit_gjr_t(rets)
    warnings += w
    ushape = fit_ushape(rets, mods, cfg.steps_per_day())
    sigma0 = float(np.mean(np.sqrt(gjr_variance_path(garch, rets))))
    tables, pricing_info, vix1d_prev, pricing_warnings = sim_library.resolve_pricing(cfg.pricing_tier)
    warnings += pricing_warnings
    if LEGACY_SMILE_PATH.exists():
        warnings.append(f"pricing: {LEGACY_SMILE_PATH.name} is no longer used (the z-model "
                        f"replaced the SVI smile); delete it")
    vix0 = 20.0
    if bars.vix_closes is not None and len(bars.vix_closes):
        vix0 = float(np.mean(bars.vix_closes[-20:]))
    else:
        warnings.append("VIX series unavailable — mapping anchored at VIX0=20")
    return CalibratedModel(garch=garch, ushape=ushape, sigma0=sigma0, pricing=tables,
                           vix0=vix0, source=bars.source, warnings=warnings,
                           pricing_info=pricing_info, vix1d_prev=vix1d_prev)
