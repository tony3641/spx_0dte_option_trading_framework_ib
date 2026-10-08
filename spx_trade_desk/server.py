"""
SPX 0DTE Dashboard Server — Thin Entrypoint

All functional logic lives in dedicated modules:
  config, app_state, ib_connection, account_manager, order_manager,
  price_bars, chain_manager, ws_handler

This file wires them together via FastAPI lifespan, HTTP routes,
and the WebSocket endpoint.
"""

import asyncio
import logging
import os
import signal
import sys
from typing import Optional

# Must be BEFORE any event-loop creation
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import nest_asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import Body, FastAPI, WebSocket, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from spx_trade_desk.ib.client import IBClient
from spx_trade_desk.ib.session import boot_session, start_background_loops

from spx_trade_desk.core import config
from spx_trade_desk.resources import CHAIN_LIBRARY_DIR, STATIC_DIR
from spx_trade_desk.core.app_state import AppState
from spx_trade_desk.core.perf import perf
from spx_trade_desk.ib.account import (
    refresh_account_state, build_account_payload,
)
from spx_trade_desk.market.chain_recorder import ChainRecorder
from spx_trade_desk.web.ws import (
    broadcast, drain_detached_tasks, make_broadcast_fn, make_ib_error_handler,
    websocket_endpoint as ws_endpoint,
)
from spx_trade_desk.market.hours import market_status, get_expiration_display
from spx_trade_desk.core.log_buffer import LogStoreHandler
from spx_trade_desk.discord.settings import (
    DiscordSettings, DiscordSettingsManager, load_initial_settings,
)
from spx_trade_desk.core.env_store import update_env
from spx_trade_desk.sim import jobs
from spx_trade_desk.sim import parallel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# ibapi 10.45 logs every protobuf callback at INFO ("ANSWER tickPriceProtoBuf ..."): thousands of
# records a second flood the Log tab ring buffer and push our own warnings out of it.
logging.getLogger("ibapi").setLevel(logging.WARNING)
logger = logging.getLogger("server")

# ---------------------------------------------------------------------------
# Module-level wiring (created in lifespan, used by routes)
# ---------------------------------------------------------------------------
ib: Optional[IBClient] = None
state = AppState()
broadcast_fn = None  # set in lifespan
chain_recorder = ChainRecorder(CHAIN_LIBRARY_DIR, source="dashboard")
discord_manager: Optional[DiscordSettingsManager] = None


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app):
    """Startup and shutdown logic."""
    global ib, broadcast_fn
    logger.info("Starting SPX 0DTE GEX Dashboard...")

    # Capture every framework log record into the Log console's ring buffer.
    logging.getLogger().addHandler(LogStoreHandler(state))

    loop = asyncio.get_event_loop()
    nest_asyncio.apply(loop)
    ib = IBClient()
    broadcast_fn = make_broadcast_fn(state)

    # Discord bot (in-process, on the same loop). Owned by the manager so
    # IB reconnects (which cancel state.background_tasks) never kill it.
    # Constructed OUTSIDE the IB setup try: Discord startup/config must not
    # depend on IB health, and the settings endpoints need the manager to
    # exist even when IB boot fails.
    global discord_manager
    discord_manager = DiscordSettingsManager(ib, state)
    try:
        # persist=False: config-derived boot never writes back to .env — only
        # UI-authored applies persist (otherwise empty DISCORD_* keys would
        # shadow params.yaml on later boots).
        result = await discord_manager.apply(load_initial_settings(),
                                             persist=False)
        if result.get("ok"):
            logger.info("Discord bot started" if result["running"]
                        else "Discord disabled (no token)")
        else:
            logger.warning(f"Discord not started: {result.get('error')}")
    except Exception as e:
        logger.error(f"Failed to start Discord bot: {e}", exc_info=True)

    try:
        await boot_session(ib, state, first_boot=True,
                           error_handler=make_ib_error_handler(state, broadcast_fn))
        start_background_loops(ib, state, broadcast_fn, recorder=chain_recorder)
        logger.info("All background tasks started")

    except Exception as e:
        logger.error(f"Startup failed: {e}", exc_info=True)

    yield  # App is running

    # Shutdown
    logger.info("Shutting down...")
    for task in state.background_tasks:
        task.cancel()
    await asyncio.gather(*state.background_tasks, return_exceptions=True)
    try:
        await drain_detached_tasks()          # cancel replies and socket closes started by connections
    except Exception as e:
        logger.warning(f"Error draining detached tasks: {e}")
    if discord_manager is not None:
        try:
            await discord_manager.stop()
        except Exception as e:
            logger.warning(f"Error stopping Discord bot: {e}")
    if getattr(ib, "connected", False):
        ib.disconnect()
        logger.info("Disconnected from IB")


# ---------------------------------------------------------------------------
# Create app & HTTP routes
# ---------------------------------------------------------------------------
app = FastAPI(title="SPX 0DTE Option Dashboard", lifespan=lifespan)


@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_state():
    """Return current full state (for initial page load / reconnection)."""
    return {
        "connected": state.connected,
        "market_status": market_status(),
        "expiration": get_expiration_display(state.expiration) if state.expiration else "N/A",
        "spot_price": round(state.spx_price, 2),
        "price_history": list(state.price_history),
        "gex": state.latest_gex,
        "last_chain_update": state.last_chain_update or "Never",
        "data_mode": state.data_mode,
        "historical_date": state.historical_date,
        "es_derived": state.es_derived,
        "es_price": round(state.es_price, 2) if state.es_price > 0 else None,
        "gex_mode": state.gex_mode,
        "monthly_gex": state.monthly_latest_gex,
        "monthly_expiration": get_expiration_display(state.monthly_expiration) if state.monthly_expiration else "N/A",
    }


@app.get("/api/perf")
async def get_perf(request: Request):
    """IB-layer timing spans and counters (localhost only)."""
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    return perf.snapshot()


async def reconnect_ib_on(port: int) -> dict:
    """Reconnect IB on `port` (validated by callers). Same teardown/reconnect/
    restart behavior the /api/reconnect_ib endpoint had."""
    global ib
    logger.info(f"Reconnecting to IB on port {port}")
    state.connected = False

    # Stop the old background loops up front. If the reconnect fails below, we
    # must not leave them running against the (soon to be disconnected) old
    # client — cancel and clear them now so a failure is atomic: no loops, but
    # a consistent fresh client, and a retry recovers cleanly.
    for task in state.background_tasks:
        task.cancel()
    await asyncio.gather(*state.background_tasks, return_exceptions=True)
    state.background_tasks.clear()

    # Tear down every active market-data stream on the old client (the native
    # bridge tracks subscriptions by reqId, not per-contract).
    try:
        ib.unsubscribe_all()
    except Exception:
        pass
    state.chain_stream_tickers.clear()
    state.chain_stream_contracts.clear()
    state.contracts.clear()
    state.quote_book.reset("")
    state.chain_quotes_cache = None     # the engine must not score against the old session's quotes

    # Disconnect the old client, then swap in a fresh IBClient — the native
    # bridge does not support re-connecting a disconnected instance.
    try:
        ib.disconnect()
        logger.info("Disconnected IB before reconnecting")
    except Exception as e:
        logger.warning(f"Error disconnecting IB before reconnect: {e}")
    ib = IBClient()
    if discord_manager is not None:
        discord_manager.ib = ib   # keep the manager on the live client

    try:
        await boot_session(ib, state, first_boot=False, port=port,
                           error_handler=make_ib_error_handler(state, broadcast_fn))
        # The loops include the strategy engine and take-profit so a manual reconnect does NOT
        # silently stop auto-trading or position management.
        start_background_loops(ib, state, broadcast_fn, recorder=chain_recorder)
        if state.force_chain_fetch_event is not None:
            state.force_chain_fetch_event.set()
        await broadcast(state, {"type": "status", "data": {
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
        }})
    except Exception as e:
        logger.error(f"Reconnect IB failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "ok", "port": port}


# ---------------------------------------------------------------------------
# Settings endpoints (localhost-only)
# ---------------------------------------------------------------------------
def _is_localhost(request: Request) -> bool:
    client = request.client
    return client is not None and client.host in ("127.0.0.1", "::1")


class DiscordSettingsIn(BaseModel):
    token: Optional[str] = None   # None = keep existing, "" = clear, str = set
    guild_id: str = ""
    channel_id: str = ""
    allowed_user_ids: str = ""
    allowed_role: str = ""


class IbSettingsIn(BaseModel):
    port: int


class SimSettingsIn(BaseModel):
    workers: int


@app.get("/api/settings/discord")
async def get_discord_settings(request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    if discord_manager is None:
        raise HTTPException(status_code=503,
                            detail="Discord settings unavailable (server startup incomplete)")
    s = discord_manager.settings
    return {
        "token_set": bool(s.token),
        "token_hint": ("…" + s.token[-4:]) if s.token else None,
        "guild_id": s.guild_id,
        "channel_id": s.channel_id,
        "allowed_user_ids": list(s.allowed_user_ids),
        "allowed_role": s.allowed_role,
        "running": discord_manager.running,
    }


@app.post("/api/settings/discord")
async def post_discord_settings(body: DiscordSettingsIn, request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    if discord_manager is None:
        raise HTTPException(status_code=503,
                            detail="Discord settings unavailable (server startup incomplete)")
    cur = discord_manager.settings
    ids = config.parse_user_ids(body.allowed_user_ids)
    if body.allowed_user_ids.strip() and not ids:
        raise HTTPException(status_code=400,
                            detail="No valid user IDs (comma-separated digits)")
    if body.guild_id.strip() and not body.guild_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="Guild ID must be numeric")
    if body.channel_id.strip() and not body.channel_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="Channel ID must be numeric")
    new = DiscordSettings(
        token=body.token if body.token is not None else cur.token,
        guild_id=body.guild_id.strip(),
        channel_id=body.channel_id.strip(),
        allowed_user_ids=ids,
        allowed_role=body.allowed_role.strip(),
    )
    result = await discord_manager.apply(new)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Apply failed"))
    return result


@app.get("/api/settings/ib")
async def get_ib_settings(request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    return {"port": state.ib_port, "connected": state.connected}


@app.post("/api/settings/ib")
async def post_ib_settings(body: IbSettingsIn, request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    if body.port <= 0 or body.port > 65535:
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")
    try:
        await reconnect_ib_on(body.port)
    except Exception as e:
        logger.error(f"IB reconnect to port {body.port} failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    persisted = True
    try:
        update_env({"IB_PORT": str(body.port)})
    except Exception as e:
        logger.warning(f"Failed to persist IB_PORT to .env: {e}")
        persisted = False
    return {"ok": True, "port": body.port, "persisted": persisted}


@app.get("/api/settings/sim")
async def get_sim_settings(request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    return {"workers": parallel.configured_workers(),
            "cpu_count": os.cpu_count() or 1}


@app.post("/api/settings/sim")
async def post_sim_settings(body: SimSettingsIn, request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    if body.workers < 0:
        raise HTTPException(status_code=400,
                            detail="workers must be >= 0 (0 = auto)")
    # Hot-apply first: resolve_workers reads the live env, so the next run picks
    # this up without a restart; the .env write only makes it survive restarts.
    os.environ["SIM_WORKERS"] = str(body.workers)
    persisted = True
    try:
        update_env({"SIM_WORKERS": str(body.workers)})
    except Exception as e:
        logger.warning(f"Failed to persist SIM_WORKERS to .env: {e}")
        persisted = False
    return {"ok": True, "workers": body.workers,
            "cpu_count": os.cpu_count() or 1, "persisted": persisted}


# ---------------------------------------------------------------------------
# WebSocket endpoint (delegates to ws_handler)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_route(ws: WebSocket):
    await ws_endpoint(ws, ib, state, broadcast_fn)


# ---------------------------------------------------------------------------
# Simulation (intraday MC stress test)
# ---------------------------------------------------------------------------

@app.post("/api/sim/run")
async def api_sim_run(body: dict = Body(...)):
    try:
        out = jobs.start_run(body, state=state, ib=ib)
        return out
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except RuntimeError as e:
        return JSONResponse(status_code=409, content={"detail": str(e)})


@app.get("/api/sim/status/{job_id}")
async def api_sim_status(job_id: str):
    try:
        return jobs.get_status(job_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "unknown job"})


@app.get("/api/sim/result/{job_id}")
async def api_sim_result(job_id: str):
    try:
        result = jobs.get_result(job_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "unknown job"})
    if result is None:
        return JSONResponse(status_code=409, content={"detail": "job not finished"})
    return result


@app.post("/api/sim/cancel/{job_id}")
async def api_sim_cancel(job_id: str):
    return {"cancelled": jobs.cancel(job_id)}


@app.get("/api/sim/pricing")
async def api_sim_pricing(tier: str = "auto"):
    from spx_trade_desk.sim import library as sim_library
    try:
        return await asyncio.to_thread(sim_library.pricing_summary, tier)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.post("/api/sim/pricing/rebuild")
async def api_sim_pricing_rebuild():
    from spx_trade_desk.sim import library as sim_library
    try:
        await asyncio.to_thread(sim_library.build_and_write)
    except Exception as e:
        logger.exception("Pricing library rebuild failed")
        return JSONResponse(status_code=500, content={"detail": f"rebuild failed: {e}"})
    return await asyncio.to_thread(sim_library.pricing_summary)


# ---------------------------------------------------------------------------
# Static files (must be after routes)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    nest_asyncio.apply(loop)

    uvi_config = uvicorn.Config(
        app,
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        log_level="info",
        loop="none",
    )
    server = uvicorn.Server(uvi_config)

    shutdown_requested = {"value": False}

    def _handle_shutdown_signal(signum, _frame):
        try:
            signame = signal.Signals(signum).name
        except Exception:
            signame = str(signum)

        if not shutdown_requested["value"]:
            shutdown_requested["value"] = True
            logger.info(f"Shutdown signal received ({signame}); stopping server gracefully...")
            server.should_exit = True
        else:
            logger.warning(f"Second shutdown signal received ({signame}); forcing exit...")
            server.force_exit = True

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_shutdown_signal)
            except Exception as e:
                logger.debug(f"Unable to register handler for {sig_name}: {e}")

    logger.info(f"Server starting on {config.SERVER_HOST}:{config.SERVER_PORT}")
    if config.SERVER_HOST == "0.0.0.0":
        logger.info(f"  Local access    : http://localhost:{config.SERVER_PORT}")
        logger.info(f"  Network access  : http://<your-local-ip>:{config.SERVER_PORT}")
    else:
        logger.info(f"  Access at       : http://{config.SERVER_HOST}:{config.SERVER_PORT}")

    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; shutting down...")
        server.should_exit = True
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logger.info("Server shutdown complete")
