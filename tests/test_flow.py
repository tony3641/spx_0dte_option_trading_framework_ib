"""market/flow.py: customer trade classification and the per-key / per-minute flow tracker.

Invented data only: strikes 100, expiry 2099-01-05, quotes around 1.00 / 1.40.
"""
from datetime import datetime, timedelta

import pytest

from spx_trade_desk.market.chain_manager import _subscribe_new_keys, record_flow
from spx_trade_desk.market.dealer_model import strike_gammas
from spx_trade_desk.market.flow import (
    FlowTracker,
    VolumeHistory,
    annotate_gex_bars,
    classify_trade,
    flow_gex_by_strike,
    hot_strikes,
)
from spx_trade_desk.market.gex import GEXResult, OptionData, _bsm_gamma
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


def test_hot_strikes_need_volume_over_oi_minimum_volume_and_a_top_n_rank():
    opts = [OptionData(100.0, "C", volume=700, open_interest=500), OptionData(100.0, "P", volume=500, open_interest=500),   # 1.2
            OptionData(105.0, "C", volume=600, open_interest=400),                                                       # 1.5
            OptionData(110.0, "C", volume=400, open_interest=100),                                                       # 4.0 but < 500
            OptionData(95.0, "P", volume=5000, open_interest=10000)]                                                     # 0.5
    hot = hot_strikes(opts, {100.0: 200, 105.0: None})
    assert {k for k, v in hot.items() if v["hot"]} == {100.0, 105.0}
    assert hot[100.0] == {"vol_oi": 1.2, "hot": True, "vol_15m": 200}
    assert {k for k, v in hot_strikes(opts, {}, top_n=2).items() if v["hot"]} == {100.0}       # 95 and 100 are the top 2 by volume


def test_no_strike_is_hot_while_open_interest_is_unreported():                   # Review Focus 5
    assert hot_strikes([OptionData(100.0, "C", volume=9000, open_interest=0)], {}) == {}


def test_volume_history_reports_what_was_added_in_the_window():
    h = VolumeHistory()
    h.record(E, "2099-01-05T10:00:00-05:00", [OptionData(100.0, "C", volume=600), OptionData(100.0, "P", volume=400)])
    h.record(E, "2099-01-05T10:15:00-05:00", [OptionData(100.0, "C", volume=1000), OptionData(100.0, "P", volume=600)])
    assert h.added(100.0, 15) == 600
    assert h.added(100.0, 30) is None
    assert h.added(105.0, 15) is None


def test_customers_buying_calls_make_flow_gex_negative():
    fgex = flow_gex_by_strike({100.0: 10}, {}, ({100.0: 0.05}, {}), 100.0)
    assert fgex[100.0] == pytest.approx(-10 * 0.05 * 100.0 ** 2)
    assert fgex[100.0] < 0


def test_bars_are_annotated_only_where_data_exists():
    t = FlowTracker()
    _cycle(t, [_row(100, "C", 1000, 1.00, 1.40, 1.20)], 0)
    _cycle(t, [_row(100, "C", 1010, 1.40, 1.80, 1.40)], 1)              # 10 calls bought at the ask
    bars = [{"strike": 100.0, "call_gex": 1.0}, {"strike": 105.0, "call_gex": 2.0}]
    hot = {100.0: {"vol_oi": 1.2, "hot": True, "vol_15m": 200}}
    annotate_gex_bars(bars, t, hot, {100.0: -5.0})
    b100, b105 = bars
    assert (b100["call_buy"], b100["call_sell"], b100["call_net_delta_usd"]) == (10, 0, 50000)
    assert (b100["flow_gex"], b100["vol_oi"], b100["hot"], b100["vol_15m"]) == (-5.0, 1.2, True, 200)
    assert "put_buy" not in b100 and "put_net_delta_usd" not in b100
    assert b105 == {"strike": 105.0, "call_gex": 2.0}


def test_strike_gammas_are_bsm_gamma_at_spot_from_the_filled_ivs():
    res = GEXResult(strikes=[100.0], call_iv_by_strike={100.0: 0.20}, put_iv_by_strike={100.0: 0.25})
    calls, puts = strike_gammas(res, 100.0, 0.1, 0.04)
    assert calls[100.0] == pytest.approx(_bsm_gamma(100.0, 100.0, 0.1, 0.04, 0.20))
    assert puts[100.0] == pytest.approx(_bsm_gamma(100.0, 100.0, 0.1, 0.04, 0.25))
    assert strike_gammas(res, 100.0, 0.0, 0.04) == ({}, {})


def test_summary_windows_across_several_minutes_and_a_minute_rollover():
    t = FlowTracker()
    keys = {(100.0, "C"), (100.0, "P")}

    def at(hm, rows):
        h, m = map(int, hm.split(":"))
        t.update(rows, keys, 100.0, datetime(2099, 1, 5, h, m, 0, tzinfo=ET), E)

    def pair(c, p, cl, pl):   # calls and puts at volume c / p, quotes 1.00 / 1.40, last cl / pl
        return [_row(100, "C", c, 1.00, 1.40, cl, delta=0.5), _row(100, "P", p, 1.00, 1.40, pl, delta=-0.5)]

    at("10:00", pair(1000, 1000, 1.2, 1.2))
    at("10:00", pair(1010, 1000, 1.4, 1.2))     # 10 calls bought at the ask: +50,000 in 10:00
    at("10:05", pair(1010, 1010, 1.4, 1.0))     # 10 puts sold at the bid: +50,000 in 10:05
    at("10:10", pair(1030, 1010, 1.4, 1.0))     # 20 calls bought: +100,000 in 10:10
    at("10:20", pair(1030, 1020, 1.4, 1.0))     # 10 puts sold: +50,000 in 10:20
    assert [m["time"] for m in t.minutes()] == [
        "2099-01-05T10:00", "2099-01-05T10:05", "2099-01-05T10:10", "2099-01-05T10:20"]
    s = t.summary(datetime(2099, 1, 5, 10, 20, 30, tzinfo=ET))
    assert s["hedge_pressure_15m_usd"] == 200000             # cutoff 10:05, bucket-inclusive: 10:05 counts
    assert s["hedge_pressure_30m_usd"] == 250000             # cutoff 09:50: every minute
    assert s["hedge_pressure_day_usd"] == 250000
    assert (s["call_delta_day_usd"], s["put_delta_day_usd"]) == (150000, 100000)
    assert s["minute"]["time"] == "2099-01-05T10:20" and s["minute"]["hedge_pressure_usd"] == 50000
    s2 = t.summary(datetime(2099, 1, 5, 10, 25, 0, tzinfo=ET))
    assert s2["hedge_pressure_15m_usd"] == 150000            # cutoff 10:10
    assert s2["hedge_pressure_30m_usd"] == 250000            # cutoff 09:55


def test_an_empty_tracker_summary_has_no_minute_time_and_no_coverage_minute_keys():
    s = FlowTracker().summary(T0)
    assert s["minute"]["time"] is None and s["coverage_start"] is None
    assert not {"coverage_lo", "coverage_hi"} & set(s["minute"])
    assert s["hedge_pressure_day_usd"] == 0


def test_a_failing_flow_tracker_never_raises_into_the_stream_loop(app_state, caplog):   # Review Focus 4
    class Boom:
        def update(self, *a): raise RuntimeError("boom")
    app_state.flow_tracker = Boom()
    record_flow(app_state, [], datetime(2099, 1, 5, 10, 0, tzinfo=ET))
    record_flow(app_state, [], datetime(2099, 1, 5, 10, 0, 1, tzinfo=ET))
    assert sum("Flow tracking failed" in r.message for r in caplog.records) == 1


def test_record_flow_passes_the_streamed_keys_from_the_stream_dict(app_state):
    seen = {}

    class Spy:
        def update(self, rows, keys, spot, now, expiration):
            seen.update(rows=rows, keys=keys, spot=spot, expiration=expiration)

    app_state.flow_tracker = Spy()
    app_state.chain_stream_tickers = {(100.0, "C"): object(), (101.0, "P"): object()}
    app_state.spx_price, app_state.expiration = 100.5, E
    rows = [_row(100, "C", 5, 1.0, 1.4, 1.2)]
    record_flow(app_state, rows, T0)
    assert seen == {"rows": rows, "keys": {(100.0, "C"), (101.0, "P")}, "spot": 100.5, "expiration": E}
