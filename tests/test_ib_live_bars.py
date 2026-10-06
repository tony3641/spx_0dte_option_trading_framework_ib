"""keepUpToDate plumbing in IBClient (no socket: EClient calls are stubbed) and the probe summary."""
import asyncio
from types import SimpleNamespace

import pytest

from spx_trade_desk.ib import bars_probe
from spx_trade_desk.ib.client import IBClient
from spx_trade_desk.ib.pacing import RequestPacer


def _bar(epoch, close):
    return SimpleNamespace(date=str(epoch), open=close, high=close, low=close, close=close)


@pytest.fixture
def client(monkeypatch):
    """An unconnected IBClient whose request sends are recorded; tests bind ``_loop`` themselves."""
    c = IBClient()
    calls = []
    monkeypatch.setattr("spx_trade_desk.ib.client.EClient.reqHistoricalData",
                        lambda self, *a: calls.append(("req",) + a))
    monkeypatch.setattr("spx_trade_desk.ib.client.EClient.cancelHistoricalData",
                        lambda self, req_id: calls.append(("cancel", req_id)))
    c.calls = calls
    return c


@pytest.mark.asyncio
async def test_live_request_returns_initial_bars_then_streams_updates(client):
    client._loop = asyncio.get_running_loop()
    got, errors = [], []
    task = asyncio.create_task(client.req_historical_bars_live(object(), got.append, lambda c, m: errors.append(c)))
    await asyncio.sleep(0)
    req_id = client.calls[0][1]
    assert client.calls[0][-2] is True and client.calls[0][3] == ""      # keepUpToDate, endDateTime ""
    client.historicalData(req_id, _bar(4070908800, 1.0))
    client.historicalDataEnd(req_id, "", "")
    rid, bars = await task
    assert rid == req_id and len(bars) == 1 and bars[0].date.tzinfo is not None
    client.historicalDataUpdate(req_id, _bar(4070908860, 2.0))
    await asyncio.sleep(0)
    assert [b.close for b in got] == [2.0] and got[0].date.tzinfo is not None
    client.error(req_id, 0, 366, "No historical data query found")
    client.error(req_id, 0, 2176, "warning")
    await asyncio.sleep(0)
    assert errors == [366]


@pytest.mark.asyncio
async def test_error_during_initial_load_resolves_the_request_and_reports_it(client):
    client._loop = asyncio.get_running_loop()
    errors = []
    task = asyncio.create_task(client.req_historical_bars_live(
        object(), lambda b: None, lambda c, m: errors.append((c, m)), timeout=5.0))
    await asyncio.sleep(0)
    req_id = client.calls[0][1]
    client.error(req_id, 0, 162, "Historical Market Data Service error message")
    rid, bars = await asyncio.wait_for(task, timeout=1.0)        # resolved by error(), not the timeout
    assert rid == req_id and bars == []
    await asyncio.sleep(0)
    assert errors == [(162, "Historical Market Data Service error message")]


@pytest.mark.asyncio
async def test_cancel_stops_updates_and_is_idempotent(client):
    client._loop = asyncio.get_running_loop()
    got = []
    task = asyncio.create_task(client.req_historical_bars_live(object(), got.append))
    await asyncio.sleep(0)
    req_id = client.calls[0][1]
    client.historicalDataEnd(req_id, "", "")
    await task
    client.cancel_historical_bars(req_id)
    client.cancel_historical_bars(req_id)
    client.historicalDataUpdate(req_id, _bar(4070908860, 2.0))
    await asyncio.sleep(0)
    assert got == [] and [c for c in client.calls if c[0] == "cancel"] == [("cancel", req_id)]


@pytest.mark.asyncio
async def test_live_request_waits_on_the_pacer_and_cancel_debits_it(client):
    client._loop = asyncio.get_running_loop()
    client.pacer = RequestPacer(10.0, 5, clock=lambda: 0.0)           # frozen clock: no refill
    task = asyncio.create_task(client.req_historical_bars_live(object(), lambda b: None))
    await asyncio.sleep(0)
    req_id = client.calls[0][1]
    assert client.pacer.available == 4.0
    client.historicalDataEnd(req_id, "", "")
    await task
    client.cancel_historical_bars(req_id)
    client.cancel_historical_bars(req_id)                             # a no-op cancel sends nothing
    assert client.pacer.available == 3.0


@pytest.mark.asyncio
async def test_initial_timeout_cancels_and_returns_empty(client):
    client._loop = asyncio.get_running_loop()
    rid, bars = await client.req_historical_bars_live(object(), lambda b: None, timeout=0.01)
    assert bars == [] and ("cancel", rid) in client.calls
    assert rid not in client._live_bars and rid not in client._requests


def test_probe_summary():
    s = bars_probe.summarize([0.0, 5.0, 10.0, 25.0], {"09:30": 3, "09:31": 1})
    assert s == {"updates": 4, "median_gap_s": 5.0, "max_gap_s": 15.0, "minutes_updated": 2}
    assert bars_probe.summarize([], {})["updates"] == 0


@pytest.mark.parametrize("port", ["7496", "4001"])
def test_probe_refuses_live_ports(port, monkeypatch):
    monkeypatch.setattr(bars_probe, "_run", lambda *a: pytest.fail("the probe connected"))
    with pytest.raises(SystemExit):
        bars_probe.main(["--port", port])
