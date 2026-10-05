# tests/test_ib_connection.py
import asyncio
import importlib

import pytest

from spx_trade_desk.core import config
from spx_trade_desk.core.app_state import create_app_state
from spx_trade_desk.ib.connection import connect_ib


class FakeIb:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def connect(self, host, port, client_id, timeout=None):
        self.calls.append((host, port, client_id, timeout))
        if self.fail:
            raise ConnectionRefusedError("no gateway")


def test_connect_records_port_on_success(monkeypatch):
    monkeypatch.delenv("IB_PORT", raising=False)
    importlib.reload(config)
    state = create_app_state()
    assert state.ib_port == config.IB_PORT
    fib = FakeIb()
    asyncio.run(connect_ib(fib, state, port=4002))
    assert state.ib_port == 4002
    assert fib.calls[0][1] == 4002


def test_connect_failure_leaves_port_unchanged(monkeypatch):
    monkeypatch.delenv("IB_PORT", raising=False)
    importlib.reload(config)
    state = create_app_state()
    fib = FakeIb(fail=True)
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(connect_ib(fib, state, port=4002))
    assert state.ib_port == config.IB_PORT
    assert state.connected is False


from types import SimpleNamespace

from spx_trade_desk.ib.connection import index_price, setup_vix1d_subscription


def test_index_price_prefers_last_then_bid_then_close():
    assert index_price(SimpleNamespace(last=12.5, bid=12.0, close=11.0)) == 12.5
    assert index_price(SimpleNamespace(last=None, bid=12.0, close=11.0)) == 12.0
    assert index_price(SimpleNamespace(last=-1.0, bid=0.0, close=11.0)) == 11.0
    assert index_price(SimpleNamespace(last=None, bid=None, close=None)) is None
    assert index_price(None) is None


def test_setup_vix1d_subscription_uses_a_fixed_line():
    from tests.conftest import MockIBClient
    ib = MockIBClient(line_shares={"fixed": 1, "order": 0, "poll": 1, "stream": 0})
    state = create_app_state()
    asyncio.run(setup_vix1d_subscription(ib, state))
    assert state.vix1d_stream.contract.symbol == "VIX1D"
    assert ib.line_budget.used("fixed") == 1
