"""Charm history: the per-minute dealer charm exposure by strike behind the price chart's heatmap."""
from spx_trade_desk.market.charm_history import CharmHistory, charm_exposure
from spx_trade_desk.market.gex import OptionData, compute_gex

SPOT = 100.0
TTE = 3.0 * 60 / (390.0 * 252.0)          # three hours left, in the GEX math's trading years
STRIKES = range(90, 111, 2)               # invented strikes, 2 apart: 2% of spot is about 2.3 sigma at this expiry


def _result(call_oi=None, put_oi=None, stamp="2099-01-05T11:30:10-05:00", expiration="20990105", spot=SPOT):
    """A GEX result over the invented strikes; ``call_oi`` / ``put_oi`` map strike -> open interest."""
    call_oi, put_oi = call_oi or {}, put_oi or {}
    options = []
    for strike in STRIKES:
        options.append(OptionData(strike=float(strike), right="C", implied_vol=0.2,
                                  open_interest=call_oi.get(strike, 0)))
        options.append(OptionData(strike=float(strike), right="P", implied_vol=0.2,
                                  open_interest=put_oi.get(strike, 0)))
    res = compute_gex(options, spot, time_to_expiry_years=TTE)
    res.expiration, res.timestamp = expiration, stamp
    return res


def test_otm_calls_and_otm_puts_are_supportive():
    exposure = charm_exposure(_result(call_oi={102: 100000}, put_oi={98: 100000}))
    assert exposure[102.0] > 0                     # the call's delta decays: dealers buy the hedge back
    assert exposure[98.0] > 0                      # the put's delta decays toward 0: same direction
    assert exposure[100.0] == 0.0                  # no open interest, no exposure


def test_in_the_money_options_are_suppressive():
    exposure = charm_exposure(_result(call_oi={98: 100000}, put_oi={102: 100000}))
    assert exposure[98.0] < 0 and exposure[102.0] < 0     # ITM delta grows toward 1: dealers sell more


def test_a_strike_without_a_charm_value_is_left_out():
    res = _result(call_oi={102: 100000})
    del res.call_charm_by_strike[102.0], res.put_charm_by_strike[102.0]
    assert 102.0 not in charm_exposure(res)


def test_a_frame_keeps_the_strikes_within_the_band_with_values_in_thousands():
    h = CharmHistory(band=0.04)
    assert h.record(_result(call_oi={102: 100000, 108: 100000})) is True
    frame = h.latest()
    assert frame["time"] == "2099-01-05T11:30"
    assert frame["strikes"] == [96.0, 98.0, 100.0, 102.0, 104.0]              # the rest is outside +-4%
    assert len(frame["values"]) == len(frame["strikes"]) and all(isinstance(v, int) for v in frame["values"])
    exposure = charm_exposure(_result(call_oi={102: 100000}))[102.0]
    assert frame["values"][frame["strikes"].index(102.0)] == round(exposure / 1000.0) > 0   # thousands of dollars


def test_a_later_publish_in_the_same_minute_replaces_the_frame_and_a_new_minute_adds_one():
    h = CharmHistory()
    h.record(_result(call_oi={102: 100000}, stamp="2099-01-05T11:30:10-05:00"))
    assert h.record(_result(call_oi={102: 200000}, stamp="2099-01-05T11:30:50-05:00")) is True
    assert len(h.frames()) == 1
    h.record(_result(call_oi={102: 200000}, stamp="2099-01-05T11:31:10-05:00"))
    assert [f["time"] for f in h.frames()] == ["2099-01-05T11:30", "2099-01-05T11:31"]


def test_a_new_expiration_starts_a_new_history_and_the_history_is_bounded():
    h = CharmHistory(maxlen=3)
    for minute in range(30, 35):
        h.record(_result(call_oi={102: 100000}, stamp=f"2099-01-05T11:{minute}:00-05:00"))
    assert [f["time"][-2:] for f in h.frames()] == ["32", "33", "34"]
    h.record(_result(call_oi={102: 100000}, stamp="2099-01-05T18:00:00-05:00", expiration="20990106"))
    assert [f["time"] for f in h.frames()] == ["2099-01-05T18:00"]


def test_nothing_is_recorded_without_a_timestamp_a_spot_or_any_strike_in_the_band():
    h = CharmHistory()
    assert h.record(None) is False
    assert h.record(_result(call_oi={102: 100000}, stamp="")) is False
    assert h.record(_result(call_oi={102: 100000}, spot=0.0)) is False
    assert CharmHistory(band=0.0001).record(_result(call_oi={102: 100000}, spot=101.0)) is False


def test_the_price_snapshot_carries_the_charm_history():
    from spx_trade_desk.core.app_state import create_app_state
    from spx_trade_desk.market.price_bars import snapshot_payload
    st = create_app_state()
    st.charm_history.record(_result(call_oi={102: 100000}))
    snap = snapshot_payload(st, rth=True, today="2099-01-05")
    assert snap["charm"] == [st.charm_history.latest()] and snap["charm"][0]["time"] == "2099-01-05T11:30"


def test_a_snapshot_sends_the_strike_list_only_when_it_changes():
    h = CharmHistory()
    h.record(_result(call_oi={102: 100000}, stamp="2099-01-05T11:30:00-05:00"))
    h.record(_result(call_oi={102: 90000}, stamp="2099-01-05T11:31:00-05:00"))
    h.record(_result(call_oi={102: 80000}, stamp="2099-01-05T11:32:00-05:00"))
    frames = h.snapshot_frames()
    assert [("strikes" in f) for f in frames] == [True, False, False]
    assert [f["values"] for f in frames] == [f["values"] for f in h.frames()]
    assert all(f["strikes"] == h.frames()[0]["strikes"] for f in h.frames())    # the axis they all share
    assert len(h.frames()) == 3                                                  # the stored history is whole
