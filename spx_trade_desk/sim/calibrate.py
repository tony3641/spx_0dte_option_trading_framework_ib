"""GJR-GARCH(1,1) with Student-t innovations, fitted by MLE (scipy).

Preset fallback on non-convergence keeps runs alive; warnings surface in the UI
calibration panel. All functions are pure — no IO.
"""
import math
import warnings
from dataclasses import dataclass
from typing import List, Tuple

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


# ---------- smile, U-shape, VIX mapping, full pipeline ----------
import json
import os
from dataclasses import dataclass, field

from spx_trade_desk.resources import CONFIG_DIR
from spx_trade_desk.sim.config import BAR_SECONDS, SimRunConfig
from spx_trade_desk.sim.data import BarSeries

SMILE_CAPTURE_PATH = str(CONFIG_DIR / "sim_smile.json")
SMILE_DEFAULT_PATH = str(CONFIG_DIR / "sim_smile_default.json")
RTH_START_MIN = 570


@dataclass
class SmileParams:
    a: float               # SVI level offset
    b: float               # SVI wing steepness (>= 0)
    rho: float             # SVI skew (negative => put skew)
    m0: float              # SVI vertex moneyness (log)
    sigma: float           # SVI curvature width
    half_spread_atm: float = 0.05

    def iv(self, m):
        m = np.asarray(m, dtype=float)
        x = m - self.m0
        return self.a + self.b * (self.rho * x + np.sqrt(x * x + self.sigma * self.sigma))

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "rho": self.rho, "m0": self.m0,
                "sigma": self.sigma, "half_spread_atm": self.half_spread_atm}

    @classmethod
    def from_dict(cls, d: dict) -> "SmileParams":
        if "rho" not in d or "m0" not in d or "sigma" not in d:
            raise ValueError(
                f"legacy quadratic smile snapshot keys {sorted(d)}; re-capture or delete the file")
        return cls(a=float(d["a"]), b=float(d["b"]), rho=float(d["rho"]),
                   m0=float(d["m0"]), sigma=float(d["sigma"]),
                   half_spread_atm=float(d.get("half_spread_atm", 0.05)))


DEFAULT_SMILE = SmileParams(a=0.04, b=1.8, rho=-0.75, m0=0.03, sigma=0.06,
                            half_spread_atm=0.05)

# SVI fit guards. The sim prices puts out to +/-ladder_range_pct (0.15 default),
# while live chain put-IV data only reaches ~+/-5-6%, so the smile is extrapolated.
# These caps keep that extrapolation sane (no >100% IV / inverted/arbitrage-invalid puts).
SVI_EDGE = 0.15            # == default SimRunConfig.ladder_range_pct
SVI_WIDE = 0.30            # wider floor/negativity horizon
SVI_WING_CAP = 1.0         # max IV (decimal) allowed across [-SVI_EDGE, SVI_EDGE]
SVI_FLOOR = 0.005          # min IV allowed anywhere
_SVI_BOUNDS = [(0.005, 3.0), (1e-4, 6.0), (-0.999, 0.999), (-0.15, 0.10), (1e-4, 0.5)]
_SVI_TOL = 1e-9            # float slack so a boundary solution is not rejected
SVI_MIN_PUT_SKEW = 0.01    # a fit flatter than 1 vol point ATM->-15% carries no skew info

# Capture input hygiene. A live 0DTE chain carries rows whose IV is not information:
# far-OTM puts with no bid (ask pinned on the 0.05 tick), strikes whose mid is stuck on
# the tick floor (a constant price across ~14 strikes -> an IV "hump" no convex SVI can
# match), and deep-ITM puts whose mid is almost pure intrinsic. Feeding those to the fit
# drives the seed wing slopes to the b bound and makes the capture fail its own guards.
CAPTURE_M_LO = -SVI_EDGE   # same band as the sim ladder
CAPTURE_M_HI = 0.02        # small ITM anchor around ATM
CAPTURE_MIN_BID = 0.10     # 2x the 0.05 SPX tick: below this the quote is tick-pinned
CAPTURE_MIN_ABS_DELTA = 0.005   # below this the option has no vega left
CAPTURE_MAX_ABS_DELTA = 0.75    # above this the mid is ~intrinsic and IV is noise


def smile_capture_points(strikes, spot) -> Tuple[np.ndarray, np.ndarray]:
    """(m, iv) capture points from live chain rows, dropping vega-less quotes.

    Rows that omit a field keep passing (wire payloads and tests may not carry it); a
    row that supplies one must clear it — the same "absent key passes" convention the
    capture endpoint already used for open interest.
    """
    pts_m, pts_iv = [], []
    for row in strikes:
        iv = row.get("put_iv")
        k = row.get("strike")
        if not iv or not spot or not k:
            continue
        oi = row.get("put_oi")
        if oi is not None and oi <= 0:
            continue
        bid = row.get("put_bid")
        if bid is not None and bid < CAPTURE_MIN_BID:
            continue
        delta = row.get("put_delta")
        if delta is not None and not (CAPTURE_MIN_ABS_DELTA <= abs(delta) <= CAPTURE_MAX_ABS_DELTA):
            continue
        m = math.log(float(k) / spot)
        if CAPTURE_M_LO <= m <= CAPTURE_M_HI:
            pts_m.append(m)
            pts_iv.append(float(iv) / 100.0)
    return np.array(pts_m), np.array(pts_iv)


@dataclass
class CalibratedModel:
    garch: GarchParams
    ushape: np.ndarray            # (steps_per_day,), mean ~ 1
    sigma0: float                 # mean per-bar conditional vol (decimal return units)
    smile: SmileParams
    vix0: float
    source: str
    warnings: List[str] = field(default_factory=list)

    def sigma_annual(self, cfg: SimRunConfig) -> float:
        return float(self.sigma0 * np.sqrt(cfg.steps_per_day() * 252))


@dataclass(frozen=True)
class SmileDynamics:
    """Run-level smile response dials + precomputed tables (smile-dynamics spec §4).

    Neutral values (skew_beta=0, skew_t_gamma=0, atm_budget=False) reproduce the
    legacy IV formula clip(smile.iv(m) + vol_beta*(sigma - sigma0), 0.01, 5.0)
    bit-for-bit — tests/test_sim_regression.py enforces the chain.
    """
    sigma0: float
    vol_beta: float
    flat_iv: bool
    iv0: float
    skew_beta: float = 0.0
    t_scale: object = None      # (steps,) T_ref/max(T_t,T_floor); ones = neutral
    skew_t_gamma: float = 0.0
    atm_budget: bool = False
    budget_beta: float = 1.0
    v_bar: float = 0.0
    a_tab: object = None        # (steps,) A(t) = v_bar*(S(t)-P(t))
    b_tab: object = None        # (steps,) B(t) = P(t)
    v0: float = 0.0


def build_dynamics(model: CalibratedModel, cfg: SimRunConfig) -> SmileDynamics:
    """Per-run smile dynamics from cfg dials. O(steps) precompute.

    Never cache this with the model: sim_jobs._CALIB_CACHE keys on the data source
    only, while these dials vary per run.
    """
    steps = cfg.steps_per_day()
    # Same bar fraction as sim_pricing.bar_year_frac (avoided here to keep
    # sim_calibrate free of a sim_pricing import): 252 RTH days x 6.5 h.
    barf = BAR_SECONDS[cfg.bar_size] / (252 * 6.5 * 3600.0)
    t_scale = np.ones(steps)
    if cfg.skew_t_gamma > 0.0 or cfg.atm_budget:
        t_left = np.arange(steps - 1, -1, -1) * barf     # years to expiry after bar t
        t_scale = t_left[0] / np.maximum(t_left, 0.5 * barf)
    v_bar = a_tab = b_tab = None
    v0 = 0.0
    if cfg.atm_budget:
        g = model.garch
        # p_eff must mirror sim_paths.py:21 (gamma_mult scales the GJR term); the
        # 1/2 comes from E[neg * eps^2] = E[eps^2]/2 by symmetry of the shocks.
        p_eff = g.alpha + g.gamma * cfg.gamma_mult / 2.0 + g.beta
        v_bar_val = g.omega / (1.0 - p_eff)
        u2 = np.asarray(model.ushape, dtype=float)[:steps] ** 2 * barf
        # S(t) = sum_{k>t} u2[k];  P(t) = sum_{k>t} p_eff^(k-t) u2[k]
        S = np.zeros(steps)
        P = np.zeros(steps)
        for t in range(steps - 2, -1, -1):
            S[t] = S[t + 1] + u2[t + 1]
            P[t] = p_eff * u2[t + 1] + p_eff * P[t + 1]
        v_bar = v_bar_val
        a_tab = v_bar_val * (S - P)      # A(t) = v_bar*(S(t)-P(t))
        b_tab = P                        # B(t) = P(t)
        v0 = v_bar_val * float(S[0])     # V(0, initial state sigma^2 = v_bar)
    return SmileDynamics(
        sigma0=model.sigma0, vol_beta=cfg.vol_beta, flat_iv=cfg.flat_iv,
        iv0=float(model.smile.iv(0.0)), skew_beta=cfg.skew_beta,
        t_scale=t_scale, skew_t_gamma=cfg.skew_t_gamma,
        atm_budget=cfg.atm_budget, budget_beta=cfg.budget_beta,
        v_bar=v_bar if v_bar is not None else 0.0,
        a_tab=a_tab, b_tab=b_tab, v0=v0)


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


def _svi_objective(theta, m, iv) -> float:
    a, b, rho, m0, sigma = theta
    # L-BFGS-B enforces the bounds; the hard penalties guard against a seed that
    # straddles a bound and any non-finite evaluation.
    if b <= 0 or abs(rho) >= 1 or sigma <= 0:
        return 1e6
    x = m - m0
    v = a + b * (rho * x + np.sqrt(x * x + sigma * sigma))
    if not np.isfinite(v).all():
        return 1e6
    return float(np.sum((v - iv) ** 2))


def _svi_seeds(m, iv):
    """Deterministic multi-initializations for the bounded SVI fit (cheap: few points)."""
    coef, *_ = np.linalg.lstsq(np.vstack([np.ones_like(m), m, m * m]).T, iv, rcond=None)
    c0, c1, c2 = (float(coef[k]) if np.isfinite(coef[k]) else 0.0 for k in range(3))
    if c2 > 1e-6:
        m0_q = float(np.clip(-c1 / (2 * c2), -0.10, 0.05))
    else:
        put_rich = float(np.mean(iv[m <= 0])) if (m <= 0).any() else 0.0
        call_rich = float(np.mean(iv[m > 0])) if (m > 0).any() else 0.0
        m0_q = -0.02 if put_rich > call_rich else 0.02
    sigma_q = 0.05
    iv_atm = float(iv[np.argmin(np.abs(m))])

    def _wing_slope(mask):
        if mask.sum() < 2:
            return None
        mm, vv = m[mask], iv[mask]
        denom = float(((mm - mm.mean()) ** 2).sum())
        if denom < 1e-12:
            return None
        return float(((mm - mm.mean()) * (vv - vv.mean())).sum() / denom)

    sL, sR = _wing_slope(m <= 0), _wing_slope(m >= 0)
    if sL is not None and sR is not None:
        span = max(sR - sL, 1e-9)
        b0 = float(np.clip(max((sR - sL) / 2.0, 1e-4), 1e-4, 6.0))
        rho0 = float(np.clip((sR + sL) / span, -0.99, 0.99))
    else:
        b0, rho0 = 1.0, -0.5
    a0 = float(np.clip(iv_atm - b0 * (rho0 * (-m0_q) + np.sqrt(m0_q * m0_q + sigma_q * sigma_q)),
                       0.01, 3.0))
    base = (a0, b0, rho0, m0_q, sigma_q)
    return [base] + [(base[0], base[1], base[2], base[3], s) for s in (0.04, 0.15)] \
                  + [(base[0], base[1], r, base[3], base[4]) for r in (-0.85, -0.4)] \
                  + [(base[0], base[1], base[2], m0, base[4]) for m0 in (-0.02, 0.03)]


def _svi_guards(smile: SmileParams, m, iv) -> List[str]:
    """Reject fits that are degenerate, non-monotone, or explode at the ladder edge.

    Returns [] on pass, else a human-readable warning (the capture endpoint surfaces
    the first one). Keeps the extrapolation used by the sim (out to +/-
    ladder_range_pct) sane — the reason the old quadratic blew up to >100% IV.
    """
    core = np.linspace(-SVI_EDGE, SVI_EDGE, 301)
    g = smile.iv(core)
    if (not np.isfinite(g).all() or g.max() > SVI_WING_CAP + _SVI_TOL
            or g.min() < SVI_FLOOR - _SVI_TOL):
        return ["smile: fit IV exceeds bounds at +-%g — using fallback snapshot" % SVI_EDGE]
    wide = smile.iv(np.linspace(-SVI_WIDE, SVI_WIDE, 401))
    if not np.isfinite(wide).all() or wide.min() < SVI_FLOOR - _SVI_TOL:
        return ["smile: fit IV negative/non-finite outside +-%g — using fallback snapshot" % SVI_EDGE]
    pw = smile.iv(np.linspace(-SVI_EDGE, 0.0, 201))
    if np.any(np.diff(pw) > 1e-6):
        return ["smile: fit non-monotone put wing — using fallback snapshot"]
    if smile.iv(-SVI_EDGE) - smile.iv(0.0) < SVI_MIN_PUT_SKEW:
        return ["smile: fit flat (no put skew) — using fallback snapshot"]
    pred = smile.iv(m)
    rmse = float(np.sqrt(np.mean((pred - iv) ** 2)))
    # Compare against a robust spread, not an RMSE baseline: one wild quote inflates
    # any mean/median-squared baseline enough to admit a fit that ignores the rest of
    # the chain (the stray-outlier case this guard exists to catch).
    if rmse > _robust_scale(iv) + 0.005:
        return ["smile: SVI fit does not track the chain — using fallback snapshot"]
    return []


def _robust_scale(iv) -> float:
    """1.4826 * MAD — a spread estimate a single wild quote cannot inflate."""
    med = float(np.median(iv))
    return 1.4826 * float(np.median(np.abs(np.asarray(iv) - med)))


def _svi_constraints() -> list:
    """The guard envelope as scalar inequalities, so the solver honours it directly.

    IV(m) = a + b*(rho*x + sqrt(x^2+sigma^2)) is convex in m, so its maximum over the
    core band sits at an endpoint and its minimum over the wide band at the vertex or an
    endpoint; the put-wing monotonicity is one slope sign at m = 0. That turns the
    guards' 800-point scan into four cheap inequalities.
    """
    def iv_at(theta, xs):
        a, b, rho, m0, sigma = theta
        x = np.asarray(xs) - m0
        return a + b * (rho * x + np.sqrt(x * x + sigma * sigma))

    def vertex(theta):
        return theta[3] + theta[4] * (-theta[2]) / np.sqrt(1.0 - theta[2] ** 2)

    return [
        {"type": "ineq", "fun": lambda t: SVI_WING_CAP - iv_at(t, -SVI_EDGE)},
        {"type": "ineq", "fun": lambda t: SVI_WING_CAP - iv_at(t, SVI_EDGE)},
        # convex in m, so the band minimum is at the vertex clamped into the band
        {"type": "ineq", "fun": lambda t: iv_at(t, np.clip(vertex(t), -SVI_WIDE, SVI_WIDE))
         - SVI_FLOOR},
        # IV'(0) <= 0 keeps the put wing monotone (IV is convex, so the largest slope
        # over [-EDGE, 0] is at m = 0)
        {"type": "ineq", "fun": lambda t: -(t[2] - t[3] / np.sqrt(t[3] ** 2 + t[4] ** 2))},
    ]


def fit_smile(m_points, iv_points, fallback: SmileParams) -> Tuple[SmileParams, List[str]]:
    """Bounded SVI fit of IV(m); falls back when too few points or degenerate fit."""
    m = np.asarray(m_points, dtype=float)
    iv = np.asarray(iv_points, dtype=float)
    ok = np.isfinite(m) & np.isfinite(iv) & (iv > 0.005) & (iv < 5.0)
    m, iv = m[ok], iv[ok]
    if len(m) < 5:
        return fallback, ["smile: too few IV points — using fallback snapshot"]
    in_win = np.abs(m) <= SVI_EDGE + 1e-9
    m, iv = m[in_win], iv[in_win]
    if len(m) < 5:
        return fallback, ["smile: too few IV points inside +-%g — using fallback snapshot" % SVI_EDGE]
    best_ok, best_val = None, float("inf")
    constraints = _svi_constraints()
    for x0 in _svi_seeds(m, iv):
        # SLSQP, not L-BFGS-B: on a steep 0DTE skew the unconstrained optimum sits on
        # the b bound and its wings blow past SVI_WING_CAP, so every seed used to be
        # thrown away even though a bounded fit exists. Constrain the search instead.
        with warnings.catch_warnings():
            # scipy's SLSQP line search clips iterates to the box and carries on; the
            # returned solution is re-checked against the bounds, so the noise is benign.
            warnings.filterwarnings(
                "ignore", message="Values in x were outside bounds during a minimize step")
            res = minimize(_svi_objective, x0, args=(m, iv), method="SLSQP",
                           bounds=_SVI_BOUNDS, constraints=constraints,
                           options=dict(maxiter=400, ftol=1e-12))
        if not res.success or not np.isfinite(res.fun):
            continue
        cand = SmileParams(*(float(v) for v in res.x),
                           half_spread_atm=fallback.half_spread_atm)
        if not _svi_guards(cand, m, iv):                 # keep the best ACCEPTABLE fit
            if float(res.fun) < best_val:
                best_ok, best_val = cand, float(res.fun)
    if best_ok is None:
        return fallback, ["smile: SVI fit failed guards — using fallback snapshot"]
    return best_ok, []


fit_svi = fit_smile   # discoverable alias


def load_smile_snapshot() -> Tuple[SmileParams, str]:
    """captured (config/sim_smile.json) -> default file -> built-in constants."""
    for path, src in ((SMILE_CAPTURE_PATH, "captured"), (SMILE_DEFAULT_PATH, "default")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return SmileParams.from_dict(json.load(f)), src
        except Exception:
            continue
    return DEFAULT_SMILE, "builtin"


def save_smile_snapshot(smile: SmileParams) -> None:
    os.makedirs(os.path.dirname(SMILE_CAPTURE_PATH), exist_ok=True)
    with open(SMILE_CAPTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(smile.to_dict(), f, indent=2)


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
    """Full calibration: returns -> GJR-t MLE -> U-shape -> smile snapshot -> VIX mapping."""
    warnings: List[str] = list(bars.warnings)
    rets, mods = _drop_overnight_returns(bars.closes, bars.minute_of_day)
    garch, w = fit_gjr_t(rets)
    warnings += w
    ushape = fit_ushape(rets, mods, cfg.steps_per_day())
    sigma0 = float(np.mean(np.sqrt(gjr_variance_path(garch, rets))))
    smile, smile_src = load_smile_snapshot()
    if smile_src != "captured":
        warnings.append(f"smile: {smile_src} snapshot (capture a live chain for best results)")
    vix0 = 20.0
    if bars.vix_closes is not None and len(bars.vix_closes):
        vix0 = float(np.mean(bars.vix_closes[-20:]))
    else:
        warnings.append("VIX series unavailable — mapping anchored at VIX0=20")
    return CalibratedModel(garch=garch, ushape=ushape, sigma0=sigma0, smile=smile,
                           vix0=vix0, source=bars.source, warnings=warnings)
