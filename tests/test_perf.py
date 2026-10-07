"""PerfRecorder: ring buffers, percentiles, counters, spans, and where it is exposed."""
import asyncio
import logging

import pytest
from starlette.requests import Request

from spx_trade_desk.core.perf import PerfRecorder, perf


def test_percentiles_use_nearest_rank():
    p = PerfRecorder()
    for v in range(1, 101):
        p.record("x", v)
    s = p.snapshot()["metrics"]["x"]
    assert (s["n"], s["p50"], s["p95"], s["max"], s["last"]) == (100, 50, 95, 100, 100)


def test_ring_buffer_keeps_only_the_latest_samples():
    p = PerfRecorder(size=3)
    for v in (1, 2, 3, 4):
        p.record("x", v)
    s = p.snapshot()["metrics"]["x"]
    assert s["n"] == 3 and s["max"] == 4 and s["p50"] == 3      # sorted [2, 3, 4]


def test_counters_accumulate_and_reset_clears_everything():
    p = PerfRecorder()
    p.count("registry.hit")
    p.count("registry.hit", 2)
    p.record("x", 1.0)
    assert p.counter("registry.hit") == 3 and p.counter("never") == 0
    assert p.snapshot()["counters"] == {"registry.hit": 3}
    p.reset()
    assert p.snapshot() == {"metrics": {}, "counters": {}}


@pytest.mark.asyncio
async def test_timer_spans_an_await():
    ticks = iter([10.0, 10.25])
    p = PerfRecorder(clock=lambda: next(ticks))
    with p.timer("slow"):
        await asyncio.sleep(0)
    assert p.snapshot()["metrics"]["slow"]["last"] == 250.0


def test_timer_records_even_when_the_block_raises():
    ticks = iter([0.0, 0.5])
    p = PerfRecorder(clock=lambda: next(ticks))
    with pytest.raises(RuntimeError):
        with p.timer("boom"):
            raise RuntimeError("x")
    assert p.snapshot()["metrics"]["boom"]["n"] == 1


def test_summary_line_lists_metrics_and_counters_and_is_empty_without_data():
    p = PerfRecorder()
    assert p.summary_line() == ""
    p.record("order.place_to_ack.single", 120.0)
    p.count("ib.error_101")
    line = p.summary_line()
    assert line.startswith("perf:") and "order.place_to_ack.single" in line
    assert "n=1" in line and "ib.error_101=1" in line


def test_summary_line_keeps_one_decimal_so_sub_millisecond_spans_do_not_read_zero():
    p = PerfRecorder()
    p.record("registry.hit", 0.4)
    p.record("order.place_to_ack.single", 120.0)
    line = p.summary_line()
    assert "registry.hit n=1 p50=0.4ms p95=0.4ms" in line
    assert "order.place_to_ack.single n=1 p50=120.0ms p95=120.0ms" in line


# -- wiring: connect span, periodic log line, /api/perf ---------------------------------------

@pytest.mark.asyncio
async def test_connect_ib_records_a_connect_span(app_state):
    from spx_trade_desk.ib.connection import connect_ib
    from tests.conftest import MockIBClient
    perf.reset()
    await connect_ib(MockIBClient(connected=False), app_state)
    assert perf.snapshot()["metrics"]["ib.connect"]["n"] == 1


def test_maybe_log_perf_logs_once_per_interval(caplog):
    from spx_trade_desk.web.ws import maybe_log_perf
    perf.reset()
    perf.record("order.place_to_ack.single", 12.0)
    with caplog.at_level(logging.INFO):
        assert maybe_log_perf(0.0, 10.0, 60.0) == 0.0            # not due yet
        assert "perf:" not in caplog.text
        assert maybe_log_perf(0.0, 61.0, 60.0) == 61.0           # due
    assert "order.place_to_ack.single" in caplog.text
    assert maybe_log_perf(0.0, 1e9, 0) == 0.0                    # 0 disables the line


def _req(host="127.0.0.1"):
    return Request({"type": "http", "client": (host, 5000)})


def test_api_perf_returns_the_snapshot_and_is_localhost_only():
    from fastapi import HTTPException
    from spx_trade_desk import server
    perf.reset()
    perf.record("order.place_to_ack.single", 12.0)
    perf.count("registry.hit")
    out = asyncio.run(server.get_perf(_req()))
    assert out["metrics"]["order.place_to_ack.single"]["n"] == 1
    assert out["counters"]["registry.hit"] == 1
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.get_perf(_req("10.0.0.5")))
    assert exc.value.status_code == 403
