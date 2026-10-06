# tests/test_sim_pricing_model.py
"""Per-run z-model pricer (SP2 spec sections 5 and 7). Invented tables only."""
import math
import pickle
from types import SimpleNamespace

import numpy as np
import pytest

from spx_trade_desk.sim.calibrate import GarchParams
from spx_trade_desk.sim.clock import CAL_TO_SIM, t_cal, t_sim
from spx_trade_desk.sim.config import SimRunConfig
from spx_trade_desk.sim.pricing import RISK_FREE_RATE, bsm_put
from spx_trade_desk.sim.pricing_model import (L_MAX, L_MIN, build_pricing_model, link_tables,
                                              ratio_at, tau_row_weights)
from spx_trade_desk.sim.pricing_tables import Z_GRID
from tests.sim_chain_fixture import ATM_OPEN, VIX1D_PREV, g_true, hs_true, r_true, truth_tables

GARCH = GarchParams(omega=2e-8, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0)   # p_eff 0.95
P_EFF = 0.95


def _model(**kw):
    base = dict(pricing=truth_tables(), garch=GARCH, ushape=np.ones(390), sigma0=6e-4,
                vix1d_prev=VIX1D_PREV)
    base.update(kw)
    return SimpleNamespace(**base)


def _cfg(**kw):
    return SimRunConfig(strategy_name="T", bar_size="1m", **kw)


def _neutral_sigma():
    """Per-bar sigma at the GJR unconditional variance (ushape = 1): L == 1."""
    return math.sqrt(GARCH.omega / (1.0 - P_EFF))


def _bar(tau):
    return int(389 - tau)                 # 1-minute bars: tau after bar t is 389 - t


def test_tau_row_weights():
    lo, w = tau_row_weights(np.array([400.0, 345.0, 195.0, 150.0, 7.5, 0.0]))
    assert lo.tolist() == [0, 0, 1, 2, 5, 5]
    assert np.allclose(w, [0.0, 0.0, 0.5, 0.0, 1.0, 1.0])


def test_lookup_matches_truth_on_grid_nodes():
    pm = build_pricing_model(_model(), _cfg())
    for tau in (345.0, 150.0, 45.0):                         # bucket centres: the row is exact
        t = _bar(tau)
        assert pm.tau_min[t] == tau
        sw = pm.atm_base[t] * math.sqrt(pm.t_cal[t])
        z = Z_GRID[8:33]
        iv = pm.iv_sim(z * sw, t, _neutral_sigma()) / CAL_TO_SIM
        assert np.allclose(iv, pm.atm_base[t] * r_true(z, tau), rtol=1e-9)


def test_ratio_at_extrapolates_in_total_variance_with_the_lee_cap():
    row = np.where(Z_GRID < 0, 1.0 - 0.5 * Z_GRID, 1.0)      # steep put wing: the edge slope clamps at -Q_MAX
    q0, slope = row[0] ** 2, max((row[1] ** 2 - row[0] ** 2) / 0.25, -2.0)
    assert ratio_at(row, np.array([-50.0]), 1e-3)[0] == pytest.approx(math.sqrt(q0 + slope * (-44.0)))
    assert ratio_at(row, np.array([-50.0]), 2.0)[0] == pytest.approx(math.sqrt(50.0))   # 2|z|/sw
    assert ratio_at(row, np.array([0.5]), 1e6)[0] == pytest.approx(1.0, rel=1e-3)


def test_ratio_at_is_continuous_across_the_grid_edges():
    row = r_true(Z_GRID, 345.0)
    for edge in (Z_GRID[0], Z_GRID[-1]):
        inside, outside = (edge + d for d in (-1e-6, 1e-6)) if edge > 0 else (edge + 1e-6, edge - 1e-6)
        a, b = ratio_at(row, np.array([inside, outside]), 1e-3)
        assert abs(a - b) < 1e-4
        assert a == pytest.approx(row[0 if edge < 0 else -1], abs=1e-4)


def test_atm_level_anchor_order():
    pm = build_pricing_model(_model(), _cfg(atm_iv=0.10))
    assert pm.anchor_source == "atm_iv" and pm.atm_open == 0.10
    assert pm.atm_base[_bar(60.0)] == pytest.approx(0.10 * g_true(60.0))
    pm = build_pricing_model(_model(), _cfg())
    assert pm.anchor_source == "vix1d" and pm.atm_open == pytest.approx(ATM_OPEN)
    no_ratio = truth_tables()
    no_ratio.atm_vix1d_ratio = float("nan")
    for m in (_model(vix1d_prev=None), _model(pricing=no_ratio)):
        pm = build_pricing_model(m, _cfg())
        assert pm.anchor_source == "garch"
        assert pm.atm_open == pytest.approx(math.sqrt(6e-4 ** 2 * 390 / float(t_cal(390.0))))


def test_link_is_one_at_the_unconditional_state_and_rises_with_sigma():
    pm = build_pricing_model(_model(), _cfg())
    s0 = _neutral_sigma()
    assert float(pm.link(0, s0)) == pytest.approx(1.0, abs=1e-12)
    up = pm.link(0, np.array([1.5 * s0, 3.0 * s0, 100.0 * s0]))
    assert 1.0 < up[0] < up[1] < up[2] == L_MAX
    assert L_MIN <= float(pm.link(0, 0.01 * s0)) < 1.0
    assert float(pm.link(389, 3.0 * s0)) == 1.0               # nothing left of the day


def test_link_tables_match_direct_summation():
    v_bar, pos = link_tables(GARCH, np.ones(390), _cfg(), 390)
    assert v_bar == pytest.approx(GARCH.omega / (1.0 - P_EFF))
    k = np.arange(1, 390)
    assert pos[0] == pytest.approx((P_EFF ** k).sum() / 389.0, rel=1e-12)
    assert pos[-1] == 0.0
    v_bar, pos = link_tables(GarchParams(omega=2e-8, alpha=0.1, gamma=0.1, beta=0.9, nu=6.0),
                             np.ones(390), _cfg(), 390)
    assert math.isnan(v_bar) and not pos.any()               # non-stationary fit: no link


def test_skew_beta_tilts_the_wing_with_the_link():
    s_hi = 3.0 * _neutral_sigma()
    pm0 = build_pricing_model(_model(), _cfg())
    pm1 = build_pricing_model(_model(), _cfg(skew_beta=1.0))
    t = _bar(150.0)
    sw = pm0.atm_base[t] * float(pm0.link(t, s_hi)) * math.sqrt(pm0.t_cal[t])
    m = np.array([-2.0, 0.0]) * sw
    iv0, iv1 = pm0.iv_sim(m, t, s_hi), pm1.iv_sim(m, t, s_hi)
    assert iv1[0] > iv0[0] and iv1[1] == pytest.approx(iv0[1], rel=1e-12)
    s0 = _neutral_sigma()
    assert np.allclose(pm1.iv_sim(m, t, s0), pm0.iv_sim(m, t, s0), rtol=1e-12)


def test_deep_otm_at_the_open_is_finite_and_monotone():
    pm = build_pricing_model(_model(), _cfg())
    S, K = 6000.0, np.arange(3000.0, 6005.0, 5.0)
    for sigma in (_neutral_sigma(), 10.0 * _neutral_sigma()):
        iv = pm.iv_sim(np.log(K / S), 0, sigma)
        assert np.isfinite(iv).all() and (iv > 0).all() and (iv <= 5.0).all()
        put = bsm_put(S, K, float(t_sim(pm.tau_min[0])), pm.rate, iv)
        assert np.all(np.diff(put) >= -1e-9)


def test_final_bar_uses_floored_clock():
    pm = build_pricing_model(_model(), _cfg())
    last = len(pm.tau_min) - 1
    assert pm.tau_min[last] == 0.0 and pm.t_cal[last] == pytest.approx(float(t_cal(0.5)))
    K = np.array([5990.0, 6000.0, 6010.0])
    iv = pm.iv_sim(np.log(K / 6000.0), last, _neutral_sigma())
    assert np.isfinite(iv).all()
    assert bsm_put(6000.0, K, 0.0, pm.rate, iv).tolist() == [0.0, 0.0, 10.0]


def test_sim_clock_price_matches_the_calendar_price():
    pm = build_pricing_model(_model(), _cfg())
    S, K = 6000.0, np.array([5950.0, 5980.0, 6000.0])
    iv = pm.iv_sim(np.log(K / S), _bar(120.0), _neutral_sigma())
    a = bsm_put(S, K, float(t_sim(120.0)), pm.rate, iv)
    b = bsm_put(S, K, float(t_cal(120.0)), RISK_FREE_RATE, iv / CAL_TO_SIM)
    assert np.allclose(a, b, rtol=1e-10)


def test_half_spread_lookup_and_floor():
    pm = build_pricing_model(_model(), _cfg())
    t = _bar(150.0)
    mids = np.array([0.25, 4.0, 30.0])
    assert np.allclose(pm.half_spread(mids, t), hs_true(mids))
    thin = truth_tables()
    thin.hs[:] = 0.001
    pm = build_pricing_model(_model(pricing=thin), _cfg())
    assert pm.half_spread(np.array([1.0, 5.0]), t).tolist() == [0.025, 0.05]


def test_flat_iv_prices_every_strike_at_the_curve_level():
    pm = build_pricing_model(_model(), _cfg(flat_iv=True))
    t = _bar(60.0)
    iv = pm.iv_sim(np.array([-0.01, 0.0, 0.01]), t, 3.0 * _neutral_sigma())
    assert np.allclose(iv, pm.atm_base[t] * CAL_TO_SIM)


def test_pricing_model_pickles_bit_identically():
    pm = build_pricing_model(_model(), _cfg(skew_beta=0.5))
    back = pickle.loads(pickle.dumps(pm))
    m = np.linspace(-0.02, 0.01, 7)
    assert np.array_equal(back.iv_sim(m, 100, 2e-3), pm.iv_sim(m, 100, 2e-3))
