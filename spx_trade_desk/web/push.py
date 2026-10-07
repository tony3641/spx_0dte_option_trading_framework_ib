"""Per-browser send channels: ``broadcast`` enqueues and returns; one writer task per socket sends.

Kinds (``message_kind``):
- critical: order status, IB errors, strategy messages, handler replies, ``init``. Never dropped,
  sent first. More than ``PUSH_ORDERED_BACKLOG_MAX`` unsent closes the client (it reconnects and
  gets a fresh ``init``).
- log: ordered, in their own bounded deque; overflow drops the oldest and sends one note.
- merge: ``chain_tick``; pending ticks are merged per (strike, right), field by field.
- latest: a newer message replaces an unsent one with the same key (type, or type + bar / point
  time for ``price_bar`` and ``price_overnight``).
A send longer than ``PUSH_SEND_TIMEOUT_S`` or a send error closes that client only. Loop thread only.
"""
import asyncio
import inspect
import itertools
import json
import logging
import math
import re
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Dict, Optional, Set, Tuple

from starlette.websockets import WebSocketDisconnect, WebSocketState

from spx_trade_desk.core.config import PUSH_ORDERED_BACKLOG_MAX, PUSH_SEND_TIMEOUT_S
from spx_trade_desk.core.perf import perf

logger = logging.getLogger(__name__)

# Strong references to in-flight socket closes (the loop only keeps weak ones).
_CLOSE_TASKS: Set[asyncio.Task] = set()
# Log-dropped notes use -1, -2, ... process-wide: the browser dedupes logs by seq and keeps its
# console across a reconnect, so a new channel's note must not reuse an old channel's seq.
_NOTE_SEQ = itertools.count(-1, -1)

LOG_BACKLOG_MAX = 500
LATEST_TYPES = frozenset({
    "status", "vix_update", "gex", "chain_quotes", "chain_progress", "account_update",
    "monthly_gex", "monthly_gex_progress", "price_snapshot", "price_overnight", "price_bar", "ping",
})
_CLIENT_SPAN_RE = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")
_CLIENT_MAX_NAMES = 50
_CLIENT_MAX_SAMPLES = 200


def message_kind(mtype: str) -> str:
    if mtype == "chain_tick":
        return "merge"
    if mtype == "log":
        return "log"
    if mtype in LATEST_TYPES:
        return "latest"
    return "critical"


def _latest_key(message: dict) -> str:
    mtype = message.get("type", "")
    if mtype == "price_bar":
        bar = (message.get("data") or {}).get("bar") or {}
        return f"price_bar:{bar.get('time', '')}"
    if mtype == "price_overnight":
        point = (message.get("data") or {}).get("point") or {}
        return f"price_overnight:{point.get('time', '')}"
    return mtype


def stamp(message: dict, now_ms: Optional[float] = None) -> dict:
    out = dict(message)
    out["ts"] = now_ms if now_ms is not None else time.time() * 1000.0
    return out


def encode(message: dict) -> Optional[str]:
    try:
        return json.dumps(message, allow_nan=False)
    except (ValueError, TypeError) as e:
        logger.error(f"Dropping non-JSON-serializable payload type={message.get('type')}: {e}")
        return None


class ClientChannel:
    def __init__(self, ws, *, on_close: Optional[Callable] = None,
                 send_timeout: float = PUSH_SEND_TIMEOUT_S,
                 backlog_max: int = PUSH_ORDERED_BACKLOG_MAX,
                 log_max: int = LOG_BACKLOG_MAX,
                 clock: Callable[[], float] = time.monotonic):
        self.ws = ws
        self.closed = False
        self._closing = False          # set by aclose: no new messages, a failed drain send is quiet
        self._close_task: Optional[asyncio.Task] = None
        self._on_close = on_close
        self._send_timeout = send_timeout
        self._backlog_max = backlog_max
        self._log_max = log_max
        self._clock = clock
        self._critical: Deque[Tuple[str, float]] = deque()
        self._log: Deque[Tuple[str, float]] = deque()
        self._log_dropped = 0
        self._latest: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        self._ticks: Dict[tuple, dict] = {}
        self._tick_meta: dict = {}
        self._tick_enq: Optional[float] = None
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- producer side ------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def send_message(self, message: dict) -> None:
        stamped = stamp(message)
        text = encode(stamped)
        if text is not None:
            self.enqueue(stamped, text)

    async def send_text(self, text: str) -> None:
        """Sender interface for code that holds a socket-like object (``ib/orders.py``).

        The text is re-encoded so it gets a ``ts`` and a NaN payload is dropped, as in ``send_message``.
        """
        try:
            message = json.loads(text)
        except (TypeError, ValueError, RecursionError) as e:
            logger.error(f"Dropping a non-JSON message for one client: {text[:80]!r}: {e}")
            return
        if not isinstance(message, dict):
            logger.error(f"Dropping a non-object message for one client: {text[:80]!r}")
            return
        out = encode(stamp(message))
        if out is not None:
            self._push_critical(out)

    def enqueue(self, message: dict, text: Optional[str]) -> None:
        if self.closed or self._closing or text is None:
            return
        kind = message_kind(message.get("type", ""))
        now = self._clock()
        if kind == "merge":
            data = message.get("data") or {}
            for t in data.get("ticks") or []:
                self._ticks.setdefault((t.get("strike"), t.get("right")), {}).update(t)
            self._tick_meta = {"timestamp_iso": data.get("timestamp_iso"), "ts": message.get("ts")}
            if self._tick_enq is None:
                self._tick_enq = now
        elif kind == "latest":
            key = _latest_key(message)
            self._latest.pop(key, None)
            self._latest[key] = (text, now)
        elif kind == "log":
            self._log.append((text, now))
            while len(self._log) > self._log_max:
                self._log.popleft()
                self._log_dropped += 1
        else:
            self._push_critical(text, now)
            return
        self._wake.set()

    def _push_critical(self, text: str, now: Optional[float] = None) -> None:
        if self.closed or self._closing:
            return
        self._critical.append((text, now if now is not None else self._clock()))
        if len(self._critical) > self._backlog_max:
            self._close(f"more than {self._backlog_max} unsent critical messages")
            return
        self._wake.set()

    # -- writer side --------------------------------------------------------

    def _next(self) -> Optional[Tuple[str, float]]:
        if self._critical:
            return self._critical.popleft()
        if self._log_dropped:
            n, self._log_dropped = self._log_dropped, 0
            note = stamp({"type": "log", "data": {"seq": next(_NOTE_SEQ), "ts": "", "level": "WARNING", "name": "push",
                                                   "msg": f"{n} log lines dropped (the browser fell behind)"}})
            return json.dumps(note), self._clock()
        if self._log:
            return self._log.popleft()
        if self._ticks:
            ticks = [dict(v) for v in self._ticks.values()]
            msg = {"type": "chain_tick", "data": {"ticks": ticks,
                                                  "timestamp_iso": self._tick_meta.get("timestamp_iso")},
                   "ts": self._tick_meta.get("ts")}
            enq = self._tick_enq if self._tick_enq is not None else self._clock()
            self._ticks, self._tick_meta, self._tick_enq = {}, {}, None
            text = encode(msg)
            return (text, enq) if text is not None else self._next()
        if self._latest:
            _, item = self._latest.popitem(last=False)
            return item
        return None

    async def _send(self, text: str, enq: float) -> bool:
        t0 = self._clock()
        perf.record("push.queue_wait", (t0 - enq) * 1000.0)
        try:
            await asyncio.wait_for(self.ws.send_text(text), timeout=self._send_timeout)
        except asyncio.TimeoutError:
            self._close(f"one send took over {self._send_timeout}s")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._close(f"send failed: {e!r}", gone=self._client_gone(e))
            return False
        perf.record("push.send", (self._clock() - t0) * 1000.0)
        return True

    async def _run(self) -> None:
        try:
            while not self.closed and not self._closing:   # aclose drains the rest itself
                item = self._next()
                if item is None:
                    self._wake.clear()
                    item = self._next()
                    if item is None:
                        await self._wake.wait()
                        continue
                if not await self._send(*item):
                    break
        except asyncio.CancelledError:
            pass

    # -- teardown -----------------------------------------------------------

    def _client_gone(self, exc: Exception) -> bool:
        """True when a send failed because the browser disconnected (not a slow or broken client)."""
        if isinstance(exc, WebSocketDisconnect):
            return True
        return WebSocketState.DISCONNECTED in (getattr(self.ws, "application_state", None),
                                               getattr(self.ws, "client_state", None))

    def _close(self, reason: str, gone: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        if self._closing:   # a failed send while aclose drains: the normal tab-close path
            logger.debug(f"push: drain ended early: {reason}")
        elif gone:          # an ordinary disconnect noticed by the writer before the read loop
            logger.debug(f"push: client went away: {reason}")
        else:
            logger.warning(f"Dropping a WebSocket client: {reason}")
        self._wake.set()
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception as e:
                logger.error(f"push on_close error: {e}")
        if getattr(self.ws, "application_state", None) == WebSocketState.DISCONNECTED:
            return          # Starlette raises on close() after the socket is gone
        close = getattr(self.ws, "close", None)
        if close is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no running loop
            return
        task = loop.create_task(self._safe_close(close))
        self._close_task = task
        _CLOSE_TASKS.add(task)
        task.add_done_callback(_CLOSE_TASKS.discard)

    @staticmethod
    async def _safe_close(close: Callable) -> None:
        try:
            res = close()
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.debug(f"push: closing the socket failed: {e!r}")

    async def aclose(self, drain: bool = True) -> None:
        """Stop the writer; with ``drain``, send what is still queued first (each send bounded).

        New messages are ignored from the first line on, so producers cannot extend the drain.
        With ``drain`` the writer is not cancelled: it finishes its in-flight send (bounded) and
        exits on ``_closing``, so that message is not lost. Exiting on the flag also covers
        Python 3.10's ``wait_for`` swallowing a cancel that lands as the send completes.
        """
        self._closing = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            if not drain:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if drain and not self.closed:
            while True:
                item = self._next()
                if item is None or not await self._send(*item):
                    break
        self.closed = True
        if self._close_task is not None and not self._close_task.done():
            try:   # on timeout wait_for cancels the hung close: intended, the client is gone either way
                await asyncio.wait_for(self._close_task, timeout=self._send_timeout)
            except asyncio.TimeoutError:
                logger.debug("push: closing the socket timed out")


def record_client_perf(raw: str) -> int:
    """Record a browser ``perf_report`` as ``client.<name>`` spans; return the samples kept."""
    try:
        body = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return 0
    spans = body.get("spans") if isinstance(body, dict) else None
    if not isinstance(spans, dict):
        return 0
    kept = 0
    for name, samples in list(spans.items())[:_CLIENT_MAX_NAMES]:
        if not isinstance(name, str) or not _CLIENT_SPAN_RE.match(name) or not isinstance(samples, list):
            continue
        for v in samples[-_CLIENT_MAX_SAMPLES:]:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            try:
                ms = float(v)            # JSON ints are unbounded: a huge one overflows here
            except (OverflowError, ValueError, TypeError):
                continue
            if math.isfinite(ms) and 0 <= ms < 60000:
                perf.record(f"client.{name}", ms)
                kept += 1
    return kept
