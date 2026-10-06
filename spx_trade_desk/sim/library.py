"""Chain library: reader, per-day extraction and the pricing-model builder (SP2 spec section 6).

Reads the SP1 recorder's daily files (``data/chain_library/YYYYMMDD.jsonl.gz``, one gzip
member per record) and turns each day into the points the pricing tables are built from.
IV stays in IB calendar units; minutes to the close come from the clock module.
"""
import json
import logging
import math
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

import numpy as np

from spx_trade_desk.core.config import CHAIN_QUOTE_MAX_AGE_S
from spx_trade_desk.resources import CHAIN_LIBRARY_DIR
from spx_trade_desk.sim.clock import RTH_MINUTES, minutes_to_close, session_close_time, t_cal

logger = logging.getLogger(__name__)

MODEL_NAME = "pricing_model.json"
MODEL_PATH = CHAIN_LIBRARY_DIR / MODEL_NAME
CACHE_DIRNAME = ".cache"
EXTRACT_VERSION = 1

# Capture hygiene (the thresholds the old smile capture used): below a 0.10 bid the quote
# is tick-pinned; outside this |delta| band the IV carries no vega information.
MIN_BID = 0.10
MIN_ABS_DELTA = 0.005
MAX_ABS_DELTA = 0.75
ATM_MAX_GAP = 10.0      # bracketing strikes further apart than this: no ATM for the record


def iter_records(path) -> Iterator[dict]:
    """v1 records from one day file, complete gzip members only.

    The recorder appends one member per record, so a file being written can end in a
    partial member; reading stops there instead of raising.
    """
    data = Path(path).read_bytes()
    while data:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out = d.decompress(data)
        except zlib.error:
            break
        if not d.eof:
            break
        for line in out.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("v") == 1:
                yield rec
        data = d.unused_data


def fresh(row: dict, max_age: float = CHAIN_QUOTE_MAX_AGE_S) -> bool:
    age = row.get("age_s")
    return age is None or age <= max_age


def record_atm(rows, spot: float, max_age: float = CHAIN_QUOTE_MAX_AGE_S) -> Optional[float]:
    """ATM IV at ``spot``: per-strike IV (put and call averaged), linear between the nearest
    strikes with an IV on each side. None when a side is missing or the gap exceeds
    ATM_MAX_GAP, so a one-sided book never yields a level."""
    by_k: Dict[float, List[float]] = {}
    for r in rows:
        iv, k = r.get("iv"), r.get("k")
        if k is None or iv is None or not (iv > 0) or not fresh(r, max_age):
            continue
        by_k.setdefault(float(k), []).append(float(iv))
    lo = [k for k in by_k if k <= spot]
    hi = [k for k in by_k if k >= spot]
    if not lo or not hi:
        return None
    k0, k1 = max(lo), min(hi)
    if k1 - k0 > ATM_MAX_GAP:
        return None
    iv0, iv1 = float(np.mean(by_k[k0])), float(np.mean(by_k[k1]))
    if k1 == k0:
        return iv0
    return iv0 + (spot - k0) / (k1 - k0) * (iv1 - iv0)


def extract_record(rec: dict, day: str):
    """(tau, atm, vix1d, shape points [(z, iv/atm)], spread points [(mid, half)]) or None."""
    if rec.get("expiry") != day:
        return None
    spot = rec.get("spot")
    if not spot or not (spot > 0):
        return None
    try:
        tau = minutes_to_close(datetime.fromisoformat(rec["ts"]))
    except (KeyError, ValueError, TypeError):
        return None
    if not (0.0 < tau <= RTH_MINUTES):
        return None
    spot = float(spot)
    rows = rec.get("rows") or []
    atm = record_atm(rows, spot)
    if atm is None:
        return None
    sw = atm * math.sqrt(float(t_cal(tau)))
    shape, spreads = [], []
    for r in rows:
        k, right = r.get("k"), r.get("r")
        if k is None or not fresh(r):
            continue
        bid, ask, iv, delta = r.get("bid"), r.get("ask"), r.get("iv"), r.get("delta")
        if right == "P" and bid is not None and ask is not None and bid > 0 and ask >= bid:
            spreads.append(((bid + ask) / 2.0, (ask - bid) / 2.0))
        otm = (right == "P" and k < spot) or (right == "C" and k > spot)
        if (otm and iv is not None and iv > 0 and bid is not None and bid >= MIN_BID
                and delta is not None and MIN_ABS_DELTA <= abs(delta) <= MAX_ABS_DELTA):
            shape.append((math.log(k / spot) / sw, iv / atm))
    v1 = rec.get("vix1d")
    return tau, atm, (float(v1) if v1 else float("nan")), shape, spreads


@dataclass
class DayData:
    day: str                   # YYYYMMDD
    session_min: float         # 390, or 210 on a half day
    rec_tau: np.ndarray        # (R,) minutes to the close
    rec_atm: np.ndarray        # (R,) ATM IV, calendar decimal
    rec_vix1d: np.ndarray      # (R,) VIX1D at the record, percent (nan when absent)
    pt_rec: np.ndarray         # (P,) record index of each OTM shape point
    pt_z: np.ndarray           # (P,) z = ln(K/spot) / (ATM * sqrt(T_cal))
    pt_ratio: np.ndarray       # (P,) IV / ATM
    hs_rec: np.ndarray         # (H,) record index of each put spread point
    hs_mid: np.ndarray         # (H,) put mid
    hs_half: np.ndarray        # (H,) put half-spread
    n_skipped: int = 0         # records dropped (wrong expiry, outside the session, no ATM)


_ARRAYS = ("rec_tau", "rec_atm", "rec_vix1d", "pt_rec", "pt_z", "pt_ratio",
           "hs_rec", "hs_mid", "hs_half")


def extract_day(path) -> Optional[DayData]:
    path = Path(path)
    day = path.name[:8]
    close = session_close_time(datetime.strptime(day, "%Y%m%d").date())
    session_min = close.hour * 60.0 + close.minute - 570.0
    taus, atms, v1s = [], [], []
    pr, pz, prt, hr, hm, hh = [], [], [], [], [], []
    skipped = 0
    for rec in iter_records(path):
        got = extract_record(rec, day)
        if got is None:
            skipped += 1
            continue
        i = len(taus)
        tau, atm, v1, shape, spreads = got
        taus.append(tau)
        atms.append(atm)
        v1s.append(v1)
        for z, ratio in shape:
            pr.append(i)
            pz.append(z)
            prt.append(ratio)
        for mid, half in spreads:
            hr.append(i)
            hm.append(mid)
            hh.append(half)
    if not taus:
        return None
    return DayData(day=day, session_min=session_min, rec_tau=np.array(taus, dtype=float),
                   rec_atm=np.array(atms, dtype=float), rec_vix1d=np.array(v1s, dtype=float),
                   pt_rec=np.array(pr, dtype=int), pt_z=np.array(pz, dtype=float),
                   pt_ratio=np.array(prt, dtype=float), hs_rec=np.array(hr, dtype=int),
                   hs_mid=np.array(hm, dtype=float), hs_half=np.array(hh, dtype=float),
                   n_skipped=skipped)


def _cache_key(path: Path) -> np.ndarray:
    st = path.stat()
    return np.array([EXTRACT_VERSION, st.st_size, st.st_mtime_ns], dtype=np.int64)


def load_day(path, cache_dir: Optional[Path] = None) -> Optional[DayData]:
    """Extract one day, reusing ``.cache/YYYYMMDD.npz`` while the file's size and mtime match."""
    path = Path(path)
    cache_dir = Path(cache_dir) if cache_dir else path.parent / CACHE_DIRNAME
    cpath = cache_dir / (path.name[:8] + ".npz")
    key = _cache_key(path)
    if cpath.exists():
        try:
            with np.load(cpath) as z:
                if np.array_equal(z["key"], key):
                    if int(z["empty"]):
                        return None
                    return DayData(day=path.name[:8], session_min=float(z["session_min"]),
                                   n_skipped=int(z["n_skipped"]),
                                   **{a: z[a] for a in _ARRAYS})
        except Exception:
            pass                      # unreadable cache: extract again
    day = extract_day(path)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if day is None:
            np.savez(cpath, key=key, empty=1)
        else:
            np.savez(cpath, key=key, empty=0, session_min=day.session_min,
                     n_skipped=day.n_skipped, **{a: getattr(day, a) for a in _ARRAYS})
    except OSError as e:
        logger.warning(f"chain library cache write failed: {e}")
    return day


def day_files(root) -> List[Path]:
    return sorted(Path(root).glob("[0-9]" * 8 + ".jsonl.gz"))


def load_days(root, exclude: Sequence[str] = ()) -> List[DayData]:
    out = []
    for p in day_files(root):
        if p.name[:8] in exclude:
            continue
        d = load_day(p)
        if d is not None:
            out.append(d)
    return out
