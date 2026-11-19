"""Profile levels: zero gamma, regime, gamma nodes and voids from the dealer gamma profile."""
import time

import numpy as np
import pytest

from spx_trade_desk.market.dealer_model import positions_from_oi
from spx_trade_desk.market.gex import OptionData, compute_gex
from spx_trade_desk.market.levels import (
    find_nodes,
    find_voids,
    profile_levels,
    resolve_gamma_flip,
    zero_crossings,
)

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


def _lv(res):
    return profile_levels(res, positions_from_oi(res), TTE, R, em=None)


def test_zero_crossings_interpolate_and_ignore_zero_runs():
    grid = np.arange(6.0)
    assert zero_crossings(grid, np.array([-1.0, 1.0, 0.0, 0.0, 2.0, -2.0])) == pytest.approx([0.5, 4.5])
    assert zero_crossings(grid, np.array([1.0, 0.0, 0.0, 0.0, 0.0, 3.0])) == []
    assert zero_crossings(grid, np.array([-2.0, 0.0, 2.0, 3.0, 4.0, 5.0])) == pytest.approx([1.0])


def test_zero_gamma_lies_between_the_put_and_call_clusters():
    lv = _lv(_result(call_oi={102: 50000, 104: 50000}, put_oi={96: 50000, 98: 50000}))
    assert 98.0 < lv.zero_gamma < 102.0 and lv.zero_gamma in lv.zero_gamma_all


def test_regime_follows_the_sign_of_gamma_at_spot():
    oi = dict(call_oi={102: 50000, 104: 50000}, put_oi={96: 50000, 98: 50000})
    up, dn = _lv(_result(spot=103.0, **oi)), _lv(_result(spot=97.0, **oi))
    assert (up.regime, up.gamma_at_spot > 0) == ("long_gamma", True)
    assert (dn.regime, dn.gamma_at_spot < 0) == ("short_gamma", True)


def test_calls_only_has_no_crossing_and_reports_no_zero_gamma():
    res = _result(call_oi={102: 50000}); lv = _lv(res)
    assert lv.zero_gamma is None and lv.zero_gamma_all == [] and lv.regime == "long_gamma"
    assert resolve_gamma_flip(res, lv) == (None, "zero_gamma")


def test_a_crossing_beyond_the_sent_band_is_still_found():
    lv = _lv(_result(call_oi={108: 100000}, put_oi={98: 100000}))       # crossing near sqrt(98*108)
    assert 102.5 < lv.zero_gamma < 103.5
    last = lv.profile_from + lv.profile_step * (len(lv.profile_values) - 1)
    assert lv.profile_step == 2.5 and lv.profile_from >= 98.0 - 2.5 and last <= 102.0 + 2.5
    assert len(lv.hedge_values) == len(lv.profile_values)


def test_a_crossing_beyond_the_search_band_is_not_reported():
    lv = _lv(_result(call_oi={110: 100000}, put_oi={102: 100000}))      # crossing near 105.9, past +5%
    assert lv.zero_gamma is None and lv.regime == "short_gamma"


def test_no_time_or_no_iv_means_no_profile_and_the_cumulative_flip():
    res = _result(call_oi={102: 50000}, put_oi={98: 80000})
    assert profile_levels(res, positions_from_oi(res), 0.0, R, em=None) is None
    assert resolve_gamma_flip(res, None) == (res.gamma_flip, "cumulative")
    no_iv = _result(call_oi={102: 50000}, put_oi={98: 80000}, iv=None)
    assert profile_levels(no_iv, positions_from_oi(no_iv), TTE, R, em=None) is None


def _gmap(peaks):
    g = {float(k): 1.0e6 for k in range(90, 111, 2)}
    g.update({float(k): v for k, v in peaks.items()})
    return g


def test_a_large_positive_strike_above_spot_is_the_r1_pin():
    nodes = find_nodes(_gmap({104: 9.0e6}), 100.0, 94.0, 106.0, lambda x: -1e6 * (x - 100.0))
    assert [(n["label"], n["strike"], n["kind"], n["side"], n["rank"]) for n in nodes] == [("R1", 104.0, "pin", "above", 1)]
    assert nodes[0]["gex"] == 9.0e6 and nodes[0]["hedge_usd"] == -4.0e6


def test_a_large_negative_strike_below_spot_is_an_accel_node():
    nodes = find_nodes(_gmap({96: -9.0e6}), 100.0, 94.0, 106.0, lambda x: 0.0)
    assert [(n["label"], n["strike"], n["kind"]) for n in nodes] == [("S1", 96.0, "accel")]


def test_nodes_outside_the_range_are_left_out():
    assert find_nodes(_gmap({108: 9.0e6}), 100.0, 94.0, 106.0, lambda x: 0.0) == []


def test_the_prominence_filter_drops_a_shoulder():
    nodes = find_nodes(_gmap({100: 10.0e6, 102: 8.0e6, 104: 8.5e6}), 99.0, 94.0, 106.0, lambda x: 0.0)
    assert [n["strike"] for n in nodes] == [100.0]


def test_labels_run_nearest_first_and_only_the_top_three_per_side_are_kept():
    g = {float(k): 1.0e6 for k in range(80, 121)}
    g.update({99.0: 2.0e6, 97.0: 5.0e6, 95.0: 4.0e6, 93.0: 6.0e6})
    nodes = find_nodes(g, 100.5, 85.0, 115.0, lambda x: 0.0)
    assert [(n["label"], n["strike"], n["rank"]) for n in nodes] == [("S1", 97.0, 2), ("S2", 95.0, 3), ("S3", 93.0, 1)]


def test_a_quiet_run_between_two_nodes_is_a_void_and_a_one_strike_gap_is_not():
    g = {94.0: 8e6, 96.0: 0.2e6, 98.0: 0.1e6, 100.0: 0.3e6, 102.0: 9e6, 104.0: 0.5e6, 106.0: 7e6}
    assert find_voids(g, 93.0, 107.0) == [{"lo": 96.0, "hi": 100.0}]


def test_profile_levels_on_a_full_chain_is_fast_enough():
    # 300 strikes 6050..7545 step 5, OI 1000 per side, IV 0.15, spot 6800, TTE as above; one warm-up call first
    spot, t_expiry = 6800.0, TTE
    options = []
    for strike in range(6050, 7550, 5):
        options.append(OptionData(strike=float(strike), right="C", implied_vol=0.15, open_interest=1000))
        options.append(OptionData(strike=float(strike), right="P", implied_vol=0.15, open_interest=1000))
    res = compute_gex(options, spot, time_to_expiry_years=t_expiry, risk_free_rate=R)
    pos = positions_from_oi(res)
    profile_levels(res, pos, t_expiry, R, em=40.0)           # warm-up
    t0 = time.perf_counter()
    profile_levels(res, pos, t_expiry, R, em=40.0)
    assert time.perf_counter() - t0 < 0.06
