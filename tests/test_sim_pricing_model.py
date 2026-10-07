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


# ---- tier selection (spec section 6.2) ---------------------------------------------
import json
from datetime import date, datetime

from spx_trade_desk.market.hours import ET
from spx_trade_desk.sim.pricing_tables import (MIN_BUCKET_SWEEPS, PricingTables, read_model_file,
                                               select_tables)

TODAY = date(2030, 3, 5)


def _src(scale, n_days=20, sweeps=100):
    t = truth_tables(n_days=n_days, sweeps=sweeps)
    t.f = t.f * scale
    return t


COLD = _src(0.9, n_days=1)


def _model_dict(days=12, regime_days=6, last="20300304"):
    return {"v": 1, "days": days, "last_capture": last,
            "pooled": _src(1.0, n_days=days).to_dict(),
            "regimes": {"12-18": _src(1.1, n_days=regime_days).to_dict()},
            "regime_days": {"12-18": regime_days},
            "scores": {"library": {"passed": 6, "scored": 7}}}


def test_auto_picks_the_regime_tables():
    t, info, w = select_tables(_model_dict(), COLD, "auto", 13.0, TODAY)
    assert info["tier"] == "library" and info["regime"] == "12-18" and info["fallback_buckets"] == []
    assert np.allclose(t.f, _src(1.1).f) and info["days"] == 12 and not info["stale"]
    assert any(x.startswith("pricing: tier library") and "6/7" in x for x in w)


@pytest.mark.parametrize("vix1d, kw", [(None, {}), (13.0, {"regime_days": 4}),
                                       (13.0, {"days": 9}), (30.0, {})])
def test_auto_falls_back_to_thin(vix1d, kw):
    t, info, _ = select_tables(_model_dict(**kw), COLD, "auto", vix1d, TODAY)
    assert info["tier"] == "thin" and np.allclose(t.f, _src(1.0).f)


def test_no_model_is_cold():
    t, info, w = select_tables(None, COLD, "auto", 13.0, TODAY)
    assert info["tier"] == "cold" and info["days"] == 0 and np.allclose(t.f, COLD.f)
    assert any("last capture none" in x for x in w)


def test_per_bucket_fallback_goes_one_level_at_a_time():
    m = _model_dict()
    reg = PricingTables.from_dict(m["regimes"]["12-18"])
    reg.f_sweeps[6] = MIN_BUCKET_SWEEPS - 1
    m["regimes"]["12-18"] = reg.to_dict()
    t, info, _ = select_tables(m, COLD, "auto", 13.0, TODAY)
    assert info["fallback_buckets"] == ["<15->thin"]
    assert np.allclose(t.f[6], _src(1.0).f[6]) and np.allclose(t.f[5], _src(1.1).f[5])
    pooled = PricingTables.from_dict(m["pooled"])
    pooled.f_sweeps[6] = 0
    m["pooled"] = pooled.to_dict()
    t, info, _ = select_tables(m, COLD, "auto", 13.0, TODAY)
    assert info["fallback_buckets"] == ["<15->cold"] and np.allclose(t.f[6], COLD.f[6])


def test_level_comes_from_the_first_source_with_a_curve():
    m = _model_dict()
    reg = PricingTables.from_dict(m["regimes"]["12-18"])
    reg.g[:] = np.nan
    reg.atm_vix1d_ratio = 2.0
    m["regimes"]["12-18"] = reg.to_dict()
    t, info, _ = select_tables(m, COLD, "auto", 13.0, TODAY)
    assert info["tier"] == "library" and np.isfinite(t.g).all()
    assert t.atm_vix1d_ratio == pytest.approx(_src(1.0).atm_vix1d_ratio)


def test_pinned_tier():
    _, info, _ = select_tables(_model_dict(), COLD, "cold", 13.0, TODAY)
    assert info["tier"] == "cold" and info["requested"] == "cold"
    _, info, w = select_tables(_model_dict(), COLD, "library", None, TODAY)
    assert info["tier"] == "thin" and any("library tier unavailable" in x for x in w)
    with pytest.raises(ValueError, match="pricing_tier"):
        select_tables(None, COLD, "regime", 13.0, TODAY)


def test_stale_after_ten_trading_days():
    _, info, _ = select_tables(_model_dict(), COLD, "auto", 13.0, date(2030, 3, 18))
    assert info["stale"] is False
    _, info, w = select_tables(_model_dict(), COLD, "auto", 13.0, date(2030, 3, 19))
    assert info["stale"] is True and any("stale" in x for x in w)


def test_model_file_errors_fall_back_to_cold(tmp_path):
    assert read_model_file(tmp_path / "missing.json") == (None, None)
    bad = tmp_path / "pricing_model.json"
    bad.write_text("{not json")
    model, err = read_model_file(bad)
    assert model is None and err.startswith("pricing: cannot read")
    _, info, w = select_tables({"v": 1, "pooled": {"v": 99}}, COLD, "auto", 13.0, TODAY)
    assert info["tier"] == "cold" and any("unusable" in x for x in w)


def test_resolve_pricing_reads_the_model_file(tmp_path, monkeypatch):
    from spx_trade_desk.sim import data as sim_data
    from spx_trade_desk.sim import library
    path = tmp_path / "pricing_model.json"
    path.write_text(json.dumps(_model_dict()))
    monkeypatch.setattr(library, "MODEL_PATH", path)
    monkeypatch.setattr(sim_data, "load_vix1d_daily", lambda period="2y": {"20300304": 13.0})
    monkeypatch.setattr(library, "load_cold", lambda: COLD)
    now = datetime(2030, 3, 5, 9, 0, tzinfo=ET)
    _, info, vix1d_prev, _ = library.resolve_pricing("auto", now=now)
    assert vix1d_prev == 13.0 and info["tier"] == "library"
    path.write_text("{oops")
    _, info, _, w = library.resolve_pricing("auto", now=now)
    assert info["tier"] == "cold" and w[0].startswith("pricing: cannot read")


def test_score_text_shows_days_and_the_in_sample_flag():
    m = _model_dict()
    m["scores"] = {"library": {"passed": 4, "scored": 4, "days": 1, "in_sample": True},
                   "thin": {"passed": 3, "scored": 5, "days": 6, "in_sample": False}}
    _, _, w = select_tables(m, COLD, "auto", 13.0, TODAY)
    assert any("harness 4/4 buckets on 1 day (in-sample)" in x for x in w)
    _, _, w = select_tables(m, COLD, "thin", 13.0, TODAY)
    assert any("harness 3/5 buckets on 6 days" in x and "in-sample" not in x for x in w)


@pytest.mark.parametrize("bad", [{"days": "x"}, {"scores": {"thin": [1]}}, {"scores": {"thin": 3}},
                                 {"regime_days": {"12-18": "many"}}, {"last_capture": 5}])
def test_malformed_but_valid_json_models_fall_back_to_cold(bad):
    m = dict(_model_dict(), **bad)
    t, info, w = select_tables(m, COLD, "auto", 13.0, TODAY)
    assert info["tier"] == "cold" and np.allclose(t.f, COLD.f)
    assert any(x.startswith("pricing: ") and "unusable" in x for x in w)


@pytest.mark.parametrize("bad", [{"days": "x"}, {"pooled": 5}, {"scores": {"thin": [1]}}])
def test_pricing_summary_survives_a_malformed_library_block(tmp_path, monkeypatch, bad):
    from spx_trade_desk.sim import library
    path = tmp_path / "pricing_model.json"
    path.write_text(json.dumps(dict(_model_dict(), **bad)))
    monkeypatch.setattr(library, "MODEL_PATH", path)
    monkeypatch.setattr(library, "load_cold", lambda: COLD)
    s = library.pricing_summary("auto")
    assert s["tier"] == "cold"
    assert any(x.startswith("pricing: ") and "unusable" in x for x in s["warnings"])


# Price-ordering tolerance for a strike step: half of the $0.10 SPX option tick. The tracked Cold
# default alone already sits at about -0.005 near the wing without any tilt (skew_beta = 0).
NO_ARB_TOL = 0.05


@pytest.mark.parametrize("skew_beta", [0.0, 0.5, 1.0])
def test_skew_tilt_keeps_put_prices_ordered_across_the_level_link(skew_beta, monkeypatch):
    """Tracked Cold default, 5m bars, strikes +-15%: for every level link L in [1, L_MAX] a put
    never falls with strike and never rises by more than the strike step (no vertical-spread
    arbitrage), at the open and into the close."""
    from spx_trade_desk.sim.pricing_tables import load_cold
    pm = build_pricing_model(_model(pricing=load_cold(), ushape=np.ones(78)),
                             SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=skew_beta))
    S, step = 6000.0, 5.0
    K = np.arange(0.85 * S, 1.15 * S + 1e-9, step)
    worst_down = worst_up = 0.0
    for L in np.linspace(1.0, L_MAX, 21):
        monkeypatch.setattr(type(pm), "link", lambda self, t, s, L=L: np.full(np.shape(s), L))
        for t in (0, 5, 20, 40, 60, 70, 75, 77):
            iv = pm.iv_sim(np.log(K / S), t, np.asarray(1e-3))
            tau = float(t_sim(max(pm.tau_min[t], 2.5)))
            d = np.diff(bsm_put(S, K, tau, pm.rate, iv))
            worst_down, worst_up = min(worst_down, d.min()), max(worst_up, (d - step).max())
    assert worst_down >= -NO_ARB_TOL and worst_up <= NO_ARB_TOL, (worst_down, worst_up)
