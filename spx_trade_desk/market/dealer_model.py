"""
Dealer model: the dealers' option positions and the vectorised greeks derived from them.

Sign convention (used everywhere in the gamma profile and flow code):
  hedge flow USD + = dealers buy the underlying, - = dealers sell.
  Customer flow is the other side of the dealers.

``DealerPositions`` is the single input every derived number uses (gamma curve, delta and hedge
curves, per-strike GEX, levels). Calls are dealer-long (+) and puts dealer-short (-) under the
OI-assumed model, so the dealer view matches ``compute_gex`` (call GEX positive, put GEX negative).

Greeks here are numpy-only (no scipy); the scalar helpers in ``market/gex.py`` remain the reference.
All greek functions expect ``T > 0``; callers handle expired or undefined inputs themselves.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

import numpy as np

from spx_trade_desk.market.gex import GEXResult

MULTIPLIER = 100.0
IV_FLOOR = 1e-4


@dataclass
class DealerPositions:
    """Contracts per strike from the dealers' side: + long, - short. ``model`` is "oi_assumed" or "flow_blend"."""
    calls: Dict[float, float]
    puts: Dict[float, float]
    model: str


def positions_from_oi(result: GEXResult) -> DealerPositions:
    """Open-interest model: dealers are long the calls and short the puts at each strike (zero OI omitted)."""
    calls = {float(k): float(oi) for k, oi in result.call_oi_by_strike.items() if oi > 0}
    puts = {float(k): -float(oi) for k, oi in result.put_oi_by_strike.items() if oi > 0}
    return DealerPositions(calls=calls, puts=puts, model="oi_assumed")


def fill_ivs(known: Dict[float, float], strikes: Iterable[float]) -> Dict[float, float]:
    """IV at each strike: linear between the nearest known strikes, flat beyond the ends; {} if nothing is known."""
    if not known:
        return {}
    ks = sorted(known)
    kx = np.array(ks, dtype=float)
    ky = np.array([known[k] for k in ks], dtype=float)
    return {float(k): float(np.interp(k, kx, ky)) for k in strikes}


@dataclass
class DealerBook:
    """One row per (strike, right) with a nonzero position and an IV."""
    K: np.ndarray
    iv: np.ndarray
    pos: np.ndarray
    is_call: np.ndarray


def dealer_book(pos: DealerPositions, result: GEXResult) -> Optional[DealerBook]:
    """Attach sticky-strike IVs to the positions; None when no position has an IV."""
    K, iv, p, is_call = [], [], [], []
    for positions, ivmap, calls in ((pos.calls, result.call_iv_by_strike, True),
                                    (pos.puts, result.put_iv_by_strike, False)):
        active = [k for k, v in positions.items() if v != 0]
        filled = fill_ivs(ivmap, active)
        for k in active:
            if k in filled and filled[k] > 0:
                K.append(k)
                iv.append(filled[k])
                p.append(positions[k])
                is_call.append(calls)
    if not K:
        return None
    return DealerBook(K=np.array(K, dtype=float), iv=np.array(iv, dtype=float),
                      pos=np.array(p, dtype=float), is_call=np.array(is_call, dtype=bool))


def norm_cdf(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF via the Abramowitz & Stegun 7.1.26 erf approximation (|error| < 1.5e-7)."""
    x = np.asarray(x, dtype=float)
    z = x / np.sqrt(2.0)
    a = np.abs(z)
    t = 1.0 / (1.0 + 0.3275911 * a)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = np.sign(z) * (1.0 - poly * np.exp(-a * a))
    return 0.5 * (1.0 + erf)


def _d1(S, K, T, r, iv):
    return (np.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * np.sqrt(T))


def bsm_gamma(S, K, T, r, iv) -> np.ndarray:
    """BSM gamma; ``S`` shaped (n, 1) against ``K`` / ``iv`` shaped (m,) gives (n, m)."""
    S, K, iv = np.asarray(S, dtype=float), np.asarray(K, dtype=float), np.asarray(iv, dtype=float)
    d1 = _d1(S, K, T, r, iv)
    return np.exp(-0.5 * d1 * d1) / np.sqrt(2.0 * np.pi) / (S * iv * np.sqrt(T))


def bsm_delta(S, K, T, r, iv, is_call) -> np.ndarray:
    """BSM delta: N(d1) for calls, N(d1) - 1 for puts; ``is_call`` shaped (m,) broadcasts over the strikes."""
    S, K, iv = np.asarray(S, dtype=float), np.asarray(K, dtype=float), np.asarray(iv, dtype=float)
    nd1 = norm_cdf(_d1(S, K, T, r, iv))
    return np.where(np.asarray(is_call, dtype=bool), nd1, nd1 - 1.0)


def _iv_array(book: DealerBook, iv_shift: float = 0.0) -> np.ndarray:
    return np.maximum(book.iv + iv_shift, IV_FLOOR)


def gamma_curve(book: DealerBook, spots: np.ndarray, T: float, r: float) -> np.ndarray:
    """G(S') = sum(pos * gamma * S'^2): dealer dollars per 1% move at each spot."""
    spots = np.asarray(spots, dtype=float)
    gamma = bsm_gamma(spots[:, None], book.K, T, r, _iv_array(book))
    return (gamma * book.pos).sum(axis=1) * spots * spots


def dealer_delta(book: DealerBook, spots: np.ndarray, T: float, r: float, iv_shift: float = 0.0) -> np.ndarray:
    """D(S') = sum(pos * delta * 100): dealer share-equivalent delta at each spot."""
    spots = np.asarray(spots, dtype=float)
    delta = bsm_delta(spots[:, None], book.K, T, r, _iv_array(book, iv_shift), book.is_call)
    return (delta * book.pos).sum(axis=1) * MULTIPLIER


def hedge_flow_usd(book: DealerBook, spot: float, target: float, T: float, r: float) -> float:
    """USD the dealers trade to re-hedge when the underlying moves from spot to target: -(D(target) - D(spot)) * target."""
    d = dealer_delta(book, np.array([spot, target]), T, r)
    return float(-(d[1] - d[0]) * target)


def hedge_curve(book: DealerBook, spot: float, targets: np.ndarray, T: float, r: float) -> np.ndarray:
    """hedge_flow_usd for each target, vectorised."""
    targets = np.asarray(targets, dtype=float)
    d_spot = dealer_delta(book, np.array([spot]), T, r)[0]
    d_targets = dealer_delta(book, targets, T, r)
    return -(d_targets - d_spot) * targets


def strike_gex(book: DealerBook, spot: float, T: float, r: float) -> Dict[float, float]:
    """Net GEX per strike at spot (calls and puts at a strike summed), in dollars per 1% move."""
    gamma = bsm_gamma(np.array([[spot]]), book.K, T, r, _iv_array(book))[0]
    contrib = book.pos * gamma * spot * spot
    out: Dict[float, float] = {}
    for k, c in zip(book.K, contrib):
        key = float(k)
        out[key] = out.get(key, 0.0) + float(c)
    return out


def strike_gammas(result: GEXResult, spot: float, T: float, r: float) -> Tuple[Dict[float, float], Dict[float, float]]:
    """BSM gamma per strike at ``spot`` for calls and puts, from each side's filled sticky-strike IVs.

    Returns ``(call_gamma, put_gamma)``; both are empty when T <= 0, spot <= 0 or no side has an IV.
    Used by the flow GEX (``flow.flow_gex_by_strike``), which is independent of the position model.
    """
    if T <= 0 or spot <= 0:
        return {}, {}
    out = []
    for ivmap in (result.call_iv_by_strike, result.put_iv_by_strike):
        strikes = sorted(set(result.strikes) | set(ivmap))
        filled = fill_ivs(ivmap, strikes)
        ks = [k for k in strikes if filled.get(k, 0.0) > 0]
        if not ks:
            out.append({})
            continue
        iv = np.maximum(np.array([filled[k] for k in ks], dtype=float), IV_FLOOR)
        gamma = bsm_gamma(float(spot), np.array(ks, dtype=float), T, r, iv)
        out.append({k: float(g) for k, g in zip(ks, gamma)})
    return out[0], out[1]
