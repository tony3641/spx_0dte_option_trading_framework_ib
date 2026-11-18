"""SignalTracker: zero-gamma crosses, node hold/break, EM touches, flow surge/divergence, vanna and charm.

All bars, flow minutes and dealer blocks are invented (2099 dates, strikes around 100).
"""

from datetime import datetime, timedelta

from spx_trade_desk.market.hours import ET
from spx_trade_desk.market.signals import SignalTracker

E = "20990105"
DEALER = {"zero_gamma": 100.0, "nodes": [{"label": "S1", "strike": 95.0, "kind": "pin"},
                                         {"label": "R1", "strike": 105.0, "kind": "pin"}],
          "em": {"high": 110.0, "low": 90.0}}


def _bar(hhmm, o, h, l, c):
    return {"time": f"2099-01-05T{hhmm}:00-05:00", "open": o, "high": h, "low": l, "close": c}


def _feed(tr, bars, dealer, flow=()):
    out = []
    for i, b in enumerate(bars):
        out += tr.on_bar_closed(E, b, bars[:i], dealer, list(flow))
    return out


def test_zero_gamma_cross_up_and_down():
    bars = [_bar("10:00", 99, 99.5, 98.5, 99), _bar("10:01", 99, 101, 99, 100.5), _bar("10:02", 100.5, 100.6, 99, 99.2)]
    assert [(s["type"], s["side"], s["text"], s["time"]) for s in _feed(SignalTracker(), bars, DEALER)] == [
        ("zg_cross", "bull", "ZG up", "2099-01-05T10:01"), ("zg_cross", "bear", "ZG dn", "2099-01-05T10:02")]


def test_a_support_touch_then_a_bounce_is_a_hold():
    bars = [_bar("10:00", 98, 98.5, 97.5, 98), _bar("10:01", 98, 98, 96.0, 96.5),
            _bar("10:02", 96.5, 97.5, 96.2, 97.0), _bar("10:03", 97, 98.5, 96.8, 98.2)]
    sig = [s for s in _feed(SignalTracker(), bars, DEALER) if s["type"].startswith("node")]
    assert [(s["type"], s["side"], s["text"], s["time"]) for s in sig] == [("node_hold", "bull", "S1", "2099-01-05T10:03")]


def test_three_closes_beyond_a_support_are_a_break():
    bars = [_bar("10:00", 97, 97.5, 96.5, 97), _bar("10:01", 97, 97, 91.5, 91.8), _bar("10:02", 91.8, 92, 91, 91.5),
            _bar("10:03", 91.5, 92, 90.5, 91.0)]
    sig = [s for s in _feed(SignalTracker(), bars, DEALER) if s["type"].startswith("node")]
    assert [(s["type"], s["side"], s["text"]) for s in sig] == [("node_break", "bear", "S1 brk")]


def test_a_touch_that_chops_is_no_signal():
    bars = [_bar("10:00", 98, 98.5, 97.5, 98), _bar("10:01", 98, 98, 96, 96.5), _bar("10:02", 96.5, 97, 95.5, 96.8),
            _bar("10:03", 96.8, 97.2, 96, 96.4), _bar("10:04", 96.4, 97, 95.8, 96.9)]
    assert [s for s in _feed(SignalTracker(), bars, DEALER) if s["type"].startswith("node")] == []


def test_the_same_signal_is_suppressed_for_the_cooldown():
    closes = [99, 101, 99, 101, 99, 101, 99, 101]
    bars = [_bar(f"10:{m:02d}", 100, 101.5, 98.5, c) for m, c in enumerate(closes)]
    assert [(s["text"], s["time"][-5:]) for s in _feed(SignalTracker(), bars, DEALER)] == [
        ("ZG up", "10:01"), ("ZG dn", "10:02"), ("ZG up", "10:07")]


def test_reaching_the_open_band_is_an_em_touch_once():
    bars = [_bar("10:00", 108, 109, 107.5, 108.5), _bar("10:01", 108.5, 110.2, 108, 109.5), _bar("10:02", 109.5, 110.5, 109, 110)]
    sig = [s for s in _feed(SignalTracker(), bars, DEALER) if s["type"] == "em_touch"]
    assert [(s["text"], s["side"], s["time"][-5:]) for s in sig] == [("EM+", "neutral", "10:01")]


def test_a_flow_spike_is_a_surge():
    flow = [{"time": f"2099-01-05T10:{m:02d}", "hedge_pressure_usd": v} for m, v in enumerate([1e6, -1e6] * 10 + [5e7])]
    bars = [_bar(f"10:{m:02d}", 99, 99.5, 98.5, 99) for m in range(21)]
    sig = [s for s in _feed(SignalTracker(), bars, DEALER, flow) if s["type"] == "flow_surge"]
    assert [(s["side"], s["text"], s["time"][-5:]) for s in sig] == [("bull", "F+", "10:20")]


def test_a_new_high_on_negative_hedge_pressure_is_a_divergence():
    bars = [_bar(f"10:{m:02d}", 99, 100, 98.5, 99) for m in range(31)] + [_bar("10:31", 99, 101, 99, 99.5)]
    flow = [{"time": f"2099-01-05T10:{m:02d}", "hedge_pressure_usd": -1e6} for m in range(32)]
    sig = [s for s in _feed(SignalTracker(), bars, DEALER, flow) if s["type"] == "flow_div"]
    assert [(s["side"], s["text"], s["time"][-5:]) for s in sig] == [("bear", "DIV", "10:31")]


def _dealer(iv15=None, flow_est=0.0, to_close=0.0):
    return {**DEALER, "vanna": {"iv_change_15m": iv15, "flow_est_usd": flow_est}, "charm": {"to_close_usd": to_close}}


def test_an_iv_drop_with_supportive_vanna_is_v_plus_once_per_minute():
    tr, now = SignalTracker(), datetime(2099, 1, 5, 11, 0, 10, tzinfo=ET)
    a = tr.on_publish(E, now, _dealer(-1.6, 3e7), 100.0)
    b = tr.on_publish(E, now + timedelta(seconds=10), _dealer(-1.7, 3.2e7), 100.0)
    assert [(s["type"], s["side"], s["text"], s["time"]) for s in a] == [("vanna_shift", "bull", "V+", "2099-01-05T11:00")] and b == []


def test_an_iv_rise_with_supportive_vanna_is_v_minus_and_a_small_move_is_nothing():
    tr, now = SignalTracker(), datetime(2099, 1, 5, 11, 0, 10, tzinfo=ET)
    a = tr.on_publish(E, now, _dealer(1.6, -3e7), 100.0)
    b = tr.on_publish(E, now + timedelta(minutes=1), _dealer(-1.0, 2e7), 100.0)
    assert [(s["type"], s["side"], s["text"], s["time"]) for s in a] == [("vanna_shift", "bear", "V-", "2099-01-05T11:00")] and b == []


def test_the_charm_window_fires_once_at_14_00():
    tr = SignalTracker()
    assert tr.on_publish(E, datetime(2099, 1, 5, 13, 59, 50, tzinfo=ET), _dealer(to_close=2.5e8), 100.0) == []
    first = tr.on_publish(E, datetime(2099, 1, 5, 14, 0, 5, tzinfo=ET), _dealer(to_close=2.5e8), 100.0)
    assert [(s["type"], s["side"], s["text"]) for s in first] == [("charm_window", "bull", "C+ $250M")]
    assert tr.on_publish(E, datetime(2099, 1, 5, 14, 30, tzinfo=ET), _dealer(to_close=2.5e8), 100.0) == []


def test_the_charm_window_is_quiet_below_the_minimum_and_on_another_days_expiry():   # Review Focus 3
    tr = SignalTracker()
    assert tr.on_publish(E, datetime(2099, 1, 5, 14, 0, tzinfo=ET), _dealer(to_close=5e7), 100.0) == []
    assert tr.on_publish(E, datetime(2099, 1, 5, 14, 10, tzinfo=ET), _dealer(to_close=5e8), 100.0) == []   # once per session
    assert SignalTracker().on_publish("20990106", datetime(2099, 1, 5, 14, 0, tzinfo=ET), _dealer(to_close=5e8), 100.0) == []


def test_a_new_expiration_clears_the_history():
    tr = SignalTracker()
    _feed(tr, [_bar("10:00", 99, 99.5, 98.5, 99), _bar("10:01", 99, 101, 99, 100.5)], DEALER)
    assert [s["text"] for s in tr.history()] == ["ZG up"]
    tr.on_bar_closed("20990106", _bar("10:00", 99, 99.5, 98.5, 99), [], DEALER, [])
    assert tr.history() == []
    new = tr.on_bar_closed("20990106", _bar("10:01", 99, 101, 99, 100.5), [_bar("10:00", 99, 99.5, 98.5, 99)], DEALER, [])
    assert [(s["text"], s["time"]) for s in new] == [("ZG up", "2099-01-05T10:01")]
    assert tr.history() == new


def test_every_dealer_rule_is_a_no_op_on_null_inputs():
    tr = SignalTracker()
    bars = [_bar("10:00", 99, 99.5, 98.5, 99), _bar("10:01", 99, 101, 99, 100.5)]
    assert _feed(tr, bars, None) == []
    empty = {"zero_gamma": None, "nodes": [], "em": {"high": None, "low": None},
             "vanna": {"iv_change_15m": None, "flow_est_usd": None}, "charm": {"to_close_usd": None}}
    assert _feed(tr, bars, empty) == []
    now = datetime(2099, 1, 5, 14, 30, tzinfo=ET)
    assert tr.on_publish(E, now, None, 100.0) == []
    assert tr.on_publish(E, now, empty, 100.0) == []
    assert tr.history() == []


def test_a_node_without_a_label_is_named_by_its_strike_as_a_string():   # carry-over F
    whole = [_bar("10:00", 98, 98.5, 97.5, 98), _bar("10:01", 98, 98, 96.0, 96.5),
             _bar("10:02", 96.5, 97.5, 96.2, 97.0), _bar("10:03", 97, 98.5, 96.8, 98.2)]
    half = [_bar("10:00", 98, 98.5, 97.5, 98), _bar("10:01", 98, 98, 96.5, 96.8),
            _bar("10:02", 96.8, 99, 96.6, 98.7)]
    for strike, bars, text in ((95.0, whole, "95"), (95.5, half, "95.5")):
        dealer = {**DEALER, "nodes": [{"strike": strike, "kind": "pin"}]}
        sig = [s for s in _feed(SignalTracker(), bars, dealer) if s["type"] == "node_hold"]
        assert [s["text"] for s in sig] == [text]
