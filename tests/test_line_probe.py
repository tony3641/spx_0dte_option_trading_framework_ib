"""line_probe: the pure recommendation and the guards around the live ramp (no IB connection)."""
import asyncio
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from spx_trade_desk.ib import line_probe
from spx_trade_desk.ib.line_probe import recommend_lines


def test_recommendation_is_ninety_percent_of_what_ib_granted():
    lines, text = recommend_lines(100, hit_cap=False)
    assert lines == 90
    assert "exactly 100" in text and "MARKET_DATA_LINES = 90" in text


def test_a_reached_cap_is_reported_as_a_lower_bound():
    lines, text = recommend_lines(1500, hit_cap=True)
    assert lines == 1350
    assert "at least 1500" in text and "raise --cap" in text


def test_zero_granted_lines_recommend_zero():
    assert recommend_lines(0, hit_cap=False)[0] == 0

# -- what the ramp result means ------------------------------------------------------------------

def test_a_ramp_that_sent_the_whole_cap_without_a_refusal_reports_the_cap():
    lines, text = line_probe.summarize(granted=2000, refused=0, sent=2000, cap=2000)
    assert lines == 1800 and "cap was reached" in text and "pool exhausted" not in text


def test_a_ramp_that_ran_out_of_contracts_reports_an_exhausted_pool_not_a_reached_cap():
    lines, text = line_probe.summarize(granted=700, refused=0, sent=700, cap=2000)
    assert lines == 630
    assert "pool exhausted" in text and "at least 700" in text
    assert "cap was reached" not in text


def test_a_refusal_reports_the_exact_allowance_whatever_was_sent():
    lines, text = line_probe.summarize(granted=100, refused=3, sent=103, cap=2000)
    assert lines == 90 and "exactly 100" in text


# -- contract pool guards --------------------------------------------------------------------------

class _PoolIb:
    def __init__(self, spx_rows):
        self.spx_rows = spx_rows

    async def req_contract_details(self, contract, timeout=30.0):
        return self.spx_rows

    async def req_chain_contract_details(self, symbol, expiry, trading_class, timeout=60.0):
        return []


@pytest.mark.asyncio
async def test_a_timed_out_spx_lookup_raises_a_clear_error_not_an_index_error():
    with pytest.raises(line_probe.ProbeError) as e:
        await line_probe._contract_pool(_PoolIb([]), cap=10)
    assert "SPX" in str(e.value) and "TWS" in str(e.value)


@pytest.mark.asyncio
async def test_an_empty_contract_pool_raises_a_clear_error(monkeypatch):
    async def no_chain(ib, spx):
        return [], []

    monkeypatch.setattr(line_probe, "get_chain_params", no_chain)
    row = SimpleNamespace(contract=SimpleNamespace(conId=416904))
    with pytest.raises(line_probe.ProbeError) as e:
        await line_probe._contract_pool(_PoolIb([row]), cap=10)
    assert "SPXW" in str(e.value)


def test_main_prints_a_probe_error_and_exits_nonzero(monkeypatch, capsys):
    def boom(coro):
        coro.close()
        raise line_probe.ProbeError("no contract details for SPX")

    monkeypatch.setattr(sys, "argv", ["line_probe"])
    monkeypatch.setattr(asyncio, "run", boom)
    with pytest.raises(SystemExit) as e:
        line_probe.main()
    assert e.value.code == 1
    assert "no contract details for SPX" in capsys.readouterr().err


# -- the event-loop policy is main's business, not an import side effect ----------------------------

def test_importing_the_module_does_not_change_the_event_loop_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: calls.append(policy))
    spec = importlib.util.find_spec("spx_trade_desk.ib.line_probe")
    fresh = importlib.util.module_from_spec(spec)          # a throw-away copy: sys.modules is untouched
    spec.loader.exec_module(fresh)
    assert calls == []


def test_main_installs_the_selector_policy_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: calls.append(policy))
    monkeypatch.setattr(asyncio, "WindowsSelectorEventLoopPolicy", asyncio.DefaultEventLoopPolicy,
                        raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["line_probe"])
    ran = []
    monkeypatch.setattr(asyncio, "run", lambda coro: (coro.close(), ran.append(1)))
    line_probe.main()
    assert len(calls) == 1 and ran == [1]
