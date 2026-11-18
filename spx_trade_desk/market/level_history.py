"""Intraday history of the four GEX levels (call wall, put wall, gamma flip, max pain).

The price chart draws each level as a step line that moves when GEX publishes a different value,
so a dashboard that opens (or reconnects) mid-session needs the earlier values too. The publisher
records one point per change; the point carries all four levels, because they are always published
together. Times are ET wall-clock to the minute, the resolution of the chart's bars: a second change
within the same minute replaces the first. A new expiration (the 0DTE roll) starts a new history.
Loop thread only.
"""
from collections import deque
from typing import List, Optional

LEVEL_KEYS = ("call_wall", "put_wall", "gamma_flip", "max_pain")
MAX_POINTS = 2000


def _level(gex: dict, key: str) -> Optional[float]:
    v = gex.get(key)
    return None if v is None else round(float(v), 2)


class LevelHistory:
    def __init__(self, maxlen: int = MAX_POINTS):
        self._points: deque = deque(maxlen=maxlen)
        self._expiration = ""

    def record(self, gex: Optional[dict]) -> bool:
        """Store the levels of a published GEX dict; True when the history changed."""
        stamp = (gex or {}).get("timestamp")
        if not isinstance(stamp, str) or len(stamp) < 16:
            return False
        expiration = gex.get("expiration") or ""
        if expiration != self._expiration:
            self._points.clear()
            self._expiration = expiration
        point = {"time": stamp[:16]}
        for key in LEVEL_KEYS:
            point[key] = _level(gex, key)
        last = self._points[-1] if self._points else None
        if last is not None and all(last[k] == point[k] for k in LEVEL_KEYS):
            return False
        if last is not None and last["time"] == point["time"]:
            self._points[-1] = point
        else:
            self._points.append(point)
        return True

    def points(self) -> List[dict]:
        return list(self._points)
