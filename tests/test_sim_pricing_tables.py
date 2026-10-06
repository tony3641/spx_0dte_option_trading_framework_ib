# tests/test_sim_pricing_tables.py
"""Pricing tables: bucketing, row finishing, builder and JSON round trip (invented data)."""
import json

import numpy as np
import pytest

from spx_trade_desk.sim.library import DayData, extract_day
from spx_trade_desk.sim.pricing_tables import (G_TAU, MIN_BUCKET_SWEEPS, N_TAU, N_Z, Q_MAX,
                                               RATIO_FLOOR, TAU_CENTERS, Z_GRID, Z_STEP,
                                               PricingTables, build_tables, fill_empty_rows,
                                               finish_row, mid_bucket, tau_bucket)
from tests.sim_chain_fixture import (ATM_OPEN, VIX1D_PREV, g_true, r_true, truth_tables,
                                     write_day)

DAYS = ("20300304", "20300305", "20300306")


def _days(tmp_path, **kw):
    return [extract_day(write_day(tmp_path, d, seed=i, **kw)) for i, d in enumerate(DAYS)]


def _manual_day(taus, points, day="20300304"):
    """DayData from (record index, z, ratio) points; ATM fixed at 0.12, no spreads."""
    taus = np.asarray(taus, dtype=float)
    pr, pz, prt = (np.array([p[i] for p in points]) for i in range(3))
    return DayData(day=day, session_min=390.0, rec_tau=taus, rec_atm=np.full(len(taus), 0.12),
                   rec_vix1d=np.full(len(taus), np.nan), pt_rec=pr.astype(int),
                   pt_z=pz.astype(float), pt_ratio=prt.astype(float),
                   hs_rec=np.zeros(0, dtype=int), hs_mid=np.zeros(0), hs_half=np.zeros(0))


def test_grid_constants():
    assert len(Z_GRID) == N_Z == 37 and Z_GRID[24] == 0.0 and Z_GRID[0] == -6.0
    assert len(G_TAU) == 20 and G_TAU[-2] == 375.0


def test_tau_and_mid_buckets():
    assert tau_bucket([389, 300.0, 299.9, 180, 120, 60, 30, 15, 0.5]).tolist() == [0, 1, 1, 2, 3, 4, 5, 6, 6]
    assert mid_bucket([0.2, 0.5, 0.99, 2.5, 3.0, 25.0]).tolist() == [0, 1, 1, 3, 4, 7]


def test_finish_row_keeps_a_linear_row_and_extrapolates_in_total_variance():
    nodes = np.full(N_Z, np.nan)
    inside = slice(12, 29)                                   # z = -3 .. +1
    nodes[inside] = 1.0 - 0.1 * Z_GRID[inside]
    row = finish_row(nodes)
    assert np.allclose(row[inside], nodes[inside], rtol=1e-12)
    q = (1.0 - 0.1 * Z_GRID) ** 2
    left = (q[13] - q[12]) / Z_STEP
    right = (q[28] - q[27]) / Z_STEP
    assert row[0] == pytest.approx(np.sqrt(q[12] + left * (Z_GRID[0] - Z_GRID[12])))
    assert row[-1] == pytest.approx(np.sqrt(q[28] + right * (Z_GRID[-1] - Z_GRID[28])))


def test_finish_row_makes_the_put_wing_monotone():
    nodes = np.full(N_Z, np.nan)
    nodes[12:25] = 1.0 - 0.1 * Z_GRID[12:25]
    nodes[14] = 1.0                                          # a dip in the put wing
    row = finish_row(nodes)
    assert np.all(np.diff(row[: 25]) <= 1e-12)               # IV/ATM never falls as z falls


def test_finish_row_clamps_extrapolation_slopes_and_floors():
    nodes = np.full(N_Z, np.nan)
    nodes[20], nodes[21] = 2.0, 1.0                          # r^2 slope -12 per z unit
    row = finish_row(nodes)
    assert row[0] == pytest.approx(np.sqrt(4.0 + Q_MAX * (Z_GRID[20] - Z_GRID[0])))
    assert row[-1] == pytest.approx(RATIO_FLOOR)


def test_finish_row_needs_two_nodes():
    nodes = np.full(N_Z, np.nan)
    nodes[24] = 1.0
    assert np.isnan(finish_row(nodes)).all()


def test_sparse_records_count_as_sweeps_but_do_not_define_nodes():
    # 25 last-hour records; only records 0-2 carry OTM points, and no z node reaches
    # 3 points from 2 different records.
    pts = [(0, -1.0, 1.2), (1, -1.0, 1.2), (0, -0.5, 1.1), (1, -0.5, 1.1),
           (2, -2.0, 1.4), (2, -2.0, 1.4), (2, -2.0, 1.4)]
    t = build_tables([_manual_day([10.0] * 25, pts)], {})
    assert t.f_sweeps[6] == 25 and t.f_sweeps[:6].sum() == 0
    assert np.isnan(t.f[6]).all() and not t.row_usable(6)
    pts += [(3, -1.0, 1.2), (3, -0.5, 1.1)]                  # now 3 points from 3 records at two nodes
    t = build_tables([_manual_day([10.0] * 25, pts)], {})
    assert t.f[6, 20] == pytest.approx(1.2) and t.f[6, 22] == pytest.approx(1.1)


def test_build_tables_recovers_truth(tmp_path):
    days = _days(tmp_path)
    t = build_tables(days, {d: VIX1D_PREV for d in DAYS})
    near = np.abs(Z_GRID) <= 2.0
    for b in (0, 1, 2):                                      # tau > 120: the early shape, no blend
        assert np.allclose(t.f[b, near], r_true(Z_GRID[near], TAU_CENTERS[b]), rtol=0.03)
    assert np.allclose(t.g, [g_true(x) for x in G_TAU], rtol=0.03)
    assert t.atm_vix1d_ratio == pytest.approx(ATM_OPEN / (VIX1D_PREV / 100.0), rel=0.01)
    one_day = np.bincount(tau_bucket(389.0 - 5.0 * np.arange(78)), minlength=N_TAU)
    assert t.f_sweeps.tolist() == (3 * one_day).tolist()
    assert np.isfinite(t.hs[0]).all() and np.all(np.diff(t.hs[0]) >= -1e-12)
    assert t.n_days == 3 and t.provisional is False


def test_g_needs_a_full_day_unless_partial(tmp_path):
    days = [extract_day(write_day(tmp_path, "20300304", seed=1, start="13:25"))]   # tau 155 onward
    t = build_tables(days, {"20300304": VIX1D_PREV})
    assert np.isnan(t.g).all() and np.isnan(t.atm_vix1d_ratio) and not t.level_usable()
    t = build_tables(days, {"20300304": VIX1D_PREV}, allow_partial_g=True)
    g = dict(zip(G_TAU, t.g))
    assert g[390.0] == 1.0 and g[180.0] == 1.0               # flat above the first record
    assert g[60.0] == pytest.approx(g_true(60.0), rel=0.02)
    assert t.atm_vix1d_ratio == pytest.approx(ATM_OPEN * g_true(155.0) / 0.13, rel=0.02)
    assert t.provisional is True


def test_half_day_never_feeds_g(tmp_path, monkeypatch):
    from spx_trade_desk.sim import clock
    monkeypatch.setattr(clock, "is_short_trading_day", lambda d=None: True)
    days = [extract_day(write_day(tmp_path, "20301129", seed=1))]
    assert days[0].session_min == 210.0
    assert np.isnan(build_tables(days, {"20301129": VIX1D_PREV}).g).all()


def test_tables_round_trip_json(tmp_path):
    t = build_tables(_days(tmp_path, step_min=30), {d: VIX1D_PREV for d in DAYS})
    back = PricingTables.from_dict(json.loads(json.dumps(t.to_dict())))
    for name in ("f", "g", "hs"):
        a, b = getattr(t, name), getattr(back, name)
        assert np.array_equal(np.isnan(a), np.isnan(b))
        assert np.allclose(a[np.isfinite(a)], b[np.isfinite(b)], atol=1e-6)
    assert back.f_sweeps.tolist() == t.f_sweeps.tolist() and back.n_days == 3
    bad = t.to_dict()
    bad["z_grid"] = [-5.0, 3.0, 0.25]
    with pytest.raises(ValueError, match="grid"):
        PricingTables.from_dict(bad)


def test_fill_empty_rows_copies_the_nearest_row():
    t = truth_tables()
    t.f[0:2] = np.nan
    t.hs[0:2] = np.nan
    filled = fill_empty_rows(t)
    assert np.array_equal(filled.f[0], t.f[2]) and np.array_equal(filled.f[1], t.f[2])
    assert np.array_equal(filled.hs[0], t.hs[2])
    assert filled.f_sweeps.tolist() == t.f_sweeps.tolist()
    assert all(filled.row_usable(b) for b in range(N_TAU))


def test_truth_tables_are_complete():
    t = truth_tables()
    assert all(t.row_usable(b) for b in range(N_TAU)) and t.level_usable()
    assert MIN_BUCKET_SWEEPS <= t.f_sweeps.min()
