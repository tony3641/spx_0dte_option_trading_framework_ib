"""ib/account.py position_rows: combo fills (a BAG execution plus its legs) group open legs into spreads."""
from spx_trade_desk.ib.account import position_rows

EXP = "20990105"                                     # invented expiry


def _pos(con_id, strike, right, qty, avg, mark, unpnl=0.0):
    return {"contract": {"conId": con_id, "symbol": "SPX", "secType": "OPT", "expiry": EXP,
                         "strike": strike, "right": right, "multiplier": "100", "localSymbol": ""},
            "position": qty, "marketPrice": mark, "marketValue": qty * mark * 100,
            "averageCost": avg, "unrealizedPNL": unpnl, "realizedPNL": 0.0, "account": "U***12345"}


def _ex(order_id, sec_type, side, shares, con_id=0, t="10:00:00 EDT"):
    return {"execId": f"{order_id}.{con_id}.{side}", "time": t, "conId": con_id, "secType": sec_type,
            "side": side, "shares": shares, "orderId": order_id, "symbol": "SPX"}


def _credit_put_spread_fill(order_id=21, qty=1):
    return [_ex(order_id, "BAG", "SLD", qty, con_id=1),
            _ex(order_id, "OPT", "SLD", qty, con_id=101),            # short the 1000 put
            _ex(order_id, "OPT", "BOT", qty, con_id=102)]            # long the 990 put


def test_a_credit_spread_filled_as_a_combo_is_one_row_with_its_legs():
    positions = [_pos(101, 1000.0, "P", -1, 300.0, 2.5, unpnl=50.0),
                 _pos(102, 990.0, "P", 1, 200.0, 1.7, unpnl=-30.0)]
    rows = position_rows(positions, _credit_put_spread_fill())
    assert len(rows) == 1
    spread = rows[0]
    assert spread["kind"] == "spread"
    assert spread["position"] == -1                          # short one credit spread
    assert spread["averageCost"] == 100.0                    # 1.00 credit per spread (IB units, x100)
    assert round(spread["marketPrice"], 2) == 0.8            # 2.50 - 1.70 to close
    assert spread["unrealizedPNL"] == 20.0
    assert spread["label"] == "SPX 2099-01-05 1000/990 P"
    assert [leg["index"] for leg in spread["legs"]] == [0, 1]
    assert [leg["position"] for leg in spread["legs"]] == [-1, 1]


def test_singles_without_a_combo_fill_stay_single():
    positions = [_pos(101, 1000.0, "P", -1, 300.0, 2.5), _pos(102, 990.0, "P", 1, 200.0, 1.7)]
    legs_one_by_one = [_ex(30, "OPT", "SLD", 1, con_id=101), _ex(31, "OPT", "BOT", 1, con_id=102)]
    rows = position_rows(positions, legs_one_by_one)
    assert [(r["kind"], r["index"]) for r in rows] == [("single", 0), ("single", 1)]


def test_extra_contracts_on_a_leg_show_as_a_single_beside_the_spread():
    positions = [_pos(101, 1000.0, "P", -3, 300.0, 2.5, unpnl=90.0), _pos(102, 990.0, "P", 2, 200.0, 1.7)]
    rows = position_rows(positions, _credit_put_spread_fill(qty=2))
    spread, single = rows
    assert spread["position"] == -2 and [leg["position"] for leg in spread["legs"]] == [-2, 2]
    assert single["kind"] == "single" and single["index"] == 0 and single["position"] == -1
    assert single["unrealizedPNL"] == 30.0                   # pro rata: one of the three short puts


def test_a_closed_leg_breaks_the_group():
    positions = [_pos(101, 1000.0, "P", -1, 300.0, 2.5)]     # the long put was sold on its own
    rows = position_rows(positions, _credit_put_spread_fill())
    assert [(r["kind"], r["index"]) for r in rows] == [("single", 0)]


def test_the_closing_combo_does_not_double_the_spread():
    """Open 2 and close 1 with the same combo: one spread of 1 remains."""
    positions = [_pos(101, 1000.0, "P", -1, 300.0, 2.5), _pos(102, 990.0, "P", 1, 200.0, 1.7)]
    fills = _credit_put_spread_fill(21, qty=2) + [
        _ex(22, "BAG", "BOT", 1, con_id=1, t="11:00:00 EDT"),
        _ex(22, "OPT", "BOT", 1, con_id=101, t="11:00:00 EDT"),
        _ex(22, "OPT", "SLD", 1, con_id=102, t="11:00:00 EDT")]
    rows = position_rows(positions, fills)
    assert len(rows) == 1 and rows[0]["kind"] == "spread" and rows[0]["position"] == -1


def test_a_debit_spread_is_long():
    positions = [_pos(201, 1010.0, "C", 1, 400.0, 4.2), _pos(202, 1020.0, "C", -1, 150.0, 1.4)]
    fills = [_ex(40, "BAG", "BOT", 1, con_id=2), _ex(40, "OPT", "BOT", 1, con_id=201),
             _ex(40, "OPT", "SLD", 1, con_id=202)]
    (spread,) = position_rows(positions, fills)
    assert spread["position"] == 1 and spread["averageCost"] == 250.0
    assert round(spread["marketPrice"], 2) == 2.8
    assert spread["label"] == "SPX 2099-01-05 1010/1020 C"


def test_non_option_positions_pass_through():
    stock = {"contract": {"conId": 9, "symbol": "ACME", "secType": "STK"}, "position": -100,
             "marketPrice": 10.0, "marketValue": -1000.0, "averageCost": 11.0,
             "unrealizedPNL": 100.0, "realizedPNL": 0.0}
    rows = position_rows([stock], [])
    assert rows[0]["kind"] == "single" and rows[0]["position"] == -100 and rows[0]["unrealizedPNL"] == 100.0
