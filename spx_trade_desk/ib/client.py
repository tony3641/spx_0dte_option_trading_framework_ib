"""
Native IB (TWS API) client bridge.

Subclasses EWrapper + EClient, runs the socket in a background thread, and
bridges callbacks to asyncio. Three bridge patterns:
  - one-shot requests  -> asyncio.Future per reqId (_Request)
  - continuous streams -> TickStream per reqId (mutable, read by asyncio)
  - discrete events    -> asyncio.Event (order ack/fill, account dirty)
"""

import asyncio
import itertools
import logging
import threading
import time
from collections import namedtuple
from datetime import datetime
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from spx_trade_desk.core.config import (
    CHAIN_STREAM_MAX_LINES_CAP,
    IB_REQUEST_BURST,
    IB_REQUEST_RATE,
    MARKET_DATA_LINES,
)
from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib.line_budget import (
    MIN_MARKET_DATA_LINES, LineBudget, LineBudgetExceeded, split_lines,
)
from spx_trade_desk.ib.pacing import RequestPacer

logger = logging.getLogger(__name__)

# Historical bar timestamps arrive as epoch seconds (format_date=2) in Eastern
# Time. DST-aware zone matching market_hours.ET (canonical project timezone).
ET = ZoneInfo("US/Eastern")

# TickType values (ibapi.ticktype.TickTypeEnum)
BID, ASK, LAST = 1, 2, 4
HIGH, LOW, CLOSE = 6, 7, 9
IMPLIED_VOL = 24
BID_SIZE, ASK_SIZE, LAST_SIZE = 0, 3, 5
VOLUME = 8
OPEN_INTEREST = 22
CALL_OPEN_INTEREST, PUT_OPEN_INTEREST = 27, 28
BID_OPT, ASK_OPT, LAST_OPT, MODEL_OPT = 10, 11, 12, 13

_UNSET = 1.7976931348623157e+308


def _finite_or_none(val):
    """Return None for -1.0 / UNSET (IB 'unavailable' sentinels)."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f == -1.0 or f == _UNSET:
        return None
    return f


def _coerce_bar(bar):
    """format_date=2 → bar.date is epoch seconds; return a tz-aware datetime."""
    try:
        dt = datetime.fromtimestamp(float(bar.date), tz=ET)
    except (TypeError, ValueError, OSError):
        dt = bar.date
    bar.date = dt
    return bar


class _Request:
    """One-shot request accumulator resolved when its ...End callback fires."""

    def __init__(self, loop):
        self.future = loop.create_future()
        self.items: list = []


def _is_warning(code) -> bool:
    """IB's warning / notification range (2100-2299, e.g. 2104 farm OK, 2176 fractional size)."""
    return 2100 <= code <= 2299


class _LiveBars:
    """Callbacks of one keepUpToDate historical-data request (loop thread)."""
    __slots__ = ("on_update", "on_error")

    def __init__(self, on_update, on_error):
        self.on_update = on_update
        self.on_error = on_error


SecDefOptParams = namedtuple(
    "SecDefOptParams", "exchange tradingClass multiplier expirations strikes")

AccountValue = namedtuple("AccountValue", "tag value currency")
PortfolioItem = namedtuple("PortfolioItem", "contract position marketPrice marketValue "
                                            "averageCost unrealizedPNL realizedPNL account")
ExecutionRecord = namedtuple("ExecutionRecord", "contract execution commission")


def _portfolio_key(contract):
    """Stable identity for a portfolio entry.

    IB re-sends every position on each account-update cycle; we must upsert by
    this key rather than append. Prefer conId; fall back to a contract-field
    composite for the rare contract without a conId.
    """
    con_id = getattr(contract, "conId", 0)
    if con_id:
        return ("conId", con_id)
    return (
        "desc",
        getattr(contract, "symbol", ""),
        getattr(contract, "secType", ""),
        getattr(contract, "lastTradeDateOrContractMonth", ""),
        float(getattr(contract, "strike", 0) or 0),
        getattr(contract, "right", ""),
    )


class Greeks:
    __slots__ = ("implied_vol", "delta", "gamma", "vega", "theta", "opt_price", "und_price")

    def __init__(self):
        self.implied_vol = self.delta = self.gamma = None
        self.vega = self.theta = self.opt_price = self.und_price = None

    def update(self, implied_vol, delta, gamma, vega, theta, opt_price, und_price):
        self.implied_vol = _finite_or_none(implied_vol)
        self.delta = _finite_or_none(delta)
        self.gamma = _finite_or_none(gamma)
        self.vega = _finite_or_none(vega)
        self.theta = _finite_or_none(theta)
        self.opt_price = _finite_or_none(opt_price)
        self.und_price = _finite_or_none(und_price)


class TickStream:
    """Per-subscription market-data state, written on the socket thread."""

    def __init__(self, req_id, contract):
        self.req_id = req_id
        self.contract = contract
        self.bid = self.ask = self.last = None
        self.high = self.low = self.close = None
        self.bid_size = self.ask_size = self.last_size = 0
        self.volume = 0
        self.call_oi = self.put_oi = self.open_interest = 0
        self.implied_volatility = None
        self.bid_greeks = Greeks()
        self.ask_greeks = Greeks()
        self.last_greeks = Greeks()
        self.model_greeks = Greeks()
        self._first_tick = False
        self._has_quote = False
        # time.monotonic() of the latest tick, written on the socket thread (a float
        # assignment): lets readers tell a silent subscription from a live one.
        self.last_tick_mono = 0.0
        self.failed_code = None      # IB error code that refused this subscription (101: ticker limit)

    def received_any_tick(self):
        return self._first_tick

    def has_quote(self):
        return self._has_quote

    def _mark(self, has_quote):
        self._first_tick = True
        self.last_tick_mono = time.monotonic()
        if has_quote:
            self._has_quote = True


_TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
_PENDING_STATUSES = {"", "PendingSubmit", "ApiPending"}


class OrderHandle:
    """Tracks one order's lifecycle. Events fire on the asyncio loop thread;
    the socket thread only touches plain attributes + set()s events."""
    def __init__(self, order_id, contract, order):
        self.order_id = order_id
        self.contract = contract
        self.order = order
        self.status = ""
        self.filled = 0.0
        self.remaining = 0.0
        self.avg_fill_price = None
        self.last_fill_price = None
        self.perm_id = None
        self.parent_id = None
        self.ack_event = asyncio.Event()
        self.fill_event = asyncio.Event()
        self.terminal_event = asyncio.Event()
        self.status_event = asyncio.Event()      # fires on every status change
        self.activated_event = asyncio.Event()   # fires on PreSubmitted -> non-PreSubmitted

    async def ack(self, timeout=5.0):
        await asyncio.wait_for(self.ack_event.wait(), timeout)

    async def wait_fill(self, timeout=30.0):
        await asyncio.wait_for(self.fill_event.wait(), timeout)

    async def wait_terminal(self, timeout=30.0):
        await asyncio.wait_for(self.terminal_event.wait(), timeout)

    def is_terminal(self):
        return self.status in _TERMINAL_STATUSES


class _SnapshotBatch:
    """One fetch_snapshot call's completion state (per call, never shared)."""
    __slots__ = ("pending", "done")

    def __init__(self):
        self.pending: set = set()
        self.done = asyncio.Event()


class IBClient(EWrapper, EClient):
    def __init__(self, line_shares: Optional[Dict[str, int]] = None,
                 pacer: Optional[RequestPacer] = None):
        EClient.__init__(self, self)
        # ibapi 10.45 useProtoBuf() crashes on None serverVersion when unconnected
        # (`unifiedVersion <= None`). Default to 0 so one-shot request methods degrade
        # to the not-connected error path instead of raising; the connect handshake
        # overwrites this with the real negotiated version. (Task 3)
        self.serverVersion_ = 0
        self.connected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._req_id = itertools.count(1)
        self._next_order_id: Optional[int] = None
        self._account_code: Optional[str] = None
        self._requests: Dict[int, _Request] = {}
        self._live_bars: Dict[int, _LiveBars] = {}               # keepUpToDate bar requests
        self._streams: Dict[int, "TickStream"] = {}
        self._snapshot_batches: Dict[int, _SnapshotBatch] = {}   # req_id -> its batch
        self._stream_share: Dict[int, str] = {}                  # req_id -> budget share
        self.line_budget = LineBudget(
            line_shares or split_lines(MARKET_DATA_LINES, CHAIN_STREAM_MAX_LINES_CAP))
        # Data-lane pacing: orders and cancels never wait on it (see ib/pacing.py).
        self.pacer = pacer if pacer is not None else RequestPacer(IB_REQUEST_RATE, IB_REQUEST_BURST)
        self._orders: Dict[int, "OrderHandle"] = {}
        self._thread: Optional[threading.Thread] = None
        self._connected_evt: Optional[asyncio.Event] = None
        self.account_values: List[AccountValue] = []
        self.portfolio: List[PortfolioItem] = []
        self.executions: List[ExecutionRecord] = []
        self.account_dirty = False
        self.on_account_dirty: Optional[Callable] = None
        self._exec_by_id: Dict[str, ExecutionRecord] = {}
        self.error_handler: Optional[Callable] = None
        self._perf_first_order_seen = False     # set by the order path after the first order's ack

    # -- connection ---------------------------------------------------------

    async def connect(self, host, port, client_id, timeout=15.0):
        self._loop = asyncio.get_running_loop()
        self._connected_evt = asyncio.Event()
        EClient.connect(self, host, port, client_id)
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.disconnect()
            raise ConnectionError(f"IB connect timed out after {timeout}s")
        self.connected = True

    def disconnect(self):
        self.connected = False
        try:
            EClient.disconnect(self)
        except Exception:
            pass

    # -- EWrapper: connection callbacks --------------------------------------

    def nextValidId(self, orderId):
        self._next_order_id = orderId
        if self._loop is not None and self._connected_evt is not None:
            self._loop.call_soon_threadsafe(self._connected_evt.set)

    def managedAccounts(self, accountsList):
        self._account_code = (accountsList or "").split(",")[0] or None

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        logger.warning("IB error reqId=%s code=%s: %s", reqId, errorCode, errorString)
        self._snapshot_settle(reqId)   # don't let a dead stream hold a batch
        if errorCode == 101 and self._loop is not None:
            # Max number of tickers reached: IB refused a market-data line. Handle it on the loop thread.
            self._loop.call_soon_threadsafe(self._handle_line_limit, reqId)
        # A keepUpToDate bar request hears every error on its id except warnings (10182, 10225 and
        # 10197 end its updates, so they must reach the caller). A warning is only logged: it
        # neither reaches on_error nor cuts the initial load short below.
        live = self._live_bars.get(reqId)
        live_warning = live is not None and _is_warning(errorCode)
        if live is not None and not live_warning and live.on_error is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._deliver_live_error, reqId, live, errorCode, errorString)
        # Resolve any pending one-shot request so awaiting callers don't hang:
        # IB rejects some requests (e.g. error 200 contract-not-found, 321 invalid
        # contract id) with an error but no matching ...End callback, which would
        # otherwise leave the request future unresolved forever.
        req = self._requests.get(reqId)
        if req is not None and not req.future.done() and not live_warning:
            self._requests.pop(reqId, None)
            self._loop.call_soon_threadsafe(req.future.set_result, list(req.items))
        handler = self.error_handler
        if handler is not None and self._loop is not None:
            handle = self._orders.get(reqId)
            contract = handle.contract if handle is not None else None
            self._loop.call_soon_threadsafe(
                handler, reqId, errorCode, errorString, contract)

    def _handle_line_limit(self, req_id):
        """Error 101 for a tracked market-data request: free its line and cap the budget.

        The cap is the number of lines granted at the moment of refusal; it only ever shrinks
        within a session (see ``LineBudget.observe_limit``). Runs on the loop thread.
        """
        share = self._stream_share.pop(req_id, None)
        if share is None:
            return                      # not a market-data request this client tracks
        perf.count("ib.error_101")
        stream = self._streams.pop(req_id, None)
        if stream is not None:
            stream.failed_code = 101
        self.line_budget.release(share)
        in_use = sum(self.line_budget.used(s) for s in self.line_budget.shares())
        before = self.line_budget.shares()
        if self.line_budget.observe_limit(in_use) != before:
            if in_use >= MIN_MARKET_DATA_LINES:
                logger.warning("IB refused a line with %d in use; capping the budget at %d; "
                               "set MARKET_DATA_LINES <= %d", in_use, in_use, in_use)
            else:
                logger.warning("IB refused a line with %d in use; capping the budget at %d, which is "
                               "below the minimum of %d that MARKET_DATA_LINES accepts: set "
                               "MARKET_DATA_LINES = %d and free the lines held by other API clients "
                               "and TWS watchlists", in_use, in_use, MIN_MARKET_DATA_LINES,
                               MIN_MARKET_DATA_LINES)

    # -- order placement / lifecycle ----------------------------------------

    def place_order(self, contract, order, order_id=None):
        """Submit a new order, or modify an existing one when ``order_id`` is given.

        Native ibapi treats ``placeOrder`` with a fresh orderId as a NEW order;
        a modify must reuse the SAME orderId. Pass the original ``order_id`` to
        reprice/re-transmit an existing order (single live order), or omit it to
        allocate the next order id.
        """
        if order_id is None:
            if self._next_order_id is None:
                raise RuntimeError("Order ID unavailable (not connected / no nextValidId)")
            order_id = self._next_order_id
            self._next_order_id += 1
        order.orderId = order_id
        handle = self._orders.get(order_id)
        if handle is None:
            handle = OrderHandle(order_id, contract, order)
            self._orders[order_id] = handle
        else:
            handle.contract = contract
            handle.order = order
        EClient.placeOrder(self, order_id, contract, order)
        return handle

    def cancel_order(self, order_id):
        from ibapi.order_cancel import OrderCancel
        try:
            EClient.cancelOrder(self, order_id, OrderCancel())
        except Exception:
            pass

    def req_open_orders(self):
        EClient.reqOpenOrders(self)

    @property
    def orders(self) -> Dict[int, "OrderHandle"]:
        """Public registry of order handles (aliases the private ``_orders``).

        Account/order consumers read ``ib.orders.values()`` / ``dict(ib.orders)``
        instead of reaching into the private dict.
        """
        return self._orders

    # -- EWrapper: order status ----------------------------------------------

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId,
                    parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        handle = self._orders.get(orderId)
        if handle is None:
            return
        prev_status = handle.status
        handle.status = status
        handle.filled = float(filled)
        handle.remaining = float(remaining)
        handle.avg_fill_price = _finite_or_none(avgFillPrice)
        handle.last_fill_price = _finite_or_none(lastFillPrice)
        handle.perm_id = permId
        handle.parent_id = parentId
        if self._loop is not None:
            self._loop.call_soon_threadsafe(handle.status_event.set)
            if prev_status == "PreSubmitted" and status != "PreSubmitted":
                self._loop.call_soon_threadsafe(handle.activated_event.set)
            if status not in _PENDING_STATUSES:
                self._loop.call_soon_threadsafe(handle.ack_event.set)
            if status == "Filled" and float(remaining) <= 0:
                self._loop.call_soon_threadsafe(handle.fill_event.set)
            if status in _TERMINAL_STATUSES:
                self._loop.call_soon_threadsafe(handle.terminal_event.set)

    def openOrder(self, orderId, contract, order, orderState):
        handle = self._orders.get(orderId)
        if handle is None:
            handle = OrderHandle(orderId, contract, order)
            self._orders[orderId] = handle
        handle.contract = contract
        handle.order = order
        handle.status = orderState.status

    # -- account requests -----------------------------------------------------

    def req_account_updates(self, subscribe, account=""):
        if subscribe:
            # IB re-pushes the full account snapshot on (re)subscribe. Start
            # clean so a reconnect/re-subscribe doesn't stack stale entries on
            # top of the incoming snapshot.
            self.account_values = []
            self.portfolio = []
        EClient.reqAccountUpdates(self, subscribe, account)

    def req_executions(self, exec_filter=None):
        if exec_filter is None:
            from ibapi.execution import ExecutionFilter
            exec_filter = ExecutionFilter()
        EClient.reqExecutions(self, next(self._req_id), exec_filter)

    def _mark_dirty(self):
        self.account_dirty = True
        if self.on_account_dirty is not None:
            # Account callbacks fire on the socket thread; route the callback onto
            # the asyncio loop so the handler never runs on the socket thread.
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self.on_account_dirty)
            else:
                self.on_account_dirty()

    # -- EWrapper: account ----------------------------------------------------

    def updateAccountValue(self, key, val, currency, accountName):
        item = AccountValue(key, val, currency)
        for i, existing in enumerate(self.account_values):
            if existing.tag == key and existing.currency == currency:
                self.account_values[i] = item   # same metric re-pushed each cycle
                break
        else:
            self.account_values.append(item)
        self._mark_dirty()

    def updatePortfolio(self, contract, position, marketPrice, marketValue,
                        averageCost, unrealizedPNL, realizedPNL, accountName):
        item = PortfolioItem(contract, float(position), marketPrice,
                             marketValue, averageCost, unrealizedPNL,
                             realizedPNL, accountName)
        key = _portfolio_key(contract)
        for i, existing in enumerate(self.portfolio):
            if _portfolio_key(existing.contract) == key:
                if float(position) == 0:
                    del self.portfolio[i]       # position fully closed
                else:
                    self.portfolio[i] = item    # refresh live fields in place
                break
        else:
            if float(position) != 0:
                self.portfolio.append(item)     # brand-new position
        self._mark_dirty()

    def accountDownloadEnd(self, accountName):
        self._mark_dirty()

    def execDetails(self, reqId, contract, execution):
        rec = ExecutionRecord(contract, execution, None)
        self._exec_by_id[execution.execId] = rec
        self.executions.append(rec)
        self._mark_dirty()

    def commissionAndFeesReport(self, commissionAndFeesReport):
        rec = self._exec_by_id.get(commissionAndFeesReport.execId)
        if rec is not None:
            new_rec = ExecutionRecord(rec.contract, rec.execution, commissionAndFeesReport)
            self.executions[self.executions.index(rec)] = new_rec
            # Keep the index pointing at the current record so a second
            # commission report for the same execId still finds it in the list.
            self._exec_by_id[commissionAndFeesReport.execId] = new_rec
        self._mark_dirty()

    # -- one-shot request plumbing ------------------------------------------

    def _start_request(self):
        req_id = next(self._req_id)
        req = _Request(self._loop)
        self._requests[req_id] = req
        return req_id, req

    def _finish_request(self, req_id):
        req = self._requests.pop(req_id, None)
        if req is not None and not req.future.done():
            self._loop.call_soon_threadsafe(req.future.set_result, list(req.items))

    async def req_contract_details(self, contract, timeout=30.0):
        req_id, req = self._start_request()
        EClient.reqContractDetails(self, req_id, contract)
        with perf.timer("ib.details"):
            try:
                return await asyncio.wait_for(req.future, timeout=timeout)
            except asyncio.TimeoutError:
                return []
            finally:
                self._requests.pop(req_id, None)

    async def req_chain_contract_details(self, symbol, expiry, trading_class, timeout=60.0):
        """One partial request (no strike, no right): every listed contract of the expiry."""
        c = Contract()
        c.symbol, c.secType, c.exchange, c.currency = symbol, "OPT", "SMART", "USD"
        c.lastTradeDateOrContractMonth = expiry
        c.tradingClass = trading_class
        with perf.timer("ib.chain_details"):
            return await self.req_contract_details(c, timeout=timeout)

    async def req_sec_def_opt_params(self, symbol, fut_fop_exchange, sec_type, con_id,
                                     timeout=30.0):
        req_id, req = self._start_request()
        EClient.reqSecDefOptParams(self, req_id, symbol, fut_fop_exchange, sec_type, con_id)
        try:
            return await asyncio.wait_for(req.future, timeout=timeout)
        except asyncio.TimeoutError:
            return []
        finally:
            self._requests.pop(req_id, None)

    async def req_historical_bars(self, contract, end_date_time="", duration="1 D",
                                  bar_size="1 min", what_to_show="TRADES",
                                  use_rth=True, format_date=2, timeout=30.0):
        req_id, req = self._start_request()
        EClient.reqHistoricalData(
            self, req_id, contract, end_date_time, duration, bar_size,
            what_to_show, use_rth, format_date, False, [])
        try:
            bars = await asyncio.wait_for(req.future, timeout=timeout)
        except asyncio.TimeoutError:
            return []
        finally:
            self._requests.pop(req_id, None)
        return [_coerce_bar(b) for b in bars]

    async def req_historical_bars_live(self, contract, on_update, on_error=None, *, duration="1 D",
                                       bar_size="1 min", what_to_show="TRADES", use_rth=True,
                                       timeout=30.0):
        """keepUpToDate request: return (req_id, initial bars); later bars go to ``on_update``.

        ``on_update(bar)`` and ``on_error(code, message)`` run on the loop thread. ``on_error`` gets
        every error on the request except IB warnings (2100-2299), including those that end the
        updates (10182, 10225, 10197); the caller re-requests on them. An error during the initial
        load reaches ``on_error`` before this coroutine returns, and the return is then short or
        empty; the request is still registered, so the caller must call ``cancel_historical_bars``.
        An initial load that times out or is cancelled is cancelled here (timeout: ``(req_id, [])``).
        Data lane: waits on the pacer.
        """
        await self.pacer.acquire()
        req_id, req = self._start_request()
        self._live_bars[req_id] = _LiveBars(on_update, on_error)
        EClient.reqHistoricalData(self, req_id, contract, "", duration, bar_size,
                                  what_to_show, use_rth, 2, True, [])
        try:
            bars = await asyncio.wait_for(req.future, timeout=timeout)
        except asyncio.TimeoutError:
            self.cancel_historical_bars(req_id)
            return req_id, []
        except asyncio.CancelledError:
            self.cancel_historical_bars(req_id)
            raise
        finally:
            self._requests.pop(req_id, None)
        return req_id, [_coerce_bar(b) for b in bars]

    def cancel_historical_bars(self, req_id) -> None:
        """Stop a keepUpToDate request; a second call (or an unknown id) sends nothing."""
        if self._live_bars.pop(req_id, None) is None:
            return
        self._requests.pop(req_id, None)
        try:
            EClient.cancelHistoricalData(self, req_id)
        except Exception:
            pass
        self.pacer.debit()

    def historicalDataUpdate(self, reqId, bar):
        live = self._live_bars.get(reqId)
        if live is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._deliver_live_bar, reqId, live, _coerce_bar(bar))

    def _deliver_live_bar(self, req_id, live, bar):
        """Loop thread: drop an update queued before its request was cancelled (or re-requested)."""
        if self._live_bars.get(req_id) is live:
            live.on_update(bar)

    def _deliver_live_error(self, req_id, live, code, message):
        """Loop thread: drop an error queued before its request was cancelled (or re-requested)."""
        if self._live_bars.get(req_id) is live:
            live.on_error(code, message)

    def historicalData(self, reqId, bar):
        req = self._requests.get(reqId)
        if req is not None:
            req.items.append(bar)

    def historicalDataEnd(self, reqId, start, end):
        self._finish_request(reqId)

    # -- EWrapper: contract details ------------------------------------------

    def contractDetails(self, reqId, contractDetails):
        req = self._requests.get(reqId)
        if req is not None:
            req.items.append(contractDetails)

    def contractDetailsEnd(self, reqId):
        self._finish_request(reqId)

    # -- EWrapper: sec-def-opt-params ----------------------------------------

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId,
                                          tradingClass, multiplier, expirations, strikes):
        req = self._requests.get(reqId)
        if req is not None:
            req.items.append(SecDefOptParams(exchange, tradingClass, multiplier,
                                             list(expirations), list(strikes)))

    def securityDefinitionOptionParameterEnd(self, reqId):
        self._finish_request(reqId)

    # -- market data callbacks (socket thread; never block) -------------------

    def _get_stream(self, req_id):
        stream = self._streams.get(req_id)
        if stream is None:
            stream = TickStream(req_id, None)
            self._streams[req_id] = stream
        return stream

    def tickPrice(self, reqId, tickType, price, attribs):
        stream = self._get_stream(reqId)
        p = _finite_or_none(price)
        if tickType == BID:
            stream.bid = p
        elif tickType == ASK:
            stream.ask = p
        elif tickType == LAST:
            stream.last = p
        elif tickType == HIGH:
            stream.high = p
        elif tickType == LOW:
            stream.low = p
        elif tickType == CLOSE:
            stream.close = p
        elif tickType == IMPLIED_VOL:
            stream.implied_volatility = p
        stream._mark(has_quote=tickType in (BID, ASK))
        self._on_stream_tick(stream)

    def tickSize(self, reqId, tickType, size):
        stream = self._get_stream(reqId)
        n = int(size) if size not in (None, -1) else 0
        if tickType == BID_SIZE:
            stream.bid_size = n
        elif tickType == ASK_SIZE:
            stream.ask_size = n
        elif tickType == LAST_SIZE:
            stream.last_size = n
        elif tickType == VOLUME:
            stream.volume = n
        elif tickType == CALL_OPEN_INTEREST:
            stream.call_oi = n
        elif tickType == PUT_OPEN_INTEREST:
            stream.put_oi = n
        elif tickType == OPEN_INTEREST:
            stream.open_interest = n
        stream._mark(has_quote=False)
        self._on_stream_tick(stream)

    def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol, delta,
                              optPrice, pvDividend, gamma, vega, theta, undPrice):
        stream = self._get_stream(reqId)
        g = {BID_OPT: stream.bid_greeks, ASK_OPT: stream.ask_greeks,
             LAST_OPT: stream.last_greeks, MODEL_OPT: stream.model_greeks}.get(tickType)
        if g is not None:
            g.update(impliedVol, delta, gamma, vega, theta, optPrice, undPrice)
        stream._mark(has_quote=False)
        self._on_stream_tick(stream)

    # -- market data subscription -------------------------------------------

    def _register_stream(self, contract):
        req_id = next(self._req_id)
        stream = TickStream(req_id, contract)
        self._streams[req_id] = stream
        return stream

    def subscribe_tick(self, contract, generic="", share="fixed"):
        if not self.line_budget.try_acquire(share):
            raise LineBudgetExceeded(
                f"no free '{share}' market-data line "
                f"({self.line_budget.used(share)}/{self.line_budget.capacity(share)})")
        stream = self._register_stream(contract)
        self._stream_share[stream.req_id] = share
        try:
            EClient.reqMktData(self, stream.req_id, contract, generic, False, False, [])
        except BaseException:
            self._streams.pop(stream.req_id, None)
            self._stream_share.pop(stream.req_id, None)
            self.line_budget.release(share)
            raise
        return stream

    async def subscribe_tick_paced(self, contract, generic="", share="fixed"):
        """``subscribe_tick`` for the data lane: waits for the pacer first, so a long fill of
        stream subscriptions never queues ahead of an order. A cancelled wait takes no line."""
        await self.pacer.acquire()
        return self.subscribe_tick(contract, generic, share)

    def unsubscribe_tick(self, req_id, send_cancel=True):
        self._streams.pop(req_id, None)
        share = self._stream_share.pop(req_id, None)
        if share is not None:
            self.line_budget.release(share)
        if not send_cancel:
            return                      # the request never went out: nothing to cancel at IB
        try:
            EClient.cancelMktData(self, req_id)
        except Exception:
            pass

    def unsubscribe_all(self):
        """Unsubscribe every active market-data stream.

        The native bridge tracks subscriptions by reqId; callers tearing down a
        client (e.g. reconnect) should use this instead of reaching into
        ``_streams`` directly.
        """
        for req_id in list(self._streams.keys()):
            self.unsubscribe_tick(req_id)

    # -- snapshot completion state ------------------------------------------

    def _snapshot_settle(self, req_id):
        batch = self._snapshot_batches.pop(req_id, None)
        if batch is None:
            return
        batch.pending.discard(req_id)
        if not batch.pending and self._loop is not None:
            self._loop.call_soon_threadsafe(batch.done.set)

    def _on_stream_tick(self, stream):
        self._snapshot_settle(stream.req_id)

    async def fetch_snapshot(self, contracts, generic="101", timeout=5.0, grace=0.5,
                             share="poll"):
        contracts = list(contracts)
        if not contracts:
            return []
        if len(contracts) > self.line_budget.capacity(share):
            raise ValueError(f"snapshot batch of {len(contracts)} exceeds the '{share}' "
                             f"share ({self.line_budget.capacity(share)})")
        await self.line_budget.acquire(share, len(contracts))
        batch = _SnapshotBatch()
        # Register the streams and mark them pending BEFORE issuing reqMktData so a
        # fast first tick can't land in the gap between subscribe and pending-set.
        streams = [self._register_stream(c) for c in contracts]
        for s in streams:
            self._stream_share[s.req_id] = share
            batch.pending.add(s.req_id)
            self._snapshot_batches[s.req_id] = batch
        sent = set()                                  # req ids whose reqMktData actually went out
        try:
            for s in streams:
                await self.pacer.acquire()            # data lane: keep the queue short for orders
                EClient.reqMktData(self, s.req_id, s.contract, generic, False, False, [])
                sent.add(s.req_id)
            try:
                await asyncio.wait_for(batch.done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            # Grace window: the batch completes on first-tick per stream, which is
            # often a bid/ask arriving before OI (tick 27/28) or greeks (10-13). Let
            # that initial burst land before cancelling so snapshots keep OI/greeks.
            if grace > 0:
                await asyncio.sleep(grace)
        finally:
            for s in streams:
                self._snapshot_batches.pop(s.req_id, None)
                self.unsubscribe_tick(s.req_id, send_cancel=s.req_id in sent)   # releases the line
            self.pacer.debit(len(sent))               # the cancels are messages too, but never awaited
        return streams
