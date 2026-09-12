"""Focused tests for websocket-side IB error forwarding."""

import asyncio
from types import SimpleNamespace

import pytest

from spx_trade_desk.ib.client import OrderHandle
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