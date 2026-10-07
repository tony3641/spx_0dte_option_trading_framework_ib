"""Focused tests for websocket-side IB error forwarding, broadcast and the endpoint's channel."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from spx_trade_desk.core.app_state import create_app_state
from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib.client import OrderHandle
from spx_trade_desk.web import ws as ws_mod
from spx_trade_desk.web.push import ClientChannel
from spx_trade_desk.web.ws import make_ib_error_handler


@pytest.mark.asyncio
async def test_ib_error_handler_ignores_informational_codes():
    messages = []

    async def broadcast_fn(message):
        messages.append(message)

    state = SimpleNamespace(active_trades={})
    handler = make_ib_error_handler(state, broadcast_fn)

    handler(-1, 2104, "Market data farm connection is OK", None)
    await asyncio.sleep(0)

    assert messages == []


@pytest.mark.asyncio
async def test_ib_error_handler_does_not_toast_a_refused_market_data_line():
    """Error 101 is the line-budget safety net's business (it logs and shrinks the budget): no browser toast."""
    messages = []

    async def broadcast_fn(message):
        messages.append(message)

    handler = make_ib_error_handler(SimpleNamespace(active_trades={}), broadcast_fn)
    handler(42, 101, "Max number of tickers has been reached", None)
    await asyncio.sleep(0)

    assert messages == []


@pytest.mark.asyncio
async def test_ib_error_handler_broadcasts_actionable_error_with_trade_contract():
    messages = []

    async def broadcast_fn(message):
        messages.append(message)

    bag_contract = SimpleNamespace(
        conId=0,
        symbol="SPX",
        secType="BAG",
        exchange="SMART",
        currency="USD",
        lastTradeDateOrContractMonth="",
        strike=0.0,
        right="",
        localSymbol="",
        tradingClass="",
        comboLegs=[
            SimpleNamespace(conId=1, ratio=1, action="BUY", exchange="SMART"),
            SimpleNamespace(conId=2, ratio=1, action="SELL", exchange="SMART"),
        ],
    )
    trade = OrderHandle(123, bag_contract, None)
    state = SimpleNamespace(active_trades={123: trade})
    handler = make_ib_error_handler(state, broadcast_fn)

    handler(123, 10043, "Missing or invalid NonGuaranteed value.", None)
    await asyncio.sleep(0)

    assert len(messages) == 1
    payload = messages[0]
    assert payload["type"] == "ib_error"
    assert payload["data"]["orderId"] == 123
    assert payload["data"]["errorCode"] == 10043
    assert payload["data"]["contract"]["secType"] == "BAG"
    assert payload["data"]["contract"]["comboLegs"][0]["action"] == "BUY"


class ScriptWS:
    """Inbound script; after it, waits on ``release`` before disconnecting."""

    def __init__(self, messages, release=None):
        self._queue = list(messages)
        self.release = release
        self.sent = []

    async def accept(self):
        pass

    async def receive_text(self):
        if self._queue:
            return self._queue.pop(0)
        if self.release is not None:
            await self.release.wait()
        raise WebSocketDisconnect()

    async def send_text(self, text):
        self.sent.append(json.loads(text))


async def _noop(_m):
    return None


@pytest.mark.asyncio
async def test_broadcast_returns_without_awaiting_a_stuck_socket():
    state = create_app_state()

    class Stuck:
        async def send_text(self, text):
            await asyncio.Event().wait()

    ch = ClientChannel(Stuck(), send_timeout=60)
    ch.start()
    state.ws_clients[object()] = ch
    t0 = time.perf_counter()
    for _ in range(50):
        await ws_mod.broadcast(state, {"type": "status", "data": {}})
    assert (time.perf_counter() - t0) < 0.05
    await ch.aclose(drain=False)


@pytest.mark.asyncio
async def test_broadcast_feeds_the_alert_bridge_and_stamps_ts():
    state = create_app_state()
    seen = []
    state.alert_bridge = SimpleNamespace(forward=seen.append)
    sock = ScriptWS([])
    ch = ClientChannel(sock)
    ch.start()
    state.ws_clients[sock] = ch
    await ws_mod.broadcast(state, {"type": "order_status", "data": {"status": "Filled"}})
    await asyncio.sleep(0.01)
    assert seen and seen[0]["type"] == "order_status"
    assert sock.sent[0]["type"] == "order_status" and isinstance(sock.sent[0]["ts"], float)
    await ch.aclose(drain=False)


@pytest.mark.asyncio
async def test_cancel_does_not_block_the_read_loop(monkeypatch):
    state = create_app_state()
    release = asyncio.Event()
    cancel_go = asyncio.Event()

    async def slow_cancel(ib, st, order_id, refresh_fn=None):
        await cancel_go.wait()
        return {"type": "order_status", "data": {"status": "Cancelled", "orderId": order_id, "message": "ok"}}

    monkeypatch.setattr(ws_mod, "handle_cancel_order", slow_cancel)
    sock = ScriptWS(["cancel_order:77", "set_tab:log"], release=release)
    task = asyncio.create_task(ws_mod.websocket_endpoint(sock, None, state, _noop))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if any(m["type"] == "log_history" for m in sock.sent):
            break
    types = [m["type"] for m in sock.sent]
    assert "log_history" in types and not any(
        m["type"] == "order_status" and m["data"].get("orderId") == 77 for m in sock.sent)
    cancel_go.set()
    for _ in range(50):
        await asyncio.sleep(0.005)
        if any(m["type"] == "order_status" for m in sock.sent):
            break
    release.set()
    await task
    replies = [m for m in sock.sent if m["type"] == "order_status"]
    assert replies and replies[0]["data"]["orderId"] == 77


@pytest.mark.asyncio
async def test_a_cancel_sent_right_before_the_tab_closes_still_runs(monkeypatch):
    """The disconnect must not cancel a cancel_order that has not reached IB yet."""
    state = create_app_state()
    done = []

    async def recording_cancel(ib, st, order_id, refresh_fn=None):
        await asyncio.sleep(0.01)
        done.append(order_id)
        return {"type": "order_status", "data": {"status": "Cancelled", "orderId": order_id, "message": "ok"}}

    monkeypatch.setattr(ws_mod, "handle_cancel_order", recording_cancel)
    sock = ScriptWS(["cancel_order:78"])
    await ws_mod.websocket_endpoint(sock, None, state, _noop)
    for _ in range(50):
        await asyncio.sleep(0.005)
        if done:
            break
    assert done == [78]


@pytest.mark.asyncio
async def test_cancel_replies_name_their_order_and_are_tagged_as_cancel_replies(monkeypatch):
    """place_order replies share the order_status type; the browser tells a cancel reply apart by the tag,
    including an early error that has no order id of its own."""
    state = create_app_state()
    release = asyncio.Event()

    async def cancel(ib, st, order_id, refresh_fn=None):
        if order_id == 79:
            return {"type": "order_status", "data": {"status": "Error", "message": "Order 79 not found in open trades"}}
        return {"type": "order_status", "data": {"status": "PendingCancel", "orderId": order_id, "message": "x"}}

    monkeypatch.setattr(ws_mod, "handle_cancel_order", cancel)
    sock = ScriptWS(["cancel_order:79", "cancel_order:80"], release=release)
    task = asyncio.create_task(ws_mod.websocket_endpoint(sock, None, state, _noop))
    for _ in range(100):
        await asyncio.sleep(0.005)
        if sum(m["type"] == "order_status" for m in sock.sent) >= 2:
            break
    release.set()
    await task
    replies = {m["data"]["orderId"]: m["data"] for m in sock.sent if m["type"] == "order_status"}
    assert replies[79] == {"status": "Error", "message": "Order 79 not found in open trades", "orderId": 79, "action": "cancel"}
    assert replies[80]["action"] == "cancel" and replies[80]["status"] == "PendingCancel"


@pytest.mark.asyncio
async def test_bad_cancel_order_id_gets_an_error_reply():
    state = create_app_state()
    sock = ScriptWS(["cancel_order:abc"])
    await ws_mod.websocket_endpoint(sock, None, state, _noop)
    errs = [m for m in sock.sent if m["type"] == "order_status"]
    assert errs and errs[0]["data"]["status"] == "Error"
    assert errs[0]["data"]["action"] == "cancel"      # like every cancel reply: never taken for a place reply


@pytest.mark.asyncio
async def test_perf_report_is_recorded():
    perf.reset()
    state = create_app_state()
    report = json.dumps({"spans": {"chain_tick.paint": [2.0, 3.0]}})
    sock = ScriptWS([f"perf_report:{report}"])
    await ws_mod.websocket_endpoint(sock, None, state, _noop)
    assert perf.snapshot()["metrics"]["client.chain_tick.paint"]["n"] == 2


@pytest.mark.asyncio
async def test_endpoint_registers_and_removes_its_channel():
    state = create_app_state()
    sock = ScriptWS([])
    await ws_mod.websocket_endpoint(sock, None, state, _noop)
    assert state.ws_clients == {}
    assert [m["type"] for m in sock.sent][:2] == ["init", "strategy_list"]


@pytest.mark.asyncio
async def test_init_carries_the_price_snapshot_instead_of_price_history():
    state = create_app_state()
    state.price_session_date = "2099-01-02"
    state.price_history.append({"time": "2099-01-02T09:30:00-05:00", "time_short": "09:30",
                                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    sock = ScriptWS([])
    await ws_mod.websocket_endpoint(sock, None, state, _noop)
    data = next(m for m in sock.sent if m["type"] == "init")["data"]
    assert "price_history" not in data
    assert data["price"]["session_date"] == "2099-01-02" and len(data["price"]["bars"]) == 1
    assert data["price"]["mode"] == "historical" and data["price"]["overnight"] == []


@pytest.mark.asyncio
async def test_a_malformed_perf_report_keeps_the_connection(monkeypatch):
    state = create_app_state()

    def boom(_raw):
        raise RuntimeError("bad report")

    monkeypatch.setattr(ws_mod, "record_client_perf", boom)
    huge = '{"spans": {"chain_tick.paint": [' + "9" * 400 + "]}}"
    sock = ScriptWS([f"perf_report:{huge}", "set_tab:log"])
    await ws_mod.websocket_endpoint(sock, None, state, _noop)
    assert any(m["type"] == "log_history" for m in sock.sent)


@pytest.mark.asyncio
async def test_a_new_client_asks_the_chain_stream_for_whole_rows():
    state = create_app_state()
    assert state.chain_resync_requested is False
    sock = ScriptWS([])
    await ws_mod.websocket_endpoint(sock, None, state, _noop)
    assert state.chain_resync_requested is True
    assert sock.sent[0]["type"] == "init"
