"""
Profile levels from the dealer gamma profile: zero gamma, regime, gamma nodes and voids.

Everything here is computed from ``DealerPositions`` (BSM gamma, sticky-strike IV) through
``market/dealer_model.py``, so the nodes, the curve and zero gamma agree with each other.
Numpy only (no scipy). Peak and prominence follow scipy's conventions, implemented inline.
"""

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from spx_trade_desk.market.dealer_model import (
    DealerPositions,
    dealer_book,
    gamma_curve,
    hedge_curve,
    hedge_flow_usd,
    strike_gex,
)
from spx_trade_desk.market.gex import GEXResult

PROFILE_BAND = 0.02          # the curve sent to the browser: spot +/- 2%
ZERO_GAMMA_BAND = 0.05       # zero gamma is searched on spot +/- 5%
PROFILE_STEP = 1.0           # search grid step, in index points
PROFILE_SEND_STEP = 2.5      # sent curve step, in index points
NODES_PER_SIDE = 3
NODE_RANGE_EM = 2.0          # node search band: spot +/- NODE_RANGE_EM * expected move (when known)
NODE_MIN_PROMINENCE = 0.15   # fraction of the largest |GEX| inside the band
VOID_FRAC = 0.10             # a void strike has |GEX| below this fraction of the band's largest |GEX|
VOID_MIN_STRIKES = 2


@dataclass
class ProfileLevels:
    zero_gamma: Optional[float]
    zero_gamma_all: List[float]
    gamma_at_spot: float
    regime: str
    profile_from: float
    profile_step: float
    profile_values: List[float]
    hedge_values: List[float]
    nodes: List[dict]
    voids: List[dict]
    strike_gex: Dict[float, float] = field(default_factory=dict)


def zero_crossings(grid: np.ndarray, values: np.ndarray) -> List[float]:
    """Sign changes of ``values`` on ``grid``, linearly interpolated, ascending.

    Zeros carry no sign: a crossing is a sign change between consecutive nonzero values.
    """
    grid = np.asarray(grid, dtype=float)
    values = np.asarray(values, dtype=float)
    idx = np.flatnonzero(values != 0.0)
    if idx.size < 2:
        return []
    g, v = grid[idx], values[idx]
    s = np.sign(v)
    out: List[float] = []
    for i in np.flatnonzero(s[:-1] != s[1:]):
        x0, x1, y0, y1 = g[i], g[i + 1], v[i], v[i + 1]
        out.append(float(x0 + (x1 - x0) * (0.0 - y0) / (y1 - y0)))
    return sorted(out)


def _peaks(a: np.ndarray) -> List[int]:
    """Indices of local maxima (edges excluded; a plateau counts at its first index)."""
    n = len(a)
    return [i for i in range(1, n - 1) if a[i] > a[i - 1] and a[i] >= a[i + 1]]


def _prominence(a: np.ndarray, i: int) -> float:
    n = len(a)
    j = i - 1
    while j >= 0 and a[j] <= a[i]:
        j -= 1
    left_min = float(a[max(j, 0):i + 1].min())
    k = i + 1
    while k < n and a[k] <= a[i]:
        k += 1
    right_min = float(a[i:min(k, n - 1) + 1].min())
    return float(a[i]) - max(left_min, right_min)


def find_nodes(strike_gex: Dict[float, float], spot: float, lo: float, hi: float,
               hedge_usd: Callable[[float], float], per_side: int = NODES_PER_SIDE,
               min_prominence: float = NODE_MIN_PROMINENCE) -> List[dict]:
    """Large |GEX| peaks inside [lo, hi], ranked per side and labelled R1.. above spot, S1.. below.

    Positive peaks are ``pin`` nodes, negative peaks ``accel``. Output is R1..R3 then S1..S3.
    """
    if not strike_gex:
        return []
    ks = sorted(strike_gex)
    gex = np.array([strike_gex[k] for k in ks], dtype=float)
    a = np.abs(gex)
    in_band = [i for i, k in enumerate(ks) if lo <= k <= hi]
    if not in_band:
        return []
    band_max = float(max(a[i] for i in in_band))
    cutoff = min_prominence * band_max
    found = []
    for i in _peaks(a):
        k = ks[i]
        if not (lo <= k <= hi):
            continue
        if _prominence(a, i) < cutoff:
            continue
        found.append({"strike": float(k), "gex": float(gex[i]), "kind": "pin" if gex[i] > 0 else "accel",
                      "side": "above" if k >= spot else "below"})
    out: List[dict] = []
    for side, prefix in (("above", "R"), ("below", "S")):
        by_size = sorted((n for n in found if n["side"] == side), key=lambda n: -abs(n["gex"]))[:per_side]
        for rank, n in enumerate(by_size, start=1):
            n["rank"] = rank
        by_distance = sorted(by_size, key=lambda n: abs(n["strike"] - spot))
        for label_no, n in enumerate(by_distance, start=1):
            out.append({"label": f"{prefix}{label_no}", "strike": n["strike"], "kind": n["kind"],
                        "gex": n["gex"], "side": side, "hedge_usd": float(hedge_usd(n["strike"])),
                        "rank": n["rank"]})
    return out


def find_voids(strike_gex: Dict[float, float], lo: float, hi: float,
               frac: float = VOID_FRAC, min_strikes: int = VOID_MIN_STRIKES) -> List[dict]:
    """Runs of at least ``min_strikes`` consecutive strikes inside [lo, hi] whose |GEX| is quiet."""
    ks = [k for k in sorted(strike_gex) if lo <= k <= hi]
    if not ks:
        return []
    band_max = max(abs(strike_gex[k]) for k in ks)
    quiet = [abs(strike_gex[k]) < frac * band_max for k in ks]
    out: List[dict] = []
    run_start = None
    for i, q in enumerate(quiet + [False]):
        if q and run_start is None:
            run_start = i
        elif not q and run_start is not None:
            if i - run_start >= min_strikes:
                out.append({"lo": float(ks[run_start]), "hi": float(ks[i - 1])})
            run_start = None
    return out


def profile_levels(result: GEXResult, pos: DealerPositions, T: float, r: float,
                   em: Optional[float]) -> Optional[ProfileLevels]:
    """Zero gamma, regime, sent curve, nodes and voids; None when T <= 0 or no position has an IV."""
    if T <= 0:
        return None
    book = dealer_book(pos, result)
    if book is None:
        return None
    spot = float(result.spot_price)

    search = np.arange(math.floor(spot * (1.0 - ZERO_GAMMA_BAND)), spot * (1.0 + ZERO_GAMMA_BAND) + 1e-9,
                       PROFILE_STEP)
    search_gamma = gamma_curve(book, search, T, r)
    crossings = zero_crossings(search, search_gamma)
    zero_gamma = round(min(crossings, key=lambda z: abs(z - spot)), 2) if crossings else None

    gamma_at_spot = float(gamma_curve(book, np.array([spot]), T, r)[0])
    regime = "long_gamma" if gamma_at_spot > 0 else "short_gamma"

    profile_from = math.floor(spot * (1.0 - PROFILE_BAND) / PROFILE_SEND_STEP) * PROFILE_SEND_STEP
    hi = spot * (1.0 + PROFILE_BAND)
    n = math.ceil((hi - profile_from) / PROFILE_SEND_STEP) + 1
    sent = profile_from + PROFILE_SEND_STEP * np.arange(n)
    profile_values = gamma_curve(book, sent, T, r).tolist()
    hedge_values = hedge_curve(book, spot, sent, T, r).tolist()

    sgex = strike_gex(book, spot, T, r)
    if em and em > 0:
        lo, nhi = spot - NODE_RANGE_EM * em, spot + NODE_RANGE_EM * em
    else:
        lo, nhi = spot * (1.0 - PROFILE_BAND), spot * (1.0 + PROFILE_BAND)
    nodes = find_nodes(sgex, spot, lo, nhi, lambda x: hedge_flow_usd(book, spot, x, T, r))
    voids = find_voids(sgex, lo, nhi)

    return ProfileLevels(
        zero_gamma=zero_gamma,
        zero_gamma_all=[round(z, 2) for z in crossings],
        gamma_at_spot=gamma_at_spot,
        regime=regime,
        profile_from=float(profile_from),
        profile_step=PROFILE_SEND_STEP,
        profile_values=profile_values,
        hedge_values=hedge_values,
        nodes=nodes,
        voids=voids,
        strike_gex=sgex,
    )


def resolve_gamma_flip(result: GEXResult, levels: Optional[ProfileLevels]) -> Tuple[Optional[float], str]:
    """Zero gamma when the profile was computed (even if it has no crossing); else the cumulative flip."""
    if levels is not None:
        return levels.zero_gamma, "zero_gamma"
    return result.gamma_flip, "cumulative"
