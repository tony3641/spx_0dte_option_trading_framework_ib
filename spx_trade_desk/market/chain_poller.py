"""Wing poller: keeps the quote book complete without pausing the chain stream.

Each cycle polls every contract in the chain range (+-8 daily sigma, multiples of 5)
that the stream does not currently hold, nearest strikes first, in batches the size of
the line budget's 'poll' share (at most POLL_BATCH_MAX, so one snapshot never paces hundreds
of subscribes ahead of an order). Results go into ``state.quote_book``.
"""
import asyncio
import logging
import time
from typing import Callable, List

from spx_trade_desk.market.bars import compute_annual_vol, fetch_historical_bars
from spx_trade_desk.market.chain_fetcher import _strike_range_for_std_devs, _stream_to_option_data
from spx_trade_desk.market.hours import (
    find_next_expiration, get_expiration_display, is_cboe_options_open,
    resolve_trading_expiration,
)
from spx_trade_desk.market.qualification import Key, norm_key

logger = logging.getLogger(__name__)

CHAIN_STD_DEV_RANGE = 8.0
ANNUAL_VOL_REFRESH_S = 300.0
POLL_PACE_S = 1.0          # breather between productive cycles
POLL_IDLE_S = 10.0         # back-off when a cycle wrote nothing (empty range / cooldown)
POLL_START_DELAY_S = 1.0
POLL_BATCH_MAX = 50        # lines per snapshot batch, whatever the poll share (as the chain fetcher)


def poll_targets(strikes, spot: float, annual_vol: float, streamed: set) -> List[Key]:
    lo, hi = _strike_range_for_std_devs(spot, CHAIN_STD_DEV_RANGE, annual_vol)
    in_range = [s for s in strikes if s % 5 == 0 and lo <= s <= hi]
    keys = [norm_key(s, r) for s in in_range for r in ("C", "P")]
    keys = [k for k in keys if k not in streamed]
    return sorted(keys, key=lambda k: (abs(k[0] - spot), k[0], k[1]))


async def poll_once(ib, state, now: Callable[[], float] = time.monotonic) -> int:
    """One full wing cycle; returns the number of contracts written to the book."""
    book = state.quote_book
    if book.expiry != state.expiration:
        book.reset(state.expiration)
    targets = poll_targets(state.strikes, state.spx_price, state.annual_vol,
                           set(state.chain_stream_tickers.keys()))
    qualified = await state.contracts.qualify_keys(ib, state.expiration, state.trading_class,
                                                   targets, now())
    ordered = [qualified[k] for k in targets if k in qualified]
    batch_n = max(1, min(ib.line_budget.capacity("poll"), POLL_BATCH_MAX))
    written = 0
    for i in range(0, len(ordered), batch_n):
        refresh = state.force_chain_fetch_event
        if refresh is not None and refresh.is_set():
            break          # the loop top clears the event and re-reads the contract listing
        streams = await ib.fetch_snapshot(ordered[i:i + batch_n], generic="101",
                                          timeout=6.0, share="poll")
        opts = [o for o in (_stream_to_option_data(s) for s in streams) if o is not None]
        book.update(opts, "poll", now())
        written += len(opts)
    return written


def refresh_expiration(state) -> None:
    """Prefer today's PM SPXW (0DTE); fall back to today's PM SPX monthly; otherwise
    roll to the next available SPXW day for display/scanning."""
    resolved = resolve_trading_expiration(state)
    if resolved:
        state.expiration, state.trading_class = resolved
    else:
        new_exp = find_next_expiration(state.expirations)
        state.trading_class = "SPXW"
        if new_exp and new_exp != state.expiration:
            state.expiration = new_exp
            logger.info(f"Expiration updated to: {get_expiration_display(new_exp)}")


async def chain_poll_loop(ib, state, broadcast_fn):
    """Poll the wings continuously; a manual refresh restarts the cycle and re-reads the
    contract listing."""
    await asyncio.sleep(POLL_START_DELAY_S)
    if state.force_chain_fetch_event is None:
        state.force_chain_fetch_event = asyncio.Event()
    last_vol = float("-inf")
    while True:
        try:
            if not state.connected or not state.expiration:
                await asyncio.sleep(10)
                continue
            if state.spx_price <= 0:
                await fetch_historical_bars(ib, state)
                if state.spx_price <= 0:
                    await asyncio.sleep(30)
                    continue
            refresh_expiration(state)
            if not is_cboe_options_open():
                await asyncio.sleep(10)
                continue
            if time.monotonic() - last_vol >= ANNUAL_VOL_REFRESH_S:
                await compute_annual_vol(ib, state, lookback_days=30)
                last_vol = time.monotonic()
            if state.force_chain_fetch_event.is_set():
                state.force_chain_fetch_event.clear()
                state.contracts.relist()
                logger.info("Manual chain refresh: contract listing will be re-read")
            if len(state.quote_book) == 0:
                state.chain_fetching = True        # drives the loading overlay until first publish
            t0 = time.monotonic()
            n = await poll_once(ib, state)
            logger.info(f"Wing poll cycle: {n} contracts in {time.monotonic() - t0:.1f}s")
            await asyncio.sleep(POLL_IDLE_S if n == 0 else POLL_PACE_S)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Wing poll error: {e}", exc_info=True)
            await asyncio.sleep(5)
