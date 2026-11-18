"""
Dealer payload: assembles the ``dealer`` block of the ``gex`` message from one GEX result.

Everything is computed from one ``DealerPositions`` through ``dealer_model`` / ``levels`` / ``atm`` /
``dealer_flows``. Rounding happens here and nowhere else: prices to 2 decimals, USD as whole dollars
(int), profile values and hedge as ints. The block is sent every publish cycle, so it stays compact
(the profile is downsampled to PROFILE_SEND_STEP and the lists are short).

``dealer["em"]["high"]`` / ``["low"]`` are the anchored open band (open spot +/- open straddle), the same
values ``level_fields`` sends as ``em_high`` / ``em_low``. The session range only feeds ``em.used_pct``.
"""

from datetime import datetime
from typing import Iterable, Optional, Tuple

from spx_trade_desk.market.atm import AtmIvHistory, ExpectedMoveAnchor, atm_iv, atm_straddle, em_used_pct, session_range
from spx_trade_desk.market.dealer_flows import (
    charm_flow_usd,
    charm_path,
    charm_pivot,
    charm_to_close_usd,
    strike_charm_usd,
    vanna_usd_per_vol_down,
)
from spx_trade_desk.market.dealer_model import DealerPositions, dealer_book
from spx_trade_desk.market.gex import GEXResult, OptionData
from spx_trade_desk.market.levels import ProfileLevels, profile_levels


def _px(x: Optional[float]) -> Optional[float]:
    """Price, 2 decimals (None passes through)."""
    return None if x is None else round(float(x), 2)


def _usd(x: Optional[float]) -> Optional[int]:
    """USD, whole dollars as int (None passes through)."""
    return None if x is None else int(round(float(x)))


def build_dealer(result: GEXResult, options: list[OptionData], pos: DealerPositions, T: float, r: float,
                 now: datetime, anchor: ExpectedMoveAnchor, iv_history: AtmIvHistory,
                 bars: Iterable[dict], session_date: str) -> Tuple[Optional[dict], Optional[ProfileLevels]]:
    """The dealer block and the profile levels behind it; ``(None, None)`` when the profile cannot be computed.

    ``options`` is the raw book list (true bid/ask), not the volume-weighted GEX copy. ``anchor`` and
    ``iv_history`` are updated in place, whether or not the profile exists.
    """
    spot = float(result.spot_price)
    expiration = result.expiration

    straddle = atm_straddle(options, spot)
    iv_now = atm_iv(options, spot)
    anchor.update(expiration, now, spot, straddle)
    iv_history.record(expiration, now.isoformat(), iv_now)

    levels = profile_levels(result, pos, T, r, em=straddle)
    if levels is None:
        return None, None
    book = dealer_book(pos, result)

    zero = levels.zero_gamma
    session_high, session_low = session_range(bars, session_date)
    band_low, band_high = _open_band(anchor)
    iv_change_15 = iv_history.change(15)
    iv_change_30 = iv_history.change(30)
    vanna_per_vol = vanna_usd_per_vol_down(book, spot, T, r)
    flow_est = None if iv_change_15 is None else vanna_per_vol * (-iv_change_15)
    pivot = charm_pivot(strike_charm_usd(book, spot, T, r), spot)

    dealer = {
        "model": pos.model,
        "regime": levels.regime,
        "gamma_at_spot": _usd(levels.gamma_at_spot),
        "zero_gamma": _px(zero),
        "zero_gamma_all": [_px(z) for z in levels.zero_gamma_all],
        "zero_gamma_distance": None if zero is None else _px(spot - zero),
        "profile": {
            "from": _px(levels.profile_from),
            "step": levels.profile_step,
            "values": [_usd(v) for v in levels.profile_values],
            "hedge": [_usd(h) for h in levels.hedge_values],
        },
        "nodes": [{"label": n["label"], "strike": _px(n["strike"]), "kind": n["kind"],
                   "gex": _usd(n["gex"]), "side": n["side"], "hedge_usd": _usd(n["hedge_usd"]),
                   "rank": n["rank"]} for n in levels.nodes],
        "voids": [{"lo": _px(v["lo"]), "hi": _px(v["hi"])} for v in levels.voids],
        "em": {
            "live": _px(straddle),
            "open": _px(anchor.open_straddle),
            "open_spot": _px(anchor.open_spot),
            "high": band_high,
            "low": band_low,
            "used_pct": _px(em_used_pct(session_high, session_low, anchor.open_straddle)),
            "anchor_time": anchor.anchor_time,
        },
        "charm": {
            "flow_15m_usd": _usd(charm_flow_usd(book, spot, T, r)),
            "to_close_usd": _usd(charm_to_close_usd(book, spot, T, r)),
            "path": [{"time": p["time"], "usd": _usd(p["usd"])} for p in charm_path(book, spot, now, T, r)],
            "pivot": _px(pivot),
        },
        "vanna": {
            "usd_per_vol_down": _usd(vanna_per_vol),
            "atm_iv": None if iv_now is None else round(iv_now * 100.0, 2),
            "iv_change_15m": None if iv_change_15 is None else round(iv_change_15, 2),
            "iv_change_30m": None if iv_change_30 is None else round(iv_change_30, 2),
            "flow_est_usd": _usd(flow_est),
        },
    }
    return dealer, levels


def _open_band(anchor: ExpectedMoveAnchor) -> Tuple[Optional[float], Optional[float]]:
    """Anchored open band (low, high) = open spot -/+ open straddle, 2 decimals; (None, None) before the anchor."""
    lo, hi = anchor.band()
    return _px(lo), _px(hi)


def level_fields(dealer: Optional[dict], anchor: Optional[ExpectedMoveAnchor] = None) -> dict:
    """Top-level values the level history and the price chart read: the open band and the nearest pin above/below.

    Without a dealer block (a failed profile) the open band still comes from ``anchor`` when one is given, so the
    EM lines do not drop out for a cycle; the pins stay None.
    """
    if dealer is None:
        em_low, em_high = _open_band(anchor) if anchor is not None else (None, None)   # band() is (low, high)
        return {"em_high": em_high, "em_low": em_low, "node_r1": None, "node_s1": None}
    em = dealer["em"]
    pins = [n for n in dealer["nodes"] if n["kind"] == "pin"]
    above = [n["strike"] for n in pins if n["side"] == "above"]
    below = [n["strike"] for n in pins if n["side"] == "below"]
    return {
        "em_high": em["high"],
        "em_low": em["low"],
        "node_r1": min(above) if above else None,
        "node_s1": max(below) if below else None,
    }
