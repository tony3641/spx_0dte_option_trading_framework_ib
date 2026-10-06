"""Shared option-contract registry.

One place where qualified option contracts live, for the chain stream, the wing poller, the
chain fetcher and the order path. It replaces the old stream/poller qualification cache and the
fetcher's module-global caches.

* Bulk fill: one partial ``reqContractDetails`` (no strike, no right) lists every contract of an
  expiry; each row is keyed from its own fields, never from the request.
* A listing is a prefill, not a statement of absence. 0DTE strikes are added intraday, so a key
  missing from a fresh listing is only marked *unknown*; once the retry cooldown has passed, the
  next miss re-lists once (one message) instead of retrying key by key. The order path never
  trusts absence: a miss there always does a live, exact-match single qualification.
* Fallback: if the bulk request fails, times out or returns fewer than ``min_bulk`` contracts the
  requested keys are qualified one by one through the pacer.

Safety invariants: an entry only comes from an exact-match single result or a self-describing bulk
row (symbol, trading class, expiry and exchange SMART); the key carries symbol, trading class and
expiry; the order path re-checks every cached hit with the exact-match guard; returned contracts
are copies. This module imports nothing from ``market``, ``web`` or ``strategy``.
"""
import asyncio
import copy
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ibapi.contract import Contract

from spx_trade_desk.core import config
from spx_trade_desk.core.perf import perf

logger = logging.getLogger(__name__)

Key = Tuple[float, str]
SYMBOL = "SPX"
MIN_BULK_CONTRACTS = 40
_ET = ZoneInfo("US/Eastern")


def norm_key(strike, right) -> Key:
    return (round(float(strike), 1), str(right).upper())


def unknown_retry_due(unknown: dict, now: float,
                      cooldown: float = config.CHAIN_STREAM_UNKNOWN_RETRY_SECS) -> set:
    """Return keys whose failed-qualification retry is due.

    The unknown map holds key -> monotonic timestamp of the last failed qualification
    attempt. Retrying after a cooldown lets the chain recover from transient sec-def
    farm outages (IB error 2157) instead of staying blacklisted until expiry rolls over.
    """
    return {k for k, ts in unknown.items() if now - ts >= cooldown}


def _option_contract(symbol, expiry, strike, right, exchange,
                     trading_class="SPXW"):
    """Build a native ibapi OPT Contract (ibapi.contract.Contract)."""
    c = Contract()
    c.symbol = symbol
    c.secType = "OPT"
    c.exchange = exchange
    c.currency = "USD"
    c.lastTradeDateOrContractMonth = expiry
    c.strike = float(strike)
    c.right = right
    c.multiplier = "100"
    c.tradingClass = trading_class
    return c


def _exact_match(details, requested):
    """Return the contract from ``details`` that exactly matches ``requested``.

    IB's ``reqContractDetails`` can return multiple near-matches; the ComboLeg
    is sent to IBKR as a bare conId, so picking the wrong one silently ships a
    leg with the wrong strike. Refuse a near-match: return only an exact
    strike/right/expiry match carrying a conId, else None.
    """
    if not details:
        return None
    req_strike = float(getattr(requested, "strike", 0) or 0)
    req_right = (getattr(requested, "right", "") or "").upper()
    req_expiry = (getattr(requested, "lastTradeDateOrContractMonth", "") or "").replace(" ", "")
    for d in details:
        dc = getattr(d, "contract", None)
        if dc is None or not getattr(dc, "conId", 0):
            continue
        try:
            strike_matches = abs(float(getattr(dc, "strike", 0) or 0) - req_strike) < 1e-6
        except (TypeError, ValueError):
            strike_matches = False
        right_matches = (getattr(dc, "right", "") or "").upper() == req_right
        returned_expiry = (getattr(dc, "lastTradeDateOrContractMonth", "") or "").replace(" ", "")
        expiry_matches = returned_expiry == req_expiry or returned_expiry.startswith(req_expiry)
        if strike_matches and right_matches and expiry_matches:
            return dc
    return None


def _norm_expiry(value) -> str:
    return str(value or "").replace(" ", "")[:8]


def _today_et() -> str:
    return datetime.now(_ET).strftime("%Y%m%d")


def _min_tick_of(details, contract) -> float:
    for d in details or []:
        if getattr(d, "contract", None) is contract:
            try:
                return max(0.0, float(getattr(d, "minTick", 0) or 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


@dataclass(frozen=True)
class ContractKey:
    symbol: str
    trading_class: str
    expiry: str
    strike: float
    right: str

    @classmethod
    def of(cls, symbol, trading_class, expiry, strike, right) -> "ContractKey":
        return cls(str(symbol).upper(), str(trading_class), _norm_expiry(expiry),
                   round(float(strike), 1), str(right).upper()[:1])

    @classmethod
    def from_contract(cls, c) -> "ContractKey":
        return cls.of(getattr(c, "symbol", ""), getattr(c, "tradingClass", ""),
                      getattr(c, "lastTradeDateOrContractMonth", ""),
                      getattr(c, "strike", 0.0) or 0.0, getattr(c, "right", ""))


@dataclass
class QualifiedContract:
    contract: Contract
    min_tick: float          # 0.0 when IB did not report one
    source: str              # "bulk" | "single"
    ts: float                # monotonic time of the write


_ListingKey = Tuple[str, str, str]      # (symbol, trading_class, expiry)


class ContractRegistry:
    def __init__(self, cooldown: Optional[float] = None, min_bulk: int = MIN_BULK_CONTRACTS,
                 today: Callable[[], str] = _today_et):
        self.cooldown = float(config.CHAIN_STREAM_UNKNOWN_RETRY_SECS if cooldown is None else cooldown)
        self.min_bulk = int(min_bulk)
        self._today = today
        self._last_today = ""
        self._items: Dict[ContractKey, QualifiedContract] = {}
        self._unknown: Dict[ContractKey, float] = {}      # key -> time of the listing it was absent from
        self._listed: Dict[_ListingKey, Tuple[float, int]] = {}
        self._bulk_failed: Dict[_ListingKey, float] = {}
        self._relist: set = set()
        self._inflight: Dict[_ListingKey, "asyncio.Future"] = {}

    # -- reads / housekeeping -------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    def unknown_count(self) -> int:
        return len(self._unknown)

    def get(self, key: ContractKey) -> Optional[QualifiedContract]:
        q = self._items.get(key)
        if q is None:
            return None
        return QualifiedContract(copy.copy(q.contract), q.min_tick, q.source, q.ts)

    def clear(self) -> None:
        """Forget everything (IB reconnect), including the listing flights still in the air.

        A flight is bound to the old connection and may hang until its timeout, so it is cancelled
        and dropped: the next ``ensure_chain`` starts a fresh request on the new connection instead of
        joining it. A cancelled flight raises before it writes, so it cannot put entries, unknown marks
        or a bulk-failure backoff into the cleared registry.

        Precondition: cancel every task that can await ``ensure_chain`` (the chain loops, the boot
        prefetch) BEFORE calling this. A waiter that joined a flight through ``shield`` receives a
        ``CancelledError`` that is indistinguishable from its own cancellation, so a loop that treats
        it as "stop" (``chain_poll_loop`` breaks out) would exit silently. ``reconnect_ib_on`` is the
        only call site and already stops the loops first.
        """
        inflight, self._inflight = self._inflight, {}
        for task in inflight.values():
            task.cancel()
        self._items.clear()
        self._unknown.clear()
        self._listed.clear()
        self._bulk_failed.clear()
        self._relist.clear()
        self._last_today = ""

    def relist(self, expiry: Optional[str] = None) -> None:
        """Manual refresh: conIds do not change, but the next miss re-reads the listing.

        It also forgets the unknown keys and any bulk-failure backoff of the affected expiry, so a
        refresh during a sec-def outage retries the bulk request instead of waiting out the cooldown.
        """
        for lk in list(self._listed):
            if expiry is None or lk[2] == expiry:
                self._relist.add(lk)
        for lk in [k for k in self._bulk_failed if expiry is None or k[2] == expiry]:
            del self._bulk_failed[lk]
        for ck in [k for k in self._unknown if expiry is None or k.expiry == expiry]:
            del self._unknown[ck]

    def _drop_expired(self) -> None:
        today = self._today()
        if today == self._last_today:
            return
        self._last_today = today
        for ck in [k for k in self._items if k.expiry < today]:
            del self._items[ck]
        for ck in [k for k in self._unknown if k.expiry < today]:
            del self._unknown[ck]
        for lk in [k for k in self._listed if k[2] < today]:
            del self._listed[lk]
            self._relist.discard(lk)

    @staticmethod
    def _ck(lk: _ListingKey, key: Key) -> ContractKey:
        return ContractKey.of(lk[0], lk[1], lk[2], key[0], key[1])

    def _put(self, contract, details, source: str, ts: float) -> ContractKey:
        key = ContractKey.from_contract(contract)
        self._items[key] = QualifiedContract(copy.copy(contract), _min_tick_of(details, contract),
                                             source, ts)
        self._unknown.pop(key, None)
        return key

    # -- bulk listing ---------------------------------------------------------------------------

    async def ensure_chain(self, ib, symbol: str, expiry: str, trading_class: str, now: float,
                           force: bool = False) -> int:
        """List every contract of ``expiry`` with one request (single-flight); return the count."""
        self._drop_expired()
        lk = (str(symbol).upper(), trading_class, expiry)
        if not force and lk in self._listed and lk not in self._relist:
            return self._listed[lk][1]
        task = self._inflight.get(lk)
        if task is None or task.done():
            task = asyncio.ensure_future(self._bulk(ib, lk, now))
            self._inflight[lk] = task

            def _forget(t, lk=lk):
                if self._inflight.get(lk) is t:
                    del self._inflight[lk]

            task.add_done_callback(_forget)
        return await asyncio.shield(task)       # a cancelled caller must not kill the shared request

    async def _bulk(self, ib, lk: _ListingKey, now: float) -> int:
        symbol, trading_class, expiry = lk
        t0 = time.perf_counter()
        try:
            await ib.pacer.acquire()
            details = await ib.req_chain_contract_details(symbol, expiry, trading_class)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Bulk listing %s %s %s failed: %s", symbol, trading_class, expiry, e)
            details = []
        perf.record("registry.bulk", (time.perf_counter() - t0) * 1000.0)
        accepted = self._accept(details or [], lk, now)
        if len(accepted) < self.min_bulk:
            logger.warning("Bulk listing %s %s %s returned %d usable contracts (minimum %d); "
                           "falling back to single qualification", symbol, trading_class, expiry,
                           len(accepted), self.min_bulk)
            self._bulk_failed[lk] = now
            return 0
        for ck, qc in accepted.items():
            self._items[ck] = qc
        # A fresh listing supersedes every unknown mark of this series: the next miss is stamped
        # with the new listing time, so there is one re-list per cooldown across all callers.
        for ck in [k for k in self._unknown if (k.symbol, k.trading_class, k.expiry) == lk]:
            del self._unknown[ck]
        self._listed[lk] = (now, len(accepted))
        self._relist.discard(lk)
        self._bulk_failed.pop(lk, None)
        logger.info("Contract listing %s %s %s: %d contracts", symbol, trading_class, expiry,
                    len(accepted))
        return len(accepted)

    def _accept(self, details, lk: _ListingKey, now: float) -> Dict[ContractKey, QualifiedContract]:
        symbol, trading_class, expiry = lk
        out: Dict[ContractKey, QualifiedContract] = {}
        for d in details:
            c = getattr(d, "contract", None)
            if c is None:
                continue
            try:
                con_id = int(getattr(c, "conId", 0) or 0)
                strike = float(getattr(c, "strike", 0) or 0)
            except (TypeError, ValueError):
                continue
            right = str(getattr(c, "right", "") or "").upper()[:1]
            if (con_id <= 0 or strike <= 0 or right not in ("C", "P")
                    or str(getattr(c, "tradingClass", "")) != trading_class
                    or _norm_expiry(getattr(c, "lastTradeDateOrContractMonth", "")) != expiry
                    or str(getattr(c, "symbol", "")).upper() != symbol):
                continue
            if str(getattr(c, "exchange", "") or "") != "SMART":
                # An order built from this row would route to whatever exchange it names; only a
                # SMART row is the contract the single-qualification path would have returned.
                perf.count("registry.reject_exchange")
                continue
            ck = ContractKey.of(symbol, trading_class, expiry, strike, right)
            prior = out.get(ck)
            if prior is not None:
                if prior.contract.conId != con_id:
                    logger.warning("Bulk listing: duplicate %s with a different conId (%s vs %s); "
                                   "keeping the first", ck, prior.contract.conId, con_id)
                continue
            try:
                min_tick = max(0.0, float(getattr(d, "minTick", 0) or 0))
            except (TypeError, ValueError):
                min_tick = 0.0
            out[ck] = QualifiedContract(copy.copy(c), min_tick, "bulk", now)
        return out

    # -- chain service (stream, poller, fetcher) ------------------------------------------------

    async def qualify_keys(self, ib, expiry: str, trading_class: str, keys: Iterable,
                           now: float) -> Dict[Key, Contract]:
        """Resolve ``(strike, right)`` keys of SPX options; unresolved keys are simply absent."""
        self._drop_expired()
        lk = (SYMBOL, trading_class, expiry)
        wanted = [norm_key(s, r) for s, r in keys]
        missing = [k for k in dict.fromkeys(wanted) if self._ck(lk, k) not in self._items]
        if missing:
            await self._fill(ib, lk, missing, now)
        out: Dict[Key, Contract] = {}
        for k in wanted:
            q = self.get(self._ck(lk, k))
            if q is not None:
                out[k] = q.contract
        return out

    async def _fill(self, ib, lk: _ListingKey, missing: List[Key], now: float) -> None:
        listed = self._listed.get(lk)
        failed_at = self._bulk_failed.get(lk)
        backing_off = failed_at is not None and now - failed_at < self.cooldown
        stamp0 = listed[0] if listed else 0.0
        due = lk in self._relist or (
            listed is not None
            and any(now - self._unknown.get(self._ck(lk, k), stamp0) >= self.cooldown
                    for k in missing))
        if (listed is None or due) and not backing_off:
            await self.ensure_chain(ib, lk[0], lk[2], lk[1], now, force=due)
            listed = self._listed.get(lk)
        if listed is not None:
            for k in missing:
                ck = self._ck(lk, k)
                if ck not in self._items:
                    self._unknown[ck] = listed[0]       # absent from the listing taken at that time
            return
        todo = [k for k in missing
                if now - self._unknown.get(self._ck(lk, k), -self.cooldown - 1.0) >= self.cooldown]
        if todo:
            await self._qualify_singly(ib, lk, todo, now)

    async def _paced_details(self, ib, contract):
        await ib.pacer.acquire()
        return await ib.req_contract_details(contract)

    async def _qualify_singly(self, ib, lk: _ListingKey, todo: List[Key], now: float) -> None:
        symbol, trading_class, expiry = lk
        step = max(1, config.QUALIFY_BATCH_SIZE)
        for i in range(0, len(todo), step):
            batch = todo[i:i + step]
            raw = [_option_contract(symbol=symbol, expiry=expiry, strike=k[0], right=k[1],
                                    exchange="SMART", trading_class=trading_class) for k in batch]
            results = await asyncio.gather(*(self._paced_details(ib, c) for c in raw),
                                           return_exceptions=True)
            for k, c, res in zip(batch, raw, results):
                if isinstance(res, BaseException):
                    logger.warning("Qualification of %s failed: %s", k, res)
                    match = None
                else:
                    match = _exact_match(res, c)
                if match is not None:
                    self._put(match, res, "single", now)
                if self._ck(lk, k) not in self._items:
                    self._unknown[self._ck(lk, k)] = now

    # -- order path -----------------------------------------------------------------------------

    async def resolve_for_order(self, ib, requested: List[Contract]) -> Optional[List[QualifiedContract]]:
        """Exact IB contracts for ``requested``, or None if any leg cannot be resolved.

        Every returned contract, cached or live, passed ``_exact_match`` against its request
        (strike to 1e-6, right, expiry; carries a conId); the key already pins symbol and trading
        class, and a cached bulk row was accepted only with ``exchange == "SMART"``. The cache key
        rounds the strike to 0.1, so a cached hit that is not an exact match (a request for
        5200.04 meeting a cached 5200.0) is refused with a warning, discarded and looked up live
        instead; if the live result is no exact match either the whole call returns None. A miss
        does a live single qualification (order lane, never paced) and remembers an on-grid result.
        Absence from a bulk listing is never trusted here. ``ORDER_USE_CONTRACT_CACHE=false`` forces
        a live lookup for every leg.
        """
        use_cache = bool(config.ORDER_USE_CONTRACT_CACHE)
        out: List[Optional[QualifiedContract]] = [None] * len(requested)
        misses = []
        for i, c in enumerate(requested):
            hit = self.get(ContractKey.from_contract(c)) if use_cache else None
            if hit is not None and _exact_match([SimpleNamespace(contract=hit.contract)], c) is None:
                logger.warning("Order path: refused a cached contract for strike %s %s: it lists "
                               "strike %s; looking it up live", getattr(c, "strike", None),
                               getattr(c, "right", ""), getattr(hit.contract, "strike", None))
                hit = None
            if hit is not None:
                perf.count("registry.hit")
                out[i] = hit
            else:
                perf.count("registry.miss")
                misses.append((i, c))
        if misses:
            results = await asyncio.gather(*(ib.req_contract_details(c) for _, c in misses))
            for (i, c), details in zip(misses, results):
                match = _exact_match(details, c)
                if match is None:
                    return None
                ts = time.monotonic()
                if ContractKey.from_contract(match).strike == float(match.strike):
                    out[i] = self.get(self._put(match, details, "single", ts))
                else:       # off the 0.1 grid: the key would collide with the on-grid neighbour
                    out[i] = QualifiedContract(copy.copy(match), _min_tick_of(details, match),
                                               "single", ts)
        return out
