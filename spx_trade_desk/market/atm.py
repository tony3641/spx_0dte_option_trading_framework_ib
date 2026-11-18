"""
ATM straddle, expected-move anchor and ATM IV history.

Strike bracketing (shared by the straddle and the ATM IV): a strike is valid only when it has both a
call and a put value. The value at spot is linear between the nearest valid strikes on either side;
if either side is missing, or the two are more than ``EM_MAX_GAP`` points apart, the result is None.
Pure Python, no scipy.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.hours import ET

EM_MAX_GAP = 10.0                       # points; wider brackets (a missing wing) give no value
EM_ANCHOR_TIME = time(9, 31)            # the open band anchors at the first publish at or after this
SESSION_CLOSE = time(16, 0)


def option_mid(o: OptionData) -> Optional[float]:
    """Bid/ask mid, or None unless both sides are quoted (bid > 0 and ask > 0)."""
    if o.bid is None or o.ask is None or o.bid <= 0 or o.ask <= 0:
        return None
    return (o.bid + o.ask) / 2.0


def _paired_values(options: List[OptionData], value_of) -> Dict[float, Tuple[float, float]]:
    """Strike -> (call value, put value) for strikes where both sides have a value."""
    calls: Dict[float, float] = {}
    puts: Dict[float, float] = {}
    for o in options:
        v = value_of(o)
        if v is None:
            continue
        (calls if o.right == "C" else puts)[o.strike] = v
    return {k: (calls[k], puts[k]) for k in calls.keys() & puts.keys()}


def _bracket(values: Dict[float, float], spot: float) -> Optional[float]:
    """Linear value at ``spot`` between the nearest valid strikes; None when a side is missing or the gap is too wide."""
    below = [k for k in values if k <= spot]
    above = [k for k in values if k >= spot]
    if not below or not above:
        return None
    lo, hi = max(below), min(above)
    if lo == hi:
        return values[lo]
    if hi - lo > EM_MAX_GAP:
        return None
    w = (spot - lo) / (hi - lo)
    return values[lo] + w * (values[hi] - values[lo])


def atm_straddle(options: List[OptionData], spot: float) -> Optional[float]:
    """Call mid + put mid at spot, interpolated across the bracketing strikes."""
    pairs = _paired_values(options, option_mid)
    return _bracket({k: c + p for k, (c, p) in pairs.items()}, spot)


def atm_iv(options: List[OptionData], spot: float) -> Optional[float]:
    """Decimal ATM implied vol: the call/put mean per strike, interpolated at spot."""
    def iv_of(o: OptionData) -> Optional[float]:
        return o.implied_vol if o.implied_vol is not None and o.implied_vol > 0 else None

    pairs = _paired_values(options, iv_of)
    return _bracket({k: (c + p) / 2.0 for k, (c, p) in pairs.items()}, spot)


def _et(now: datetime) -> datetime:
    """Aware datetimes convert to ET; naive ones are taken as ET (the series format)."""
    return now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)


@dataclass
class ExpectedMoveAnchor:
    """Open-band anchor: the spot and ATM straddle at the first eligible publish of the expiration's session.

    A mid-session start anchors at its first publish and says so in ``anchor_time``. A publish for another
    expiration (GTH publishes for the next day) or outside [EM_ANCHOR_TIME, SESSION_CLOSE) never anchors.
    """

    open_spot: Optional[float] = None
    open_straddle: Optional[float] = None
    anchor_time: Optional[str] = None
    _expiration: Optional[str] = None

    def update(self, expiration: str, now: datetime, spot: float, straddle: Optional[float]) -> None:
        if expiration != self._expiration:
            self._expiration = expiration
            self.open_spot = self.open_straddle = self.anchor_time = None
        if self.open_spot is not None or straddle is None:
            return
        local = _et(now)
        if local.strftime("%Y%m%d") != expiration:
            return
        if not (EM_ANCHOR_TIME <= local.time() < SESSION_CLOSE):
            return
        self.open_spot = spot
        self.open_straddle = straddle
        self.anchor_time = local.strftime("%H:%M")

    def band(self) -> Tuple[Optional[float], Optional[float]]:
        if self.open_spot is None or self.open_straddle is None:
            return (None, None)
        return (self.open_spot - self.open_straddle, self.open_spot + self.open_straddle)


def session_range(bars: Iterable[dict], session_date: str) -> Tuple[Optional[float], Optional[float]]:
    """(high, low) over the bars whose ``time`` falls on ``session_date`` (YYYY-MM-DD)."""
    highs: List[float] = []
    lows: List[float] = []
    for b in bars:
        if str(b["time"])[:10] != session_date:
            continue
        if b.get("high") is not None:
            highs.append(float(b["high"]))
        if b.get("low") is not None:
            lows.append(float(b["low"]))
    if not highs or not lows:
        return (None, None)
    return (max(highs), min(lows))


def em_used_pct(high: Optional[float], low: Optional[float], open_straddle: Optional[float]) -> Optional[float]:
    """Session range as a percent of the open band width (2 x the open straddle)."""
    if high is None or low is None or open_straddle is None or open_straddle <= 0:
        return None
    return (high - low) / (2.0 * open_straddle) * 100.0


class AtmIvHistory:
    """ATM IV by minute for one expiration. Values are decimal; ``change`` is in vol points."""

    def __init__(self) -> None:
        self._expiration: Optional[str] = None
        self._points: Dict[datetime, float] = {}

    def record(self, expiration: str, stamp: str, iv: Optional[float]) -> None:
        if expiration != self._expiration:
            self._expiration = expiration
            self._points = {}
        if iv is None:
            return
        minute = _et(datetime.fromisoformat(stamp)).replace(second=0, microsecond=0)
        self._points[minute] = float(iv)

    def latest(self) -> Optional[float]:
        if not self._points:
            return None
        return self._points[max(self._points)]

    def change(self, minutes: int) -> Optional[float]:
        """Latest IV minus the newest value at or before (latest minute - ``minutes``), in vol points."""
        if not self._points:
            return None
        last = max(self._points)
        prior = [m for m in self._points if m <= last - timedelta(minutes=minutes)]
        if not prior:
            return None
        return (self._points[last] - self._points[max(prior)]) * 100.0
