"""Dealer model core: positions, IV filling, vectorised greeks, gamma and delta curves, hedge flow."""
import numpy as np
import pytest

from spx_trade_desk.market.dealer_model import (
    IV_FLOOR,
    dealer_book,
    dealer_delta,
    fill_ivs,
    gamma_curve,
    hedge_curve,
    hedge_flow_usd,
    bsm_delta,
    bsm_gamma,
    positions_from_oi,
    strike_gex,
)
from spx_trade_desk.market.gex import OptionData, _bsm_delta, _bsm_gamma, compute_gex

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


def test_positions_from_oi_are_long_calls_and_short_puts():
    pos = positions_from_oi(_result(call_oi={102: 500}, put_oi={98: 700}))
    assert pos.model == "oi_assumed" and pos.calls == {102.0: 500.0} and pos.puts == {98.0: -700.0}

def test_missing_ivs_are_filled_linearly_inside_and_flat_outside():
    assert fill_ivs({100.0: 0.2, 104.0: 0.3}, [98.0, 100.0, 102.0, 106.0]) == pytest.approx(
        {98.0: 0.2, 100.0: 0.2, 102.0: 0.25, 106.0: 0.3})
    assert fill_ivs({}, [100.0]) == {}

def test_vectorised_greeks_match_the_scalar_bsm_helpers():
    S = np.array([[95.0], [100.0], [104.0]]); K = np.array([98.0, 100.0, 103.0]); iv = np.array([0.18, 0.2, 0.25])
    g = bsm_gamma(S, K, TTE, R, iv); d = bsm_delta(S, K, TTE, R, iv, np.array([True, True, False]))
    for i, s in enumerate(S[:, 0]):
        for j, right in enumerate("CCP"):
            assert g[i, j] == pytest.approx(_bsm_gamma(s, K[j], TTE, R, iv[j]), rel=1e-9)
            assert d[i, j] == pytest.approx(_bsm_delta(s, K[j], TTE, R, iv[j], right), abs=2e-7)

def test_gamma_curve_and_strike_gex_at_spot_equal_the_compute_gex_total():
    res = _result(call_oi={102: 50000, 104: 20000}, put_oi={98: 50000, 96: 20000})
    book = dealer_book(positions_from_oi(res), res)
    assert gamma_curve(book, np.array([SPOT]), TTE, R)[0] == pytest.approx(res.total_net_gex, rel=1e-9)
    assert sum(strike_gex(book, SPOT, TTE, R).values()) == pytest.approx(res.total_net_gex, rel=1e-9)

def test_dealer_delta_is_contracts_times_delta_times_the_multiplier():
    res = _result(call_oi={100: 10}); book = dealer_book(positions_from_oi(res), res)
    assert dealer_delta(book, np.array([SPOT]), TTE, R)[0] == pytest.approx(
        10 * 100 * _bsm_delta(SPOT, 100.0, TTE, R, 0.2, "C"), rel=1e-6)

def test_long_gamma_hedge_flow_sells_rallies_and_buys_dips():
    res = _result(call_oi={100: 100000}); book = dealer_book(positions_from_oi(res), res)
    assert hedge_flow_usd(book, SPOT, 102.0, TTE, R) < 0 < hedge_flow_usd(book, SPOT, 98.0, TTE, R)
    assert hedge_flow_usd(book, SPOT, SPOT, TTE, R) == 0.0
    assert hedge_curve(book, SPOT, np.array([98.0, 102.0]), TTE, R) == pytest.approx(
        [hedge_flow_usd(book, SPOT, 98.0, TTE, R), hedge_flow_usd(book, SPOT, 102.0, TTE, R)])

def test_a_book_without_positions_or_ivs_is_none():
    assert dealer_book(positions_from_oi(_result()), _result()) is None
    res = _result(call_oi={102: 100}, iv=None)
    assert dealer_book(positions_from_oi(res), res) is None

def test_iv_shift_raises_an_otm_call_delta_and_lowers_an_itm_call_delta():
    otm = _result(call_oi={102: 100}); otm_book = dealer_book(positions_from_oi(otm), otm)
    up, base, down = (dealer_delta(otm_book, np.array([SPOT]), TTE, R, iv_shift=s)[0] for s in (0.05, 0.0, -0.05))
    assert down < base < up
    itm = _result(call_oi={98: 100}); itm_book = dealer_book(positions_from_oi(itm), itm)
    assert dealer_delta(itm_book, np.array([SPOT]), TTE, R, iv_shift=0.05)[0] < \
        dealer_delta(itm_book, np.array([SPOT]), TTE, R)[0]

def test_a_very_large_negative_iv_shift_stays_finite_because_iv_is_floored():
    res = _result(call_oi={100: 1000, 102: 1000}, put_oi={98: 1000}); book = dealer_book(positions_from_oi(res), res)
    spots = np.array([95.0, 100.0, 105.0])
    floored = dealer_delta(book, spots, TTE, R, iv_shift=-10.0)
    assert np.isfinite(floored).all()
    assert floored == pytest.approx(dealer_delta(book, spots, TTE, R, iv_shift=IV_FLOOR - 0.2), rel=1e-6)


def test_flow_blend_takes_the_other_side_of_todays_customer_flow():
    from spx_trade_desk.market.dealer_model import DealerPositions, positions_flow_blend
    base = DealerPositions(calls={100.0: 1000.0}, puts={100.0: -800.0}, model="oi_assumed")
    out = positions_flow_blend(base, {100.0: 30}, {100.0: -20, 105.0: 5})
    assert (out.calls, out.puts, out.model) == ({100.0: 970.0}, {100.0: -780.0, 105.0: -5.0}, "flow_blend")


def test_flow_blend_without_any_net_flow_keeps_the_oi_positions():
    from spx_trade_desk.market.dealer_model import DealerPositions, positions_flow_blend
    base = DealerPositions(calls={100.0: 1000.0}, puts={100.0: -800.0}, model="oi_assumed")
    out = positions_flow_blend(base, {}, {})
    assert (out.calls, out.puts, out.model) == ({100.0: 1000.0}, {100.0: -800.0}, "flow_blend")
