# tests/test_sim_calibrate.py
import json

import numpy as np
import pytest

from spx_trade_desk.sim.calibrate import fit_gjr_t, gjr_variance_path


def _simulate_gjr(n=8000, omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, seed=3):
    rng = np.random.default_rng(seed)
    eps = np.empty(n)
    s2 = omega / (1 - alpha - gamma / 2 - beta)
    for t in range(n):
        z = rng.standard_t(nu) / np.sqrt(nu / (nu - 2))
        eps[t] = np.sqrt(s2) * z
        s2 = omega + alpha * eps[t] ** 2 + gamma * eps[t] ** 2 * (eps[t] < 0) + beta * s2
    return eps


def test_fit_recovers_parameters_loosely():
    eps = _simulate_gjr()
    params, warnings = fit_gjr_t(eps)
    assert params.converged
    assert not warnings
    assert 0 < params.alpha < 0.35 and 0 <= params.gamma < 0.5 and 0.3 < params.beta < 0.99
    assert params.alpha + params.beta + params.gamma / 2 < 1.0    # stationary
    assert 2.5 < params.nu < 30


def test_variance_path_matches_recursion():
    eps = _simulate_gjr(n=200)
    p, _ = fit_gjr_t(eps)
    v = gjr_variance_path(p, eps)
    assert v.shape == eps.shape
    assert (v > 0).all()


def test_fit_degenerate_returns_preset():
    eps = np.zeros(100)                    # zero variance -> optimizer cannot work
    p, warnings = fit_gjr_t(eps)
    assert not p.converged
    assert warnings and "preset" in warnings[0].lower()
    assert p.alpha > 0 and p.beta < 1.0    # preset values, variance-targeted omega


from spx_trade_desk.sim.calibrate import (CalibratedModel, DEFAULT_SMILE, SmileParams,
                           calibrate, fit_smile, fit_ushape, load_smile_snapshot)


def _make_bars(n_days=6, bars=390, seed=11):
    from spx_trade_desk.sim.data import BarSeries
    rng = np.random.default_rng(seed)
    # morning + afternoon active, midday quiet -> U-shape
    ushape = np.interp(np.arange(bars), [0, 30, 195, 360, 385], [2.0, 0.7, 0.6, 1.6, 2.4])
    ushape = ushape / ushape.mean()
    closes, mods = [], []
    s = 6000.0
    for d in range(n_days):
        for b in range(bars):
            eps = 0.0005 * ushape[b] * rng.standard_normal()
            s *= float(np.exp(eps))
            closes.append(s)
            mods.append(570 + (b + 1))
    return BarSeries(closes=np.array(closes), minute_of_day=np.array(mods),
                     bar_seconds=60, source="csv")


def test_fit_ushape_peaks_at_open_and_close():
    bars = _make_bars()
    rets = np.diff(np.log(bars.closes))
    u = fit_ushape(rets, bars.minute_of_day[1:], steps_per_day=390)
    assert u.shape == (390,)
    assert abs(u.mean() - 1.0) < 0.2
    assert u[:30].mean() > u[150:250].mean()   # open busier than midday
    assert u[-30:].mean() > u[150:250].mean()  # close busier than midday


def test_fit_smile_svi_recovers_skew():
    # exactly in-family SVI data: the fit should recover a sane, bounded smile.
    m = np.linspace(-0.06, 0.01, 12)
    iv_true = DEFAULT_SMILE.iv(m)
    smile, warnings = fit_smile(m, iv_true, DEFAULT_SMILE)
    assert not warnings
    assert 0.10 < smile.iv(0.0) < 0.30                 # ATM IV sane
    far = smile.iv(-0.15)
    assert np.isfinite(far) and 0.0 < far < 1.0        # far-OTM bounded (the core fix)
    assert far > smile.iv(0.0)                          # put skew present


def test_fit_smile_insufficient_points_falls_back():
    smile, warnings = fit_smile(np.array([-0.01]), np.array([0.22]), DEFAULT_SMILE)
    assert warnings and smile == DEFAULT_SMILE


def test_snapshot_round_trip(tmp_path, monkeypatch):
    from spx_trade_desk.sim import calibrate as sc
    monkeypatch.setattr(sc, "SMILE_CAPTURE_PATH", str(tmp_path / "sim_smile.json"))
    monkeypatch.setattr(sc, "SMILE_DEFAULT_PATH", str(tmp_path / "sim_smile_default.json"))
    sc.save_smile_snapshot(SmileParams(a=0.05, b=2.0, rho=-0.60, m0=0.02, sigma=0.07,
                                       half_spread_atm=0.06))
    smile, src = sc.load_smile_snapshot()
    assert src == "captured"
    assert abs(smile.rho + 0.60) < 1e-9 and abs(smile.sigma - 0.07) < 1e-9
    assert smile.half_spread_atm == 0.06
    (tmp_path / "sim_smile.json").unlink()
    smile2, src2 = sc.load_smile_snapshot()
    assert src2 == "builtin" and smile2 == sc.DEFAULT_SMILE


def test_legacy_quadratic_snapshot_is_skipped(tmp_path, monkeypatch):
    from spx_trade_desk.sim import calibrate as sc
    with pytest.raises(ValueError):
        sc.SmileParams.from_dict({"a": 0.2, "b": -0.35, "c": 1.2, "half_spread_atm": 0.05})
    monkeypatch.setattr(sc, "SMILE_CAPTURE_PATH", str(tmp_path / "sim_smile.json"))
    (tmp_path / "sim_smile.json").write_text(
        json.dumps({"a": 0.2, "b": -0.35, "c": 1.2, "half_spread_atm": 0.05}), encoding="utf-8")
    smile, src = sc.load_smile_snapshot()
    assert src != "captured" and smile == sc.DEFAULT_SMILE


def test_skewed_chain_bounded_far_otm():
    # Observed put IVs reach only ~5-6% OTM (the live-chain limit); the fit must
    # extrapolate to the sim's +/-15% ladder without exploding or dipping.
    m = np.linspace(-0.06, 0.01, 12)
    iv = DEFAULT_SMILE.iv(m)                       # put-skewed chain (~35% at -6% OTM)
    smile, warnings = fit_smile(m, iv, DEFAULT_SMILE)
    assert not warnings
    far = smile.iv(-0.15)
    assert np.isfinite(far) and 0.0 < far < 1.0    # bounded at the ladder edge
    assert smile.iv(-0.15) >= smile.iv(-0.06)      # wing keeps rising, no dip


def test_degenerate_stray_outlier_falls_back():
    m = np.linspace(-0.06, 0.01, 12)
    iv = DEFAULT_SMILE.iv(m)
    iv[0] = 1.8                                    # wild far-OTM put IV outlier
    smile, warnings = fit_smile(m, iv, DEFAULT_SMILE)
    assert warnings and smile == DEFAULT_SMILE


def test_default_smile_bounded():
    iv = DEFAULT_SMILE.iv(np.array([-0.15, 0.0, 0.15]))
    assert np.isfinite(iv).all() and (iv > 0).all()
    assert iv[0] < 1.0
    assert abs(DEFAULT_SMILE.iv(0.0) - 0.20) < 0.02


def _two_day_series_with_overnight_gap():
    from spx_trade_desk.sim.data import BarSeries
    rng = np.random.default_rng(21)
    bars = 390
    mods = 570 + (np.arange(bars) + 1)                    # 571..960 = one RTH day at 1m
    day0 = 6000.0 * np.exp(0.0004 * np.cumsum(rng.standard_normal(bars)))
    # +50% overnight gap: day1's first close is 1.5x day0's last close. The cross-day
    # log-diff (~0.405) is a prior-16:00 -> next-09:3x move, NOT part of 0DTE intraday
    # dynamics. If it were folded in it would land in the opening U-shape bucket + inflate sigma0.
    day1 = (float(day0[-1]) * 1.5) * np.exp(0.0004 * np.cumsum(rng.standard_normal(bars)))
    return BarSeries(closes=np.concatenate([day0, day1]),
                     minute_of_day=np.concatenate([mods, mods]),
                     bar_seconds=60, source="csv")


def test_calibrate_excludes_overnight_cross_day_return():
    from spx_trade_desk.sim.calibrate import calibrate
    from spx_trade_desk.sim.config import SimRunConfig
    bars = _two_day_series_with_overnight_gap()
    model = calibrate(bars, SimRunConfig(strategy_name="Main"))
    # The overnight log-diff (~0.405) would land in the opening minute bucket and blow it out
    # (pre-fix ushape[:3] clips at the 4.0 cap). Excluding cross-day diffs keeps the opening
    # U-shape at the intraday scale (~1.0) instead of an inflated 4.0.
    assert model.ushape[:3].mean() < 3.0


def test_calibrate_end_to_end():
    from spx_trade_desk.sim.config import SimRunConfig
    from spx_trade_desk.sim.calibrate import calibrate
    bars = _make_bars(n_days=8)
    model = calibrate(bars, SimRunConfig(strategy_name="Main"))
    assert model.garch.converged or model.warnings
    assert model.ushape.shape == (390,)
    assert model.sigma0 > 0 and model.vix0 > 0
    assert model.source == "csv"
    ann = model.sigma_annual(SimRunConfig(strategy_name="Main"))
    assert 0.02 < ann < 5.0
    assert model.pricing_info["tier"] == "cold" and model.vix1d_prev is None
    assert model.pricing.f.shape == (7, 37)
    assert any(w.startswith("pricing: tier cold") for w in model.warnings)


def test_conditional_expectation_closed_form():
    """E[sigma^2_{t+k}] = v_bar + p^k (sigma^2 - v_bar): iterate the exact E-map."""
    from spx_trade_desk.sim.config import SimRunConfig
    bars = _make_bars()
    cfg = SimRunConfig(strategy_name="T", source="csv", bar_size="1m")
    model = calibrate(bars, cfg)
    g = model.garch
    p_eff = g.alpha + g.gamma / 2.0 + g.beta
    v_bar = g.omega / (1.0 - p_eff)
    s2 = 3.0 * v_bar
    for k in range(1, 60):
        s2 = g.omega + p_eff * s2                      # exact expectation recursion
        assert s2 == pytest.approx(v_bar + p_eff ** k * (3.0 * v_bar - v_bar),
                                   rel=1e-12)


def test_fit_smile_steep_0dte_skew_stays_bounded():
    """A real 0DTE put skew is far steeper than the synthetic fixtures: IV ~36% at -2%
    moneyness falling to ~12% ATM. The unconstrained SVI optimum then sits on the b
    bound with sigma -> 0, whose wings blow past SVI_WING_CAP at the +/-15% ladder edge,
    so every seed used to be rejected and the capture 409'd. The fit must instead return
    the best BOUNDED smile, not the fallback.
    """
    # live SPX 0DTE slice, 2026-09-09 12:05 ET, spot 7638.21 (informative quotes only)
    pairs = [(7490, 36.01), (7500, 33.82), (7520, 30.13), (7540, 26.85),
             (7560, 22.62), (7580, 19.65), (7600, 16.56), (7620, 14.11),
             (7635, 12.77), (7640, 12.44), (7650, 12.05)]
    m = np.log(np.array([k for k, _ in pairs], float) / 7638.21)
    iv = np.array([v for _, v in pairs]) / 100.0
    smile, warnings = fit_smile(m, iv, DEFAULT_SMILE)
    assert not warnings
    # the cap is the active constraint here, so the fit sits ON it (float slack only)
    assert 0.0 < smile.iv(-0.15) <= 1.0 + 1e-6 and 0.0 < smile.iv(0.15) <= 1.0 + 1e-6
    assert smile.iv(-0.15) > smile.iv(0.0)              # put skew present
    assert 0.08 < smile.iv(0.0) < 0.25                  # ATM IV near the observed 12-14%
    rmse = float(np.sqrt(np.mean((smile.iv(m) - iv) ** 2)))
    flat_rmse = float(np.sqrt(np.mean((iv - iv.mean()) ** 2)))
    assert rmse < flat_rmse                             # beats a flat line


def test_fit_smile_flat_degenerate_is_rejected():
    """A constant IV cloud has no skew for the SVI to find. Returning a flat smile would
    silently feed the sim an information-free curve; the fit must fall back instead."""
    m = np.linspace(-0.03, 0.005, 15)
    iv = np.full(m.shape, 0.18)
    smile, warnings = fit_smile(m, iv, DEFAULT_SMILE)
    assert warnings and smile == DEFAULT_SMILE


def test_calibrate_warns_about_a_leftover_smile_snapshot(tmp_path, monkeypatch):
    from spx_trade_desk.sim import calibrate as sc
    from spx_trade_desk.sim.config import SimRunConfig
    legacy = tmp_path / "sim_smile.json"
    legacy.write_text("{}")
    monkeypatch.setattr(sc, "LEGACY_SMILE_PATH", legacy)
    model = sc.calibrate(_make_bars(), SimRunConfig(strategy_name="Main"))
    assert any("sim_smile.json is no longer used" in w for w in model.warnings)
