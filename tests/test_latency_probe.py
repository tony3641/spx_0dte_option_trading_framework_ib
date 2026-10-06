"""latency_probe: verdicts, safety guards and payload shapes (the live run itself is manual).

Everything here runs on small fakes: no test opens a socket or talks to TWS.
"""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spx_trade_desk.core.perf import PerfRecorder
from spx_trade_desk.ib import latency_probe as lp

EXP = "20261006"


def test_verdict_bands():
    assert lp.verdict(100.0, 100.0) == "PASS"
    assert lp.verdict(110.0, 100.0) == "NEAR"           # within 10%: recorded with a note, not a failure
    assert lp.verdict(110.1, 100.0) == "MISS"
    assert lp.verdict(None, 100.0) == "N/A"
    assert lp.verdict(0.0, 0.0) == "PASS" and lp.verdict(1.0, 0.0) == "MISS"


def test_targets_are_the_specs_table():
    assert {k: v[0] for k, v in lp.TARGETS.items()} == {
        "single_ack_p50": 100.0, "combo_ack_p50": 100.0,
        "single_bracket_ack_p50": 150.0, "combo_bracket_ack_p50": 150.0,
        "cancel_p50": 105.0, "ack_during_fill_p95": 150.0, "ack_during_bulk_max": 300.0,
        "bulk_first_call": 1000.0, "bulk_failed_lookups": 0.0, "boot": 1500.0}


def test_evaluate_reports_every_target_and_marks_missing_values_na():
    rows = lp.evaluate({"single_ack_p50": 95.0, "cancel_p50": 130.0})
    assert [r["key"] for r in rows] == list(lp.TARGETS)
    by = {r["key"]: r for r in rows}
    assert by["single_ack_p50"]["verdict"] == "PASS"
    assert by["cancel_p50"]["verdict"] == "MISS"
    assert by["boot"]["verdict"] == "N/A" and by["boot"]["value"] is None


def test_format_table_lists_every_check_with_its_verdict():
    text = lp.format_table(lp.evaluate({"single_ack_p50": 95.0}))
    assert text.count("\n") == len(lp.TARGETS)
    assert "PASS" in text and "N/A" in text


def test_format_table_prints_one_decimal_so_a_near_miss_does_not_read_as_equal():
    rows = lp.evaluate({"single_ack_p50": 100.4}, samples={"single_ack_p50": 5})
    line = [l for l in lp.format_table(rows).splitlines() if "single-leg order" in l][0]
    assert "100.4" in line and "NEAR" in line


def test_format_table_shows_samples_and_failures_per_row():
    rows = lp.evaluate({"single_ack_p50": 95.0}, failed={"single_ack_p50": 4}, samples={"single_ack_p50": 1})
    text = lp.format_table(rows)
    assert text.splitlines()[0].split()[-3:] == ["n", "fail", "verdict"]
    line = [l for l in text.splitlines() if "single-leg order" in l][0]
    assert line.split()[-3:] == ["1", "4", "MISS"]


def test_pct_is_nearest_rank():
    xs = [float(i) for i in range(1, 11)]
    assert lp._pct(xs, 50) == 5.0 and lp._pct(xs, 95) == 10.0
    assert lp._pct([7.0], 95) == 7.0
    assert lp._pct([], 50) is None


@pytest.mark.parametrize("build", [lp.single_payload, lp.spread_payload])
@pytest.mark.parametrize("stop", [False, True])
def test_the_built_payloads_pass_the_safety_check(build, stop):
    p = build(EXP, 7700.0, stop)
    lp._assert_safe(p)
    assert ("stopLoss" in p) is stop
    assert all(leg["right"] == "P" and leg["qty"] == 1 for leg in p["legs"])


def test_the_spread_is_a_debit_put_spread():
    p = lp.spread_payload(EXP, 7700.0)
    assert [(l["action"], l["strike"]) for l in p["legs"]] == [("BUY", 7700.0), ("SELL", 7695.0)]
    assert p["comboAction"] == "BUY" and p["comboLmtPrice"] == 0.05


@pytest.mark.parametrize("edit", [
    lambda p: p["legs"][0].update(lmtPrice=0.11),
    lambda p: p["legs"][0].update(qty=2),
    lambda p: p.update(orderType="MKT"),
    lambda p: p.update(dynamicFill=True),
    lambda p: p.update(stopLoss={"stopPrice": 0.50, "limitPrice": 0.05}),
])
def test_assert_safe_refuses_anything_that_could_fill_or_grow(edit):
    p = lp.single_payload(EXP, 7700.0)
    edit(p)
    with pytest.raises(ValueError):
        lp._assert_safe(p)


def test_assert_safe_refuses_a_combo_debit_above_the_cap():
    p = lp.spread_payload(EXP, 7700.0)
    p["comboLmtPrice"] = 0.50
    with pytest.raises(ValueError):
        lp._assert_safe(p)


# -- _assert_safe is a closed whitelist of what the two builders emit ----------------------------

def _case(edit, id):
    return pytest.param(edit, id=id)


_STOP = {"stopPrice": 0.10, "limitPrice": 0.05}

SINGLE_REFUSALS = [
    _case(lambda p: p["legs"][0].update(action="SELL"), "sell-leg-is-marketable"),
    _case(lambda p: p.update(comboAction="BUY"), "combo-action-on-a-single"),
    _case(lambda p: p.update(comboQuantity=1), "combo-quantity-on-a-single"),
    _case(lambda p: p.update(comboLmtPrice=0.05), "combo-price-on-a-single"),
    _case(lambda p: p["legs"][0].update(lmtPrice=-5.0), "negative-limit-becomes-abs-5"),
    _case(lambda p: p["legs"][0].update(lmtPrice=0.0), "zero-limit"),
    _case(lambda p: p["legs"][0].pop("lmtPrice"), "missing-limit"),
    _case(lambda p: p["legs"][0].update(lmtPrice=float("nan")), "nan-limit"),
    _case(lambda p: p["legs"][0].update(lmtPrice=float("inf")), "infinite-limit"),
    _case(lambda p: p["legs"][0].update(lmtPrice=True), "bool-limit"),
    _case(lambda p: p["legs"][0].update(lmtPrice="0.05"), "string-limit"),
    _case(lambda p: p.update(stopLoss={"stopPrice": -0.50, "limitPrice": 0.05}), "negative-stop-price"),
    _case(lambda p: p.update(stopLoss={"stopPrice": 0.10, "limitPrice": -0.50}), "negative-stop-limit"),
    _case(lambda p: p.update(stopLoss={"stopPrice": 0.0, "limitPrice": 0.05}), "zero-stop-price"),
    _case(lambda p: p.update(stopLoss={"stopPrice": 0.10}), "stop-without-limit"),
    _case(lambda p: p.update(stopLoss={**_STOP, "extra": 1}), "stop-with-extra-key"),
    _case(lambda p: p.update(stopLoss=0.05), "scalar-stop"),
    _case(lambda p: p.update(stopLoss=None), "null-stop"),
    _case(lambda p: p.update(legs=[]), "no-legs"),
    _case(lambda p: p.update(legs=None), "legs-not-a-list"),
    _case(lambda p: p["legs"].extend(copy.deepcopy(p["legs"]) * 2), "three-legs"),
    _case(lambda p: p.update(tif="GTC"), "gtc"),
    _case(lambda p: p.pop("tif"), "tif-not-explicit"),
    _case(lambda p: p.update(outsideRth=True), "outside-rth"),
    _case(lambda p: p.update(outsideRth=False), "any-outside-rth-key"),
    _case(lambda p: p.update(dynamicFill=False), "any-dynamic-fill-key"),
    _case(lambda p: p.update(repriceIntervalSec=0.1), "reprice-key"),
    _case(lambda p: p.update(orderType="MKT"), "market-order"),
    _case(lambda p: p.pop("orderType"), "order-type-not-explicit"),
    _case(lambda p: p.update(algoStrategy="Adaptive"), "unknown-top-level-key"),
    _case(lambda p: p["legs"][0].update(symbol="SPY"), "other-symbol"),
    _case(lambda p: p["legs"][0].update(right="C"), "call"),
    _case(lambda p: p["legs"][0].update(secType="STK"), "stock"),
    _case(lambda p: p["legs"][0].update(qty=True), "bool-qty"),
    _case(lambda p: p["legs"][0].update(qty=1.0), "float-qty"),
    _case(lambda p: p["legs"][0].update(qty=0), "zero-qty"),
    _case(lambda p: p["legs"][0].update(trading_class="SPX"), "unknown-leg-key"),
    _case(lambda p: p["legs"][0].update(strike="7700"), "string-strike"),
]


@pytest.mark.parametrize("edit", SINGLE_REFUSALS)
def test_assert_safe_refuses_a_single_leg_payload_outside_the_whitelist(edit):
    p = lp.single_payload(EXP, 7700.0)
    edit(p)
    with pytest.raises(ValueError):
        lp._assert_safe(p)


SPREAD_REFUSALS = [
    _case(lambda p: p.update(comboAction="SELL"), "sell-combo"),
    _case(lambda p: p.pop("comboAction"), "combo-action-missing"),
    _case(lambda p: p.update(comboQuantity=100), "combo-quantity-100"),
    _case(lambda p: p.update(comboQuantity=2), "combo-quantity-2"),
    _case(lambda p: p.pop("comboQuantity"), "combo-quantity-missing"),
    _case(lambda p: p.update(comboLmtPrice=-5.0), "negative-combo-price"),
    _case(lambda p: p.update(comboLmtPrice=0.50), "combo-price-above-cap"),
    _case(lambda p: p.update(comboLmtPrice=0.0), "zero-combo-price"),
    _case(lambda p: p.update(comboLmtPrice=-0.05), "negative-combo-price-within-the-cap"),
    _case(lambda p: p.pop("comboLmtPrice"), "combo-price-missing"),
    _case(lambda p: p.update(comboLmtPrice=float("nan")), "nan-combo-price"),
    _case(lambda p: p["legs"].reverse(), "legs-swapped-credit-spread"),
    _case(lambda p: p["legs"][1].update(action="BUY"), "both-buy"),
    _case(lambda p: p["legs"][0].update(action="SELL"), "both-sell"),
    _case(lambda p: p["legs"][1].update(strike=7705.0), "short-above-long"),
    _case(lambda p: p["legs"][1].update(expiry="20261007"), "calendar-legs"),
    _case(lambda p: p["legs"][1].update(qty=2), "ratio-leg"),
    _case(lambda p: p["legs"][1].update(lmtPrice=0.50), "short-leg-price-above-cap"),
    _case(lambda p: p["legs"].append(copy.deepcopy(p["legs"][1])), "three-legs"),
    _case(lambda p: p.update(tif="GTC"), "gtc"),
    _case(lambda p: p.update(outsideRth=True), "outside-rth"),
    _case(lambda p: p.update(stopLoss={"stopPrice": -0.50, "limitPrice": 0.05}), "negative-stop-price"),
]


@pytest.mark.parametrize("edit", SPREAD_REFUSALS)
def test_assert_safe_refuses_a_spread_payload_outside_the_whitelist(edit):
    p = lp.spread_payload(EXP, 7700.0)
    edit(p)
    with pytest.raises(ValueError):
        lp._assert_safe(p)


def test_assert_safe_refuses_a_payload_that_is_not_a_dict():
    with pytest.raises(ValueError):
        lp._assert_safe([lp.single_payload(EXP, 7700.0)])


@pytest.mark.asyncio
async def test_the_paper_guard_refuses_a_live_account_without_printing_its_code():
    with pytest.raises(SystemExit) as ei:
        await lp._assert_paper(SimpleNamespace(_account_code="U1234567"), wait_s=0.0)
    assert "U1234567" not in str(ei.value)
    await lp._assert_paper(SimpleNamespace(_account_code="DU0000001"), wait_s=0.0)      # paper: fine


@pytest.mark.asyncio
async def test_the_paper_guard_refuses_when_the_account_is_unknown():
    with pytest.raises(SystemExit):
        await lp._assert_paper(SimpleNamespace(_account_code=None), wait_s=0.0)


@pytest.mark.asyncio
async def test_no_bulk_wrapper_refuses_the_bulk_request_and_forwards_the_rest():
    w = lp._NoBulk(SimpleNamespace(pacer="p"))
    assert w.pacer == "p"
    with pytest.raises(RuntimeError):
        await w.req_chain_contract_details("SPX", EXP, "SPXW")


# -- command line: client ids and ports ----------------------------------------------------------

def test_the_parser_defaults():
    a = lp._parser().parse_args([])
    assert (a.port, a.client_id, a.skip_bulk, a.out) == (7497, 150, False, "")


@pytest.mark.parametrize("cid", ["0", "1", "-3", "96", "97", "139", "140", "abc"])
def test_the_parser_rejects_ids_that_collide_with_the_dashboard_capture_or_line_probe(cid):
    # 0 falls back to the dashboard id inside connect_ib; the probe also connects as N + 1.
    with pytest.raises(SystemExit) as ei:
        lp._parser().parse_args(["--client-id", cid])
    assert ei.value.code == 2


@pytest.mark.parametrize("cid", ["2", "95", "98", "138", "141", "150"])
def test_the_parser_accepts_other_client_ids(cid):
    assert lp._parser().parse_args(["--client-id", cid]).client_id == int(cid)


@pytest.mark.parametrize("port", [7496, 4001])
def test_the_standard_live_ports_are_refused_before_connecting(port):
    with pytest.raises(SystemExit) as ei:
        lp._refuse_live_port(port)
    assert str(port) in str(ei.value) and "live" in str(ei.value)


@pytest.mark.parametrize("port", [7497, 4002])
def test_the_paper_ports_are_allowed(port):
    lp._refuse_live_port(port)


# -- fakes: no socket is ever opened -------------------------------------------------------------

class _FakeHandle:
    def __init__(self, needs=1, terminal=False):
        self.needs, self.cancel_calls, self.terminal = needs, 0, terminal

    def is_terminal(self):
        return self.terminal


class _FakeIB:
    """The slice of IBClient the probe touches; connect and every order call are recorded."""

    def __init__(self, account_code="DU0000001"):
        self._account_code = account_code
        self.connected = True
        self.orders = {}
        self.disconnected = False
        self.cancelled = []
        self.connects = []
        self.pacer = SimpleNamespace()

    async def connect(self, host, port, client_id, timeout=15.0):
        self.connects.append((host, port, client_id))

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        handle = self.orders.get(oid)
        if handle is not None:
            handle.cancel_calls += 1
            if handle.cancel_calls >= handle.needs:
                handle.terminal = True

    def disconnect(self):
        self.connected = False
        self.disconnected = True


def _handlers(monkeypatch, place_status="Submitted", cancel_status="Cancelled", stop_id=None,
              ack_delay=0.0, cancel_delay=0.0, forbid=False, place_error_when=None):
    """Stand-ins for handle_place_order / handle_cancel_order that record every call."""
    h = SimpleNamespace(places=[], cancels=[])

    async def place(ib, state, payload, ws=None, refresh_fn=None):
        h.places.append(payload)
        if forbid:
            raise AssertionError("handle_place_order must not be reached")
        await asyncio.sleep(ack_delay)
        if place_status == "Error" or (place_error_when and place_error_when(payload)):
            return {"type": "order_status", "data": {"status": "Error", "message": "Failed to qualify contract"}}
        data = {"status": place_status, "orderId": 100 + len(h.places)}
        if stop_id:
            data["stopOrderId"] = stop_id
        return {"type": "order_status", "data": data}

    async def cancel(ib, state, order_id, refresh_fn=None):
        h.cancels.append(order_id)
        await asyncio.sleep(cancel_delay)
        return {"type": "order_status", "data": {"status": cancel_status, "orderId": order_id}}

    monkeypatch.setattr(lp.orders, "handle_place_order", place)
    monkeypatch.setattr(lp.orders, "handle_cancel_order", cancel)
    return h


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """The pacing sleeps of the live run are irrelevant to the fakes."""
    for name in ("ORDER_GAP_S", "FILL_LEAD_S", "FILL_GAP_S", "BULK_LEAD_S", "SWEEP_WAIT_S"):
        monkeypatch.setattr(lp, name, 0.0)


# -- one order through the real handlers' entry points -------------------------------------------

@pytest.mark.asyncio
async def test_place_cancel_refuses_an_unsafe_payload_before_any_order_is_sent(monkeypatch):
    h = _handlers(monkeypatch, forbid=True)
    bad = lp.single_payload(EXP, 7700.0)
    bad["legs"][0]["lmtPrice"] = 5.0
    with pytest.raises(ValueError):
        await lp._place_cancel(_FakeIB(), SimpleNamespace(), bad)
    assert h.places == [] and h.cancels == []            # the handler tripwire was never reached


@pytest.mark.asyncio
async def test_place_cancel_happy_path_times_both_legs_and_cancels_the_stop_order(monkeypatch):
    h = _handlers(monkeypatch, stop_id=77, ack_delay=0.02, cancel_delay=0.02)
    ib = _FakeIB()
    s = await lp._place_cancel(ib, SimpleNamespace(), lp.single_payload(EXP, 7700.0, True))
    assert s.ack_ms >= 20.0 and s.cancel_ms >= 20.0 and s.confirmed and s.status == "Cancelled"
    assert h.cancels == [101] and ib.cancelled == [77]


@pytest.mark.asyncio
async def test_place_cancel_reports_a_placement_error_without_cancelling(monkeypatch):
    h = _handlers(monkeypatch, place_status="Error")
    ib = _FakeIB()
    s = await lp._place_cancel(ib, SimpleNamespace(), lp.single_payload(EXP, 7700.0))
    assert s.ack_ms is None and s.cancel_ms is None and not s.confirmed
    assert s.status == "Failed to qualify contract" and h.cancels == [] and ib.cancelled == []


@pytest.mark.asyncio
async def test_place_cancel_keeps_a_slow_unconfirmed_cancel_as_a_timed_sample(monkeypatch):
    _handlers(monkeypatch, cancel_status="PendingCancel", cancel_delay=0.05)
    s = await lp._place_cancel(_FakeIB(), SimpleNamespace(), lp.single_payload(EXP, 7700.0))
    assert s.ack_ms is not None and s.cancel_ms >= 50.0       # recorded, not dropped
    assert not s.confirmed and s.status == "PendingCancel"


@pytest.mark.asyncio
async def test_place_cancel_treats_a_cancel_that_found_no_order_as_unconfirmed_and_untimed(monkeypatch):
    _handlers(monkeypatch, cancel_status="Error")
    s = await lp._place_cancel(_FakeIB(), SimpleNamespace(), lp.single_payload(EXP, 7700.0))
    assert s.ack_ms is not None and s.cancel_ms is None and not s.confirmed


# -- tallies, grading and the exit code ------------------------------------------------------------

def test_record_counts_failures_and_keeps_the_slow_cancel_as_a_sample():
    t = lp._new_tally()
    lp._record(t, lp.Sample(90.0, 80.0, True, "Cancelled"))
    lp._record(t, lp.Sample(None, None, False, "Failed to qualify contract"))
    lp._record(t, lp.Sample(95.0, 2003.0, False, "PendingCancel"))
    assert t["n"] == 3 and t["acks_ms"] == [90.0, 95.0] and t["cancels_ms"] == [80.0, 2003.0]
    assert t["placement_errors"] == 1 and t["unconfirmed_cancels"] == 1
    assert t["problems"] == ["Failed to qualify contract", "PendingCancel"]


def _tally(acks, cancels=(), errors=0, unconfirmed=0):
    t = lp._new_tally()
    t.update(n=len(acks) + errors, acks_ms=list(acks), cancels_ms=list(cancels),
             placement_errors=errors, unconfirmed_cancels=unconfirmed)
    return t


def _good_result():
    bulk = _tally([100.0, 110.0, 120.0])
    bulk.update(first_call_ms=800.0, listed=600, failed_lookups=0, acks_during_bulk_ms=[110.0, 120.0],
                overlapped=2, warmup_failed=0, live_orders_left=0)
    return {"boot_ms": 900.0,
            "orders": {"single": _tally([90.0] * 5, [80.0] * 5), "combo": _tally([90.0] * 5, [80.0] * 5),
                       "single_bracket": _tally([120.0] * 3, [80.0] * 3),
                       "combo_bracket": _tally([120.0] * 3, [80.0] * 3)},
            "fill": _tally([100.0] * 10, [80.0] * 10),
            "bulk": bulk}


def _by(rows):
    return {r["key"]: r for r in rows}


def test_a_clean_result_passes_every_row_and_exits_zero():
    rows = lp.grade(_good_result())
    assert {r["verdict"] for r in rows} == {"PASS"}
    by = _by(rows)
    assert by["single_ack_p50"]["n"] == 5 and by["single_ack_p50"]["failed"] == 0
    assert by["cancel_p50"]["n"] == 16
    assert lp.exit_code(rows, 0) == 0


def test_four_failed_placements_out_of_five_is_a_miss_not_a_pass():
    r = _good_result()
    r["orders"]["single"] = _tally([50.0], [50.0], errors=4)
    by = _by(lp.grade(r))
    assert by["single_ack_p50"]["verdict"] == "MISS" and by["single_ack_p50"]["failed"] == 4
    assert by["combo_ack_p50"]["verdict"] == "PASS"                     # only that section is affected
    assert lp.exit_code(lp.grade(r), 0) == 1


def test_a_section_where_every_placement_failed_is_a_miss_not_na():
    r = _good_result()
    r["orders"]["combo_bracket"] = _tally([], [], errors=3)
    row = _by(lp.grade(r))["combo_bracket_ack_p50"]
    assert row["value"] is None and row["verdict"] == "MISS" and row["failed"] == 3
    assert lp.exit_code(lp.grade(r), 0) == 1


def test_an_unconfirmed_cancel_fails_its_section_and_the_cancel_row():
    r = _good_result()
    r["orders"]["combo"] = _tally([90.0] * 5, [80.0] * 4 + [2003.0], unconfirmed=1)
    by = _by(lp.grade(r))
    assert by["combo_ack_p50"]["verdict"] == "MISS" and by["combo_ack_p50"]["failed"] == 1
    assert by["cancel_p50"]["verdict"] == "MISS" and by["cancel_p50"]["failed"] == 1
    assert by["cancel_p50"]["n"] == 16                                  # the slow cancel is a sample
    assert by["single_ack_p50"]["verdict"] == "PASS"


def test_failures_in_the_fill_and_bulk_sections_fail_their_rows():
    r = _good_result()
    r["fill"] = _tally([100.0] * 9, [80.0] * 9, errors=1)
    r["bulk"]["unconfirmed_cancels"] = 1
    by = _by(lp.grade(r))
    assert by["ack_during_fill_p95"]["verdict"] == "MISS"
    assert by["ack_during_bulk_max"]["verdict"] == "MISS" and by["ack_during_bulk_max"]["failed"] == 1
    r = _good_result()
    r["bulk"]["warmup_failed"] = 1
    assert _by(lp.grade(r))["ack_during_bulk_max"]["verdict"] == "MISS"


def test_nothing_measured_is_not_a_pass_and_exits_nonzero():
    rows = lp.grade({})
    assert {r["verdict"] for r in rows} == {"N/A"} and not any(r["skipped"] for r in rows)
    assert lp.exit_code(rows, 0) == 1


def test_an_unmeasured_row_that_was_not_skipped_exits_nonzero():
    r = _good_result()
    del r["boot_ms"]
    rows = lp.grade(r)
    assert _by(rows)["boot"]["verdict"] == "N/A"
    assert lp.exit_code(rows, 0) == 1


def test_skip_bulk_leaves_the_bulk_rows_na_and_skipped_and_still_exits_zero():
    r = _good_result()
    r["bulk"] = None
    rows = lp.grade(r, skip_bulk=True)
    by = _by(rows)
    for key in ("ack_during_bulk_max", "bulk_first_call", "bulk_failed_lookups"):
        assert by[key]["verdict"] == "N/A" and by[key]["skipped"] and "skipped" in by[key]["note"]
    assert {r_["verdict"] for r_ in rows if not r_["skipped"]} == {"PASS"}
    assert lp.exit_code(rows, 0) == 0


def test_a_bulk_call_with_no_overlapping_ack_is_not_measured_and_says_why():
    r = _good_result()
    r["bulk"].update(acks_during_bulk_ms=[], overlapped=0)
    row = _by(lp.grade(r))["ack_during_bulk_max"]
    assert row["value"] is None and row["verdict"] == "N/A" and not row["skipped"]
    assert "overlap" in row["note"]
    assert lp.exit_code(lp.grade(r), 0) == 1


def test_a_bulk_listing_below_the_minimum_fails_its_rows():
    r = _good_result()
    r["bulk"].update(listed=0, failed_lookups=1)
    by = _by(lp.grade(r))
    assert by["bulk_failed_lookups"]["verdict"] == "MISS"
    assert by["bulk_first_call"]["verdict"] == "MISS" and by["bulk_first_call"]["failed"] == 1


@pytest.mark.parametrize("left,expected", [(0, 0), (1, 1), (None, 1)])
def test_exit_code_fails_on_orders_left_live_or_unknown(left, expected):
    assert lp.exit_code(lp.grade(_good_result()), left) == expected


@pytest.mark.parametrize("value,expected", [(100.0, 0), (110.0, 0), (110.1, 1)])
def test_exit_code_tolerates_near_but_not_miss(value, expected):
    r = _good_result()
    r["orders"]["single"] = _tally([value] * 5, [80.0] * 5)
    assert lp.exit_code(lp.grade(r), 0) == expected


# -- the sections ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orders_section_tallies_each_check_and_grades_the_failing_one(monkeypatch):
    _handlers(monkeypatch, place_error_when=lambda p: len(p["legs"]) == 2 and "stopLoss" not in p)
    out = await lp._orders_section(_FakeIB(), SimpleNamespace(), EXP, 7700.0, {})
    assert out["single"]["n"] == 5 and out["single"]["placement_errors"] == 0
    assert out["combo"]["n"] == 5 and out["combo"]["placement_errors"] == 5
    assert out["single_bracket"]["n"] == 3 and out["combo_bracket"]["n"] == 3
    r = _good_result()
    r["orders"] = out
    by = _by(lp.grade(r))
    assert by["combo_ack_p50"]["verdict"] == "MISS" and by["single_ack_p50"]["verdict"] == "PASS"


class _FakeRegistry:
    """Stands in for ContractRegistry: a bulk call that takes ``delay`` seconds, a qualification that never ends."""

    def __init__(self, delay=0.0, listed=600, error_200=0):
        self.delay, self.listed, self.error_200 = delay, listed, error_200

    async def ensure_chain(self, ib, symbol, expiry, trading_class, now, force=False):
        await asyncio.sleep(self.delay)
        for _ in range(self.error_200):
            lp.ERRS.append((0.0, 1, 200, "No security definition has been found"))
        return self.listed

    async def qualify_keys(self, ib, expiry, trading_class, keys, now):
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_during_fill_tallies_acks_and_failures_and_cleans_up_the_load(monkeypatch):
    _handlers(monkeypatch, place_error_when=lambda p: False)
    monkeypatch.setattr(lp, "ContractRegistry", lambda: _FakeRegistry())
    out = await lp._during_fill(_FakeIB(), SimpleNamespace(), EXP, 7700.0, {}, n_orders=4)
    assert out["n"] == 4 and len(out["acks_ms"]) == 4 and out["placement_errors"] == 0
    assert out["load_still_running_at_the_end"] is True


def _bulk_ib(monkeypatch):
    ib2 = _FakeIB()
    monkeypatch.setattr(lp, "IBClient", lambda *a, **k: ib2)
    return ib2


@pytest.mark.asyncio
async def test_bulk_first_call_is_the_listing_alone_and_the_second_connection_is_n_plus_one(monkeypatch):
    # A fast listing (50 ms) with slow orders (3 x 200 ms): the old measurement read ~650 ms.
    _handlers(monkeypatch, ack_delay=0.15, cancel_delay=0.05)
    monkeypatch.setattr(lp, "ContractRegistry", lambda: _FakeRegistry(delay=0.05))
    ib2 = _bulk_ib(monkeypatch)
    out = await lp._bulk_section(SimpleNamespace(port=7497, client_id=150), EXP, 7700.0, {})
    assert 40.0 <= out["first_call_ms"] < 250.0
    assert ib2.connects[0][1:] == (7497, 151) and ib2.disconnected
    assert len(out["acks_ms"]) == 3 and out["overlapped"] == 0 and out["acks_during_bulk_ms"] == []
    assert out["listed"] == 600 and out["failed_lookups"] == 0 and out["live_orders_left"] == 0


@pytest.mark.asyncio
async def test_bulk_counts_only_the_acks_that_finished_while_the_listing_was_running(monkeypatch):
    # Listing 0.5 s, each order 0.2 s: acks at ~0.2 s and ~0.4 s overlap, the one at ~0.6 s does not.
    _handlers(monkeypatch, ack_delay=0.2)
    monkeypatch.setattr(lp, "ContractRegistry", lambda: _FakeRegistry(delay=0.5))
    _bulk_ib(monkeypatch)
    out = await lp._bulk_section(SimpleNamespace(port=7497, client_id=150), EXP, 7700.0, {})
    assert len(out["acks_ms"]) == 3 and out["overlapped"] == 2 and len(out["acks_during_bulk_ms"]) == 2
    r = _good_result()
    r["bulk"] = out
    assert _by(lp.grade(r))["ack_during_bulk_max"]["value"] == max(out["acks_during_bulk_ms"])


@pytest.mark.asyncio
async def test_bulk_failed_lookups_counts_error_200_and_an_unusable_listing(monkeypatch):
    _handlers(monkeypatch)
    _bulk_ib(monkeypatch)
    monkeypatch.setattr(lp, "ContractRegistry", lambda: _FakeRegistry(error_200=2))
    out = await lp._bulk_section(SimpleNamespace(port=7497, client_id=150), EXP, 7700.0, {})
    assert out["failed_lookups"] == 2
    monkeypatch.setattr(lp, "ContractRegistry", lambda: _FakeRegistry(listed=0))
    out = await lp._bulk_section(SimpleNamespace(port=7497, client_id=150), EXP, 7700.0, {})
    assert out["failed_lookups"] == 1


@pytest.mark.asyncio
async def test_bulk_warmup_failure_is_recorded(monkeypatch):
    _handlers(monkeypatch, place_status="Error")
    monkeypatch.setattr(lp, "ContractRegistry", lambda: _FakeRegistry())
    _bulk_ib(monkeypatch)
    out = await lp._bulk_section(SimpleNamespace(port=7497, client_id=150), EXP, 7700.0, {})
    assert out["warmup_failed"] == 1 and out["placement_errors"] == 3


@pytest.mark.asyncio
async def test_bulk_section_refuses_a_live_account_sends_no_order_and_disconnects(monkeypatch):
    h = _handlers(monkeypatch, forbid=True)
    ib2 = _FakeIB("U1234567")
    monkeypatch.setattr(lp, "IBClient", lambda *a, **k: ib2)
    with pytest.raises(SystemExit):
        await lp._bulk_section(SimpleNamespace(port=7497, client_id=150), EXP, 7700.0, {})
    assert h.places == [] and ib2.disconnected


# -- sweep -----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweep_cancels_only_live_orders_and_retries_a_stubborn_one_once():
    ib = _FakeIB()
    ib.orders = {1: _FakeHandle(terminal=True), 2: _FakeHandle(needs=1), 3: _FakeHandle(needs=2)}
    assert await lp._sweep(ib) == 0
    assert ib.cancelled == [2, 3, 3]                       # 1 was already terminal; 3 needed the retry


@pytest.mark.asyncio
async def test_sweep_reports_what_is_still_live_after_the_retry():
    ib = _FakeIB()
    ib.orders = {5: _FakeHandle(needs=99)}
    assert await lp._sweep(ib) == 1
    assert ib.cancelled == [5, 5]                          # one retry, not a loop


# -- run() wiring: refusals, cleanup and partial results -------------------------------------------

def _run_args(tmp_path, **kw):
    base = dict(port=7497, client_id=150, skip_bulk=False, out=str(tmp_path / "out.json"))
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_run(monkeypatch, ib, boot_calls=None):
    monkeypatch.setattr(lp, "IBClient", lambda *a, **k: ib)
    monkeypatch.setattr(lp, "perf", PerfRecorder())            # leave the process-wide recorder alone

    async def fake_boot(ib_, state, **kwargs):
        if boot_calls is not None:
            boot_calls.append(kwargs)
        lp.perf.record("startup.total", 900.0)
        state.connected, state.expiration, state.spx_price = True, EXP, 7702.0
    monkeypatch.setattr(lp, "boot_session", fake_boot)


def _patch_sections(monkeypatch, result, bulk_calls=None):
    async def orders_(ib, state, expiry, atm, out):
        out.update(copy.deepcopy(result["orders"]))
        return out

    async def fill_(ib, state, expiry, atm, out):
        out.update(copy.deepcopy(result["fill"]))
        return out

    async def bulk_(args, expiry, atm, out):
        if bulk_calls is not None:
            bulk_calls.append(1)
        out.update(copy.deepcopy(result["bulk"]))
        return out
    monkeypatch.setattr(lp, "_orders_section", orders_)
    monkeypatch.setattr(lp, "_during_fill", fill_)
    monkeypatch.setattr(lp, "_bulk_section", bulk_)


@pytest.mark.asyncio
async def test_run_a_clean_paper_run_prints_the_table_and_exits_zero(monkeypatch, tmp_path, capsys):
    ib = _FakeIB()
    _patch_run(monkeypatch, ib)
    _patch_sections(monkeypatch, _good_result())
    args = _run_args(tmp_path)
    assert await lp.run(args) == 0
    text = capsys.readouterr().out
    assert "PASS" in text and "MISS" not in text and "live orders left after the sweep: 0" in text
    saved = json.loads(Path(args.out).read_text(encoding="utf-8"))
    assert saved["live_orders_left"] == 0 and len(saved["rows"]) == len(lp.TARGETS)
    assert ib.disconnected


@pytest.mark.asyncio
async def test_run_with_skip_bulk_never_starts_the_bulk_section_and_still_exits_zero(monkeypatch, tmp_path, capsys):
    _patch_run(monkeypatch, _FakeIB())
    calls = []
    _patch_sections(monkeypatch, _good_result(), bulk_calls=calls)
    assert await lp.run(_run_args(tmp_path, skip_bulk=True)) == 0
    assert calls == [] and "skipped" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_exits_nonzero_when_an_order_is_left_live_and_prints_the_count(monkeypatch, tmp_path, capsys):
    ib = _FakeIB()
    ib.orders = {9: _FakeHandle(needs=99)}
    _patch_run(monkeypatch, ib)
    _patch_sections(monkeypatch, _good_result())
    assert await lp.run(_run_args(tmp_path)) == 1
    assert "live orders left after the sweep: 1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_adds_the_second_connections_leftovers_to_the_count(monkeypatch, tmp_path, capsys):
    _patch_run(monkeypatch, _FakeIB())
    result = _good_result()
    result["bulk"]["live_orders_left"] = 2
    _patch_sections(monkeypatch, result)
    assert await lp.run(_run_args(tmp_path)) == 1
    assert "live orders left after the sweep: 2" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_exits_nonzero_when_a_section_failed_every_placement(monkeypatch, tmp_path):
    _patch_run(monkeypatch, _FakeIB())
    result = _good_result()
    result["orders"]["single"] = _tally([], [], errors=5)
    _patch_sections(monkeypatch, result)
    assert await lp.run(_run_args(tmp_path)) == 1


@pytest.mark.asyncio
async def test_run_refuses_a_live_account_sends_no_order_and_disconnects(monkeypatch, tmp_path):
    h = _handlers(monkeypatch, forbid=True)
    ib = _FakeIB("U1234567")
    _patch_run(monkeypatch, ib)
    with pytest.raises(SystemExit) as ei:
        await lp.run(_run_args(tmp_path))
    assert "not a paper account" in str(ei.value) and "U1234567" not in str(ei.value)
    assert h.places == [] and ib.disconnected


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [7496, 4001])
async def test_run_refuses_a_live_port_before_connecting(monkeypatch, tmp_path, port):
    ib, boots = _FakeIB(), []
    _patch_run(monkeypatch, ib, boot_calls=boots)
    with pytest.raises(SystemExit):
        await lp.run(_run_args(tmp_path, port=port))
    assert boots == [] and ib.connects == [] and not ib.disconnected


@pytest.mark.asyncio
async def test_run_sweeps_disconnects_and_keeps_partial_results_when_a_section_raises(monkeypatch, tmp_path, capsys):
    ib = _FakeIB()
    ib.orders = {5: _FakeHandle(needs=1)}
    _patch_run(monkeypatch, ib)

    async def orders_(ib_, state, expiry, atm, out):
        out["single"] = _tally([90.0, 95.0])
        raise RuntimeError("section blew up")
    monkeypatch.setattr(lp, "_orders_section", orders_)
    args = _run_args(tmp_path)
    with pytest.raises(RuntimeError):
        await lp.run(args)
    assert ib.cancelled == [5] and ib.disconnected                       # swept, then disconnected
    assert "live orders left after the sweep: 0" in capsys.readouterr().out
    saved = json.loads(Path(args.out).read_text(encoding="utf-8"))
    assert saved["orders"]["single"]["acks_ms"] == [90.0, 95.0]           # nothing measured is lost
    assert saved["live_orders_left"] == 0 and saved["boot_ms"] == 900.0
