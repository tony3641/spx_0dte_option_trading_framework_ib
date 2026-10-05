"""Chain recorder: appends the merged 0DTE quote book to a daily gzipped JSONL file.

The files under CHAIN_LIBRARY_DIR are the raw material for the simulator's smile library
(SP2). IV is stored exactly as IB reports it (decimal, calendar clock); unit handling
belongs to the consumer. One gzip member per record, so a crash mid-day leaves a valid
file. The recorder never raises into the loop that calls it.
"""
import gzip
import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from spx_trade_desk.ib.connection import index_price
from spx_trade_desk.market.hours import is_short_trading_day
from spx_trade_desk.market.qualification import norm_key

logger = logging.getLogger(__name__)

RECORD_START = time(9, 31)
FAST_S, SLOW_S = 60, 120


def session_close(d: date) -> time:
    return time(13, 0) if is_short_trading_day(d) else time(16, 0)


def record_interval(t: datetime) -> Optional[int]:
    """Seconds between records at ET time ``t``; None outside the recording window.

    Fast in the first half hour (overnight premium burns off) and in the last hour before
    the close (T -> 0), slow in between.
    """
    close = session_close(t.date())
    tt = t.time()
    if tt < RECORD_START or tt >= close:
        return None
    last_hour = (datetime.combine(t.date(), close) - timedelta(hours=1)).time()
    return FAST_S if (tt < time(10, 0) or tt >= last_hour) else SLOW_S


def build_record(book, expiry, spot, vix, vix1d, source, t: datetime, now_mono: float) -> dict:
    ages, srcs = book.ages(now_mono), book.sources()
    rows = []
    for o in book.options():
        k = norm_key(o.strike, o.right)
        rows.append({"k": o.strike, "r": o.right, "bid": o.bid, "ask": o.ask, "last": o.last,
                     "iv": o.implied_vol, "delta": o.delta, "gamma": o.gamma,
                     "oi": o.open_interest, "vol": o.volume,
                     "age_s": round(ages[k], 1), "src": srcs[k]})
    return {"v": 1, "ts": t.isoformat(timespec="seconds"), "expiry": expiry, "spot": spot,
            "vix": vix, "vix1d": vix1d, "source": source, "rows": rows}


class ChainRecorder:
    def __init__(self, root, source: str = "dashboard"):
        self.root = Path(root)
        self.source = source
        self.heartbeat = self.root / f".heartbeat-{source}"
        self._last: Optional[datetime] = None

    def due(self, t: datetime) -> bool:
        iv = record_interval(t)
        return iv is not None and (self._last is None
                                   or (t - self._last).total_seconds() >= iv)

    def write(self, record: dict, t: datetime) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{t:%Y%m%d}.jsonl.gz"
        with gzip.open(path, "at", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.heartbeat.touch()
        self._last = t
        return path

    def maybe_record(self, state, t: datetime, now_mono: float) -> bool:
        if (not self.due(t) or state.expiration != f"{t:%Y%m%d}"
                or len(state.quote_book) == 0):
            return False
        try:
            rec = build_record(state.quote_book, state.expiration, state.spx_price,
                               index_price(getattr(state, "vix_stream", None)),
                               index_price(getattr(state, "vix1d_stream", None)),
                               self.source, t, now_mono)
            self.write(rec, t)
            return True
        except Exception as e:
            logger.warning(f"Chain recorder write failed: {e}")
            return False
