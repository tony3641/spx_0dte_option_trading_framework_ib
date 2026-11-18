"""Level history: the wall / flip / max-pain changes the price chart draws as step lines."""
from spx_trade_desk.core.app_state import create_app_state
from spx_trade_desk.market.level_history import LevelHistory
from spx_trade_desk.market.price_bars import snapshot_payload


def _gex(stamp, call_wall=110.0, put_wall=90.0, gamma_flip=100.123, max_pain=None, expiration="20990105"):
    return {"timestamp": f"2099-01-05T{stamp}-05:00", "expiration": expiration, "call_wall": call_wall,
            "put_wall": put_wall, "gamma_flip": gamma_flip, "max_pain": max_pain}


def test_only_a_change_adds_a_point_and_levels_are_rounded_to_cents():
    h = LevelHistory()
    assert h.record(_gex("09:31:10")) is True
    assert h.record(_gex("09:31:50")) is False             # same levels: nothing new
    assert h.record(_gex("09:40:00")) is False
    assert h.record(_gex("09:45:00", call_wall=115.0)) is True
    assert h.points() == [
        {"time": "2099-01-05T09:31", "call_wall": 110.0, "put_wall": 90.0, "gamma_flip": 100.12, "max_pain": None},
        {"time": "2099-01-05T09:45", "call_wall": 115.0, "put_wall": 90.0, "gamma_flip": 100.12, "max_pain": None},
    ]


def test_a_second_change_in_the_same_minute_replaces_the_first():
    h = LevelHistory()
    h.record(_gex("09:31:10"))
    assert h.record(_gex("09:31:40", put_wall=88.0)) is True
    assert [(p["time"], p["put_wall"]) for p in h.points()] == [("2099-01-05T09:31", 88.0)]


def test_a_level_that_disappears_is_recorded_as_none():
    h = LevelHistory()
    h.record(_gex("09:31:00", max_pain=105.0))
    assert h.record(_gex("09:32:00", max_pain=None)) is True
    assert [p["max_pain"] for p in h.points()] == [105.0, None]


def test_a_new_expiration_starts_a_new_history():
    h = LevelHistory()
    h.record(_gex("15:50:00"))
    h.record(_gex("15:55:00", call_wall=111.0))
    assert h.record(_gex("18:00:00", expiration="20990106")) is True
    assert [p["time"] for p in h.points()] == ["2099-01-05T18:00"]


def test_a_payload_without_a_timestamp_is_ignored_and_the_history_is_bounded():
    h = LevelHistory(maxlen=3)
    assert h.record(None) is False
    assert h.record({"call_wall": 1.0}) is False
    for i in range(5):
        h.record(_gex(f"09:{30 + i}:00", call_wall=100.0 + i))
    assert [p["call_wall"] for p in h.points()] == [102.0, 103.0, 104.0]


def test_the_price_snapshot_carries_the_history():
    st = create_app_state()
    st.level_history.record(_gex("09:31:00"))
    st.level_history.record(_gex("09:45:00", call_wall=115.0))
    snap = snapshot_payload(st, rth=True, today="2099-01-05")
    assert [p["call_wall"] for p in snap["levels"]] == [110.0, 115.0]
