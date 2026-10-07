"""The merged 0DTE quote book.

The chain stream (near-money, live) and the wing poller (everything else, refreshed
every cycle) both write here; the publisher builds GEX and the chain payload from it
and the recorder snapshots it. Each row remembers who wrote it and when, so consumers
can tell a live quote from a stale one.

Merge rules: a field an update does not carry (None) never overwrites a present one;
bid_size/ask_size only follow a bid/ask carried by the same update; and an update with
no quote data (bid, ask, last, delta, gamma, implied_vol) does not restamp the age or
source of a row that already exists.

The stream is the authority for the keys it holds: its update carries the whole quote
state, so a bid or ask it reports as absent (IB's -1) clears the book's value (and that
side's size). Poll rows keep the "None never overwrites" rule.
"""
from dataclasses import dataclass, fields, replace
from typing import Dict, Iterable, List, Optional, Tuple

from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.qualification import Key, norm_key

_FIELDS = [f.name for f in fields(OptionData) if f.name not in ("strike", "right")]
# Cumulative counters: a 0 means "not reported in this update" (OI often arrives late),
# never "dropped to zero", so it does not overwrite a positive value.
_COUNTERS = ("open_interest", "volume")
# A size is meaningful only alongside its own price (the size default is 0, not None).
_SIZE_OF = {"bid_size": "bid", "ask_size": "ask"}
_QUOTE_FIELDS = ("bid", "ask", "last", "delta", "gamma", "implied_vol")


@dataclass
class _Entry:
    option: OptionData
    source: str
    ts: float


_QUOTE_SIDE = ("bid", "ask", "bid_size", "ask_size")


def _merge(old: OptionData, new: OptionData, authoritative: bool = False) -> OptionData:
    out = replace(old)
    for name in _FIELDS:
        v = getattr(new, name)
        if authoritative and name in _QUOTE_SIDE:
            setattr(out, name, v)
            continue
        if v is None:
            continue
        if name in _SIZE_OF and getattr(new, _SIZE_OF[name]) is None:
            continue
        if name in _COUNTERS and not v and getattr(old, name):
            continue
        setattr(out, name, v)
    return out


class QuoteBook:
    def __init__(self):
        self.expiry = ""
        self._rows: Dict[Key, _Entry] = {}

    def reset(self, expiry: str) -> None:
        self.expiry = expiry
        self._rows.clear()

    def __len__(self) -> int:
        return len(self._rows)

    def update(self, options: Iterable[OptionData], source: str, now: float) -> None:
        for o in options:
            k = norm_key(o.strike, o.right)
            old = self._rows.get(k)
            if old is None:
                self._rows[k] = _Entry(replace(o, strike=k[0], right=k[1]), source, now)
                continue
            old.option = _merge(old.option, o, authoritative=(source == "stream"))
            if any(getattr(o, f) is not None for f in _QUOTE_FIELDS):
                old.source, old.ts = source, now

    def options(self) -> List[OptionData]:
        return [replace(self._rows[k].option) for k in sorted(self._rows)]

    def ages(self, now: float) -> Dict[Key, float]:
        return {k: max(0.0, now - e.ts) for k, e in self._rows.items()}

    def get(self, key: Key, now: float) -> Optional[Tuple[OptionData, float]]:
        """A copy of one row and its age in seconds, or None."""
        e = self._rows.get(key)
        return None if e is None else (replace(e.option), max(0.0, now - e.ts))

    def sources(self) -> Dict[Key, str]:
        return {k: e.source for k, e in self._rows.items()}
