# tests/test_sim_clock.py
"""One clock for the sim's option pricing (SP2 spec section 4.1)."""
import math
from datetime import datetime

import numpy as np
import pytest

from spx_trade_desk.market.hours import ET
from spx_trade_desk.sim import clock


def test_cal_to_sim_constant():
    assert clock.CAL_TO_SIM == pytest.approx(math.sqrt(98280 / 525600), rel=1e-15)
    assert clock.CAL_TO_SIM == pytest.approx(0.43242, abs=1e-5)


def test_bar_year_frac_is_the_sim_clock_and_reexported():
    from spx_trade_desk.sim.pricing import bar_year_frac
    assert bar_year_frac is clock.bar_year_frac
    assert clock.bar_year_frac(60) == pytest.approx(float(clock.t_sim(1.0)), rel=1e-15)


@pytest.mark.parametrize("tau", [389.0, 120.0, 15.0, 0.5])
def test_total_variance_and_z_are_clock_free(tau):
    iv_cal = 0.11
    w_cal = iv_cal ** 2 * float(clock.t_cal(tau))
    w_sim = float(clock.sim_sigma(iv_cal)) ** 2 * float(clock.t_sim(tau))
    assert w_sim == pytest.approx(w_cal, rel=1e-12)
    m = -0.01
    assert m / math.sqrt(w_sim) == pytest.approx(m / math.sqrt(w_cal), rel=1e-12)


def test_rth_day_vol_identity_with_the_old_trading_unit():
    # A calendar IV of (trading IV / CAL_TO_SIM) gives the old iv / sqrt(252) day vol.
    assert clock.rth_day_vol(0.167 / clock.CAL_TO_SIM) == pytest.approx(0.167 / math.sqrt(252), rel=1e-12)
    assert clock.rth_day_vol(0.11) == pytest.approx(0.11 * math.sqrt(390 / 525600), rel=1e-12)


def test_sim_rate_discounts_like_the_calendar_rate():
    from spx_trade_desk.sim.pricing import bsm_put
    r = 0.043
    assert clock.sim_rate(r) * float(clock.t_sim(120.0)) == pytest.approx(r * float(clock.t_cal(120.0)), rel=1e-12)
    S, K, iv = 6000.0, np.array([5950.0, 5990.0, 6010.0]), 0.12
    a = bsm_put(S, K, float(clock.t_sim(120.0)), clock.sim_rate(r), float(clock.sim_sigma(iv)))
    b = bsm_put(S, K, float(clock.t_cal(120.0)), r, iv)
    assert np.allclose(a, b, rtol=1e-12)


def test_bar_tau_minutes():
    tau = clock.bar_tau_minutes(390, 60)
    assert len(tau) == 390 and tau[0] == 389.0 and tau[-1] == 0.0
    assert clock.bar_tau_minutes(78, 300)[0] == 385.0


def test_minutes_to_close_full_and_half_day(monkeypatch):
    monkeypatch.setattr(clock, "is_short_trading_day", lambda d=None: False)
    assert clock.minutes_to_close(datetime(2030, 3, 4, 15, 30, tzinfo=ET)) == 30.0
    monkeypatch.setattr(clock, "is_short_trading_day", lambda d=None: True)
    assert clock.minutes_to_close(datetime(2030, 11, 29, 12, 0, tzinfo=ET)) == 60.0
    assert clock.minutes_to_close(datetime(2030, 11, 29, 13, 5, tzinfo=ET)) == -5.0
