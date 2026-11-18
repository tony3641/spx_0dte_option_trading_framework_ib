"""Publisher: turns the quote book into GEX, the chain payload and dashboard state.

Runs every CHAIN_REFRESH_SECONDS. It replaces the old 5-minute full fetch, so GEX,
``state.chain_data`` and ``state.chain_quotes_cache`` (which the strategy engine reads)
are at most one cycle old, and each row says how old its quote is.
"""
import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime

from spx_trade_desk.core.config import CHAIN_QUOTE_MAX_AGE_S, CHAIN_REFRESH_SECONDS
from spx_trade_desk.market.chain_manager import build_chain_quotes
from spx_trade_desk.market.dealer_model import positions_flow_blend, positions_from_oi, strike_gammas
from spx_trade_desk.market.dealer_payload import build_dealer, level_fields
from spx_trade_desk.market.flow import HOT_FRESH_MIN, annotate_gex_bars, flow_gex_by_strike, hot_strikes
from spx_trade_desk.market.gex import compute_gex, gex_result_to_dict
from spx_trade_desk.market.hours import is_cboe_options_open, now_et
from spx_trade_desk.market.levels import resolve_gamma_flip

logger = logging.getLogger(__name__)


def tte_years(expiration: str, now: datetime) -> float:
    """Time to expiry in RTH-trading years (390 min x 252 days), as the GEX math expects."""
    try:
        exp_date = datetime.strptime(expiration, "%Y%m%d").date()
    except (TypeError, ValueError):
        return 0.0
    if exp_date == now.date():
        close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
        mins_left = max((close_dt - now).total_seconds() / 60.0, 1.0)
        return mins_left / (390.0 * 252.0)
    return max((exp_date - now.date()).days, 1) / 252.0


def _flow_outputs(state, gex, options, dealer, now, T: float) -> None:
    """Hot strikes, flow GEX and the flow summary, written into the gex bars and the dealer block.

    Runs after the dealer block exists. ``dealer["flow"]`` is set only when there is a dealer block. Never
    raises: a flow failure must not drop the GEX publish.
    """
    try:
        state.volume_history.record(state.expiration, now.isoformat(), options)
        recent = {o.strike: state.volume_history.added(o.strike, HOT_FRESH_MIN) for o in options}
        hot = hot_strikes(options, recent)
        fgex = flow_gex_by_strike(*state.flow_tracker.net_volumes(),
                                  strike_gammas(gex, state.spx_price, T, state.risk_free_rate), state.spx_price)
        annotate_gex_bars(state.latest_gex["gex_bars"], state.flow_tracker, hot, fgex)
        if dealer is not None:
            dealer["flow"] = state.flow_tracker.summary(now)
    except Exception:
        logger.exception("Flow outputs failed; publishing this cycle without flow")


async def publish_chain(state, broadcast_fn, now_mono: float) -> bool:
    book = state.quote_book
    if len(book) == 0 or state.spx_price <= 0:
        return False
    options = book.options()
    # The "all OI zero -> weight by volume" rule only shapes GEX; the book, chain_data
    # and the recorder keep the raw OI.
    gex_opts = [replace(o) for o in options]
    if sum(o.open_interest for o in gex_opts) == 0:
        for o in gex_opts:
            o.open_interest = o.volume
    now = now_et()
    T = tte_years(state.expiration, now)
    gex = compute_gex(gex_opts, state.spx_price,
                      time_to_expiry_years=T,
                      risk_free_rate=state.risk_free_rate)
    gex.expiration = state.expiration
    gex.timestamp = now.isoformat()
    pos = positions_from_oi(gex)
    if state.dealer_model == "flow_blend":
        try:
            pos = positions_flow_blend(pos, *state.flow_tracker.net_volumes())
        except Exception:
            # Keep the OI positions for this cycle: the profile still renders and the GEX / chain publish is never dropped.
            logger.exception("Flow-blend positions failed; using OI-assumed positions this cycle")
    try:
        dealer, levels = build_dealer(gex, options, pos, T, state.risk_free_rate, now, state.em_anchor,
                                      state.atm_iv_history, state.price_history, state.price_session_date)
    except Exception:
        # The dealer block is an extra: a failure here must not stop the GEX, chain and strategy publish.
        logger.exception("Dealer profile failed; publishing this cycle with the cumulative gamma flip")
        dealer, levels = None, None
    cumulative = gex.gamma_flip
    gex.gamma_flip, gamma_flip_method = resolve_gamma_flip(gex, levels)   # zero gamma when the profile exists
    state.gex_result = gex
    state.latest_gex = gex_result_to_dict(gex)
    state.latest_gex["dealer"] = dealer
    state.latest_gex["gamma_flip_method"] = gamma_flip_method
    state.latest_gex["gamma_flip_cumulative"] = cumulative
    state.latest_gex.update(level_fields(dealer))
    _flow_outputs(state, gex, options, dealer, now, T)
    state.latest_dealer = dealer
    state.level_history.record(state.latest_gex)
    state.charm_history.record(gex)
    state.last_chain_update = now.strftime("%H:%M:%S")
    state.chain_data = options
    state.chain_quotes_cache = build_chain_quotes(
        options, state.spx_price, gex, state.annual_vol, state.expiration,
        trading_class=state.trading_class, ages=book.ages(now_mono),
        max_age_s=CHAIN_QUOTE_MAX_AGE_S)
    state.chain_quotes_cache["scope"] = "full"
    # The engine adds (now - built_mono) to every side's age, so a cache that stops being
    # replaced (reconnect, a persistent build error) cannot keep passing as fresh.
    state.chain_quotes_cache["built_mono"] = time.monotonic()
    payload = dict(state.latest_gex)
    payload["es_derived"] = state.es_derived
    payload["charm_frame"] = state.charm_history.latest()     # the chart appends it to the snapshot's history
    await broadcast_fn({"type": "gex", "data": payload})
    await broadcast_fn({"type": "chain_quotes", "data": state.chain_quotes_cache})
    if state.chain_fetching:
        state.chain_fetching = False      # hides the dashboard's loading overlay
        await broadcast_fn({"type": "chain_progress", "data": {
            "phase": "done", "batch": 1, "total_batches": 1, "pct": 100}})
    return True


async def publish_tick(state, broadcast_fn, recorder, now_mono: float) -> bool:
    """One publish cycle. A successful publish proves the dashboard is alive, so the
    recorder's heartbeat is touched whether or not a record is due."""
    ok = await publish_chain(state, broadcast_fn, now_mono)
    if recorder is not None:
        if ok:
            recorder.touch_heartbeat()
        recorder.maybe_record(state, now_et(), now_mono)
    return ok


async def chain_publish_loop(ib, state, broadcast_fn, recorder=None):
    while True:
        try:
            if state.connected and state.expiration and is_cboe_options_open():
                await publish_tick(state, broadcast_fn, recorder, time.monotonic())
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Chain publish error: {e}", exc_info=True)
        await asyncio.sleep(CHAIN_REFRESH_SECONDS)
