"""Dealer flow aggregates: charm to the close, charm path, vanna exposure and the charm pivot."""
from datetime import datetime

import pytest

from spx_trade_desk.market.charm_history import DT15_YEARS, charm_exposure
from spx_trade_desk.market.dealer_flows import (
    T_END_YEARS,
    YEAR_MINUTES,
    charm_flow_usd,
    charm_path,
    charm_pivot,
    charm_to_close_usd,
    strike_charm_usd,
    vanna_usd_per_vol_down,
)
from spx_trade_desk.market.dealer_model import dealer_book, positions_from_oi
from spx_trade_desk.market.gex import OptionData, compute_gex
from spx_trade_desk.market.hours import ET

SPOT = 100.0
TTE = 3.0 * 60 / (390.0 * 252.0)          # three hours left, in the GEX math's trading years
R = 0.043
STRIKES = range(90, 111, 2)               # invented strikes, 2 apart


def _result(call_oi=None, put_oi=None, iv=0.2, spot=SPOT):
    """GEXResult over the invented strikes; ``call_oi`` / ``put_oi`` map strike -> open interest."""
    call_oi, put_oi = call_oi or {}, put_oi or {}
    options = []
    for strike in STRIKES:
        options.append(OptionData(strike=float(strike), right="C", implied_vol=iv, open_interest=call_oi.get(strike, 0)))
        options.append(OptionData(strike=float(strike), right="P", implied_vol=iv, open_interest=put_oi.get(strike, 0)))
    return compute_gex(options, spot, time_to_expiry_years=TTE, risk_free_rate=R)


def _book(res):
    return dealer_book(positions_from_oi(res), res)


def test_charm_flow_over_15_minutes_equals_the_charm_heatmap_total():
    res = _result(call_oi={102: 100000, 106: 30000}, put_oi={98: 100000, 94: 30000})
    assert charm_flow_usd(_book(res), SPOT, TTE, R) == pytest.approx(sum(charm_exposure(res).values()), rel=1e-3)
    assert sum(strike_charm_usd(_book(res), SPOT, TTE, R).values()) == pytest.approx(
        charm_flow_usd(_book(res), SPOT, TTE, R), rel=1e-9)

def test_dealers_short_otm_puts_get_supportive_charm_and_vanna():
    book = _book(_result(put_oi={98: 100000}))
    assert charm_to_close_usd(book, SPOT, TTE, R) > 0 and vanna_usd_per_vol_down(book, SPOT, TTE, R) > 0

def test_dealers_short_itm_puts_get_suppressive_charm():
    assert charm_to_close_usd(_book(_result(put_oi={102: 100000})), SPOT, TTE, R) < 0

def test_no_time_left_means_no_charm():
    book = _book(_result(put_oi={98: 100000}))
    assert charm_to_close_usd(book, SPOT, T_END_YEARS, R) == 0.0 and charm_flow_usd(book, SPOT, 0.0, R) == 0.0

def test_charm_path_marks_each_half_hour_to_the_close():
    book, T = _book(_result(put_oi={98: 100000})), 110 / YEAR_MINUTES
    path = charm_path(book, SPOT, datetime(2099, 1, 5, 14, 10, tzinfo=ET), T, R)
    assert [p["time"] for p in path] == ["14:30", "15:00", "15:30", "15:59"]
    assert path[-1]["usd"] == pytest.approx(charm_to_close_usd(book, SPOT, T, R), rel=1e-9)
    assert all(a["usd"] <= b["usd"] for a, b in zip(path, path[1:]))

def test_charm_pivot_is_the_sign_change_nearest_spot():
    assert charm_pivot({96.0: 5.0, 98.0: 3.0, 100.0: -2.0, 102.0: 0.0, 104.0: -1.0, 106.0: 4.0}, 101.0) == 99.0
    assert charm_pivot({96.0: 5.0, 98.0: 3.0}, 97.0) is None
