"""Boot of one IB session, shared by the server start and the manual reconnect.

``boot_session`` runs the setup steps as a dependency graph instead of one serial list; ``first_boot``
adds the steps that only a fresh process needs (price bars, ES baseline, risk-free rate).
``start_background_loops`` is the single list of loops that live as long as one IB session.
"""
import asyncio
import logging
import time
from typing import Optional

from spx_trade_desk.core.log_buffer import log_push_loop
from spx_trade_desk.core.perf import perf
from spx_trade_desk.core.rates import get_risk_free_rate
from spx_trade_desk.ib.account import account_push_loop, setup_account_subscription
from spx_trade_desk.ib.connection import (
    connect_ib, fetch_es_baseline, setup_chain_info, setup_es_subscription,
    setup_monthly_chain_info, setup_spx_subscription, setup_vix1d_subscription,
    setup_vix_subscription,
)
from spx_trade_desk.market.chain_manager import chain_stream_loop
from spx_trade_desk.market.chain_poller import chain_poll_loop
from spx_trade_desk.market.chain_publisher import chain_publish_loop
from spx_trade_desk.market.hours import is_within_rth
from spx_trade_desk.market.price_bars import price_bars_loop, seed_price_bars
from spx_trade_desk.strategy.engine import strategy_evaluation_loop, take_profit_loop
from spx_trade_desk.strategy.store import load_strategies
from spx_trade_desk.web.ws import status_push_loop

logger = logging.getLogger(__name__)


async def _all(*awaitables):
    """Run awaitables concurrently; the first failure cancels the others and is re-raised.

    Python 3.10 has no ``asyncio.TaskGroup``, and ``gather`` alone leaves the siblings running.
    """
    tasks = [asyncio.ensure_future(a) for a in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException as first:
        for t in tasks:
            t.cancel()
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            # Siblings that failed at the same moment: only the first failure is raised, so log the rest.
            if isinstance(result, Exception) and result is not first:
                logger.warning("Another boot step failed too: %r", result)
        raise


async def _spx_branch(ib, state, first_boot: bool, prefetch: list) -> None:
    await setup_spx_subscription(ib, state)         # qualifies SPX: the next steps need its conId
    steps = [setup_chain_info(ib, state), setup_monthly_chain_info(ib, state)]
    if first_boot:
        steps.append(seed_price_bars(ib, state))
    await _all(*steps)
    if state.expiration:
        # The expiration is known now: list its contracts while the rest of the boot runs. Not awaited;
        # the boot owns the task until it succeeds (``boot_session`` cancels it on a failed boot).
        task = asyncio.create_task(_prefetch_chain(ib, state))
        prefetch.append(task)
        state.background_tasks.append(task)


async def _es_branch(ib, state, fetch_baseline: bool) -> None:
    await setup_es_subscription(ib, state)
    if fetch_baseline:
        await fetch_es_baseline(ib, state)


async def _risk_free_rate(state) -> None:
    # A blocking yfinance call: keep it off the event loop.
    state.risk_free_rate = await asyncio.to_thread(get_risk_free_rate)
    logger.info(f"Risk-free rate set from SGOV 7 Day Yield: {state.risk_free_rate:.4%}")


async def _prefetch_chain(ib, state) -> None:
    """List today's contracts so the first chain cycle finds them; never fails the boot."""
    try:
        await state.contracts.ensure_chain(ib, "SPX", state.expiration, state.trading_class,
                                           time.monotonic())
    except Exception as e:
        logger.warning("Chain prefetch failed (the chain loop will list it): %s", e)


async def _cancel_prefetch(state, prefetch: list) -> None:
    """A failed or cancelled boot leaves no listing task behind."""
    for task in prefetch:
        task.cancel()
    await asyncio.gather(*prefetch, return_exceptions=True)
    state.background_tasks[:] = [t for t in state.background_tasks if t not in prefetch]


async def boot_session(ib, state, *, first_boot: bool, port: Optional[int] = None,
                       client_id: Optional[int] = None, error_handler=None) -> None:
    """Connect and set up one IB session. An exception in a step that raises today aborts the boot."""
    prefetch: list = []
    try:
        with perf.timer("startup.total"):
            await connect_ib(ib, state, port=port, client_id=client_id)
            if error_handler is not None:
                ib.error_handler = error_handler
            historical = first_boot and not is_within_rth()      # once per boot: baseline and log agree
            branches = [
                _spx_branch(ib, state, first_boot, prefetch),
                _es_branch(ib, state, historical),
                setup_account_subscription(ib, state),
                setup_vix_subscription(ib, state),
                setup_vix1d_subscription(ib, state),
            ]
            if first_boot:
                branches.append(_risk_free_rate(state))
            await _all(*branches)
            state.strategies = load_strategies()

        if historical:
            logger.info(
                f"Historical mode: showing {state.historical_date}, "
                f"ref price={state.spx_price:.2f}, "
                f"ES baseline={state.es_at_spx_close:.2f}"
            )
    except BaseException:
        await _cancel_prefetch(state, prefetch)
        raise


def start_background_loops(ib, state, broadcast_fn, *, recorder) -> None:
    """Start the nine loops that live as long as one IB session (cancelled on reconnect).

    The strategy loops are part of the set so a manual reconnect never silently stops auto-entry
    or position management. The chain service is the wing poller, the live stream and the
    publisher that turns the merged quote book into GEX plus the chain payload.
    """
    if state.force_chain_fetch_event is None:
        state.force_chain_fetch_event = asyncio.Event()
    loops = (
        price_bars_loop(ib, state, broadcast_fn),
        status_push_loop(state, broadcast_fn),
        account_push_loop(ib, state, broadcast_fn),
        log_push_loop(state, broadcast_fn),
        strategy_evaluation_loop(ib, state, broadcast_fn),
        take_profit_loop(ib, state, broadcast_fn),
        chain_poll_loop(ib, state, broadcast_fn),
        chain_stream_loop(ib, state, broadcast_fn),
        chain_publish_loop(ib, state, broadcast_fn, recorder=recorder),
    )
    for coro in loops:
        state.background_tasks.append(asyncio.create_task(coro))
