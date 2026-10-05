"""The merged 0DTE quote book.

The chain stream (near-money, live) and the wing poller (everything else, refreshed
every cycle) both write here; the publisher builds GEX and the chain payload from it
and the recorder snapshots it. Each row remembers who wrote it and when, so consumers
can tell a live quote from a stale one.
"""
from dataclasses import dataclass, fields, replace
from typing import Dict, Iterable, List

from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.qualification import Key, norm_key

_FIELDS = [f.name for f in fields(OptionData) if f.name not in ("strike", "right")]
# Cumulative counters: a 0 means "not reported in this update" (OI often arrives late),
# never "dropped to zero", so it does not overwrite a positive value.
_COUNTERS = ("open_interest", "volume")


@dataclass
class _Entry:
    option: OptionData
    source: str
    ts: float


def _merge(old: OptionData, new: OptionData) -> OptionData:
    out = replace(old)
    for name in _FIELDS:
        v = getattr(new, name)
        if v is None:
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
            merged = (replace(o, strike=k[0], right=k[1]) if old is None
                      else _merge(old.option, o))
            self._rows[k] = _Entry(merged, source, now)

    def options(self) -> List[OptionData]:
        return [replace(self._rows[k].option) for k in sorted(self._rows)]

    def ages(self, now: float) -> Dict[Key, float]:
        return {k: max(0.0, now - e.ts) for k, e in self._rows.items()}

    def sources(self) -> Dict[Key, str]:
        return {k: e.source for k, e in self._rows.items()}
