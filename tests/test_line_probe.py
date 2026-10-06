"""line_probe: the pure recommendation (the ramp itself is live-only)."""
from spx_trade_desk.ib.line_probe import recommend_lines


def test_recommendation_is_ninety_percent_of_what_ib_granted():
    lines, text = recommend_lines(100, hit_cap=False)
    assert lines == 90
    assert "exactly 100" in text and "MARKET_DATA_LINES = 90" in text


def test_a_reached_cap_is_reported_as_a_lower_bound():
    lines, text = recommend_lines(1500, hit_cap=True)
    assert lines == 1350
    assert "at least 1500" in text and "raise --cap" in text


def test_zero_granted_lines_recommend_zero():
    assert recommend_lines(0, hit_cap=False)[0] == 0
