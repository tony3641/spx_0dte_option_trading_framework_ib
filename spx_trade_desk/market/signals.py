"""
Signal tracker: timestamped market events for the price chart and the signal feed.

Pure Python over plain dicts: the ``dealer`` block from ``dealer_payload``, ``FlowTracker.minutes()`` records
and 1-minute bars (``time``, ``open``, ``high``, ``low``, ``close``). ``on_bar_closed`` runs per closed bar,
``on_publish`` per gex publish. A new expiration clears the history and all per-node / per-type state.
Thresholds are placeholders until the Phase 5 replay calibrates them.
"""

import math
from collections import deque
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from spx_trade_desk.market.hours import ET

SIGNAL_COOLDOWN_MIN = 5
NODE_TOUCH_PTS = 2.0
NODE_CONFIRM_BARS = 3
NODE_CONFIRM_PTS = 3.0
SURGE_Z = 2.5
SURGE_MIN_USD = 10_000_000
SURGE_MIN_SAMPLES = 10
SURGE_WINDOW_MIN = 30
DIV_WINDOW_MIN = 30
VANNA_IV_PTS = 1.5
CHARM_WINDOW_ET = time(14, 0)
CHARM_MIN_USD = 100_000_000
MAX_SIGNALS = 500

_CHARM_END_ET = time(16, 0)
_FMT = "%Y-%m-%dT%H:%M"


def _et_naive(stamp: str) -> datetime:
    """ISO timestamp as a naive ET wall-clock datetime at minute resolution (naive input is taken as ET)."""
    dt = datetime.fromisoformat(stamp)
    if dt.tzinfo is not None:
        dt = dt.astimezone(ET).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def _as_et(now: datetime) -> datetime:
    return now.replace(tzinfo=ET) if now.tzinfo is None else now.astimezone(ET)


def _px(x: float) -> float:
    return round(float(x), 2)


def _usd_short(x: float) -> str:
    """Whole-dollar magnitude as ``250M`` / ``1.2B`` (sign is added by the caller)."""
    a = abs(x)
    if round(a / 1e6) >= 1000:
        return f"{a / 1e9:.1f}".rstrip("0").rstrip(".") + "B"
    return f"{round(a / 1e6)}M"


def _side_of(close: float, strike: float) -> str:
    return "support" if close > strike else "resistance"


class SignalTracker:
    def __init__(self) -> None:
        self._expiration: Optional[str] = None
        self._exp_date = None
        self._history: deque = deque(maxlen=MAX_SIGNALS)
        self._reset_state()

    def _reset_state(self) -> None:
        self._last_emit: Dict[Tuple[str, str], datetime] = {}   # (type, text) -> minute of last emitted signal
        self._last_type_minute: Dict[str, str] = {}             # type -> minute of its last emitted signal
        self._origin: Dict[float, str] = {}                     # node strike -> "support" | "resistance"
        self._pending: Dict[float, int] = {}                    # node strike -> bars left to confirm a hold
        self._run: Dict[float, int] = {}                        # node strike -> consecutive closes beyond it
        self._charm_done = False

    def _switch(self, expiration: str) -> None:
        if expiration == self._expiration:
            return
        self._expiration = expiration
        try:
            self._exp_date = datetime.strptime(expiration, "%Y%m%d").date()
        except (TypeError, ValueError):
            self._exp_date = None
        self._history.clear()
        self._reset_state()

    def history(self) -> List[dict]:
        return list(self._history)

    def _emit(self, out: List[dict], minute: str, type_: str, side: str, price: float, text: str, detail: dict) -> None:
        if self._last_type_minute.get(type_) == minute:
            return
        now = datetime.strptime(minute, _FMT)
        last = self._last_emit.get((type_, text))
        if last is not None and now - last < timedelta(minutes=SIGNAL_COOLDOWN_MIN):
            return
        self._last_type_minute[type_] = minute
        self._last_emit[(type_, text)] = now
        rec = {"time": minute, "type": type_, "side": side, "price": _px(price), "text": text, "detail": detail}
        self._history.append(rec)
        out.append(rec)

    # ---- per closed bar ----

    def on_bar_closed(self, expiration: str, bar: dict, prior_bars: List[dict], dealer: Optional[dict],
                      flow_minutes: List[dict]) -> List[dict]:
        self._switch(expiration)
        out: List[dict] = []
        minute = _et_naive(bar["time"]).strftime(_FMT)
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        prev = prior_bars[-1] if prior_bars else None
        dealer = dealer or {}

        self._zero_gamma(out, minute, prev, close, dealer.get("zero_gamma"))
        self._nodes(out, minute, high, low, close, dealer.get("nodes") or [])
        self._expected_move(out, minute, prev, high, low, close, dealer.get("em") or {})
        self._flow_surge(out, minute, close, flow_minutes)
        self._flow_divergence(out, minute, prior_bars, high, low, close, flow_minutes)
        return out

    def _zero_gamma(self, out, minute, prev, close, zero) -> None:
        if zero is None or prev is None:
            return
        zero = float(zero)
        prev_close = float(prev["close"])
        if prev_close < zero <= close:
            self._emit(out, minute, "zg_cross", "bull", close, "ZG up", {"zero_gamma": _px(zero)})
        elif prev_close >= zero > close:
            self._emit(out, minute, "zg_cross", "bear", close, "ZG dn", {"zero_gamma": _px(zero)})

    def _expected_move(self, out, minute, prev, high, low, close, em) -> None:
        if prev is None:
            return
        band_high, band_low = em.get("high"), em.get("low")
        if band_high is not None and float(prev["high"]) < band_high <= high:
            self._emit(out, minute, "em_touch", "neutral", close, "EM+", {"em_high": _px(band_high)})
        if band_low is not None and float(prev["low"]) > band_low >= low:
            self._emit(out, minute, "em_touch", "neutral", close, "EM-", {"em_low": _px(band_low)})

    def _nodes(self, out, minute, high, low, close, nodes) -> None:
        current: Dict[float, dict] = {}
        for n in nodes:
            if n.get("strike") is None:
                continue
            current[float(n["strike"])] = n
        for store in (self._origin, self._pending, self._run):
            for k in [k for k in store if k not in current]:
                del store[k]
        for k, n in current.items():
            self._node_step(out, minute, high, low, close, k, n.get("label") or _px(k), n.get("kind"))

    def _node_step(self, out, minute, high, low, close, k, label, kind) -> None:
        if k not in self._origin:
            self._origin[k] = _side_of(close, k)
        origin = self._origin[k]
        support = origin == "support"
        had_pending = k in self._pending
        touched = low <= k + NODE_TOUCH_PTS if support else high >= k - NODE_TOUCH_PTS
        detail = {"strike": _px(k), "kind": kind}
        signaled = False

        if had_pending:
            holds = close >= k + NODE_CONFIRM_PTS if support else close <= k - NODE_CONFIRM_PTS
            if holds:
                self._emit(out, minute, "node_hold", "bull" if support else "bear", close, label, detail)
                self._pending.pop(k, None)
                self._run.pop(k, None)
                self._origin[k] = _side_of(close, k)
                signaled = True
            else:
                left = self._pending[k] - 1
                if left > 0:
                    self._pending[k] = left
                else:
                    self._pending.pop(k, None)

        beyond = close <= k - NODE_CONFIRM_PTS if support else close >= k + NODE_CONFIRM_PTS
        run = self._run.get(k, 0) + 1 if beyond else 0
        if run >= NODE_CONFIRM_BARS:
            self._emit(out, minute, "node_break", "bear" if support else "bull", close, f"{label} brk", detail)
            self._run.pop(k, None)
            self._pending.pop(k, None)
            self._origin[k] = _side_of(close, k)
            signaled = True
        elif run:
            self._run[k] = run
        else:
            self._run.pop(k, None)

        # A touch opens a confirmation window only when none was open before this bar.
        if touched and not had_pending and not signaled:
            self._pending[k] = NODE_CONFIRM_BARS

    def _flow_surge(self, out, minute, close, flow_minutes) -> None:
        current = next((r for r in flow_minutes if r["time"] == minute), None)
        if current is None:
            return
        end = datetime.strptime(minute, _FMT)
        start = (end - timedelta(minutes=SURGE_WINDOW_MIN)).strftime(_FMT)
        prior = [float(r.get("hedge_pressure_usd") or 0.0) for r in flow_minutes if start <= r["time"] < minute]
        if len(prior) < SURGE_MIN_SAMPLES:
            return
        mean = sum(prior) / len(prior)
        std = math.sqrt(sum((v - mean) ** 2 for v in prior) / len(prior))
        x = float(current.get("hedge_pressure_usd") or 0.0)
        if std <= 0 or abs(x) < SURGE_Z * std or abs(x) < SURGE_MIN_USD:
            return
        up = x > 0
        self._emit(out, minute, "flow_surge", "bull" if up else "bear", close, "F+" if up else "F-",
                   {"usd": round(x), "z": round(x / std, 2)})

    def _flow_divergence(self, out, minute, prior_bars, high, low, close, flow_minutes) -> None:
        if len(prior_bars) < DIV_WINDOW_MIN:
            return
        recent = prior_bars[-DIV_WINDOW_MIN:]
        prior_high = max(float(b["high"]) for b in recent)
        prior_low = min(float(b["low"]) for b in recent)
        end = datetime.strptime(minute, _FMT)
        start = (end - timedelta(minutes=DIV_WINDOW_MIN)).strftime(_FMT)
        pressure = sum(float(r.get("hedge_pressure_usd") or 0.0) for r in flow_minutes if start < r["time"] <= minute)
        if high > prior_high and pressure < 0:
            self._emit(out, minute, "flow_div", "bear", close, "DIV", {"hedge_window_usd": round(pressure)})
        if low < prior_low and pressure > 0:
            self._emit(out, minute, "flow_div", "bull", close, "DIV", {"hedge_window_usd": round(pressure)})

    # ---- per gex publish ----

    def on_publish(self, expiration: str, now: datetime, dealer: Optional[dict], price: float) -> List[dict]:
        self._switch(expiration)
        out: List[dict] = []
        if dealer is None:
            return out
        et = _as_et(now)
        minute = et.replace(second=0, microsecond=0).strftime(_FMT)

        vanna = dealer.get("vanna") or {}
        iv_change, flow_est = vanna.get("iv_change_15m"), vanna.get("flow_est_usd")
        if iv_change is not None and flow_est and abs(iv_change) >= VANNA_IV_PTS:
            up = flow_est > 0
            self._emit(out, minute, "vanna_shift", "bull" if up else "bear", price, "V+" if up else "V-",
                       {"iv_change_15m": iv_change, "flow_est_usd": round(flow_est)})

        self._charm_window(out, minute, et, price, (dealer.get("charm") or {}).get("to_close_usd"))
        return out

    def _charm_window(self, out, minute, et, price, to_close) -> None:
        if self._charm_done or self._exp_date is None or et.date() != self._exp_date:
            return
        if not (CHARM_WINDOW_ET <= et.time() < _CHARM_END_ET) or to_close is None:
            return
        self._charm_done = True
        if abs(to_close) < CHARM_MIN_USD:
            return
        up = to_close > 0
        text = ("C+ $" if up else "C- $") + _usd_short(to_close)
        self._emit(out, minute, "charm_window", "bull" if up else "bear", price, text,
                   {"to_close_usd": round(to_close)})
