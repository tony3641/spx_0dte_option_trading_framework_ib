"""One qualification cache for the chain stream and the wing poller."""
import pytest

from spx_trade_desk.market.qualification import QualificationCache, norm_key
from tests.conftest import MockIBClient


class PickyIb(MockIBClient):
    """MockIBClient that cannot find the strikes listed in ``bad``."""

    def __init__(self, bad=()):
        super().__init__()
        self.bad = set(bad)

    async def req_contract_details(self, contract):
        if float(contract.strike) in self.bad:
            self.call_log.append({"method": "req_contract_details", "found": False})
            return []
        return await super().req_contract_details(contract)


def _n(ib):
    return sum(1 for c in ib.call_log if c["method"] == "req_contract_details")


def test_norm_key_rounds_strike_and_uppercases_right():
    assert norm_key(7700.04, "p") == (7700.0, "P")


@pytest.mark.asyncio
async def test_qualifies_each_key_once():
    ib = PickyIb()
    cache = QualificationCache(cooldown=120)
    got = await cache.qualify(ib, "20261005", "SPXW", [(7700, "P"), (7700.0, "c")], now=0.0)
    assert set(got) == {(7700.0, "P"), (7700.0, "C")}
    assert got[(7700.0, "P")].tradingClass == "SPXW"
    await cache.qualify(ib, "20261005", "SPXW", [(7700, "P")], now=1.0)
    assert _n(ib) == 2


@pytest.mark.asyncio
async def test_unknown_key_waits_for_the_cooldown():
    ib = PickyIb(bad={7705.0})
    cache = QualificationCache(cooldown=120)
    assert await cache.qualify(ib, "20261005", "SPXW", [(7705, "P")], now=0.0) == {}
    await cache.qualify(ib, "20261005", "SPXW", [(7705, "P")], now=60.0)
    assert _n(ib) == 1
    ib.bad.clear()
    got = await cache.qualify(ib, "20261005", "SPXW", [(7705, "P")], now=120.0)
    assert (7705.0, "P") in got and _n(ib) == 2


@pytest.mark.asyncio
async def test_expiry_or_trading_class_change_resets():
    ib = PickyIb()
    cache = QualificationCache()
    await cache.qualify(ib, "20261005", "SPXW", [(7700, "P")], now=0.0)
    await cache.qualify(ib, "20261006", "SPXW", [(7700, "P")], now=0.0)
    await cache.qualify(ib, "20261006", "SPX", [(7700, "P")], now=0.0)
    assert _n(ib) == 3


@pytest.mark.asyncio
async def test_clear_forgets_everything():
    ib = PickyIb()
    cache = QualificationCache()
    await cache.qualify(ib, "20261005", "SPXW", [(7700, "P")], now=0.0)
    cache.clear()
    await cache.qualify(ib, "20261005", "SPXW", [(7700, "P")], now=0.0)
    assert _n(ib) == 2


@pytest.mark.asyncio
async def test_a_qualify_overtaken_by_an_expiry_roll_does_not_write_its_results():
    import asyncio

    class SlowIb(PickyIb):
        def __init__(self):
            super().__init__()
            self.gate = asyncio.Event()

        async def req_contract_details(self, contract):
            if contract.lastTradeDateOrContractMonth == "20261005":
                await self.gate.wait()
            return await super().req_contract_details(contract)

    ib = SlowIb()
    cache = QualificationCache()
    old = asyncio.create_task(cache.qualify(ib, "20261005", "SPXW", [(7700, "P")], now=0.0))
    await asyncio.sleep(0)                                  # old call is now awaiting IB
    new = await cache.qualify(ib, "20261006", "SPXW", [(7800, "P")], now=1.0)
    ib.gate.set()
    assert await old == {}                                  # stale caller gets nothing
    assert set(cache.contracts) == {(7800.0, "P")}          # old-expiry contract not written
    assert set(new) == {(7800.0, "P")} and cache.expiry == "20261006"
    assert (7700.0, "P") not in cache.unknown
