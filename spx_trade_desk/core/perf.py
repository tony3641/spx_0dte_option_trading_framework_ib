"""Timing recorder for the IB layer: one ring buffer per metric plus counters.

Spans are recorded in milliseconds by the IB client, the contract registry, the order path and
the chain loops through the module-level ``perf``. ``GET /api/perf`` and a periodic log line
expose it. It imports nothing from the rest of the package.

Loop thread only: ``PerfRecorder`` is not locked, so call ``record``/``count``/``timer`` from the
asyncio loop thread (never from the ibapi socket thread). The log line shows n / p50 / p95 per
span; ``max`` and ``last`` are in ``snapshot()`` (``/api/perf``) only.
"""
import math
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, Deque, Dict, Iterator, List


class PerfRecorder:
    """Ring buffers per span plus counters. Loop thread only (see the module docstring)."""

    def __init__(self, size: int = 512, clock: Callable[[], float] = time.perf_counter):
        self._size = size
        self._clock = clock
        self._samples: Dict[str, Deque[float]] = {}
        self._counters: Dict[str, int] = {}

    def record(self, name: str, ms: float) -> None:
        buf = self._samples.get(name)
        if buf is None:
            buf = self._samples[name] = deque(maxlen=self._size)
        buf.append(float(ms))

    def count(self, name: str, n: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + n

    def counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        """Record the wall time of the ``with`` block (it may contain ``await``s)."""
        t0 = self._clock()
        try:
            yield
        finally:
            self.record(name, (self._clock() - t0) * 1000.0)

    def reset(self) -> None:
        self._samples.clear()
        self._counters.clear()

    @staticmethod
    def _pct(sorted_vals: List[float], p: float) -> float:
        idx = max(0, min(len(sorted_vals) - 1, math.ceil(p / 100.0 * len(sorted_vals)) - 1))
        return sorted_vals[idx]

    def snapshot(self) -> dict:
        metrics = {}
        for name, buf in self._samples.items():
            if not buf:
                continue
            vals = sorted(buf)
            metrics[name] = {"n": len(vals), "p50": round(self._pct(vals, 50), 2),
                             "p95": round(self._pct(vals, 95), 2), "max": round(vals[-1], 2),
                             "last": round(buf[-1], 2)}
        return {"metrics": metrics, "counters": dict(self._counters)}

    def summary_line(self) -> str:
        snap = self.snapshot()
        parts = [f"{name} n={m['n']} p50={m['p50']:.1f}ms p95={m['p95']:.1f}ms"
                 for name, m in sorted(snap["metrics"].items())]
        parts += [f"{name}={n}" for name, n in sorted(snap["counters"].items())]
        return "perf: " + " | ".join(parts) if parts else ""


perf = PerfRecorder()
