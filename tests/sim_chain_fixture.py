"""Invented 0DTE chain days for the SP2 library / pricing tests (never real captures).

Truth: IV(K) = ATM(tau) * r(z, tau), z on the calendar clock, ATM(tau) = ATM_OPEN * g(tau),
put/call half-spread hs(mid). Days are written in the SP1 recorder format, one gzip
member per record. Dates are in 2030 so they can never be mistaken for a capture.
"""
import gzip
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from spx_trade_desk.market.hours import ET
from spx_trade_desk.sim.clock import t_cal
from spx_trade_desk.sim.pricing import bsm_put, bsm_put_delta

ATM_OPEN = 0.12          # ATM IV at 09:45, calendar decimal
VIX1D_PREV = 13.0        # VIX1D prior close, percent
R = 0.043


def _blend(tau: float) -> float:
    """0 before tau = 120, 1 from tau = 30: the last hours flatten the put skew."""
    return min(1.0, max(0.0, (120.0 - tau) / 90.0))


def r_true(z, tau):
    z = np.asarray(z, dtype=float)
    early = np.where(z <= 0, 1 - 0.12 * z + 0.03 * z * z, 1 - 0.06 * z + 0.02 * z * z)
    late = np.where(z <= 0, 1 - 0.06 * z + 0.015 * z * z, 1 - 0.03 * z + 0.01 * z * z)
    return early + _blend(float(tau)) * (late - early)


def g_true(tau) -> float:
    return 1.0 + 0.5 * max(0.0, 120.0 - float(tau)) / 120.0


def hs_true(mid):
    return np.maximum(0.025, 0.02 * np.asarray(mid, dtype=float) + 0.02)


def chain_rows(spot, tau, strikes, atm=None, stale=()):
    """Put and call rows for one record from the truth (calendar clock)."""
    atm = ATM_OPEN * g_true(tau) if atm is None else atm
    T = float(t_cal(tau))
    k = np.asarray(strikes, dtype=float)
    z = np.log(k / spot) / (atm * math.sqrt(T))
    iv = atm * r_true(z, tau)
    put = bsm_put(spot, k, T, R, iv)
    call = put + spot - k * math.exp(-R * T)
    dput = bsm_put_delta(spot, k, T, R, iv)
    rows = []
    for i, kk in enumerate(k):
        for right, mid, delta in (("P", put[i], dput[i]), ("C", call[i], dput[i] + 1.0)):
            h = float(hs_true(mid))
            bid = float(mid) - h
            rows.append({"k": float(kk), "r": right, "bid": bid if bid >= 0.05 else None,
                         "ask": max(float(mid) + h, 0.05), "last": None, "iv": float(iv[i]),
                         "delta": float(delta), "gamma": None, "oi": 100, "vol": 10,
                         "age_s": 999.0 if float(kk) in stale else 0.0, "src": "poll"})
    return rows


def write_record(path: Path, rec: dict) -> None:
    """Append one record as its own gzip member, exactly like the SP1 recorder."""
    with gzip.open(path, "at", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def write_day(root, day: str = "20300304", *, seed: int = 0, spot0: float = 6000.0,
              step_min: int = 5, start: str = "09:31", close_min: int = 16 * 60,
              expiry: str = None, stale=()) -> Path:
    """One invented 0DTE day (records every ``step_min`` from ``start`` to the close)."""
    rng = np.random.default_rng(seed)
    base = datetime.strptime(day, "%Y%m%d").replace(tzinfo=ET)
    h, m = (int(x) for x in start.split(":"))
    t = base.replace(hour=h, minute=m)
    close = base + timedelta(minutes=close_min)
    strikes = np.arange(round(spot0 * 0.97 / 5) * 5, spot0 * 1.03 + 5, 5.0)
    path = Path(root) / f"{day}.jsonl.gz"
    spot = spot0
    while t < close:
        tau = (close - t).total_seconds() / 60.0
        write_record(path, {"v": 1, "ts": t.isoformat(timespec="seconds"),
                            "expiry": expiry or day, "spot": spot, "vix": 15.0,
                            "vix1d": VIX1D_PREV, "source": "standalone",
                            "rows": chain_rows(spot, tau, strikes, stale=stale)})
        spot *= math.exp(rng.normal(0.0, 0.0008))
        t += timedelta(minutes=step_min)
    return path


TRUTH_MIDS = (0.25, 0.75, 1.5, 2.5, 4.0, 7.5, 15.0, 30.0)   # one representative mid per MID bucket


def truth_tables(n_days: int = 20, sweeps: int = 100):
    """The fixture's truth as pricing tables (f at the bucket centres, exact g, hs at TRUTH_MIDS)."""
    from spx_trade_desk.sim.pricing_tables import G_TAU, N_TAU, TAU_CENTERS, Z_GRID, PricingTables
    f = np.vstack([r_true(Z_GRID, c) for c in TAU_CENTERS])
    g = np.array([g_true(t) for t in G_TAU])
    hs = np.tile(hs_true(np.array(TRUTH_MIDS)), (N_TAU, 1))
    return PricingTables(f=f, f_sweeps=np.full(N_TAU, sweeps), g=g, hs=hs,
                         atm_vix1d_ratio=ATM_OPEN / (VIX1D_PREV / 100.0), n_days=n_days)
