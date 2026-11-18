"""Call and put walls: the largest call GEX at or above spot and the largest put GEX at or below spot."""
from spx_trade_desk.market.gex import OptionData, compute_gex

SPOT = 100.0
TTE = 3.0 * 60 / (390.0 * 252.0)          # three hours left, in the GEX math's trading years
STRIKES = range(90, 111, 2)               # invented strikes, 2 apart


def _walls(call_oi=None, put_oi=None, spot=SPOT):
    """(call_wall, put_wall) over the invented strikes; ``call_oi`` / ``put_oi`` map strike -> open interest."""
    call_oi, put_oi = call_oi or {}, put_oi or {}
    options = []
    for strike in STRIKES:
        options.append(OptionData(strike=float(strike), right="C", implied_vol=0.2, open_interest=call_oi.get(strike, 0)))
        options.append(OptionData(strike=float(strike), right="P", implied_vol=0.2, open_interest=put_oi.get(strike, 0)))
    res = compute_gex(options, spot, time_to_expiry_years=TTE)
    return res.call_wall, res.put_wall


def test_walls_are_the_largest_call_above_and_put_below_spot():
    call_wall, put_wall = _walls(call_oi={102: 50000, 106: 20000}, put_oi={98: 50000, 94: 20000})
    assert (call_wall, put_wall) == (102.0, 98.0)


def test_an_in_the_money_put_above_spot_is_not_the_put_wall():
    # The biggest put GEX sits at 102, above spot: the put wall is the largest one below spot instead.
    _, put_wall = _walls(put_oi={102: 400000, 98: 50000, 94: 30000})
    assert put_wall == 98.0


def test_an_in_the_money_call_below_spot_is_not_the_call_wall():
    call_wall, _ = _walls(call_oi={98: 400000, 102: 50000, 106: 30000})
    assert call_wall == 102.0


def test_a_strike_at_spot_counts_for_both_walls():
    assert _walls(call_oi={100: 50000}, put_oi={100: 50000}) == (100.0, 100.0)


def test_a_side_with_no_gex_on_its_side_of_spot_has_no_wall():
    # Calls only below spot and puts only above it: nothing is resistance or support.
    assert _walls(call_oi={96: 50000}, put_oi={104: 50000}) == (None, None)
    assert _walls() == (None, None)                                  # no open interest at all


def test_a_wall_moves_when_spot_crosses_the_strike():
    oi = {"call_oi": {102: 50000, 98: 40000}, "put_oi": {98: 50000, 102: 40000}}
    assert _walls(spot=100.0, **oi) == (102.0, 98.0)
    assert _walls(spot=99.0, **oi) == (102.0, 98.0)
    # Spot above 102: the 102 call is now in the money and no longer a wall; the 98 call is further below.
    assert _walls(spot=103.0, **oi)[0] is None
    assert _walls(spot=103.0, **oi)[1] == 102.0                      # 102 is the nearest big put below spot
