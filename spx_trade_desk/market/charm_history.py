"""Intraday history of dealer charm exposure per strike, for the price chart's charm heatmap.

Charm is how fast an option's delta decays as time passes (``gex._compute_charm_fd``, a 15-minute
finite difference). The chart colors each strike of each minute by the dealers' delta drift, under the
same positioning assumption as GEX (dealers long calls, short puts):

    exposure = 100 x (call_oi x call_charm - put_oi x put_charm) x 15 min x spot

in dollars of delta per 15 minutes. Positive is *supportive*: the decay leaves dealers over-hedged and
they buy the underlying back as time passes. Negative is *suppressive*: they sell. A frame holds the
strikes within ``CHARM_BAND`` of spot and the values in thousands of dollars (ints keep the snapshot
small). One frame per ET minute: a later publish in the same minute replaces it, and a new expiration
(the 0DTE roll) starts a new history. Loop thread only.
"""
from collections import deque
from typing import Dict, List, Optional

from spx_trade_desk.market.gex import GEXResult

DT15_YEARS = 15.0 / (390.0 * 252.0)       # the finite-difference step charm is expressed per
MULTIPLIER = 100.0                         # SPX option multiplier
CHARM_BAND = 0.03                          # strikes within +-3% of spot are kept
MAX_FRAMES = 1500


def charm_exposure(result: GEXResult) -> Dict[float, float]:
    """Dealer charm exposure per strike in dollars of delta per 15 minutes (+ supportive, - suppressive)."""
    out: Dict[float, float] = {}
    for strike in result.strikes:
        call_charm = result.call_charm_by_strike.get(strike)
        put_charm = result.put_charm_by_strike.get(strike)
        if call_charm is None and put_charm is None:
            continue
        calls = (call_charm or 0.0) * result.call_oi_by_strike.get(strike, 0)
        puts = (put_charm or 0.0) * result.put_oi_by_strike.get(strike, 0)
        out[strike] = MULTIPLIER * (calls - puts) * DT15_YEARS * result.spot_price
    return out


class CharmHistory:
    def __init__(self, maxlen: int = MAX_FRAMES, band: float = CHARM_BAND):
        self._frames: deque = deque(maxlen=maxlen)
        self._band = band
        self._expiration = ""

    def record(self, result: Optional[GEXResult]) -> bool:
        """Store a frame for a computed GEX; True when the history changed."""
        stamp = getattr(result, "timestamp", None)
        if result is None or result.spot_price <= 0 or not isinstance(stamp, str) or len(stamp) < 16:
            return False
        if result.expiration != self._expiration:
            self._frames.clear()
            self._expiration = result.expiration
        lo, hi = result.spot_price * (1 - self._band), result.spot_price * (1 + self._band)
        exposure = charm_exposure(result)
        strikes = [k for k in sorted(exposure) if lo <= k <= hi]
        if not strikes:
            return False
        frame = {"time": stamp[:16], "strikes": [int(k) if k == int(k) else k for k in strikes],   # 7300, not 7300.0
                 "values": [int(round(exposure[k] / 1000.0)) for k in strikes]}
        if self._frames and self._frames[-1]["time"] == frame["time"]:
            self._frames[-1] = frame
        else:
            self._frames.append(frame)
        return True

    def frames(self) -> List[dict]:
        return list(self._frames)

    def latest(self) -> Optional[dict]:
        return self._frames[-1] if self._frames else None
