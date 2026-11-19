"""Customer trade classification and intraday flow tracking for streamed option quotes (Phase 2).

Sign convention (repo-wide): hedge flow USD positive = dealers buy the underlying, negative = dealers sell.
Customer flow is the other side of the dealers, so a customer buying a call (or selling a put) is positive
hedge pressure, and a customer buying a put (or selling a call) is negative.

Reported volume is sampled from the stream, so the side of a cycle's increment is approximate. Sizes come from
the volume counter and are exact per cycle.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from spx_trade_desk.core.config import spx_tick_for_price
from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.qualification import Key, norm_key

TIME_FMT = "%Y-%m-%dT%H:%M"

HOT_VOL_OI = 1.0          # volume / open interest at or above this marks a strike hot
HOT_MIN_VOL = 500         # ...and the strike must have at least this much volume (calls + puts)
HOT_TOP_N = 8             # ...and be among the N strikes with the most volume
HOT_FRESH_MIN = 15        # minutes of volume added that the hot strike rows report as vol_15m
VOLUME_HISTORY_MINUTES = 120   # per-minute volume snapshots kept for the vol_15m figure


def classify_trade(last: Optional[float], bid: Optional[float], ask: Optional[float],
                   prev_last: Optional[float]) -> int:
    """+1 customer buy, -1 customer sell, 0 unclassified.

    ``bid``/``ask`` are the quote before the trade. The tick is the repo's SPX rule.
    """
    if last is None:
        return 0
    last = float(last)
    tick = spx_tick_for_price(last)
    if ask is not None and last >= ask - tick / 2:
        return 1
    if bid is not None and last <= bid + tick / 2:
        return -1
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
        if last > mid:
            return 1
        if last < mid:
            return -1
    if prev_last is None:
        return 0
    if last > prev_last:
        return 1
    if last < prev_last:
        return -1
    return 0


@dataclass
class KeyFlow:
    """Per contract key baseline and accumulators, independent of the stream object."""
    last_volume: int
    prev_bid: Optional[float]
    prev_ask: Optional[float]
    prev_last: Optional[float]
    gapped: bool = False
    rebase: bool = False                # a 0 reading after a baseline: the next nonzero reading is a fresh baseline
    buy_vol: int = 0
    sell_vol: int = 0
    unclassified_vol: int = 0
    net_delta_usd: float = 0.0
    net_premium_usd: float = 0.0

    @property
    def net_vol(self) -> int:
        return self.buy_vol - self.sell_vol


@dataclass
class _Minute:
    ts: datetime
    call: float = 0.0
    put: float = 0.0
    lo: Optional[float] = None
    hi: Optional[float] = None


def _usd(x: float) -> int:
    return int(round(x))


def _strike(x: Optional[float]) -> Optional[int]:
    return None if x is None else int(round(x))


def _minute_dict(r: _Minute) -> dict:
    return {
        "time": r.ts.strftime(TIME_FMT),
        "call_delta_usd": _usd(r.call),
        "put_delta_usd": _usd(r.put),
        "hedge_pressure_usd": _usd(r.call + r.put),
        "coverage_lo": _strike(r.lo),
        "coverage_hi": _strike(r.hi),
    }


class FlowTracker:
    """Classifies per-cycle volume increments of the streamed option keys into customer buy/sell flow."""

    def __init__(self, maxlen: int = 1440):
        self._maxlen = maxlen
        self._keys: Dict[Key, KeyFlow] = {}
        self._minutes: deque = deque(maxlen=maxlen)
        self._expiration: Optional[str] = None
        self._coverage: Tuple[Optional[float], Optional[float]] = (None, None)
        self.coverage_start: Optional[str] = None

    def update(self, rows: Iterable[OptionData], streamed_keys: Set[Key], spot: float, now: datetime,
               expiration: str) -> None:
        rows = list(rows)
        streamed = {norm_key(s, r) for s, r in streamed_keys}
        if expiration != self._expiration:
            self._keys.clear()
            self._minutes.clear()
            self.coverage_start = None
            self._coverage = (None, None)
            self._expiration = expiration
        for key, kf in self._keys.items():
            if key not in streamed:
                kf.gapped = True
        if streamed:
            strikes = [k[0] for k in streamed]
            self._coverage = (min(strikes), max(strikes))
        minute = self._minute_for(now)
        minute.lo, minute.hi = self._coverage
        spot = float(spot)

        for row in rows:
            key = norm_key(row.strike, row.right)
            vol = int(row.volume or 0)
            kf = self._keys.get(key)
            if vol <= 0:                        # 0 = not reported yet (fresh stream): never a baseline
                if kf is not None:              # a resubscribe restarts the counter: rebase on the next reading
                    kf.rebase = True
                    kf.prev_bid, kf.prev_ask, kf.prev_last = row.bid, row.ask, row.last
                continue
            if kf is None:                      # first sighting: baseline only
                self._keys[key] = KeyFlow(last_volume=vol, prev_bid=row.bid, prev_ask=row.ask,
                                          prev_last=row.last)
                continue
            if kf.rebase:                       # counter restarted: this reading is a fresh baseline, nothing classified
                kf.rebase = False
                kf.gapped = False
                kf.last_volume = vol
                kf.prev_bid, kf.prev_ask, kf.prev_last = row.bid, row.ask, row.last
                continue
            dv = vol - kf.last_volume
            if kf.gapped:                       # volume traded while unstreamed: size known, side not
                if dv > 0:
                    kf.unclassified_vol += dv
                kf.gapped = False
            elif dv > 0:
                bid = kf.prev_bid if kf.prev_bid is not None else row.bid
                ask = kf.prev_ask if kf.prev_ask is not None else row.ask
                side = classify_trade(row.last, bid, ask, kf.prev_last)
                if side == 0:
                    kf.unclassified_vol += dv
                else:
                    if side > 0:
                        kf.buy_vol += dv
                    else:
                        kf.sell_vol += dv
                    if row.delta is not None:
                        d_usd = side * dv * row.delta * 100 * spot
                        kf.net_delta_usd += d_usd
                        if key[1] == "C":
                            minute.call += d_usd
                        else:
                            minute.put += d_usd
                    if row.last is not None:
                        kf.net_premium_usd += side * dv * row.last * 100
            # dv < 0 (data reset) falls through: baseline only, nothing classified
            kf.last_volume = vol
            kf.prev_bid, kf.prev_ask, kf.prev_last = row.bid, row.ask, row.last

    def _minute_for(self, now: datetime) -> _Minute:
        ts = now.replace(second=0, microsecond=0)
        if self._minutes and self._minutes[-1].ts == ts:
            return self._minutes[-1]
        rec = _Minute(ts=ts)
        self._minutes.append(rec)
        if self.coverage_start is None:
            self.coverage_start = ts.strftime("%H:%M")
        return rec

    def keys(self) -> Dict[Key, KeyFlow]:
        return dict(self._keys)

    def minutes(self) -> List[dict]:
        return [_minute_dict(r) for r in self._minutes]

    def net_volumes(self) -> Tuple[Dict[float, int], Dict[float, int]]:
        calls: Dict[float, int] = {}
        puts: Dict[float, int] = {}
        for (strike, right), kf in self._keys.items():
            (calls if right == "C" else puts)[strike] = kf.net_vol
        return calls, puts

    def summary(self, now: datetime) -> dict:
        """Dashboard summary of the flow so far.

        A window of N minutes is bucket-inclusive: its cutoff is the minute of ``now`` minus N minutes, and a
        minute record counts when its start is at or after that cutoff (so the minute exactly N minutes back
        is included). ``minute`` is the newest minute record; on an empty tracker it has ``time`` None and
        no coverage keys, and a populated one also carries ``coverage_lo`` / ``coverage_hi``.
        """
        def window(minutes_back: int) -> float:
            cutoff = now.replace(second=0, microsecond=0) - timedelta(minutes=minutes_back)
            return sum(r.call + r.put for r in self._minutes if r.ts >= cutoff)

        day_call = sum(r.call for r in self._minutes)
        day_put = sum(r.put for r in self._minutes)
        buy = sum(kf.buy_vol for kf in self._keys.values())
        sell = sum(kf.sell_vol for kf in self._keys.values())
        unc = sum(kf.unclassified_vol for kf in self._keys.values())
        total = buy + sell + unc
        lo, hi = self._coverage
        if self._minutes:
            minute = _minute_dict(self._minutes[-1])
        else:
            minute = {"time": None, "call_delta_usd": 0, "put_delta_usd": 0, "hedge_pressure_usd": 0}
        return {
            "coverage_start": self.coverage_start,
            "coverage": {"lo": _strike(lo), "hi": _strike(hi)},
            "hedge_pressure_15m_usd": _usd(window(15)),
            "hedge_pressure_30m_usd": _usd(window(30)),
            "hedge_pressure_day_usd": _usd(day_call + day_put),
            "call_delta_day_usd": _usd(day_call),
            "put_delta_day_usd": _usd(day_put),
            "classified_pct": round(100.0 * (buy + sell) / total, 1) if total else 0.0,
            "minute": minute,
        }


class VolumeHistory:
    """Total call + put volume per strike, one snapshot per minute, for the "volume added lately" figure.

    A later record in the same minute replaces that minute's snapshot. A new expiration clears the history.
    """

    def __init__(self, maxlen: int = VOLUME_HISTORY_MINUTES):
        self._snaps: deque = deque(maxlen=maxlen)     # (minute start, {strike: total volume})
        self._expiration: Optional[str] = None

    def record(self, expiration: str, stamp: str, options: List[OptionData]) -> None:
        if expiration != self._expiration:
            self._snaps.clear()
            self._expiration = expiration
        minute = datetime.fromisoformat(stamp).replace(second=0, microsecond=0)
        totals: Dict[float, int] = {}
        for o in options:
            k = float(o.strike)
            totals[k] = totals.get(k, 0) + int(o.volume or 0)
        if self._snaps and self._snaps[-1][0] == minute:
            self._snaps[-1] = (minute, totals)
        else:
            self._snaps.append((minute, totals))

    def added(self, strike: float, minutes: int) -> Optional[int]:
        """Volume added at ``strike`` since the newest snapshot at or before ``minutes`` ago; None when no such
        baseline exists or the strike is not in the newest snapshot."""
        if not self._snaps:
            return None
        latest_ts, latest = self._snaps[-1]
        cutoff = latest_ts - timedelta(minutes=minutes)
        base = None
        for ts, totals in self._snaps:
            if ts <= cutoff:
                base = totals
        k = float(strike)
        if base is None or k not in latest or k not in base:
            return None
        return max(0, latest[k] - base[k])


def hot_strikes(options: Iterable[OptionData], vol_recent: Dict[float, Optional[int]],
                vol_oi_min: float = HOT_VOL_OI, min_vol: int = HOT_MIN_VOL,
                top_n: int = HOT_TOP_N) -> Dict[float, dict]:
    """``{strike: {"vol_oi", "hot", "vol_15m"}}`` for every strike with volume.

    ``vol_oi`` is the strike's call + put volume over its call + put open interest (floor 1). A strike is hot
    when vol_oi >= ``vol_oi_min``, its volume >= ``min_vol`` and it is among the ``top_n`` strikes by total
    volume, ranked over all strikes. Returns {} while the book's total open interest is 0 (not reported yet).
    """
    rows = list(options)
    if sum(int(o.open_interest or 0) for o in rows) <= 0:
        return {}
    agg: Dict[float, List[int]] = {}                  # strike -> [volume, open interest]
    for o in rows:
        a = agg.setdefault(float(o.strike), [0, 0])
        a[0] += int(o.volume or 0)
        a[1] += int(o.open_interest or 0)
    ranked = sorted((k for k, (v, _) in agg.items() if v > 0), key=lambda k: (-agg[k][0], k))
    top = set(ranked[:top_n])
    out: Dict[float, dict] = {}
    for k in ranked:
        vol, oi = agg[k]
        vol_oi = round(vol / max(oi, 1), 2)
        out[k] = {"vol_oi": vol_oi, "hot": vol_oi >= vol_oi_min and vol >= min_vol and k in top,
                  "vol_15m": vol_recent.get(k)}
    return out


def flow_gex_by_strike(net_calls: Dict[float, int], net_puts: Dict[float, int],
                       gammas: Tuple[Dict[float, float], Dict[float, float]], spot: float) -> Dict[float, float]:
    """Flow GEX per strike: -(net_call x gamma_call + net_put x gamma_put) x spot^2.

    Customers buying calls (net > 0) leave dealers short gamma there, so the value is negative. A strike is
    included when at least one of its sides has both a net volume and a gamma.
    """
    gamma_c, gamma_p = gammas
    out: Dict[float, float] = {}
    for k in set(net_calls) | set(net_puts):
        parts = []
        if k in net_calls and k in gamma_c:
            parts.append(net_calls[k] * gamma_c[k])
        if k in net_puts and k in gamma_p:
            parts.append(net_puts[k] * gamma_p[k])
        if parts:
            out[k] = -sum(parts) * float(spot) * float(spot)
    return out


def annotate_gex_bars(bars: List[dict], tracker: FlowTracker, hot: Dict[float, dict],
                      fgex: Dict[float, float]) -> None:
    """Add the flow keys to each gex bar in place; a key appears only when its source has that strike."""
    keys = tracker.keys()
    for bar in bars:
        k = float(bar["strike"])
        call = keys.get(norm_key(k, "C"))
        put = keys.get(norm_key(k, "P"))
        if call is not None:
            bar["call_buy"], bar["call_sell"] = call.buy_vol, call.sell_vol
            bar["call_net_delta_usd"] = _usd(call.net_delta_usd)
        if put is not None:
            bar["put_buy"], bar["put_sell"] = put.buy_vol, put.sell_vol
            bar["put_net_delta_usd"] = _usd(put.net_delta_usd)
        if k in fgex:
            bar["flow_gex"] = _usd(fgex[k])
        if k in hot:
            bar["vol_oi"] = hot[k]["vol_oi"]
            bar["hot"] = hot[k]["hot"]
            bar["vol_15m"] = hot[k]["vol_15m"]
