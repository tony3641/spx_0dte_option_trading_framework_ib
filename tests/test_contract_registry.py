"""ContractRegistry: bulk listing, authoritative misses, single-flight, order-path resolution."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from spx_trade_desk.core import config
from spx_trade_desk.ib.contracts import ContractKey, ContractRegistry, _option_contract
from spx_trade_desk.ib.pacing import RequestPacer

EXP = "20261005"


def det(strike, right, con_id, expiry=EXP, cls="SPXW", symbol="SPX", min_tick=0.05):
    c = SimpleNamespace(conId=con_id, symbol=symbol, secType="OPT", tradingClass=cls,
                        lastTradeDateOrContractMonth=expiry, strike=float(strike), right=right,
                        exchange="SMART", currency="USD", multiplier="100")
    return SimpleNamespace(minTick=min_tick, contract=c)


class ListIb:
    """Serves a prepared bulk listing and records every request."""

    def __init__(self, listing=None):
        self.pacer = RequestPacer(0, 1)
        self.listing = listing if listing is not None else []
        self.bulk_calls = 0
        self.single_calls = []              # (strike, right) per single qualification
        self.unlisted = set()
        self.gate = None
        self._next = 9000

    async def req_chain_contract_details(self, symbol, expiry, trading_class, timeout=60.0):
        self.bulk_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.listing, BaseException):
            raise self.listing
        return list(self.listing)

    async def req_contract_details(self, contract, timeout=30.0):
        self.single_calls.append((contract.strike, contract.right))
        if contract.strike in self.unlisted:
            return []
        self._next += 1
        return [det(contract.strike, contract.right, self._next, contract.lastTradeDateOrContractMonth,
                    contract.tradingClass, contract.symbol)]


def _reg(**kw):
    kw.setdefault("cooldown", 120.0)
    kw.setdefault("min_bulk", 1)
    kw.setdefault("today", lambda: "20261005")
    return ContractRegistry(**kw)


def _key(strike, right, expiry=EXP, cls="SPXW"):
    return ContractKey.of("SPX", cls, expiry, strike, right)


def _req(strike, right="P", expiry=EXP, cls="SPXW", exchange="SMART"):
    return _option_contract("SPX", expiry, strike, right, exchange, cls)


# -- bulk fill ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bulk_fill_keys_each_contract_by_its_own_fields():
    ib = ListIb([det(7700, "P", 1), det(7700, "C", 2), det(7705, "P", 3, min_tick=0.1)])
    reg = _reg()
    assert await reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=0.0) == 3
    got = reg.get(_key(7705, "P"))
    assert (got.contract.conId, got.min_tick, got.source) == (3, 0.1, "bulk")
    assert reg.get(_key(7700, "C")).contract.conId == 2
    assert await reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=1.0) == 3    # already listed
    assert ib.bulk_calls == 1


@pytest.mark.asyncio
async def test_bulk_rows_that_do_not_describe_the_request_are_rejected():
    rows = [det(7700, "P", 1),
            det(7701, "P", 2, cls="SPX"),               # wrong trading class
            det(7702, "P", 3, expiry="20261006"),       # wrong expiry
            det(7703, "P", 0),                          # no conId
            det(7704, "X", 5),                          # bad right
            det(0, "P", 6),                             # no strike
            det(7706, "P", 7, symbol="XSP")]            # wrong symbol
    reg = _reg()
    assert await reg.ensure_chain(ListIb(rows), "SPX", EXP, "SPXW", now=0.0) == 1
    assert len(reg) == 1 and reg.get(_key(7700, "P")).contract.conId == 1


@pytest.mark.asyncio
async def test_a_duplicate_key_with_another_conid_keeps_the_first(caplog):
    reg = _reg()
    with caplog.at_level(logging.WARNING):
        await reg.ensure_chain(ListIb([det(7700, "P", 1), det(7700, "P", 2)]), "SPX", EXP, "SPXW", now=0.0)
    assert reg.get(_key(7700, "P")).contract.conId == 1
    assert "duplicate" in caplog.text.lower()


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_bulk_request_and_a_cancelled_caller_does_not_kill_it():
    ib = ListIb([det(7700, "P", 1)])
    ib.gate = asyncio.Event()
    reg = _reg()
    t1 = asyncio.create_task(reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=0.0))
    t2 = asyncio.create_task(reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=0.0))
    await asyncio.sleep(0.01)
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1
    ib.gate.set()
    assert await t2 == 1 and ib.bulk_calls == 1


# -- fallback ----------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("listing", [RuntimeError("farm down"), [], [det(7700, "P", 1)]])
async def test_a_failed_or_short_bulk_falls_back_to_single_qualification_for_the_requested_keys(listing):
    ib = ListIb(listing)
    reg = _reg(min_bulk=40)
    got = await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P"), (7710, "C")], now=0.0)
    assert set(got) == {(7705.0, "P"), (7710.0, "C")}
    assert sorted(ib.single_calls) == [(7705.0, "P"), (7710.0, "C")]      # only what was asked for
    assert ib.bulk_calls == 1


@pytest.mark.asyncio
async def test_a_failed_bulk_is_not_retried_inside_the_cooldown():
    ib = ListIb([])
    reg = _reg(min_bulk=40)
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=0.0)
    await reg.qualify_keys(ib, EXP, "SPXW", [(7710, "P")], now=10.0)
    assert ib.bulk_calls == 1
    ib.listing = [det(s, r, 100 + i) for i, (s, r) in
                  enumerate((s, r) for s in range(7700, 7800, 5) for r in "CP")]     # 40 rows
    got = await reg.qualify_keys(ib, EXP, "SPXW", [(7715, "P")], now=130.0)
    assert ib.bulk_calls == 2 and (7715.0, "P") in got


# -- misses are authoritative ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_key_absent_from_the_listing_becomes_unknown_without_a_request():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    assert await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=1.0) == {}
    assert ib.single_calls == [] and ib.bulk_calls == 1 and reg.unknown_count() == 1
    assert await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=60.0) == {}
    assert ib.bulk_calls == 1                          # inside the cooldown: no re-list, no request


@pytest.mark.asyncio
async def test_a_due_unknown_key_triggers_one_relist_not_a_per_key_retry():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P"), (7710, "P")], now=1.0)
    ib.listing = [det(7700, "P", 1), det(7705, "P", 2), det(7710, "P", 3)]   # strikes added intraday
    got = await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P"), (7710, "P")], now=121.0)
    assert set(got) == {(7705.0, "P"), (7710.0, "P")}
    assert ib.bulk_calls == 2 and ib.single_calls == [] and reg.unknown_count() == 0


@pytest.mark.asyncio
async def test_manual_relist_forces_the_next_listing_even_inside_the_cooldown():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=1.0)
    ib.listing = [det(7700, "P", 1), det(7705, "P", 2)]
    reg.relist(EXP)
    got = await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=2.0)
    assert (7705.0, "P") in got and ib.bulk_calls == 2
    reg.relist()
    await reg.qualify_keys(ib, EXP, "SPXW", [(7700, "P")], now=3.0)         # nothing missing: no request
    assert ib.bulk_calls == 2


@pytest.mark.asyncio
async def test_manual_relist_during_a_failed_bulk_retries_the_bulk_instead_of_waiting_out_the_cooldown():
    ib = ListIb(RuntimeError("farm down"))
    ib.unlisted = {7705.0}                              # the single fallback cannot resolve it either
    reg = _reg(min_bulk=40)
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=0.0)
    assert (ib.bulk_calls, len(ib.single_calls)) == (1, 1)
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=10.0)
    assert (ib.bulk_calls, len(ib.single_calls)) == (1, 1)                   # backing off: nothing sent
    reg.relist("20261006")                              # another expiry: no effect here
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=11.0)
    assert (ib.bulk_calls, len(ib.single_calls)) == (1, 1)
    ib.listing = [det(s, r, 100 + i) for i, (s, r) in
                  enumerate((s, r) for s in range(7700, 7800, 5) for r in "CP")]     # 40 rows
    reg.relist(EXP)
    got = await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=12.0)
    assert (7705.0, "P") in got
    assert (ib.bulk_calls, len(ib.single_calls)) == (2, 1)                   # re-listed, no per-key request


@pytest.mark.asyncio
async def test_disjoint_key_sets_cause_one_relist_per_cooldown_not_one_per_caller():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=1.0)           # first listing; 7705 unknown
    await reg.qualify_keys(ib, EXP, "SPXW", [(7710, "P")], now=121.0)         # other caller: its miss is due
    assert ib.bulk_calls == 2
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=122.0)         # the re-list covered 7705 too
    assert ib.bulk_calls == 2
    await reg.qualify_keys(ib, EXP, "SPXW", [(7705, "P")], now=242.0)         # a full cooldown later: one more
    assert ib.bulk_calls == 3


# -- invariants --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returned_contracts_are_copies():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    got = await reg.qualify_keys(ib, EXP, "SPXW", [(7700, "P")], now=0.0)
    got[(7700.0, "P")].conId = 999
    reg.get(_key(7700, "P")).contract.strike = 1.0
    assert reg.get(_key(7700, "P")).contract.conId == 1
    assert reg.get(_key(7700, "P")).contract.strike == 7700.0


@pytest.mark.asyncio
async def test_a_late_result_for_another_expiry_lands_under_its_own_key():
    class TwoExpiries(ListIb):
        async def req_chain_contract_details(self, symbol, expiry, trading_class, timeout=60.0):
            if expiry == "20261005":
                await self.gate.wait()
            return [det(7700, "P", 11 if expiry == "20261005" else 22, expiry=expiry)]

    ib = TwoExpiries()
    ib.gate = asyncio.Event()
    reg = _reg()
    old = asyncio.create_task(reg.qualify_keys(ib, "20261005", "SPXW", [(7700, "P")], now=0.0))
    await asyncio.sleep(0.01)
    new = await reg.qualify_keys(ib, "20261006", "SPXW", [(7700, "P")], now=1.0)
    ib.gate.set()
    assert set(await old) == {(7700.0, "P")} and set(new) == {(7700.0, "P")}
    assert reg.get(_key(7700, "P", "20261005")).contract.conId == 11
    assert reg.get(_key(7700, "P", "20261006")).contract.conId == 22


@pytest.mark.asyncio
async def test_weekly_and_monthly_with_the_same_date_and_strike_never_share_a_conid():
    class BothSeries(ListIb):
        async def req_chain_contract_details(self, symbol, expiry, trading_class, timeout=60.0):
            return [det(7700, "P", 1 if trading_class == "SPXW" else 2, cls=trading_class)]

    ib = BothSeries()
    reg = _reg()
    await reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=0.0)
    await reg.ensure_chain(ib, "SPX", EXP, "SPX", now=0.0)
    assert reg.get(_key(7700, "P", cls="SPXW")).contract.conId == 1
    assert reg.get(_key(7700, "P", cls="SPX")).contract.conId == 2


@pytest.mark.asyncio
async def test_expired_dates_are_dropped_lazily():
    today = {"d": "20261005"}
    reg = _reg(today=lambda: today["d"])
    await reg.ensure_chain(ListIb([det(7700, "P", 1)]), "SPX", EXP, "SPXW", now=0.0)
    assert reg.get(_key(7700, "P")) is not None
    today["d"] = "20261006"
    await reg.ensure_chain(ListIb([]), "SPX", "20261006", "SPXW", now=1.0)    # first call after the date changed
    assert reg.get(_key(7700, "P")) is None


@pytest.mark.asyncio
async def test_clear_forgets_everything():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    await reg.qualify_keys(ib, EXP, "SPXW", [(7700, "P"), (7705, "P")], now=0.0)
    reg.clear()
    assert len(reg) == 0 and reg.unknown_count() == 0
    await reg.qualify_keys(ib, EXP, "SPXW", [(7700, "P")], now=1.0)
    assert ib.bulk_calls == 2


async def _until_requested(ib):
    """Let the loop run until ``ib`` has received its bulk request (the flight is in the air)."""
    for _ in range(100):
        if ib.bulk_calls:
            return
        await asyncio.sleep(0)
    raise AssertionError("the bulk request was never sent")


@pytest.mark.asyncio
async def test_clear_cancels_the_inflight_listing_so_the_new_session_never_joins_the_dead_one():
    dead, live = ListIb([det(7799, "P", 99)]), ListIb([det(7700, "P", 1)])
    dead.gate = asyncio.Event()                         # the old connection never answers
    reg = _reg()
    old = asyncio.create_task(reg.ensure_chain(dead, "SPX", EXP, "SPXW", now=0.0))
    await _until_requested(dead)
    reg.clear()                                         # IB reconnect
    got = await asyncio.wait_for(reg.ensure_chain(live, "SPX", EXP, "SPXW", now=1.0), timeout=1.0)
    assert got == 1 and live.bulk_calls == 1            # a fresh flight, not a join of the dead one
    await asyncio.wait({old}, timeout=1.0)
    assert old.cancelled()                              # the dead flight is gone, its waiter does not hang
    dead.gate.set()
    await asyncio.sleep(0)
    assert reg.get(_key(7799, "P")) is None and reg.get(_key(7700, "P")).contract.conId == 1


@pytest.mark.asyncio
async def test_a_cleared_listing_flight_cannot_stamp_a_bulk_failure_into_the_new_registry():
    dead, live = ListIb([]), ListIb([det(7700, "P", 1)])       # an empty answer would count as a failed bulk
    dead.gate = asyncio.Event()
    reg = _reg()
    old = asyncio.create_task(reg.ensure_chain(dead, "SPX", EXP, "SPXW", now=0.0))
    await _until_requested(dead)
    reg.clear()
    dead.gate.set()                                     # the old request "answers" right after the clear
    await asyncio.wait({old}, timeout=1.0)
    assert old.cancelled()
    assert len(reg) == 0 and reg.unknown_count() == 0
    got = await reg.qualify_keys(live, EXP, "SPXW", [(7700, "P")], now=1.0)
    assert set(got) == {(7700.0, "P")}
    assert live.bulk_calls == 1 and live.single_calls == []    # no stale backoff: it listed in bulk


# -- order path --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_for_order_hit_makes_no_request():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    await reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=0.0)
    out = await reg.resolve_for_order(ib, [_req(7700)])
    assert out[0].contract.conId == 1 and ib.single_calls == []


@pytest.mark.asyncio
async def test_a_strike_listed_after_the_morning_listing_is_still_orderable_and_then_cached():
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    await reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=0.0)
    out = await reg.resolve_for_order(ib, [_req(7705)])            # absent from the listing: live lookup
    assert out[0].source == "single" and ib.single_calls == [(7705.0, "P")]
    await reg.resolve_for_order(ib, [_req(7705)])
    assert ib.single_calls == [(7705.0, "P")]                      # remembered


@pytest.mark.asyncio
async def test_resolve_for_order_refuses_a_near_match_and_stores_nothing():
    class WrongStrike(ListIb):
        async def req_contract_details(self, contract, timeout=30.0):
            return [det(contract.strike + 10, contract.right, 55)]

    reg = _reg()
    assert await reg.resolve_for_order(WrongStrike(), [_req(7700), _req(7705)]) is None
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_kill_switch_forces_a_live_lookup_every_time(monkeypatch):
    monkeypatch.setattr(config, "ORDER_USE_CONTRACT_CACHE", False)
    ib = ListIb([det(7700, "P", 1)])
    reg = _reg()
    await reg.ensure_chain(ib, "SPX", EXP, "SPXW", now=0.0)
    await reg.resolve_for_order(ib, [_req(7700)])
    await reg.resolve_for_order(ib, [_req(7700)])
    assert ib.single_calls == [(7700.0, "P")] * 2
