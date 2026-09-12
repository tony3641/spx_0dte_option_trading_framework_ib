"""Simulation job registry: background execution, progress, cancel, memoized calibration."""
import asyncio
import logging
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

import numpy as np

from spx_trade_desk.sim.sim_calibrate import CalibratedModel, build_dynamics, calibrate
from spx_trade_desk.sim.sim_config import SimRunConfig, sweep_cells
from spx_trade_desk.sim.sim_data import BarSeries, load_bars
from spx_trade_desk.sim.sim_parallel import chunk_payloads, compute_chunk, resolve_workers, spawn_pool
from spx_trade_desk.sim.sim_pricing import build_ladder
from spx_trade_desk.sim.sim_risk import build_cell_payload, spot_fan_quantiles

logger = logging.getLogger(__name__)

_STRATEGY_CACHE: Dict[str, "Strategy"] = {}     # name -> Strategy (filled by _get_strategy)
_BARS_CACHE: Dict[tuple, BarSeries] = {}
_CALIB_CACHE: Dict[tuple, CalibratedModel] = {}

_registry: Dict[str, dict] = {}
_busy: Optional[str] = None
_lock = threading.Lock()
_REGISTRY_MAX_KEPT = 10          # spec §7: keep the last ~10 finished results in memory
_TERMINAL_STATES = ("done", "error", "cancelled")


def reset_registry() -> None:
    global _busy
    with _lock:
        _registry.clear()
        _busy = None


def _prune_registry(max_keep: int = _REGISTRY_MAX_KEPT) -> None:
    """Drop the oldest COMPLETED jobs beyond the most recent ``max_keep``.

    Never drops a job that is still active (queued/loading/calibrating/simulating)
    or whose thread may yet write to it; only terminal states are candidates.
    """
    if max_keep < 1:
        return
    with _lock:
        finished = sorted(
            (job["created"], jid) for jid, job in _registry.items()
            if job.get("state") in _TERMINAL_STATES)
        for _, jid in finished[:-max_keep]:
            _registry.pop(jid, None)


def get_status(job_id: str) -> dict:
    job = _registry[job_id]           # KeyError -> 404 at the API layer
    return dict(state=job["state"], progress=job["progress"], message=job["message"])


def get_result(job_id: str) -> dict:
    return _registry[job_id]["result"]


def cancel(job_id: str) -> bool:
    job = _registry.get(job_id)
    if job is None:
        return False
    job["cancelled"] = True
    return True


def start_run(cfg_dict: dict, state=None, ib=None) -> dict:
    global _busy
    cfg = SimRunConfig.from_dict(cfg_dict)
    cfg.validate()
    with _lock:
        if _busy is not None and _registry.get(_busy, {}).get("state") in (
                "queued", "loading", "calibrating", "simulating"):
            raise RuntimeError("a simulation job is already running")
        job_id = uuid.uuid4().hex[:12]
        _busy = job_id
        _registry[job_id] = dict(id=job_id, cfg=cfg, state="queued", progress=0.0,
                                 message="", result=None, cancelled=False,
                                 created=time.time())
    job = _registry[job_id]
    try:
        loop = asyncio.get_running_loop()
        job["loop"] = loop
        try:
            from spx_trade_desk.server import broadcast_fn   # module-level bound WS coroutine (Conflict N-b)
            job["broadcast_fn"] = broadcast_fn
        except Exception:
            job["broadcast_fn"] = None
    except RuntimeError:
        pass  # no running loop (scripts): job still runs detached; WS push stays disabled
    # Keep the in-memory registry bounded (spec §7): finished jobs beyond the most
    # recent ~10 are dropped before a new run is admitted. Active jobs are untouched.
    _prune_registry()
    # Run the job on a dedicated daemon thread, NOT as an asyncio task on the caller's loop.
    # A loop-tied task (asyncio.to_thread) is drained by the request's event loop, so a
    # request that starts a job would block until the job finishes and the busy-guard in
    # the API layer could never observe a second run while the first is in flight
    # (TestClient closes each request's portal and waits on its pending tasks).
    try:
        threading.Thread(target=_execute, args=(job_id, state, ib), daemon=True).start()
    except Exception as e:
        # A failed start would strand `_busy` (nothing reaches _execute's finally to
        # clear it) and leave a queued job that can never finish. Reset and error it.
        with _lock:
            if _busy == job_id:
                _busy = None
        job["state"] = "error"
        job["message"] = f"failed to start sim worker thread: {e}"
    return {"job_id": job_id}


def _load_bars_for(cfg: SimRunConfig) -> Optional[BarSeries]:
    key = (cfg.source, cfg.csv_path, cfg.bar_size, cfg.lookback_days)
    return _BARS_CACHE.get(key)


def _get_strategy(cfg: SimRunConfig, state=None):
    from spx_trade_desk.strategy.strategy_models import Strategy
    if state is not None and cfg.strategy_name in getattr(state, "strategies", {}):
        return state.strategies[cfg.strategy_name]
    if cfg.strategy_name in _STRATEGY_CACHE:
        return _STRATEGY_CACHE[cfg.strategy_name]
    from spx_trade_desk.strategy.strategy_store import load_strategies   # same file the live engine uses
    strategies = load_strategies()
    for s in strategies.values():
        _STRATEGY_CACHE[s.name] = s
    if cfg.strategy_name not in _STRATEGY_CACHE:
        raise ValueError(f"unknown strategy: {cfg.strategy_name}")
    return _STRATEGY_CACHE[cfg.strategy_name]


def execute_pipeline(cfg: SimRunConfig, bars: BarSeries, progress_cb: Callable,
                     spot0: float, cancel_check: Optional[Callable[[], bool]] = None,
                     state=None) -> dict:
    """Full pipeline over all sweep cells. Deterministic per (cfg, bars, spot0).

    Chunks run on a process pool when the resolved worker count allows; both
    paths share ``sim_parallel.compute_chunk`` and reassemble strictly by
    (cell, chunk) index, so a parallel run is bit-identical to the serial one.
    """
    cancelled = False
    if cancel_check is None:
        cancel_check = lambda: False   # noqa: E731
    progress_cb(0.0, "calibrating")
    key = (cfg.source, cfg.csv_path, cfg.bar_size, cfg.lookback_days)
    model = _CALIB_CACHE.get(key)
    if model is None:
        model = calibrate(bars, cfg)
        _CALIB_CACHE[key] = model
    strat = _get_strategy(cfg, state)
    children = []
    if cfg.mode == "family" and state is not None:
        children = [s for s in state.strategies.values() if s.parent_name == strat.name]
    ladder = build_ladder(spot0, cfg.ladder_range_pct)
    dyn = build_dynamics(model, cfg)   # per-run dials; NOT cached with the model
    cells = sweep_cells(cfg)
    n_chunks = (cfg.n_paths + cfg.chunk_size - 1) // cfg.chunk_size
    payloads = chunk_payloads(cfg, model, strat, children, ladder, dyn, cells,
                              n_chunks, spot0)
    workers = resolve_workers(cfg, len(payloads))

    # Index-keyed aggregation: a cell's payload is built only once every one of
    # its chunks is in, in chunk order — completion order never reaches a result.
    results: List[Optional[dict]] = [None] * len(cells)
    spx_chunks: List[Optional[np.ndarray]] = [None] * n_chunks
    cell_trials: Dict[int, List[Optional[list]]] = {
        ci: [None] * n_chunks for ci in range(len(cells))}
    pending = [n_chunks] * len(cells)
    done = 0

    def consume(res: dict) -> None:
        nonlocal done
        ci, ch = res["ci"], res["ch"]
        # Land each chunk's trials at its chunk index. imap_unordered completes
        # chunks in arbitrary order, so extending on arrival would make a cell's
        # trial order (and its bootstrap/DD stats) depend on worker timing.
        cell_trials[ci][ch] = res["trials"]
        if res["spots"] is not None:
            spx_chunks[ch] = res["spots"]
        pending[ci] -= 1
        done += 1
        if pending[ci] == 0:
            trials = [t for chunk in cell_trials.pop(ci) for t in chunk]
            results[ci] = build_cell_payload(
                trials, cfg,
                sl_multiplier=cells[ci]["sl_multiplier"], k=cells[ci]["k"])
        progress_cb(done / len(payloads),
                    f"cell {ci + 1}/{len(cells)} chunk {ch + 1}/{n_chunks}")

    if workers <= 1:
        for payload in payloads:
            if cancel_check():
                cancelled = True
                break
            consume(compute_chunk(payload))
    else:
        pool = spawn_pool(workers)
        clean = False
        try:
            for res in pool.imap_unordered(compute_chunk, payloads, chunksize=1):
                consume(res)
                if cancel_check():
                    cancelled = True
                    break
            clean = not cancelled
        finally:
            # Cancel or a worker crash kills the in-flight chunks (they are pure
            # compute, nothing to unwind); a completed run drains instead. Either way,
            # a cell with an unfinished chunk stays None and is dropped below.
            if clean:
                pool.close()
            else:
                pool.terminate()
            pool.join()
    meta = dict(strategy=cfg.strategy_name, mode=cfg.mode, source=bars.source,
                bar_size=cfg.bar_size, steps_per_day=cfg.steps_per_day(),
                n_paths=cfg.n_paths, seed=cfg.seed, spot0=spot0,
                garch=vars(model.garch),
                garch_warnings=model.warnings, data_warnings=bars.warnings,
                smile=vars(model.smile), dials=dict(nu_override=cfg.nu_override,
                                                    gamma_mult=cfg.gamma_mult,
                                                    vol_beta=cfg.vol_beta, flat_iv=cfg.flat_iv,
                                                    atm_iv=cfg.atm_iv,
                                                    vol_cap_mult=cfg.vol_cap_mult,
                                                    skew_beta=cfg.skew_beta,
                                                    skew_t_gamma=cfg.skew_t_gamma,
                                                    atm_budget=cfg.atm_budget,
                                                    budget_beta=cfg.budget_beta),
                workers=workers, cancelled=cancelled)
    # SPX path fan: a property of the market simulation, not of any sweep cell (spot
    # dynamics ignore SL/k), so the first cell's full path set represents the run. A
    # cancelled run keeps whatever chunks completed before the cancel.
    have = [c for c in spx_chunks if c is not None]
    spx_mat = np.vstack(have) if have else np.zeros((0, 0))
    progress_cb(1.0, "done" if not cancelled else "cancelled")
    return dict(meta=meta, cells=[r for r in results if r is not None],
                spx_fan=spot_fan_quantiles(spx_mat))


def _execute(job_id: str, state, ib=None) -> None:
    """Run a job on the worker thread.

    ``ib`` is UNUSED / reserved: the simulator rejects ``source='ib'`` in
    SimRunConfig.validate(), so bar data here always comes from csv/yfinance.
    """
    global _busy
    job = _registry[job_id]
    cfg: SimRunConfig = job["cfg"]

    def progress(p, msg):
        job["progress"], job["message"] = float(p), msg
        # Spec §7 state machine: expose the pipeline's "calibrating" phase (the loader
        # already sets loading; the sweep loop maps to simulating) instead of only ever
        # reporting loading/simulating while the message text says calibrating.
        if msg == "calibrating":
            job["state"] = "calibrating"
        elif msg.startswith("cell "):
            job["state"] = "simulating"
        _push_ws(job, p, msg)

    try:
        job["state"] = "loading"
        bars = _load_bars_for(cfg)
        if bars is None:
            # No IB path exists on the worker thread; only the sync layered loader
            # (csv/yfinance) is reachable here.
            bars = load_bars(cfg)
            _BARS_CACHE[(cfg.source, cfg.csv_path, cfg.bar_size, cfg.lookback_days)] = bars
        spot0 = float(cfg.spot0) if cfg.spot0 else float(bars.closes[-1])
        job["state"] = "simulating"
        # Only forward `state` when we have one: server runs carry the AppState (family-mode
        # children + strategy resolution); pure/scripted runs (state=None) must not — the
        # test suite's stub execute_pipeline has no `state` parameter.
        kwargs = {} if state is None else {"state": state}
        result = execute_pipeline(cfg, bars, progress, spot0,
                                  cancel_check=lambda: job["cancelled"], **kwargs)
        job["result"] = result
        # Terminal state from the registry flag (== result meta.cancelled for the real
        # pipeline, both driven by job["cancelled"]). A stub execute_pipeline may omit
        # `cancelled` from its meta, so never index it here.
        job["state"] = "cancelled" if job["cancelled"] else "done"
    except Exception as e:
        logger.exception("sim job failed")
        job["state"], job["message"] = "error", str(e)
    finally:
        with _lock:
            if _busy == job_id:
                _busy = None


def _push_ws(job: dict, progress: float, message: str) -> None:
    """Best-effort WS push; the loop was captured at start_run time when available."""
    loop = job.get("loop")
    broadcast = job.get("broadcast_fn")
    if loop is not None and broadcast is not None:
        try:
            # Controller correction (Conflict N-b): `broadcast` is a bound COROUTINE, so it cannot
            # be passed to call_soon_threadsafe (it would be created and never awaited). Schedule it
            # on the captured loop via a lambda + asyncio.create_task. `_push_ws` runs from the
            # worker thread, so loop.call_soon_threadsafe is the loop-thread-safe bridge.
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(broadcast({"type": "sim_progress",
                                                       "data": {"job_id": job["id"],
                                                                "progress": progress,
                                                                "message": message}})))
        except RuntimeError:
            pass
