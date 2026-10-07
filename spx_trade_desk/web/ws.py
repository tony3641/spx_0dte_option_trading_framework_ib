"""
WebSocket endpoint, message routing, and broadcast utility.

The WebSocket handler dispatches incoming messages to the appropriate
module-level handlers (order_manager, account_manager, etc.).
"""

import asyncio
import json
import logging
import math
import time
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from spx_trade_desk.core import config
from spx_trade_desk.core.config import VIEWPORT_CENTER_MIN_INTERVAL
from spx_trade_desk.core.perf import perf
from spx_trade_desk.market.hours import now_et, market_status, get_expiration_display, is_within_rth
from spx_trade_desk.market.chain_manager import monthly_gex_fetch
from spx_trade_desk.market.price_bars import snapshot_payload
from spx_trade_desk.ib.account import refresh_account_state, build_account_payload
from spx_trade_desk.ib.orders import handle_place_order, handle_cancel_order
from spx_trade_desk.ib.connection import update_vix
from spx_trade_desk.strategy.engine import reset_strategy_runtime
from spx_trade_desk.strategy.store import load_strategies, save_strategy, delete_strategy
from spx_trade_desk.strategy.models import Strategy
from spx_trade_desk.web.push import ClientChannel, encode, record_client_perf, stamp

logger = logging.getLogger(__name__)

# 101 (max market-data lines): IBClient logs it and shrinks the line budget; no toast per refused line.
_IGNORED_IB_ERROR_CODES = {101, 2104, 2106, 2107, 2108, 2119, 2158}

# Strong references to in-flight cancel_order tasks (the loop only keeps weak ones). They outlive
# their connection: a cancel the user clicked right before closing the tab still reaches IB.
_CANCEL_TASKS: set = set()


def _unquote_name(name: str) -> str:
    """The strategy_* handlers expect a raw name, but a JSON-stringifying client
    may send one wrapped in quotes (e.g. strategy_delete:\"Strat1\"). Strip a
    single layer of surrounding double quotes so the delete/arm/disarm still work.
    """
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1]
    return name


def _budget_error(state, budget) -> Optional[str]:
    """Return an error string when a strategy budget exceeds ExcessLiquidity.

    Returns None when the budget is unset or liquidity is unknown (so a
    disconnecting account never blocks saving).
    """
    if budget is None:
        return None
    summary = getattr(state, "account_summary", {}) or {}
    excess = summary.get("ExcessLiquidity")
    if excess is None:
        return None
    if budget > float(excess):
        return f"Budget ${budget:,.2f} exceeds available excess liquidity ${float(excess):,.2f}"
    return None


def _strategy_list_payload(state) -> dict:
    """The full strategy_list message clients use to repaint the Strategies tab."""
    return {
        "type": "strategy_list",
        "data": {
            "strategies": [s.to_dict() for s in state.strategies.values()],
            "kill_switch": state.auto_trade_kill_switch,
        },
    }


async def broadcast(state, message: dict):
    """Feed the alert bridge, then enqueue on every client's channel. Never awaits a socket."""
    t0 = time.perf_counter()
    # Feed the Discord alert observer (no-op when None / not configured).
    bridge = getattr(state, "alert_bridge", None)
    if bridge is not None:
        try:
            bridge.forward(message)
        except Exception as e:
            logger.error(f"AlertBridge forward error: {e}")
    if not state.ws_clients:
        return
    stamped = stamp(message)
    text = encode(stamped)
    if text is None:
        return
    for channel in list(state.ws_clients.values()):
        channel.enqueue(stamped, text)
    perf.record("push.broadcast", (time.perf_counter() - t0) * 1000.0)


def make_broadcast_fn(state):
    """Return a broadcast(message) coroutine bound to the given state."""
    async def _broadcast(message: dict):
        await broadcast(state, message)
    return _broadcast


async def _cancel_and_reply(ib, state, order_id: int, out) -> None:
    """Run the confirmed cancel off the read loop; reply on the client's channel when it settles."""
    try:
        resp = await handle_cancel_order(ib, state, order_id, refresh_fn=refresh_account_state)
    except Exception as e:
        logger.error(f"cancel_order error: {e}", exc_info=True)
        resp = {"type": "order_status", "data": {"status": "Error", "orderId": order_id, "message": str(e)}}
    data = resp.get("data")
    if isinstance(data, dict):
        # Every cancel reply names its order and says it is a cancel reply: place_order replies share the
        # `order_status` type, and the browser must never take one for the other (even an early error that
        # has no order id of its own).
        data["action"] = "cancel"
        data.setdefault("orderId", order_id)
    out.send_message(resp)      # a no-op once the client has gone


def _serialize_ib_error_contract(contract) -> dict | None:
    if contract is None:
        return None
    combo_legs = []
    for combo_leg in getattr(contract, "comboLegs", []) or []:
        combo_legs.append({
            "conId": getattr(combo_leg, "conId", None),
            "ratio": getattr(combo_leg, "ratio", None),
            "action": getattr(combo_leg, "action", None),
            "exchange": getattr(combo_leg, "exchange", None),
        })
    return {
        "conId": getattr(contract, "conId", None),
        "symbol": getattr(contract, "symbol", None),
        "secType": getattr(contract, "secType", None),
        "exchange": getattr(contract, "exchange", None),
        "currency": getattr(contract, "currency", None),
        "lastTradeDateOrContractMonth": getattr(contract, "lastTradeDateOrContractMonth", None),
        "strike": getattr(contract, "strike", None),
        "right": getattr(contract, "right", None),
        "localSymbol": getattr(contract, "localSymbol", None),
        "tradingClass": getattr(contract, "tradingClass", None),
        "comboLegs": combo_legs,
    }


def make_ib_error_handler(state, broadcast_fn):
    """Return a synchronous IB error callback that forwards actionable errors to clients."""

    def _on_ib_error(req_id, error_code, error_string, contract=None, *args):
        if error_code in _IGNORED_IB_ERROR_CODES:
            return

        resolved_contract = contract
        if resolved_contract is None and isinstance(req_id, int):
            trade = state.active_trades.get(req_id)
            if trade is not None:
                resolved_contract = getattr(trade, "contract", None)

        payload = {
            "type": "ib_error",
            "data": {
                "reqId": req_id,
                "orderId": req_id if isinstance(req_id, int) and req_id > 0 else None,
                "errorCode": error_code,
                "message": error_string,
                "contract": _serialize_ib_error_contract(resolved_contract),
            },
        }
        logger.warning("IB error reqId=%s code=%s: %s", req_id, error_code, error_string)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(broadcast_fn(payload))
        except RuntimeError:
            asyncio.create_task(broadcast_fn(payload))

    return _on_ib_error


def maybe_log_perf(last_ts: float, now: float, interval: float) -> float:
    """Log the perf summary when ``interval`` seconds passed since ``last_ts``; return the new stamp."""
    if interval <= 0 or now - last_ts < interval:
        return last_ts
    line = perf.summary_line()
    if line:
        logger.info(line)
    return now


async def status_push_loop(state, broadcast_fn):
    """Push status updates every few seconds."""
    last_perf_log = time.monotonic()
    while True:
        try:
            await asyncio.sleep(5)
            update_vix(state)
            await broadcast_fn({"type": "vix_update", "data": {"vix": state.vix}})

            if not is_within_rth() and state.data_mode == "live":
                state.data_mode = "historical"

            status = {
                "type": "status",
                "data": {
                    "connected": state.connected,
                    "market_status": market_status(),
                    "expiration": get_expiration_display(state.expiration) if state.expiration else "N/A",
                    "chain_fetching": state.chain_fetching,
                    "last_chain_update": state.last_chain_update or "Never",
                    "spot_price": round(state.spx_price, 2),
                    "price_history_len": len(state.price_history),
                    "data_mode": state.data_mode,
                    "historical_date": state.historical_date,
                    "es_derived": state.es_derived,
                    "es_price": round(state.es_price, 2) if state.es_price > 0 else None,
                },
            }
            await broadcast_fn(status)
            last_perf_log = maybe_log_perf(last_perf_log, time.monotonic(), config.PERF_LOG_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Status push error: {e}")
            await asyncio.sleep(5)


async def websocket_endpoint(ws: WebSocket, ib, state, broadcast_fn):
    """Handle a single WebSocket client connection.

    Called from the FastAPI route; ib, state, and broadcast_fn are injected
    by the server module.
    """
    await ws.accept()
    out = ClientChannel(ws, on_close=lambda _ch: state.ws_clients.pop(ws, None))
    state.ws_clients[ws] = out
    out.start()
    logger.info(f"WebSocket client connected (total: {len(state.ws_clients)})")

    try:
        # Send initial state
        init_msg = {
            "type": "init",
            "data": {
                "connected": state.connected,
                "market_status": market_status(),
                "expiration": get_expiration_display(state.expiration) if state.expiration else "N/A",
                "spot_price": round(state.spx_price, 2),
                "price": snapshot_payload(state, is_within_rth()),
                "gex": state.latest_gex,
                "last_chain_update": state.last_chain_update or "Never",
                "data_mode": state.data_mode,
                "historical_date": state.historical_date,
                "es_derived": state.es_derived,
                "es_price": round(state.es_price, 2) if state.es_price > 0 else None,
                "chain_quotes": state.chain_quotes_cache,
                "account": build_account_payload(state),
                "gex_mode": state.gex_mode,
                "monthly_gex": state.monthly_latest_gex,
                "monthly_expiration": get_expiration_display(state.monthly_expiration) if state.monthly_expiration else "N/A",
            }
        }
        out.send_message(init_msg)
        # Push the strategy list on connect so a client opening any tab directly
        # (e.g. #sim) receives strategies without first visiting the Strategies
        # tab — otherwise its strategy dropdown stays empty. If the boot path never
        # reached load_strategies() (e.g. IB was down), load from disk now.
        if not state.strategies:
            state.strategies = load_strategies()
        out.send_message(_strategy_list_payload(state))

        while not out.closed:      # the channel closed itself (slow or gone client): stop reading
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)

                if msg == "refresh_chain":
                    logger.info("Client requested chain refresh")
                    state.contracts.relist()
                    if state.force_chain_fetch_event is not None:
                        state.force_chain_fetch_event.set()

                elif msg == "set_tab:chain":
                    state.active_tab = "chain"
                    state.viewport_center_strike = 0.0
                    logger.info("Client active tab: chain")

                elif msg == "set_tab:dashboard":
                    state.active_tab = "dashboard"
                    state.viewport_center_strike = 0.0
                    logger.info("Client active tab: dashboard")

                elif msg == "set_gex_mode:monthly":
                    state.gex_mode = "monthly"
                    logger.info("GEX mode set to monthly")
                    asyncio.create_task(monthly_gex_fetch(ib, state, broadcast_fn))

                elif msg == "set_gex_mode:0dte":
                    state.gex_mode = "0dte"
                    logger.info("GEX mode set to 0DTE")
                    # Re-send cached 0DTE GEX immediately
                    if state.latest_gex:
                        gex_payload = dict(state.latest_gex)
                        gex_payload["es_derived"] = state.es_derived
                        await broadcast_fn({"type": "gex", "data": gex_payload})

                elif msg == "set_tab:account":
                    state.active_tab = "account"
                    logger.info("Client active tab: account")
                    refresh_account_state(ib, state)
                    out.send_message({
                        "type": "account_update",
                        "data": build_account_payload(state),
                    })

                elif msg == "set_tab:strategies":
                    state.active_tab = "strategies"
                    logger.info("Client active tab: strategies")
                    state.strategies = load_strategies()
                    out.send_message(_strategy_list_payload(state))

                elif msg == "set_tab:log":
                    state.active_tab = "log"
                    # Push current log backlog so a freshly-opened console fills up.
                    out.send_message({"type": "log_history", "data": list(state.log_buffer)})

                elif msg == "set_tab:sim":
                    state.active_tab = "sim"

                elif msg.startswith("strategy_save:"):
                    try:
                        body = json.loads(msg.split(":", 1)[1])
                        strat = Strategy.from_dict(body)
                        err = _budget_error(state, strat.budget)
                        if err:
                            out.send_message({"type": "strategy_error", "data": {"message": err}})
                            continue
                        state.strategies[strat.name] = strat
                        save_strategy(None, strat)
                        out.send_message(_strategy_list_payload(state))
                    except Exception as e:
                        logger.error(f"strategy_save error: {e}", exc_info=True)

                elif msg.startswith("strategy_delete:"):
                    name = _unquote_name(msg.split(":", 1)[1])
                    state.strategies.pop(name, None)
                    delete_strategy(None, name)

                elif msg.startswith("strategy_arm:"):
                    s = state.strategies.get(_unquote_name(msg.split(":", 1)[1]))
                    if s is not None:
                        s.armed = True
                        reset_strategy_runtime(state, s.name)
                        out.send_message(_strategy_list_payload(state))

                elif msg.startswith("strategy_disarm:"):
                    s = state.strategies.get(_unquote_name(msg.split(":", 1)[1]))
                    if s is not None:
                        s.armed = False
                        out.send_message(_strategy_list_payload(state))

                elif msg.startswith("strategy_kill_switch:"):
                    state.auto_trade_kill_switch = msg.split(":", 1)[1] == "true"

                elif msg.startswith("place_order:"):
                    try:
                        payload = json.loads(msg.split(":", 1)[1])
                        resp = await handle_place_order(
                            ib, state, payload, ws=out,
                            refresh_fn=refresh_account_state,
                        )
                        out.send_message(resp)
                    except Exception as e:
                        logger.error(f"place_order error: {e}", exc_info=True)
                        out.send_message({
                            "type": "order_status",
                            "data": {"status": "Error", "message": str(e)},
                        })

                elif msg.startswith("cancel_order:"):
                    try:
                        order_id = int(msg.split(":", 1)[1])
                    except (TypeError, ValueError):
                        out.send_message({"type": "order_status",
                                          "data": {"status": "Error", "action": "cancel", "message": "Bad order id"}})
                        continue
                    # Off the read loop: the confirmed cancel can wait seconds for IB.
                    task = asyncio.create_task(_cancel_and_reply(ib, state, order_id, out))
                    _CANCEL_TASKS.add(task)
                    task.add_done_callback(_CANCEL_TASKS.discard)

                elif msg.startswith("perf_report:"):
                    try:
                        record_client_perf(msg.split(":", 1)[1])
                    except Exception as e:      # a bad report never ends the connection
                        logger.debug(f"perf_report ignored: {e!r}")

                elif msg.startswith("viewport_center:"):
                    try:
                        strike = float(msg.split(":", 1)[1])
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(strike) or strike <= 0:
                        continue
                    now_mono = time.monotonic()
                    if (now_mono - state.viewport_center_last_ts) < VIEWPORT_CENTER_MIN_INTERVAL:
                        continue
                    state.viewport_center_last_ts = now_mono
                    state.viewport_center_strike = round(strike, 1)

            except asyncio.TimeoutError:
                out.send_message({"type": "ping"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
    finally:
        # Unregister first so broadcasts stop feeding a closing channel, then flush what is queued.
        state.ws_clients.pop(ws, None)
        await out.aclose(drain=True)
        logger.info(f"WebSocket client disconnected (total: {len(state.ws_clients)})")
