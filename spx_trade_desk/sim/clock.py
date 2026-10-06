"""One clock for the simulator's option pricing (SP2 spec section 4.1).

Every stored IV (IB quotes, VIX1D, the chain library, the pricing tables) is
calendar-time: annualized over 365 x 24 h. The simulated path runs on trading time:
252 RTH days x 6.5 h. Total variance w = IV_cal^2 * T_cal is the same on both clocks,
so z = m / sqrt(w) needs no conversion. The only conversion is the volatility handed
to BSM on the sim clock: sigma_sim = iv_cal * sqrt(T_cal / T_sim), a constant factor
because both clocks are linear in minutes to the close.
"""
import math
from datetime import date, datetime, time

import numpy as np

from spx_trade_desk.market.hours import ET, is_short_trading_day

CAL_MIN_PER_YEAR = 365.0 * 24.0 * 60.0          # 525600
SIM_MIN_PER_YEAR = 252.0 * 6.5 * 60.0           # 98280
CAL_TO_SIM = math.sqrt(SIM_MIN_PER_YEAR / CAL_MIN_PER_YEAR)   # 0.43242
RATE_TO_SIM = CAL_TO_SIM ** 2                                  # T_cal / T_sim
RTH_MINUTES = 390.0


def bar_year_frac(bar_seconds: int) -> float:
    """Sim-clock year fraction of one bar: 252 RTH days x 6.5 h."""
    return bar_seconds / (252 * 6.5 * 3600.0)


def t_cal(tau_min):
    """Calendar years for ``tau_min`` minutes to the close."""
    return np.asarray(tau_min, dtype=float) / CAL_MIN_PER_YEAR


def t_sim(tau_min):
    """Sim-clock years for ``tau_min`` minutes to the close."""
    return np.asarray(tau_min, dtype=float) / SIM_MIN_PER_YEAR


def sim_sigma(iv_cal):
    """Calendar-unit IV -> the BSM volatility on the sim clock (same total variance)."""
    return np.asarray(iv_cal, dtype=float) * CAL_TO_SIM


def sim_rate(r: float) -> float:
    """Calendar annual rate -> the BSM rate on the sim clock (r_sim * T_sim == r * T_cal).
    With sim_sigma this reproduces the calendar-clock BSM price exactly."""
    return float(r) * RATE_TO_SIM


def rth_day_vol(atm_iv_cal: float) -> float:
    """1-day RTH return std implied by a calendar-unit annual IV: sqrt(iv^2 * 6.5 h / 1 yr)."""
    return float(atm_iv_cal) * math.sqrt(RTH_MINUTES / CAL_MIN_PER_YEAR)


def bar_tau_minutes(steps: int, bar_seconds: int) -> np.ndarray:
    """Minutes to the close after bar t, t = 0..steps-1 (the last bar closes at 0)."""
    return np.arange(steps - 1, -1, -1, dtype=float) * bar_seconds / 60.0


def session_close_time(d: date) -> time:
    """13:00 ET on half days, else 16:00 ET (the SPXW close)."""
    return time(13, 0) if is_short_trading_day(d) else time(16, 0)


def minutes_to_close(ts: datetime) -> float:
    """Minutes from ``ts`` to that session's close (negative after the close)."""
    et = ts.astimezone(ET) if ts.tzinfo else ts.replace(tzinfo=ET)
    close = datetime.combine(et.date(), session_close_time(et.date()), tzinfo=ET)
    return (close - et).total_seconds() / 60.0
