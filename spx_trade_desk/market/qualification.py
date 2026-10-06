"""Option-contract qualification shared by the chain stream and the wing poller.

Strikes are qualified once per (expiry, trading class), the first time they enter the
chain range. A strike IB cannot resolve is retried after a cooldown, so a transient
sec-def farm outage (IB error 2157) heals by itself.
"""
import asyncio
import logging
from typing import Any, Dict, Iterable, Tuple

from spx_trade_desk.core.config import CHAIN_STREAM_UNKNOWN_RETRY_SECS, QUALIFY_BATCH_SIZE
from spx_trade_desk.ib.orders import _option_contract

logger = logging.getLogger(__name__)

Key = Tuple[float, str]


def norm_key(strike, right) -> Key:
    return (round(float(strike), 1), str(right).upper())


def unknown_retry_due(unknown: dict, now: float,
                      cooldown: float = CHAIN_STREAM_UNKNOWN_RETRY_SECS) -> set:
    """Return keys whose failed-qualification retry is due.

    The unknown map holds key -> monotonic timestamp of the last failed qualification
    attempt. Retrying after a cooldown lets the chain recover from transient sec-def
    farm outages (IB error 2157) instead of staying blacklisted until expiry rolls over.
    """
    return {k for k, ts in unknown.items() if now - ts >= cooldown}


class QualificationCache:
    def __init__(self, cooldown: float = CHAIN_STREAM_UNKNOWN_RETRY_SECS):
        self.cooldown = cooldown
        self.expiry = ""
        self.trading_class = ""
        self.contracts: Dict[Key, Any] = {}
        self.unknown: Dict[Key, float] = {}

    def clear(self) -> None:
        self.contracts.clear()
        self.unknown.clear()

    async def qualify(self, ib, expiry: str, trading_class: str,
                      keys: Iterable, now: float) -> Dict[Key, Any]:
        if (expiry, trading_class) != (self.expiry, self.trading_class):
            self.expiry, self.trading_class = expiry, trading_class
            self.clear()
        wanted = [norm_key(s, r) for s, r in keys]
        due = unknown_retry_due(self.unknown, now, self.cooldown)
        todo = [k for k in dict.fromkeys(wanted)
                if k not in self.contracts and (k not in self.unknown or k in due)]
        for i in range(0, len(todo), max(1, QUALIFY_BATCH_SIZE)):
            batch = todo[i:i + QUALIFY_BATCH_SIZE]
            raw = [_option_contract(symbol="SPX", expiry=expiry, strike=k[0], right=k[1],
                                    exchange="SMART", trading_class=trading_class)
                   for k in batch]
            try:
                results = await asyncio.gather(*(ib.req_contract_details(c) for c in raw))
            except Exception as e:
                logger.warning(f"Qualification batch failed: {e}")
                results = [None] * len(batch)
            if (expiry, trading_class) != (self.expiry, self.trading_class):
                return {}       # an expiry roll or a clear overtook this call: write nothing
            for k, res in zip(batch, results):
                if res and res[0].contract.conId > 0:
                    self.contracts[k] = res[0].contract
                    self.unknown.pop(k, None)
                else:
                    self.unknown[k] = now
        return {k: self.contracts[k] for k in wanted if k in self.contracts}
