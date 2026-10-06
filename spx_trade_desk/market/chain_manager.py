"""
Chain fetch/stream loops and chain_quotes payload builder.

All long-running loops accept `ib`, `state`, and `broadcast_fn`.
`build_chain_quotes` is a pure function for easy unit testing.
"""

import asyncio
import math
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

from spx_trade_desk.ib.line_budget import LineBudgetExceeded

from spx_trade_desk.core.config import (
    CHAIN_QUOTE_MAX_AGE_S, CHAIN_STREAM_UPDATE_INTERVAL, MONTHLY_CACHE_TTL,
)
from spx_trade_desk.market.hours import now_et, is_cboe_options_open
from spx_trade_desk.market.chain_fetcher import fetch_option_chain
from spx_trade_desk.market.gex import compute_gex, gex_result_to_dict, GEXResult, OptionData
from spx_trade_desk.market.qualification import norm_key, unknown_retry_due  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)


def chain_stream_status_line(contracts: int, quotes_present: int,
                             active_subs: int) -> str:
    """Build the periodic chain-stream status log line.

    When subscriptions are active but no quote data has arrived, append a
    hint at the usual causes so an all-zero chain is not mistaken for a
    broken data path.
    """
    line = (f"Chain stream ticks: {contracts} contracts, "
            f"quotes_present={quotes_present}, active_subs={active_subs}")
    if quotes_present == 0 and active_subs > 0:
        line += (" (no quote data flowing - check IB secdef farm errors "
                 "such as 2157, or overnight GTH liquidity)")
    return line


def _cancel_stream_subs(ib, state):
    """Unsubscribe every active chain stream and clear subscription state.

    Uses the TickStream's own req_id (the native bridge's unsubscribe key)
    rather than the raw contract, mirroring the old `cancelMktData` teardown.
    """
    for key, stream in list(state.chain_stream_tickers.items()):
        try:
            ib.unsubscribe_tick(stream.req_id)
        except Exception:
            pass
    state.chain_stream_tickers.clear()
    state.chain_stream_contracts.clear()


def _safe_stream_float(val):
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _normalize_stream_iv(iv_val):
    iv = _safe_stream_float(iv_val)
    if iv is None or iv <= 0:
        return None
    if iv > 3.0:
        iv = iv / 100.0
    return iv


def _extract_stream_greeks(stream):
    """Pick greek fields from model/last/bid/ask greeks with sane fallbacks."""
    gamma = None
    delta = None
    iv_dec = None

    for greeks in (
        getattr(stream, 'model_greeks', None),
        getattr(stream, 'last_greeks', None),
        getattr(stream, 'bid_greeks', None),
        getattr(stream, 'ask_greeks', None),
    ):
        if greeks is None:
            continue

        if delta is None:
            delta = _safe_stream_float(getattr(greeks, 'delta', None))
        if gamma is None:
            gamma = _safe_stream_float(getattr(greeks, 'gamma', None))
        if iv_dec is None:
            iv_dec = _normalize_stream_iv(getattr(greeks, 'implied_vol', None))

        if delta is not None and gamma is not None and iv_dec is not None:
            break

    if iv_dec is None:
        iv_dec = _normalize_stream_iv(getattr(stream, 'implied_volatility', None))

    return delta, gamma, iv_dec


def build_chain_quotes(options: List[OptionData], spot_price: float,
                       gex_result: Optional[GEXResult] = None,
                       annual_vol: float = 0.20,
                       expiration: str = "",
                       trading_class: str = "SPXW",
                       ages: Optional[Dict[tuple, float]] = None,
                       max_age_s: Optional[float] = None) -> dict:
    """Serialize a list of OptionData into the chain_quotes payload.

    `ages` maps (strike, right) to the seconds since that quote last updated; a side
    gets `<call|put>_age_s` only when `ages` covers it."""
    sigma_tte_years = 0.0
    if expiration:
        try:
            exp_date = datetime.strptime(expiration, "%Y%m%d").date()
            now = now_et()
            if exp_date == now.date():
                close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
                mins_left = max((close_dt - now).total_seconds() / 60.0, 1.0)
                sigma_tte_years = mins_left / (390.0 * 252.0)
            else:
                days_left = (exp_date - now.date()).days
                sigma_tte_years = max(days_left, 1) / 252.0
        except Exception:
            sigma_tte_years = 0.0

    sigma_move = None
    if spot_price > 0 and annual_vol and sigma_tte_years > 0:
        sigma_move = spot_price * annual_vol * math.sqrt(sigma_tte_years)

    calls = {}
    puts = {}
    for o in options:
        if o.right == 'C':
            calls[o.strike] = o
        else:
            puts[o.strike] = o

    all_strikes = sorted(set(list(calls.keys()) + list(puts.keys())))

    rows = []
    for s in all_strikes:
        sigma_abs = None
        sigma_signed = None
        if sigma_move and sigma_move > 0:
            sigma_signed = (s - spot_price) / sigma_move
            sigma_abs = abs(sigma_signed)

        row = {
            "strike": s,
            "sigma_distance_abs": round(sigma_abs, 4) if sigma_abs is not None else None,
            "sigma_distance_signed": round(sigma_signed, 4) if sigma_signed is not None else None,
        }
        c = calls.get(s)
        p = puts.get(s)
        if c:
            row.update({
                "call_bid": c.bid, "call_ask": c.ask,
                "call_bid_size": c.bid_size, "call_ask_size": c.ask_size,
                "call_last": c.last,
                "call_delta": round(c.delta, 4) if c.delta is not None else None,
                "call_gamma": round(c.gamma, 6) if c.gamma is not None else None,
                "call_oi": c.open_interest, "call_volume": c.volume,
                "call_iv": round(c.implied_vol * 100, 2) if c.implied_vol else None,
            })
        if p:
            row.update({
                "put_bid": p.bid, "put_ask": p.ask,
                "put_bid_size": p.bid_size, "put_ask_size": p.ask_size,
                "put_last": p.last,
                "put_delta": round(p.delta, 4) if p.delta is not None else None,
                "put_gamma": round(p.gamma, 6) if p.gamma is not None else None,
                "put_oi": p.open_interest, "put_volume": p.volume,
                "put_iv": round(p.implied_vol * 100, 2) if p.implied_vol else None,
            })
        if ages:
            for right, prefix in (("C", "call"), ("P", "put")):
                age = ages.get(norm_key(s, right))
                if age is not None:
                    row[f"{prefix}_age_s"] = round(age, 1)
        rows.append(row)

    call_wall = gex_result.call_wall if gex_result else None
    put_wall = gex_result.put_wall if gex_result else None
    gamma_flip = gex_result.gamma_flip if gex_result else None

    return {
        "strikes": rows,
        "spot_price": round(spot_price, 2),
        "annual_vol": annual_vol,
        "expiration_raw": expiration,
        "trading_class": trading_class,
        "tte_years": round(sigma_tte_years, 8),
        "sigma_move": round(sigma_move, 4) if sigma_move is not None else None,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_flip": gamma_flip,
        "max_age_s": max_age_s,
        "timestamp": now_et().strftime("%H:%M:%S"),
        "timestamp_iso": now_et().isoformat(),
    }


def _stream_num(val):
    """Finite float, or None for missing / NaN / IB's -1 'unavailable' sentinel."""
    f = _safe_stream_float(val)
    if f is None or f == -1.0:
        return None
    return f


def _stream_int(val) -> int:
    f = _stream_num(val)
    return int(f) if f is not None else 0


def _collect_stream_quotes(tickers: dict, oi_fallback: dict, seen: Optional[dict] = None):
    """Turn live chain-stream TickStreams into (tick payloads, rows, rows for the book).

    Only streams that have received a tick go to the quote book: a fresh subscription
    with no data yet must not stamp an older polled quote as current. With ``seen``
    (key -> the last tick time already merged), only streams that ticked since the
    previous pass go to the book, so a silent data farm lets the rows age instead of
    being restamped fresh every pass. ``seen`` is updated in place.
    """
    ticks, live_options, book_options = [], [], []
    for (strike, right), stream in tickers.items():
        bid = _stream_num(stream.bid)
        ask = _stream_num(stream.ask)
        last_val = _stream_num(stream.last)
        bid_sz, ask_sz, vol = (_stream_int(stream.bid_size), _stream_int(stream.ask_size),
                               _stream_int(stream.volume))
        delta_raw, gamma_raw, iv_dec = _extract_stream_greeks(stream)
        delta = round(delta_raw, 4) if delta_raw is not None else None
        gamma = round(gamma_raw, 6) if gamma_raw is not None else None
        iv = round(iv_dec * 100, 2) if iv_dec is not None else None
        oi_raw = _stream_num(stream.call_oi if right == "C" else stream.put_oi)
        # TickStream defaults OI to 0, so a non-positive value means 'not reported yet':
        # fall back to the last chain value (same rule as the book: a 0 never wipes a positive).
        oi = int(oi_raw) if oi_raw is not None and oi_raw > 0 else oi_fallback.get((strike, right), 0)
        ticks.append({
            "strike": strike, "right": right,
            "bid": round(bid, 2) if bid is not None else None,
            "ask": round(ask, 2) if ask is not None else None,
            "bid_size": bid_sz, "ask_size": ask_sz,
            "last": round(last_val, 2) if last_val is not None else None,
            "volume": vol, "delta": delta, "gamma": gamma, "iv": iv,
        })
        opt = OptionData(
            strike=strike, right=right, delta=delta, gamma=gamma, implied_vol=iv_dec,
            open_interest=oi, volume=vol,
            bid=round(bid, 2) if bid is not None else None,
            ask=round(ask, 2) if ask is not None else None,
            last=round(last_val, 2) if last_val is not None else None,
            bid_size=bid_sz, ask_size=ask_sz,
        )
        live_options.append(opt)
        if stream.received_any_tick():
            tick_mono = getattr(stream, "last_tick_mono", 0.0)
            if seen is not None:
                if tick_mono <= seen.get((strike, right), -1.0):
                    continue
                seen[(strike, right)] = tick_mono
            book_options.append(opt)
    return ticks, live_options, book_options


async def chain_stream_loop(ib, state, broadcast_fn):
    """Maintain persistent market-data subscriptions for the strikes nearest the focus.

    Uses the line budget's 'stream' share and is never paused: the wing poller works
    from its own share (chain_poller.py). Ticked rows are merged into the quote book.
    """
    await asyncio.sleep(2)
    last_expiration = ""
    last_sub_count = -1
    last_tick_log_ts = 0.0
    last_center_log = ""
    seen_ticks: Dict[tuple, float] = {}     # key -> last stream tick time merged into the book

    while True:
        try:
            if not state.connected or not state.expiration or state.spx_price <= 0:
                await asyncio.sleep(5)
                continue
            if not is_cboe_options_open():
                await asyncio.sleep(5)
                continue

            if state.expiration != last_expiration:
                _cancel_stream_subs(ib, state)
                seen_ticks.clear()
                last_expiration = state.expiration
                logger.info(f"Chain stream expiration switched to {state.expiration}; reset subscriptions")

            spot = state.spx_price
            viewport_center = state.viewport_center_strike if state.active_tab == "chain" else 0.0
            focus_center = viewport_center if viewport_center > 0 else spot
            center_log = f"{'viewport' if viewport_center > 0 else 'spot'}:{focus_center:.1f}"
            if center_log != last_center_log:
                last_center_log = center_log
                logger.info(f"Chain stream center -> {center_log}")

            available_pairs = {norm_key(o.strike, o.right) for o in state.chain_data}
            avail = sorted({s for (s, _) in available_pairs if s % 5 == 0})
            if not avail:
                avail = [s for s in state.strikes if s % 5 == 0]
            if not avail:
                await asyncio.sleep(10)
                continue

            max_strikes = max(1, ib.line_budget.capacity("stream") // 2)
            nearest = sorted(avail, key=lambda s: (abs(s - focus_center), s))[:max_strikes]
            desired_keys = {norm_key(s, r) for s in nearest for r in ("C", "P")}
            if available_pairs:
                desired_keys = {k for k in desired_keys if k in available_pairs}

            current_keys = set(state.chain_stream_tickers.keys())
            for key in current_keys - desired_keys:
                stream = state.chain_stream_tickers.pop(key, None)
                state.chain_stream_contracts.pop(key, None)
                if stream is not None:
                    try:
                        ib.unsubscribe_tick(stream.req_id)
                    except Exception:
                        pass

            new_keys = sorted(desired_keys - current_keys,
                              key=lambda k: (abs(k[0] - focus_center), k))
            if new_keys:
                qualified = await state.qual_cache.qualify(
                    ib, state.expiration, state.trading_class, new_keys, time.monotonic())
                subscribed = 0
                for key in new_keys:
                    qc = qualified.get(key)
                    if qc is None:
                        continue
                    try:
                        stream = ib.subscribe_tick(qc, "101", share="stream")
                    except LineBudgetExceeded as e:
                        logger.warning(f"Chain stream: {e}")
                        break
                    state.chain_stream_tickers[key] = stream
                    state.chain_stream_contracts[key] = qc
                    subscribed += 1
                if subscribed:
                    logger.info(f"Chain stream subscribed {subscribed}/{len(new_keys)}; "
                                f"active_subs={len(state.chain_stream_tickers)}")

            if len(state.chain_stream_tickers) != last_sub_count:
                last_sub_count = len(state.chain_stream_tickers)
                logger.info(f"Chain stream active subscriptions: {last_sub_count} "
                            f"(unknown_blacklist={len(state.qual_cache.unknown)})")

            await asyncio.sleep(CHAIN_STREAM_UPDATE_INTERVAL)

            oi_fallback = {(o.strike, o.right): o.open_interest for o in state.chain_data}
            for k in [k for k in seen_ticks if k not in state.chain_stream_tickers]:
                del seen_ticks[k]
            ticks, live_options, book_options = _collect_stream_quotes(
                state.chain_stream_tickers, oi_fallback, seen_ticks)
            if state.quote_book.expiry != state.expiration:
                state.quote_book.reset(state.expiration)
            state.quote_book.update(book_options, "stream", time.monotonic())
            book_ages = state.quote_book.ages(time.monotonic())

            if ticks:
                now_iso = now_et().isoformat()
                await broadcast_fn({"type": "chain_tick",
                                    "data": {"ticks": ticks, "timestamp_iso": now_iso}})
                live_quotes = build_chain_quotes(
                    options=live_options, spot_price=state.spx_price,
                    gex_result=state.gex_result, annual_vol=state.annual_vol,
                    expiration=state.expiration, trading_class=state.trading_class,
                    ages={k: book_ages[k] for k in (norm_key(o.strike, o.right)
                                                    for o in live_options) if k in book_ages},
                    max_age_s=CHAIN_QUOTE_MAX_AGE_S)
                live_quotes["timestamp_iso"] = now_iso
                live_quotes["scope"] = "stream"
                state.last_chain_update = now_et().strftime("%H:%M:%S")

                now_monotonic = time.monotonic()
                if now_monotonic - last_tick_log_ts >= 10.0:
                    last_tick_log_ts = now_monotonic
                    with_quotes = sum(1 for t in ticks if t.get("bid") is not None
                                      or t.get("ask") is not None or t.get("last") is not None)
                    logger.info(chain_stream_status_line(
                        len(ticks), with_quotes, len(state.chain_stream_tickers)))
                await broadcast_fn({"type": "chain_quotes", "data": live_quotes})

        except asyncio.CancelledError:
            _cancel_stream_subs(ib, state)
            break
        except Exception as e:
            logger.error(f"Chain stream error: {e}")
            await asyncio.sleep(5)


async def monthly_gex_fetch(ib, state, broadcast_fn):
    """Fetch SPX monthly option chain and compute GEX, broadcast result.

    Skips fetch if cached data is less than MONTHLY_CACHE_TTL seconds old.
    """
    import time as _time

    now_mono = _time.monotonic()
    if (state.monthly_latest_gex is not None
            and (now_mono - state.monthly_last_fetch_ts) < MONTHLY_CACHE_TTL):
        logger.info("Monthly GEX cache still fresh, re-broadcasting cached data")
        await broadcast_fn({"type": "monthly_gex", "data": state.monthly_latest_gex})
        return

    if not state.connected or not state.monthly_expiration:
        logger.warning("Cannot fetch monthly GEX: not connected or no monthly expiration")
        return
    if state.spx_price <= 0:
        logger.warning("Cannot fetch monthly GEX: no reference price")
        return

    logger.info(
        f"Starting monthly GEX fetch: exp={state.monthly_expiration}, "
        f"spot={state.spx_price:.2f}, {len(state.monthly_strikes)} strikes"
    )

    await broadcast_fn({"type": "monthly_gex_progress", "data": {"phase": "starting"}})

    try:
        options = await fetch_option_chain(
            ib=ib,
            underlying=state.spx_contract,
            expiration=state.monthly_expiration,
            strikes=state.monthly_strikes,
            spot_price=state.spx_price,
            std_dev_range=8.0,
            annual_vol=state.annual_vol,
            trading_class='SPX',
        )

        if not options:
            logger.warning("No option data returned from monthly chain fetch")
            await broadcast_fn({"type": "monthly_gex_progress", "data": {"phase": "done"}})
            return

        total_oi = sum(o.open_interest for o in options)
        if total_oi == 0:
            logger.info("Monthly OI all zeros, using volume as proxy")
            for o in options:
                o.open_interest = o.volume

        # Compute time to expiry
        exp_date = datetime.strptime(state.monthly_expiration, "%Y%m%d").date()
        now = now_et()
        if exp_date == now.date():
            close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
            mins_left = max((close_dt - now).total_seconds() / 60.0, 1.0)
            tte_years = mins_left / (390.0 * 252.0)
        else:
            days_left = (exp_date - now.date()).days
            tte_years = max(days_left, 1) / 252.0

        gex_result = compute_gex(
            options, state.spx_price,
            time_to_expiry_years=tte_years,
            risk_free_rate=state.risk_free_rate,
        )
        gex_result.expiration = state.monthly_expiration
        gex_result.timestamp = now_et().isoformat()

        state.monthly_gex_result = gex_result
        state.monthly_latest_gex = gex_result_to_dict(gex_result)
        state.monthly_latest_gex["es_derived"] = state.es_derived
        state.monthly_chain_data = options
        state.monthly_last_fetch_ts = _time.monotonic()

        logger.info(
            f"Monthly GEX computed: Call Wall={gex_result.call_wall}, "
            f"Put Wall={gex_result.put_wall}, Gamma Flip={gex_result.gamma_flip}, "
            f"Max Pain={gex_result.max_pain}"
        )

        await broadcast_fn({"type": "monthly_gex", "data": state.monthly_latest_gex})

    except Exception as e:
        logger.error(f"Monthly GEX fetch error: {e}", exc_info=True)
    finally:
        await broadcast_fn({"type": "monthly_gex_progress", "data": {"phase": "done"}})
