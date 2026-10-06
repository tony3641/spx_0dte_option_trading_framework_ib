# tests/test_sim_pricing.py
"""BSM, tick and ladder primitives (the z-model pricer is tested in test_sim_pricing_model.py)."""
import math

import numpy as np

from spx_trade_desk.market.gex import _bsm_delta
from spx_trade_desk.sim.pricing import (bsm_put, bsm_put_delta, build_ladder, combo_fill_credit,
                                        tick_floor)


def test_bsm_put_put_call_parity():
    S, K, T, r, sig = np.array([6000.0]), np.array([5900.0]), 1 / 252, 0.043, 0.25
    put = bsm_put(S, K, T, r, sig)
    # call via parity: C = P + S - K*e^{-rT}
    call = put + S - K * math.exp(-r * T)
    intrinsic = max(0.0, (K - S)[0])
    assert put[0] > intrinsic
    assert abs(float(put[0] - 250.0)) < 250.0        # sane magnitude
    assert (call > 0).all()


def test_bsm_put_expiry_is_intrinsic():
    S = np.array([6000.0, 5850.0])
    K = np.array([5900.0, 5900.0])
    put = bsm_put(S, K, 0.0, 0.043, 0.25)
    np.testing.assert_allclose(put, [0.0, 50.0])


def test_put_delta_matches_gex_calculator():
    # parity with the live dashboard's BSM delta implementation
    S, K, T, r, sig = 6000.0, 5800.0, 2 / (252 * 390), 0.043, 0.30
    mine = float(bsm_put_delta(np.array([S]), np.array([K]), T, r, np.array([sig]))[0])
    theirs = _bsm_delta(S, K, T, r, sig, "P")
    assert abs(mine - theirs) < 1e-9


def test_tick_floor_and_combo_fill():
    assert tick_floor(0.27) == 0.25
    assert tick_floor(0.23) == 0.20
    assert tick_floor(0.25) == 0.25           # exact multiple stays
    np.testing.assert_allclose(tick_floor(np.array([0.27, 0.23])), [0.25, 0.20])
    assert combo_fill_credit(0.27, 0.25, 0.05) == 0.25   # natural 0.25 is not the constraint
    assert combo_fill_credit(0.23, 0.20, 0.05) == 0.20
    assert combo_fill_credit(0.07, 0.02, 0.05) == 0.05   # floored at one tick
    # off-grid NATURAL must also be floored onto the 0.05 grid (regression: the old
    # implementation returns the raw natural when it binds, e.g. 0.187 / 0.54 / 0.69).
    assert combo_fill_credit(0.30, 0.187, 0.05) == 0.15
    assert combo_fill_credit(0.187, 0.54, 0.05) == 0.15
    assert combo_fill_credit(0.30, 0.02, 0.05) == 0.05


def test_build_ladder():
    K = build_ladder(6000.0, 0.15)
    assert K[0] == 5100.0 and K[-1] == 6900.0
    assert (np.diff(K) == 5.0).all() and 6000.0 in K
