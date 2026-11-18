"""
Dealer flow aggregates: charm (delta decay with time) and vanna (delta change with implied vol).

Built on ``dealer_model``: every flow is the change in dealer share-equivalent delta, valued at spot,
under the same sign convention as the rest of the profile code: hedge flow USD + = dealers buy the
underlying, - = dealers sell. The charm step ``DT15_YEARS`` matches ``charm_history.charm_exposure``,
so the 15-minute flow equals the charm heatmap total.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np

from spx_trade_desk.market.charm_history import DT15_YEARS
from spx_trade_desk.market.dealer_model import IV_FLOOR, MULTIPLIER, DealerBook, bsm_delta
from spx_trade_desk.market.hours import ET

YEAR_MINUTES = 390.0 * 252.0              # trading minutes per year (the GEX math's time unit)
T_END_YEARS = 1.0 / YEAR_MINUTES          # one minute before the close: the last charm mark
VOL_POINT = 0.01                          # one vol point (1% absolute IV) for vanna
CLOSE_HOUR = 16                           # SPXW expirations stop at 16:00 ET


def _row_delta(book: DealerBook, spot: float, T: float, r: float, iv_shift: float = 0.0) -> np.ndarray:
    """Dealer share-equivalent delta of each book row (dollars of underlying per row, signed by position)."""
    iv = np.maximum(book.iv + iv_shift, IV_FLOOR)
    return book.pos * bsm_delta(spot, book.K, T, r, iv, book.is_call) * MULTIPLIER


def _flow_between(book: DealerBook, spot: float, T_from: float, T_to: float, r: float) -> float:
    """USD the dealers trade as time runs from T_from to T_to at spot: -(D(T_to) - D(T_from)) * spot."""
    d_from = _row_delta(book, spot, T_from, r).sum()
    d_to = _row_delta(book, spot, T_to, r).sum()
    return float(-(d_to - d_from) * spot)


def charm_flow_usd(book: DealerBook, spot: float, T: float, r: float, dt: float = DT15_YEARS) -> float:
    """Dealer hedge flow over ``dt`` years of decay: -(D(spot, T - dt) - D(spot, T)) * spot. ``dt`` halves when T <= dt."""
    if T <= 0:
        return 0.0
    if T <= dt:
        dt = T * 0.5
    return _flow_between(book, spot, T, T - dt, r)


def strike_charm_usd(book: DealerBook, spot: float, T: float, r: float, dt: float = DT15_YEARS) -> Dict[float, float]:
    """charm_flow_usd per strike (calls and puts at a strike summed); the values add up to the total."""
    if T <= 0:
        return {}
    if T <= dt:
        dt = T * 0.5
    later = _row_delta(book, spot, T - dt, r)
    now = _row_delta(book, spot, T, r)
    out: Dict[float, float] = {}
    for k, change in zip(book.K, -(later - now) * spot):
        key = float(k)
        out[key] = out.get(key, 0.0) + float(change)
    return out


def charm_to_close_usd(book: DealerBook, spot: float, T: float, r: float) -> float:
    """Dealer hedge flow from now to the close: the charm flow over the remaining time; 0 when no time is left."""
    if T <= T_END_YEARS:
        return 0.0
    return _flow_between(book, spot, T, T_END_YEARS, r)


def charm_path(book: DealerBook, spot: float, now: datetime, T: float, r: float) -> List[dict]:
    """Cumulative charm flow from now at each :00 / :30 ET mark before 16:00, plus "15:59" (the close)."""
    now = now.astimezone(ET)
    close = now.replace(hour=CLOSE_HOUR, minute=0, second=0, microsecond=0)
    mark = now.replace(minute=now.minute - now.minute % 30, second=0, microsecond=0) + timedelta(minutes=30)
    path: List[dict] = []
    while mark < close:
        T_mark = (close - mark).total_seconds() / 60.0 / YEAR_MINUTES
        path.append({"time": mark.strftime("%H:%M"), "usd": _flow_between(book, spot, T, T_mark, r)})
        mark += timedelta(minutes=30)
    last = close - timedelta(minutes=1)
    if now < last:
        path.append({"time": last.strftime("%H:%M"), "usd": _flow_between(book, spot, T, T_END_YEARS, r)})
    return path


def vanna_usd_per_vol_down(book: DealerBook, spot: float, T: float, r: float) -> float:
    """Dealer hedge flow if implied vol falls one vol point: -(D(iv - VOL_POINT) - D(iv)) * spot."""
    d_down = _row_delta(book, spot, T, r, iv_shift=-VOL_POINT).sum()
    d_now = _row_delta(book, spot, T, r).sum()
    return float(-(d_down - d_now) * spot)


def charm_pivot(strike_charm: Dict[float, float], spot: float) -> Optional[float]:
    """Midpoint of the adjacent nonzero strikes with opposite charm signs nearest spot; None if the sign never changes."""
    nonzero = sorted((k, v) for k, v in strike_charm.items() if v != 0)
    best: Optional[float] = None
    for (k1, v1), (k2, v2) in zip(nonzero, nonzero[1:]):
        if (v1 < 0) != (v2 < 0):
            mid = round((k1 + k2) / 2.0, 2)
            if best is None or abs(mid - spot) < abs(best - spot):
                best = mid
    return best
