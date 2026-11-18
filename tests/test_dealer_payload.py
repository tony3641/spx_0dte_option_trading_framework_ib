"""Dealer payload: the gex message's ``dealer`` block built from one GEX result (invented strikes only)."""
from datetime import datetime

import pytest

from spx_trade_desk.market.atm import AtmIvHistory, ExpectedMoveAnchor
from spx_trade_desk.market.chain_publisher import tte_years
from spx_trade_desk.market.dealer_model import dealer_book, hedge_flow_usd, positions_from_oi
from spx_trade_desk.market.dealer_payload import build_dealer, level_fields
from spx_trade_desk.market.gex import OptionData, compute_gex
from spx_trade_desk.market.hours import ET

SPOT = 7700.0
RATE = 0.043
EXPIRATION = "20261005"
OPEN = datetime(2026, 10, 5, 10, 0, tzinfo=ET)


def _chain(iv=0.15):
    """Calls with OI above 7700, puts with OI below it; the 7700 straddle mids to 20.0."""
    opts = []
    for k in range(7600, 7810, 10):
        for right in ("C", "P"):
            oi = 1000 if (right == "C" and k > 7700) or (right == "P" and k < 7700) else 0
            bid, ask = (9.8, 10.2) if k == 7700 else (1.0, 1.2)
            opts.append(OptionData(k, right, implied_vol=iv, open_interest=oi, bid=bid, ask=ask))
    return opts


def _build(options, now=OPEN, anchor=None, iv_history=None, bars=(), session="2026-10-05"):
    T = tte_years(EXPIRATION, now)
    gex = compute_gex(options, SPOT, time_to_expiry_years=T, risk_free_rate=RATE)
    gex.expiration = EXPIRATION
    gex.timestamp = now.isoformat()
    pos = positions_from_oi(gex)
    anchor = anchor if anchor is not None else ExpectedMoveAnchor()
    iv_history = iv_history if iv_history is not None else AtmIvHistory()
    dealer, levels = build_dealer(gex, options, pos, T, RATE, now, anchor, iv_history, bars, session)
    return dealer, levels, gex, pos, T, anchor, iv_history


def test_a_clear_peak_becomes_a_pin_node_whose_hedge_is_the_model_value():
    dealer, _, gex, pos, T, _, _ = _build(_chain())
    assert dealer["nodes"], "a chain with a clear call peak above spot must yield nodes"
    node = next(n for n in dealer["nodes"] if n["label"] == "R1")
    assert node["kind"] == "pin" and node["strike"] == 7710.0 and node["side"] == "above"
    expected = int(round(hedge_flow_usd(dealer_book(pos, gex), SPOT, node["strike"], T, RATE)))
    assert node["hedge_usd"] == expected


def test_payload_values_are_rounded_as_specified():
    dealer, levels, *_ = _build(_chain())
    assert all(isinstance(v, int) for v in dealer["profile"]["values"])
    assert all(isinstance(v, int) for v in dealer["profile"]["hedge"])
    assert isinstance(dealer["gamma_at_spot"], int)
    assert isinstance(dealer["charm"]["flow_15m_usd"], int)
    assert dealer["zero_gamma"] == round(dealer["zero_gamma"], 2)
    assert dealer["profile"]["from"] == round(dealer["profile"]["from"], 2)
    assert dealer["em"]["live"] == pytest.approx(20.0)
    assert dealer["em"]["open"] == pytest.approx(20.0) and dealer["em"]["open_spot"] == SPOT
    assert dealer["em"]["anchor_time"] == "10:00"
    assert len(dealer["profile"]["values"]) == len(dealer["profile"]["hedge"]) == len(levels.profile_values)


def test_before_the_open_band_exists_the_anchor_fields_are_none_and_nothing_crashes():
    early = datetime(2026, 10, 5, 9, 0, tzinfo=ET)                     # before 09:31: no anchor yet
    dealer, levels, *_ = _build(_chain(), now=early)
    assert levels is not None
    assert dealer["em"]["open"] is None and dealer["em"]["open_spot"] is None
    assert dealer["em"]["anchor_time"] is None
    assert dealer["em"]["high"] is None and dealer["em"]["low"] is None and dealer["em"]["used_pct"] is None


def test_em_high_and_low_are_the_anchored_open_band_and_match_the_level_fields():
    bars = [{"time": "2026-10-05T10:00:00-04:00", "high": 7725.0, "low": 7690.0},
            {"time": "2026-10-05T10:01:00-04:00", "high": 7712.0, "low": 7695.0},
            {"time": "2026-10-02T10:01:00-04:00", "high": 9999.0, "low": 1.0}]   # another day: ignored
    dealer, *_ = _build(_chain(), bars=bars)
    # open spot 7700 +/- open straddle 20.0: the band, not the session range
    assert dealer["em"]["high"] == 7720.0 and dealer["em"]["low"] == 7680.0
    fields = level_fields(dealer)
    assert dealer["em"]["high"] == fields["em_high"] and dealer["em"]["low"] == fields["em_low"]


def test_used_pct_is_the_session_range_over_the_open_band_width():
    bars = [{"time": "2026-10-05T10:00:00-04:00", "high": 7725.0, "low": 7690.0},
            {"time": "2026-10-05T10:01:00-04:00", "high": 7712.0, "low": 7695.0},
            {"time": "2026-10-02T10:01:00-04:00", "high": 9999.0, "low": 1.0}]   # another day: ignored
    dealer, *_ = _build(_chain(), bars=bars)
    assert dealer["em"]["used_pct"] == pytest.approx((7725.0 - 7690.0) / (2 * 20.0) * 100.0, abs=0.01)


def test_level_fields_read_the_open_band_and_the_nearest_pins():
    dealer, *_ = _build(_chain())
    fields = level_fields(dealer)
    assert fields["em_high"] == 7720.0 and fields["em_low"] == 7680.0
    assert fields["node_r1"] == 7710.0
    assert fields["node_s1"] is None or fields["node_s1"] < SPOT


def test_level_fields_are_all_none_without_a_dealer_block():
    assert level_fields(None) == {"em_high": None, "em_low": None, "node_r1": None, "node_s1": None}


def test_level_fields_keep_the_open_band_from_the_anchor_when_the_dealer_block_is_missing():
    anchor = ExpectedMoveAnchor()
    anchor.update(EXPIRATION, OPEN, SPOT, 20.0)                        # the 10:00 anchor exists
    assert level_fields(None, anchor) == {"em_high": 7720.0, "em_low": 7680.0, "node_r1": None, "node_s1": None}
    early = ExpectedMoveAnchor()                                       # no anchor yet: still None
    assert level_fields(None, early) == {"em_high": None, "em_low": None, "node_r1": None, "node_s1": None}


def test_atm_iv_is_in_vol_points_and_the_vanna_flow_follows_the_iv_trend():
    anchor, iv_history = ExpectedMoveAnchor(), AtmIvHistory()
    first, *_ = _build(_chain(iv=0.15), anchor=anchor, iv_history=iv_history)
    assert first["vanna"]["atm_iv"] == 15.0
    assert first["vanna"]["iv_change_15m"] is None and first["vanna"]["flow_est_usd"] is None

    later = datetime(2026, 10, 5, 10, 15, tzinfo=ET)
    second, *_ = _build(_chain(iv=0.16), now=later, anchor=anchor, iv_history=iv_history)
    vanna = second["vanna"]
    assert vanna["iv_change_15m"] == pytest.approx(1.0)
    # IV rose one vol point: the flow for a one-point fall is reversed.
    assert vanna["flow_est_usd"] == -vanna["usd_per_vol_down"]
    assert second["em"]["anchor_time"] == "10:00"                      # the anchor does not move


def test_zero_gamma_and_its_distance_come_from_the_profile():
    dealer, levels, *_ = _build(_chain())
    assert dealer["zero_gamma"] == levels.zero_gamma
    assert dealer["zero_gamma_distance"] == pytest.approx(round(SPOT - levels.zero_gamma, 2))
