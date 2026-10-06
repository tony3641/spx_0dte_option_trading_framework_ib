"""web/push.py: message kinds, coalescing, ordering, merge, log overflow, slow-client close."""
import asyncio
import json

import pytest

from spx_trade_desk.core.perf import perf
from spx_trade_desk.web import push


class SlowWS:
    """Records sends; ``gate`` (an Event) blocks every send until set."""

    def __init__(self, gate=None, fail=False):
        self.sent = []
        self.gate = gate
        self.fail = fail
        self.closed = False

    async def send_text(self, text):
        if self.fail:
            raise RuntimeError("socket gone")
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        self.closed = True


def _types(ws):
    return [m["type"] for m in ws.sent]


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


def test_message_kinds():
    assert push.message_kind("status") == "latest"
    assert push.message_kind("price_bar") == "latest"
    assert push.message_kind("chain_tick") == "merge"
    assert push.message_kind("log") == "log"
    assert push.message_kind("order_status") == "critical"
    assert push.message_kind("something_new") == "critical"


def test_stamp_copies_and_adds_ts():
    m = {"type": "status", "data": {}}
    s = push.stamp(m, now_ms=1234.0)
    assert s["ts"] == 1234.0 and "ts" not in m


def test_encode_rejects_nan(caplog):
    assert push.encode({"type": "gex", "data": {"x": float("nan")}}) is None
    assert "gex" in caplog.text


@pytest.mark.asyncio
async def test_latest_wins_replaces_an_unsent_message_of_the_same_key():
    gate = asyncio.Event()
    ws = SlowWS(gate)
    ch = push.ClientChannel(ws)
    ch.start()
    ch.send_message({"type": "ping"})                     # occupies the writer until the gate opens
    await _settle()
    for i in range(5):
        ch.send_message({"type": "status", "data": {"n": i}})
    gate.set()
    await _settle()
    statuses = [m for m in ws.sent if m["type"] == "status"]
    assert [m["data"]["n"] for m in statuses] == [4]
    await ch.aclose(drain=False)


@pytest.mark.asyncio
async def test_price_bar_coalesces_per_minute_only():
    gate = asyncio.Event()
    ws = SlowWS(gate)
    ch = push.ClientChannel(ws)
    ch.start()
    ch.send_message({"type": "ping"})
    await _settle()
    for minute, close in (("09:31", 1.0), ("09:31", 2.0), ("09:32", 3.0)):
        ch.send_message({"type": "price_bar", "data": {"session_date": "2099-01-02",
                         "bar": {"time": f"2099-01-02T{minute}:00-05:00", "close": close}}})
    gate.set()
    await _settle()
    bars = [m["data"]["bar"]["close"] for m in ws.sent if m["type"] == "price_bar"]
    assert bars == [2.0, 3.0]
    await ch.aclose(drain=False)


@pytest.mark.asyncio
async def test_critical_goes_before_log_merge_and_latest():
    gate = asyncio.Event()
    ws = SlowWS(gate)
    ch = push.ClientChannel(ws)
    ch.start()
    ch.send_message({"type": "ping"})
    await _settle()
    ch.send_message({"type": "status", "data": {}})
    ch.send_message({"type": "chain_tick", "data": {"ticks": [{"strike": 5000.0, "right": "C", "bid": 1.0}]}})
    ch.send_message({"type": "log", "data": {"msg": "x"}})
    ch.send_message({"type": "order_status", "data": {"status": "Submitted"}})
    gate.set()
    await _settle()
    assert _types(ws) == ["ping", "order_status", "log", "chain_tick", "status"]
    await ch.aclose(drain=False)


@pytest.mark.asyncio
async def test_chain_ticks_merge_field_by_field():
    gate = asyncio.Event()
    ws = SlowWS(gate)
    ch = push.ClientChannel(ws)
    ch.start()
    ch.send_message({"type": "ping"})
    await _settle()
    ch.send_message({"type": "chain_tick", "data": {"ticks": [{"strike": 5000.0, "right": "C", "bid": 1.0, "ask": 1.2}],
                                                    "timestamp_iso": "a"}})
    ch.send_message({"type": "chain_tick", "data": {"ticks": [{"strike": 5000.0, "right": "C", "bid": 1.1},
                                                              {"strike": 5005.0, "right": "P", "ask": 2.0}],
                                                    "timestamp_iso": "b"}})
    gate.set()
    await _settle()
    ticks = [m for m in ws.sent if m["type"] == "chain_tick"]
    assert len(ticks) == 1
    by_key = {(t["strike"], t["right"]): t for t in ticks[0]["data"]["ticks"]}
    assert by_key[(5000.0, "C")] == {"strike": 5000.0, "right": "C", "bid": 1.1, "ask": 1.2}
    assert by_key[(5005.0, "P")]["ask"] == 2.0
    assert ticks[0]["data"]["timestamp_iso"] == "b" and "ts" in ticks[0]
    await ch.aclose(drain=False)


@pytest.mark.asyncio
async def test_log_overflow_drops_oldest_and_says_so():
    gate = asyncio.Event()
    ws = SlowWS(gate)
    ch = push.ClientChannel(ws, log_max=3)
    ch.start()
    ch.send_message({"type": "ping"})
    await _settle()
    for i in range(5):
        ch.send_message({"type": "log", "data": {"seq": i, "msg": f"line {i}"}})
    gate.set()
    await _settle()
    logs = [m["data"] for m in ws.sent if m["type"] == "log"]
    assert "2 log lines dropped" in logs[0]["msg"]
    assert [d["seq"] for d in logs[1:]] == [2, 3, 4]
    assert not ch.closed


@pytest.mark.asyncio
async def test_critical_backlog_over_cap_closes_the_client():
    gate = asyncio.Event()
    ws = SlowWS(gate)
    closed = []
    ch = push.ClientChannel(ws, backlog_max=3, on_close=closed.append)
    ch.start()
    ch.send_message({"type": "ping"})
    await _settle()
    for i in range(5):
        ch.send_message({"type": "order_status", "data": {"n": i}})
    await _settle()
    assert ch.closed and closed == [ch] and ws.closed
    gate.set()


@pytest.mark.asyncio
async def test_a_send_over_the_timeout_closes_only_that_client():
    stuck = SlowWS(asyncio.Event())                         # never released
    fine = SlowWS()
    closed = []
    a = push.ClientChannel(stuck, send_timeout=0.05, on_close=closed.append)
    b = push.ClientChannel(fine, send_timeout=0.05, on_close=closed.append)
    a.start(); b.start()
    for ch in (a, b):
        ch.send_message({"type": "status", "data": {}})
    await asyncio.sleep(0.2)
    assert a.closed and not b.closed and closed == [a]
    assert _types(fine) == ["status"]
    await b.aclose(drain=False)


@pytest.mark.asyncio
async def test_send_failure_closes_the_client():
    closed = []
    ch = push.ClientChannel(SlowWS(fail=True), on_close=closed.append)
    ch.start()
    ch.send_message({"type": "status", "data": {}})
    await _settle()
    assert ch.closed and closed == [ch]


@pytest.mark.asyncio
async def test_send_text_is_critical_and_aclose_drains():
    ws = SlowWS()
    ch = push.ClientChannel(ws)                              # not started: aclose must still deliver
    await ch.send_text(json.dumps({"type": "order_status", "data": {"status": "Filled"}}))
    ch.send_message({"type": "status", "data": {}})
    await ch.aclose(drain=True)
    assert _types(ws) == ["order_status", "status"] and ch.closed


@pytest.mark.asyncio
async def test_queue_wait_and_send_spans_are_recorded():
    perf.reset()
    ws = SlowWS()
    ch = push.ClientChannel(ws)
    ch.start()
    ch.send_message({"type": "status", "data": {}})
    await _settle()
    snap = perf.snapshot()["metrics"]
    assert "push.queue_wait" in snap and "push.send" in snap
    await ch.aclose(drain=False)


def test_record_client_perf_accepts_only_sane_samples():
    perf.reset()
    raw = json.dumps({"spans": {"chain_tick.paint": [3.0, 4.5], "bad name!": [1.0],
                                "longtask": [80.0, float("inf"), -1.0, 70000.0]}})
    assert push.record_client_perf(raw) == 3
    m = perf.snapshot()["metrics"]
    assert m["client.chain_tick.paint"]["n"] == 2 and m["client.longtask"]["n"] == 1
    assert push.record_client_perf("not json") == 0
