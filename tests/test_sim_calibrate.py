# tests/test_sim_calibrate.py
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


from spx_trade_desk.sim.calibrate import calibrate, fit_ushape


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


def test_calibrate_warns_about_a_leftover_smile_snapshot(tmp_path, monkeypatch):
    from spx_trade_desk.sim import calibrate as sc
    from spx_trade_desk.sim.config import SimRunConfig
    legacy = tmp_path / "sim_smile.json"
    legacy.write_text("{}")
    monkeypatch.setattr(sc, "LEGACY_SMILE_PATH", legacy)
    model = sc.calibrate(_make_bars(), SimRunConfig(strategy_name="Main"))
    assert any("sim_smile.json is no longer used" in w for w in model.warnings)
