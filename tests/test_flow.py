"""market/flow.py: customer trade classification and the per-key / per-minute flow tracker.

Invented data only: strikes 100, expiry 2099-01-05, quotes around 1.00 / 1.40.
"""
from datetime import datetime, timedelta

import pytest

from spx_trade_desk.market.chain_manager import _subscribe_new_keys
from spx_trade_desk.market.flow import FlowTracker, classify_trade
from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.hours import ET

E = "20990105"
T0 = datetime(2099, 1, 5, 10, 0, tzinfo=ET)


def _row(strike, right, vol, bid, ask, last, delta=0.5):
    return OptionData(strike=strike, right=right, volume=vol, bid=bid, ask=ask, last=last, delta=delta)


def _cycle(t, rows, i, keys=None, spot=100.0):
    streamed = keys if keys is not None else {(r.strike, r.right) for r in rows}
    t.update(rows, streamed, spot, T0 + timedelta(seconds=i), E)


@pytest.mark.parametrize("last,bid,ask,prev_last,side", [
    (1.40, 1.00, 1.40, None, 1), (1.00, 1.00, 1.40, None, -1), (1.30, 1.00, 1.40, None, 1), (1.10, 1.00, 1.40, None, -1),
    (1.20, 1.00, 1.40, 1.15, 1), (1.20, 1.00, 1.40, 1.25, -1), (1.20, 1.00, 1.40, 1.20, 0), (None, 1.00, 1.40, 1.20, 0)])
def test_classify_trade(last, bid, ask, prev_last, side):
    assert classify_trade(last, bid, ask, prev_last) == side


def test_the_first_sighting_only_sets_a_baseline():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 5000, 1.00, 1.40, 1.40)], 0)
    kf = t.keys()[(100.0, "C")]
    assert (kf.buy_vol, kf.sell_vol, kf.unclassified_vol) == (0, 0, 0)
    assert kf.last_volume == 5000


def test_a_volume_increase_is_classified_against_the_previous_quote():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 5000, 1.00, 1.40, 1.20)], 0)
    _cycle(t, [_row(100, "C", 5010, 1.40, 1.80, 1.40)], 1)              # at the old ask, at the new bid
    kf = t.keys()[(100.0, "C")]
    assert (kf.buy_vol, kf.sell_vol, kf.net_delta_usd) == (10, 0, 50000.0)


def test_a_resubscribed_stream_adds_only_new_volume():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 5000, 1.00, 1.40, 1.20)], 0)
    _cycle(t, [], 1, keys=set())                                        # gap: key not streamed
    _cycle(t, [_row(100, "C", 5000, 1.00, 1.40, 1.20)], 2)              # same cumulative volume
    kf = t.keys()[(100.0, "C")]
    assert (kf.buy_vol, kf.sell_vol, kf.unclassified_vol) == (0, 0, 0)
    _cycle(t, [_row(100, "C", 5030, 1.40, 1.80, 1.40)], 3)              # 30 at the ask
    assert (kf.buy_vol, kf.sell_vol) == (30, 0)


def test_volume_that_traded_during_a_gap_is_counted_unclassified():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 5000, 1.00, 1.40, 1.20)], 0)
    _cycle(t, [], 1, keys=set())
    _cycle(t, [_row(100, "C", 5200, 1.00, 1.40, 1.40)], 2)
    kf = t.keys()[(100.0, "C")]
    assert (kf.unclassified_vol, kf.buy_vol, kf.sell_vol) == (200, 0, 0)


def test_a_fresh_stream_with_no_volume_yet_never_counts_the_days_volume():      # Review Focus 2
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 0, 1.0, 1.4, None)], 0)
    _cycle(t, [_row(100, "C", 5000, 1.0, 1.4, 1.2)], 1)
    _cycle(t, [_row(100, "C", 5005, 1.0, 1.4, 1.4)], 2)
    kf = t.keys()[(100.0, "C")]
    assert (kf.buy_vol, kf.sell_vol, kf.unclassified_vol) == (5, 0, 0)
    _cycle(t, [], 3, keys=set())                                        # unsubscribed
    _cycle(t, [_row(100, "C", 0, 1.0, 1.4, None)], 4)                   # resubscribed, no volume tick yet
    _cycle(t, [_row(100, "C", 5005, 1.0, 1.4, 1.4)], 5)
    assert (kf.buy_vol, kf.unclassified_vol) == (5, 0)


def test_a_volume_drop_resets_the_baseline_without_classifying():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 5000, 1.00, 1.40, 1.20)], 0)
    _cycle(t, [_row(100, "C", 100, 1.00, 1.40, 1.40)], 1)               # data reset: no classification
    kf = t.keys()[(100.0, "C")]
    assert (kf.buy_vol, kf.sell_vol, kf.unclassified_vol) == (0, 0, 0)
    assert kf.last_volume == 100
    _cycle(t, [_row(100, "C", 110, 1.40, 1.80, 1.40)], 2)               # 10 at the ask
    assert kf.buy_vol + kf.sell_vol + kf.unclassified_vol == 10


def test_a_new_expiration_clears_everything():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 5000, 1.00, 1.40, 1.20), _row(101, "C", 4000, 1.00, 1.40, 1.20)], 0)
    _cycle(t, [_row(101, "C", 4010, 1.40, 1.80, 1.40)], 1)
    assert t.minutes() and t.coverage_start == "10:00"
    t.update([_row(100, "C", 7000, 1.00, 1.40, 1.20)], {(100.0, "C")}, 100.0, T0 + timedelta(seconds=2),
             "20990106")
    assert set(t.keys()) == {(100.0, "C")}
    kf = t.keys()[(100.0, "C")]
    assert (kf.last_volume, kf.buy_vol, kf.sell_vol) == (7000, 0, 0)
    assert t.minutes()[0]["call_delta_usd"] == 0 and len(t.minutes()) == 1
    assert t.coverage_start == "10:00"


def test_customer_delta_and_hedge_pressure_signs():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 1000, 1.0, 1.4, 1.2), _row(100, "P", 1000, 1.0, 1.4, 1.2, delta=-0.4)], 0)
    _cycle(t, [_row(100, "C", 1010, 1.0, 1.4, 1.4), _row(100, "P", 1010, 1.0, 1.4, 1.4, delta=-0.4)], 1)   # buy 10 calls, buy 10 puts
    _cycle(t, [_row(100, "P", 1015, 1.0, 1.4, 1.0, delta=-0.4)], 2, keys={(100.0, "C"), (100.0, "P")})     # sell 5 puts
    m = t.minutes()[-1]
    assert (m["time"], m["call_delta_usd"], m["put_delta_usd"], m["hedge_pressure_usd"]) == ("2099-01-05T10:00", 50000, -20000, 30000)
    s = t.summary(T0 + timedelta(seconds=3))
    assert (s["hedge_pressure_day_usd"], s["classified_pct"], s["coverage"], s["coverage_start"]) == (30000, 100.0, {"lo": 100.0, "hi": 100.0}, "10:00")


@pytest.mark.asyncio
async def test_chain_stream_subscriptions_still_ask_only_for_generic_tick_101(app_state):   # no new IB data
    calls = []

    class FakeIB:
        async def subscribe_tick_paced(self, contract, generic, share):
            calls.append((generic, share))
            return object()

    keys = [(100.0, "C"), (100.0, "P")]
    assert await _subscribe_new_keys(FakeIB(), app_state, {k: object() for k in keys}, keys) == 2
    assert calls == [("101", "stream"), ("101", "stream")]
