"""ATM straddle, expected-move anchor and ATM IV history (invented strikes and 2099 dates only)."""
from datetime import datetime

import pytest

from spx_trade_desk.market.atm import (
    AtmIvHistory,
    ExpectedMoveAnchor,
    atm_iv,
    atm_straddle,
    em_used_pct,
    session_range,
)
from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.hours import ET


def _opt(k, right, bid, ask, iv=None):
    return OptionData(strike=float(k), right=right, bid=bid, ask=ask, implied_vol=iv)


def test_straddle_is_interpolated_between_the_bracketing_strikes():
    opts = [_opt(100, "C", 2.0, 2.2), _opt(100, "P", 1.0, 1.2), _opt(105, "C", 0.9, 1.1), _opt(105, "P", 3.0, 3.2)]
    assert atm_straddle(opts, 101.0) == pytest.approx(3.38)        # 3.2 at 100, 4.1 at 105
    assert atm_straddle(opts, 100.0) == pytest.approx(3.2)


def test_a_strike_without_a_two_sided_quote_is_skipped_and_a_wide_gap_gives_none():
    opts = [_opt(95, "C", 3.0, 3.2), _opt(95, "P", 0.5, 0.7), _opt(100, "C", 0.0, 2.2), _opt(100, "P", 1.0, 1.2),
            _opt(105, "C", 0.9, 1.1), _opt(105, "P", 3.0, 3.2)]
    assert atm_straddle(opts, 101.0) == pytest.approx(3.94)        # 100 skipped: 95 (3.7) to 105 (4.1)
    wide = [o for o in opts if o.strike != 95.0] + [_opt(90, "C", 5.0, 5.2), _opt(90, "P", 0.2, 0.4)]
    assert atm_straddle(wide, 101.0) is None                       # 90 to 105 is more than EM_MAX_GAP


def test_atm_iv_averages_call_and_put_iv_and_interpolates():
    opts = [_opt(100, "C", 1, 1, iv=0.20), _opt(100, "P", 1, 1, iv=0.22), _opt(105, "C", 1, 1, iv=0.18), _opt(105, "P", 1, 1, iv=0.20)]
    assert atm_iv(opts, 102.5) == pytest.approx(0.20)


def test_the_open_band_anchors_at_the_first_publish_after_0931_and_holds():
    a = ExpectedMoveAnchor()
    a.update("20990105", datetime(2099, 1, 5, 9, 30, 30, tzinfo=ET), 100.0, 3.0)
    assert a.band() == (None, None)
    a.update("20990105", datetime(2099, 1, 5, 9, 31, 5, tzinfo=ET), 100.0, 3.0)
    a.update("20990105", datetime(2099, 1, 5, 11, 0, tzinfo=ET), 110.0, 2.0)
    assert a.band() == (97.0, 103.0) and a.anchor_time == "09:31"


def test_a_mid_session_start_anchors_late_and_says_when():                      # Review Focus 1
    a = ExpectedMoveAnchor()
    a.update("20990105", datetime(2099, 1, 5, 13, 2, tzinfo=ET), 100.0, 1.5)
    assert a.band() == (98.5, 101.5) and a.anchor_time == "13:02"


def test_no_anchor_outside_the_expiration_session():                            # Review Focus 3
    a = ExpectedMoveAnchor()
    a.update("20990106", datetime(2099, 1, 5, 20, 30, tzinfo=ET), 100.0, 3.0)   # GTH, next day's expiry
    a.update("20990105", datetime(2099, 1, 5, 16, 5, tzinfo=ET), 100.0, 3.0)    # after the close
    a.update("20990105", datetime(2099, 1, 5, 10, 0, tzinfo=ET), 100.0, None)   # no straddle yet
    assert a.band() == (None, None)


def test_a_new_expiration_clears_the_anchor():
    a = ExpectedMoveAnchor()
    a.update("20990105", datetime(2099, 1, 5, 9, 35, tzinfo=ET), 100.0, 3.0)
    a.update("20990106", datetime(2099, 1, 6, 9, 35, tzinfo=ET), 105.0, 2.5)
    assert a.band() == (102.5, 107.5)


def test_em_used_is_the_session_range_over_the_open_band():
    bars = [{"time": "2099-01-05T09:30:00-05:00", "high": 101.0, "low": 99.5},
            {"time": "2099-01-05T09:31:00-05:00", "high": 102.5, "low": 100.0},
            {"time": "2099-01-04T15:59:00-05:00", "high": 120.0, "low": 80.0}]
    assert session_range(bars, "2099-01-05") == (102.5, 99.5)
    assert em_used_pct(102.5, 99.5, 3.0) == pytest.approx(50.0)
    assert em_used_pct(None, None, 3.0) is None and em_used_pct(102.5, 99.5, None) is None


def test_atm_iv_change_is_in_vol_points_over_the_window():
    h = AtmIvHistory()
    for m, iv in [(0, 0.20), (5, 0.195), (15, 0.185), (30, 0.17)]:
        h.record("20990105", f"2099-01-05T10:{m:02d}:00-05:00", iv)
    assert h.change(15) == pytest.approx(-1.5) and h.change(30) == pytest.approx(-3.0)
    assert h.change(45) is None and h.latest() == 0.17


def test_atm_iv_history_keeps_one_value_per_minute_and_resets_on_a_new_expiration():
    h = AtmIvHistory()
    h.record("20990105", "2099-01-05T10:00:05-05:00", 0.20); h.record("20990105", "2099-01-05T10:00:50-05:00", 0.21)
    h.record("20990105", "2099-01-05T10:01:00-05:00", None)               # ignored
    assert h.latest() == 0.21
    h.record("20990106", "2099-01-05T18:00:00-05:00", 0.25)
    assert h.change(1) is None and h.latest() == 0.25
