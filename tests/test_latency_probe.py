"""latency_probe: verdicts, safety guards and payload shapes (the live run itself is manual)."""
from types import SimpleNamespace

import pytest

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


# -- refusal paths through the real entry points (fakes only: no socket is ever opened) -----------

class _FakeIB:
    """The slice of IBClient the probe touches around a refusal; any order attempt is recorded."""

    def __init__(self, account_code):
        self._account_code = account_code
        self.connected = True
        self.orders = {}
        self.disconnected = False
        self.placed = []

    async def connect(self, *args, **kwargs):
        return None

    def isConnected(self):
        return self.connected

    def place_order(self, *args, **kwargs):
        self.placed.append(args)
        raise AssertionError("the probe must not place an order here")

    def disconnect(self):
        self.connected = False
        self.disconnected = True


def _forbid_handlers(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("handle_place_order must not be reached")
    monkeypatch.setattr(lp.orders, "handle_place_order", boom)


@pytest.mark.asyncio
async def test_place_cancel_refuses_an_unsafe_payload_before_any_order_is_sent(monkeypatch):
    _forbid_handlers(monkeypatch)
    ib = _FakeIB("DU0000001")
    bad = lp.single_payload(EXP, 7700.0)
    bad["legs"][0]["lmtPrice"] = 5.0
    with pytest.raises(ValueError):
        await lp._place_cancel(ib, SimpleNamespace(), bad)
    assert ib.placed == []


@pytest.mark.asyncio
async def test_run_refuses_a_live_account_sends_no_order_and_disconnects(monkeypatch):
    _forbid_handlers(monkeypatch)
    ib = _FakeIB("U1234567")
    monkeypatch.setattr(lp, "IBClient", lambda *a, **k: ib)
    monkeypatch.setattr(lp, "perf", lp.perf.__class__())        # leave the process-wide recorder alone

    async def fake_boot(ib_, state, **kwargs):
        state.connected = True
    monkeypatch.setattr(lp, "boot_session", fake_boot)

    args = SimpleNamespace(port=7497, client_id=150, skip_bulk=False, out="")
    with pytest.raises(SystemExit) as ei:
        await lp.run(args)
    assert "not a paper account" in str(ei.value) and "U1234567" not in str(ei.value)
    assert ib.placed == [] and ib.disconnected


@pytest.mark.asyncio
async def test_bulk_section_refuses_a_live_account_sends_no_order_and_disconnects(monkeypatch):
    _forbid_handlers(monkeypatch)
    ib2 = _FakeIB("U1234567")
    monkeypatch.setattr(lp, "IBClient", lambda *a, **k: ib2)
    args = SimpleNamespace(port=7497, client_id=150)
    with pytest.raises(SystemExit):
        await lp._bulk_section(args, EXP, 7700.0)
    assert ib2.placed == [] and ib2.disconnected
