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
from spx_trade_desk.market.gex import compute_gex, gex_result_to_dict
from spx_trade_desk.market.hours import is_cboe_options_open, now_et

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
    gex = compute_gex(gex_opts, state.spx_price,
                      time_to_expiry_years=tte_years(state.expiration, now),
                      risk_free_rate=state.risk_free_rate)
    gex.expiration = state.expiration
    gex.timestamp = now.isoformat()
    state.gex_result = gex
    state.latest_gex = gex_result_to_dict(gex)
    state.last_chain_update = now.strftime("%H:%M:%S")
    state.chain_data = options
    state.chain_quotes_cache = build_chain_quotes(
        options, state.spx_price, gex, state.annual_vol, state.expiration,
        trading_class=state.trading_class, ages=book.ages(now_mono),
        max_age_s=CHAIN_QUOTE_MAX_AGE_S)
    state.chain_quotes_cache["scope"] = "full"
    payload = dict(state.latest_gex)
    payload["es_derived"] = state.es_derived
    await broadcast_fn({"type": "gex", "data": payload})
    await broadcast_fn({"type": "chain_quotes", "data": state.chain_quotes_cache})
    if state.chain_fetching:
        state.chain_fetching = False      # hides the dashboard's loading overlay
        await broadcast_fn({"type": "chain_progress", "data": {
            "phase": "done", "batch": 1, "total_batches": 1, "pct": 100}})
    return True


async def chain_publish_loop(ib, state, broadcast_fn, recorder=None):
    while True:
        try:
            if state.connected and state.expiration and is_cboe_options_open():
                mono = time.monotonic()
                await publish_chain(state, broadcast_fn, mono)
                if recorder is not None:
                    recorder.maybe_record(state, now_et(), mono)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Chain publish error: {e}", exc_info=True)
        await asyncio.sleep(CHAIN_REFRESH_SECONDS)
