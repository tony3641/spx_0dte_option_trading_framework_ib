"""Per-run option pricer for the simulator's z-model (SP2 spec sections 5 and 7).

    IV(K, t) = ATM(t) * f(z, tau),   ATM(t) = ATM_open * g(tau) * L(t),
    z = ln(K / S) / (ATM(t) * sqrt(T_cal)).

f is bilinear in (z, tau) inside the grid and linear in total variance outside it, with
the wing capped at Lee's moment bound. L links the level to the path's GJR state (the
former atm_budget tables). BSM runs on the sim clock with sigma_sim = IV * CAL_TO_SIM and
the rate scaled the same way, which reproduces the calendar-clock price exactly.
One PricingModel per job: it is read-only and picklable, so serial and parallel runs stay
bit-identical.
"""
import math
from dataclasses import dataclass
from typing import Tuple

import numpy as np

from spx_trade_desk.sim.clock import CAL_TO_SIM, RTH_MINUTES, bar_tau_minutes, sim_rate, t_cal
from spx_trade_desk.sim.config import BAR_SECONDS
from spx_trade_desk.sim.pricing import RISK_FREE_RATE
from spx_trade_desk.sim.pricing_tables import (G_TAU, N_TAU, Q_MAX, RATIO_FLOOR, TAU_CENTERS,
                                               Z_GRID, Z_STEP, mid_bucket, tau_bucket)

L_MIN, L_MAX = 0.5, 3.0
SKEW_TILT_MAX = 1.25         # skew_beta never steepens the smile by more than this factor: beyond
                             # it the wing puts stop being ordered in strike (arbitrageable)
LEE_MIN_ABS_Z = 1.0          # the Lee cap only applies in the wings, never near the money
SIGMA_MIN, SIGMA_MAX = 1e-4, 5.0
_CENTERS_ASC = np.asarray(TAU_CENTERS[::-1])


def tau_row_weights(tau) -> Tuple[np.ndarray, np.ndarray]:
    """Rows (lo, lo + 1) and weight w on row lo + 1 for linear interpolation in tau
    between bucket centres (flat beyond the first and last centre)."""
    x = (N_TAU - 1) - np.interp(np.asarray(tau, dtype=float), _CENTERS_ASC,
                                np.arange(N_TAU, dtype=float))
    lo = np.minimum(np.floor(x).astype(int), N_TAU - 2)
    return lo, x - lo


def ratio_at(row, z, sw, tilt=None):
    """IV / ATM at z from one tau-interpolated f row.

    Inside the grid: linear in z. Outside: linear in r^2 with the edge slope (left clamped
    to [-Q_MAX, 0], right to [-Q_MAX, Q_MAX]). ``tilt`` (None: off) scales (r - 1) before the
    cap. Where |z| >= LEE_MIN_ABS_Z, r^2 <= 2|z| / sw (Lee: total variance <= 2|log-moneyness|;
    sw = ATM * sqrt(T_cal)). Floored at RATIO_FLOOR.
    """
    z = np.asarray(z, dtype=float)
    r = np.interp(z, Z_GRID, row)
    q = r * r
    q0, qn = row[0] ** 2, row[-1] ** 2
    left = min(max((row[1] ** 2 - q0) / Z_STEP, -Q_MAX), 0.0)
    right = min(max((qn - row[-2] ** 2) / Z_STEP, -Q_MAX), Q_MAX)
    q = np.where(z < Z_GRID[0], q0 + left * (z - Z_GRID[0]), q)
    q = np.where(z > Z_GRID[-1], qn + right * (z - Z_GRID[-1]), q)
    if tilt is not None:
        r = np.maximum(1.0 + (np.sqrt(np.maximum(q, RATIO_FLOOR ** 2)) - 1.0) * tilt, RATIO_FLOOR)
        q = r * r
    az = np.abs(z)
    q = np.where(az >= LEE_MIN_ABS_Z, np.minimum(q, 2.0 * az / sw), q)
    return np.sqrt(np.maximum(q, RATIO_FLOOR ** 2))


def g_at(g, tau):
    return np.interp(np.asarray(tau, dtype=float), np.asarray(G_TAU), g)


def half_spread_at(hs_row, mid):
    """Table half-spread for a put mid, floored at half a tick (0.05 below $3, 0.10 above)."""
    mid = np.asarray(mid, dtype=float)
    return np.maximum(hs_row[mid_bucket(mid)], np.where(mid < 3.0, 0.025, 0.05))


def link_tables(garch, ushape, cfg, steps: int):
    """(v_bar, P/S per bar) for L: the GJR unconditional per-bar variance and the share of
    the remaining day's U-shape-weighted variance that still remembers today's state.
    p_eff must mirror paths.py (gamma_mult scales the GJR term). A non-stationary fit
    (p_eff outside (0, 1)) returns (nan, zeros): L is then 1."""
    p_eff = garch.alpha + garch.gamma * cfg.gamma_mult / 2.0 + garch.beta
    if not (0.0 < p_eff < 1.0):
        return float("nan"), np.zeros(steps)
    u2 = np.asarray(ushape, dtype=float)[:steps] ** 2
    S = np.zeros(steps)
    P = np.zeros(steps)
    for t in range(steps - 2, -1, -1):
        S[t] = S[t + 1] + u2[t + 1]
        P[t] = p_eff * u2[t + 1] + p_eff * P[t + 1]
    pos = S > 0
    return garch.omega / (1.0 - p_eff), np.where(pos, P / np.where(pos, S, 1.0), 0.0)


@dataclass(frozen=True, eq=False)
class PricingModel:
    tau_min: np.ndarray       # (steps,) minutes to the close after bar t
    t_cal: np.ndarray         # (steps,) calendar years, floored at half a bar (z never divides by 0)
    atm_base: np.ndarray      # (steps,) ATM_open * g(tau), calendar units
    f_rows: np.ndarray        # (steps, 37) f interpolated to each bar's tau
    hs_rows: np.ndarray       # (steps, 8) the spread row of each bar's tau bucket
    ushape: np.ndarray        # (steps,)
    v_bar: float
    p_over_s: np.ndarray      # (steps,)
    budget_beta: float
    skew_beta: float
    flat_iv: bool
    atm_open: float
    anchor_source: str
    rate: float               # BSM rate on the sim clock (discounts like RISK_FREE_RATE on calendar time)

    def link(self, t: int, sigma):
        """L(t) from the path's per-bar sigma: sqrt(conditional / unconditional remaining
        variance), clipped to [L_MIN, L_MAX]."""
        sigma = np.asarray(sigma, dtype=float)
        if not (math.isfinite(self.v_bar) and self.v_bar > 0.0):
            return np.ones_like(sigma)
        state = (sigma / self.ushape[t]) ** 2
        sig2 = np.maximum(self.v_bar + self.budget_beta * (state - self.v_bar), 0.0)
        ratio = np.maximum(1.0 + self.p_over_s[t] * (sig2 / self.v_bar - 1.0), 0.0)
        return np.clip(np.sqrt(ratio), L_MIN, L_MAX)

    def iv_sim(self, m, t: int, sigma):
        """Sim-clock BSM vol for log-moneyness m at bar t (broadcasts m against sigma)."""
        m = np.asarray(m, dtype=float)
        if self.flat_iv:
            shape = np.broadcast_shapes(m.shape, np.shape(sigma))
            return np.full(shape, self.atm_base[t] * CAL_TO_SIM)
        L = self.link(t, sigma)
        atm = self.atm_base[t] * L
        sw = atm * math.sqrt(self.t_cal[t])
        tilt = (np.clip(1.0 + self.skew_beta * (L - 1.0), 0.0, SKEW_TILT_MAX)
                if self.skew_beta else None)
        ratio = ratio_at(self.f_rows[t], m / sw, sw, tilt)
        return np.clip(atm * ratio * CAL_TO_SIM, SIGMA_MIN, SIGMA_MAX)

    def half_spread(self, mid, t: int):
        return half_spread_at(self.hs_rows[t], mid)


def resolve_anchor(model, cfg) -> Tuple[float, str]:
    """ATM_open: cfg.atm_iv -> VIX1D prior close x library ratio -> GARCH sigma0."""
    if cfg.atm_iv is not None:
        return float(cfg.atm_iv), "atm_iv"
    ratio = model.pricing.atm_vix1d_ratio
    if model.vix1d_prev and math.isfinite(ratio):
        return float(model.vix1d_prev) / 100.0 * float(ratio), "vix1d"
    # sigma0 is a mean per-bar return std: sigma0^2 * steps is the day's variance, spread
    # over one RTH day of calendar time.
    day_var = float(model.sigma0) ** 2 * cfg.steps_per_day()
    return math.sqrt(day_var / float(t_cal(RTH_MINUTES))), "garch"


def build_pricing_model(model, cfg) -> PricingModel:
    tables = model.pricing
    steps = cfg.steps_per_day()
    bar_seconds = BAR_SECONDS[cfg.bar_size]
    tau = bar_tau_minutes(steps, bar_seconds)
    atm_open, source = resolve_anchor(model, cfg)
    lo, w = tau_row_weights(tau)
    f_rows = tables.f[lo] * (1.0 - w)[:, None] + tables.f[lo + 1] * w[:, None]
    v_bar, p_over_s = link_tables(model.garch, model.ushape, cfg, steps)
    return PricingModel(
        tau_min=tau, t_cal=t_cal(np.maximum(tau, 0.5 * bar_seconds / 60.0)),
        atm_base=atm_open * g_at(tables.g, tau), f_rows=f_rows,
        hs_rows=tables.hs[tau_bucket(tau)], ushape=np.asarray(model.ushape, dtype=float)[:steps],
        v_bar=v_bar, p_over_s=p_over_s, budget_beta=float(cfg.budget_beta),
        skew_beta=float(cfg.skew_beta), flat_iv=bool(cfg.flat_iv), atm_open=atm_open,
        anchor_source=source, rate=sim_rate(RISK_FREE_RATE))
