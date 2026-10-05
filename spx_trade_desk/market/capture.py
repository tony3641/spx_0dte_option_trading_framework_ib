"""Standalone 0DTE chain capture: the recorder's fallback when the dashboard is not running.

    python -m spx_trade_desk.market.capture

Writes the same daily files as the dashboard's recorder (source "standalone"). While the
dashboard's heartbeat is fresh it only watches; once the dashboard stops writing for
HEARTBEAT_STALE_S it captures with full sweeps over the whole line budget (no stream
runs here, so the 'poll' share gets almost every line). Exits at the session close.
Places no orders.
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from spx_trade_desk.core.app_state import create_app_state
from spx_trade_desk.core.config import CAPTURE_CLIENT_ID, MARKET_DATA_LINES
from spx_trade_desk.ib.client import IBClient
from spx_trade_desk.ib.connection import (
    connect_ib, setup_spx_subscription, setup_vix1d_subscription, setup_vix_subscription,
    update_spx_es_prices,
)
from spx_trade_desk.ib.line_budget import split_lines
from spx_trade_desk.market.chain_fetcher import fetch_option_chain, get_chain_params
from spx_trade_desk.market.chain_recorder import ChainRecorder, session_close
from spx_trade_desk.market.hours import now_et
from spx_trade_desk.resources import CHAIN_LIBRARY_DIR

logger = logging.getLogger(__name__)

HEARTBEAT_STALE_S = 180.0
IDLE_CHECK_S = 60
STEP_S = 5


def heartbeat_fresh(path: Path, now_epoch: float, max_age: float = HEARTBEAT_STALE_S) -> bool:
    try:
        return now_epoch - path.stat().st_mtime < max_age
    except OSError:
        return False


async def _sweep(ib, state):
    return await fetch_option_chain(ib, state.spx_contract, state.expiration, state.strikes,
                                    state.spx_price, std_dev_range=8.0,
                                    annual_vol=state.annual_vol, trading_class="SPXW")


async def capture_session(ib, state, recorder: ChainRecorder, dashboard_heartbeat: Path, *,
                          clock=now_et, epoch=time.time, mono=time.monotonic,
                          sleep=asyncio.sleep, fetch=_sweep) -> int:
    """Capture until the session close; return the number of records written."""
    written = 0
    while True:
        t = clock()
        if t.time() >= session_close(t.date()):
            return written
        if heartbeat_fresh(dashboard_heartbeat, epoch()):
            await sleep(IDLE_CHECK_S)
            continue
        if recorder.due(t):
            await update_spx_es_prices(state)
            opts = await fetch(ib, state)
            if state.quote_book.expiry != state.expiration:
                state.quote_book.reset(state.expiration)
            state.quote_book.update(opts, "poll", mono())
            if recorder.maybe_record(state, t, mono()):
                written += 1
                logger.info(f"Captured {len(opts)} contracts at {t:%H:%M:%S}")
        await sleep(STEP_S)


async def main(port: Optional[int] = None) -> None:
    ib = IBClient(line_shares=split_lines(MARKET_DATA_LINES, stream_cap=0))
    state = create_app_state()
    await connect_ib(ib, state, port=port, client_id=CAPTURE_CLIENT_ID)
    try:
        await setup_spx_subscription(ib, state)
        await setup_vix_subscription(ib, state)
        await setup_vix1d_subscription(ib, state)
        exps, strikes = await get_chain_params(ib, state.spx_contract)
        today = now_et().strftime("%Y%m%d")
        if today not in exps:
            logger.info("No 0DTE SPXW expiry today; nothing to capture")
            return
        state.expiration, state.strikes, state.trading_class = today, strikes, "SPXW"
        await asyncio.sleep(2)
        await update_spx_es_prices(state)
        recorder = ChainRecorder(CHAIN_LIBRARY_DIR, source="standalone")
        n = await capture_session(ib, state, recorder,
                                  CHAIN_LIBRARY_DIR / ".heartbeat-dashboard")
        logger.info(f"Session over: {n} records written to {CHAIN_LIBRARY_DIR}")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(main())
