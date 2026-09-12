"""Process-pool execution of simulation chunks.

One task = one (sweep cell, chunk) pair. The serial and parallel paths both run
``compute_chunk``, so a parallel run yields the same trials, in the same order,
as the serial path: the RNG stream is a pure function of
``SeedSequence(entropy=cfg.seed, spawn_key=(cell_index, chunk_index))`` and
results are reassembled by index, never by completion order.

Processes (not threads): the hot loop in ``sim_engine.run_exits`` is per-path
Python, which the GIL serializes. Workers must be spawned (Windows default),
so the task payload is pickled — the model, config, strategy and ladder are
plain dataclasses/arrays; the paths are regenerated inside the worker rather
than shipped.
"""
import multiprocessing as mp
import os
from typing import List

import numpy as np

from spx_trade_desk.core import config
from spx_trade_desk.sim.calibrate import CalibratedModel, SmileDynamics
from spx_trade_desk.sim.config import SimRunConfig
from spx_trade_desk.sim.engine import TrialResult, run_cell, run_family
from spx_trade_desk.sim.paths import simulate_chunk
from spx_trade_desk.strategy.models import Strategy


# Auto mode grants one worker per this many paths. A 250-path chunk is ~4s of
# compute, comfortably above the ~2-3s pool startup, so a few hundred paths stay
# in-process and 1000+ fan out. Explicit n_workers/SIM_WORKERS override it.
AUTO_MIN_PATHS_PER_WORKER = 250


def configured_workers() -> int:
    """Operator worker setting, 0 = auto.

    The live ``SIM_WORKERS`` environment value wins (the settings panel writes
    both ``os.environ`` and ``.env``, so it hot-applies without a restart),
    falling back to the ``.env``-backed ``config.SIM_WORKERS``.
    """
    env = os.getenv("SIM_WORKERS")
    if env is not None:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return max(0, int(config.SIM_WORKERS or 0))


def resolve_workers(cfg: SimRunConfig, n_tasks: int) -> int:
    """Worker-process count for a run with ``n_tasks`` (cell, chunk) tasks.

    An explicit setting — ``cfg.n_workers`` per run, else ``SIM_WORKERS`` — wins
    (1 = serial); otherwise auto: CPU count, capped by the task count and by the
    run's size, so a few hundred paths stay in-process where a pool would cost
    more than it saves.
    """
    explicit = cfg.n_workers or configured_workers()
    if explicit > 0:
        return max(1, min(explicit, n_tasks))
    cpu = max(1, os.cpu_count() or 1)
    by_work = max(1, int(cfg.n_paths) // AUTO_MIN_PATHS_PER_WORKER)
    return max(1, min(cpu, n_tasks, by_work))


def compute_chunk(payload: tuple) -> dict:
    """Evaluate one (cell, chunk) task; the worker entry point.

    Payload is a picklable tuple (see ``chunk_payloads``); returns the
    trial list plus this chunk's market paths for the first cell (the SPX fan
    is strategy-agnostic, so only one cell needs to ship them back).
    """
    (cfg, model, strategy, children, ladder, dyn, cell,
     ci, ch, n_here, spot0) = payload
    seed_seq = np.random.SeedSequence(entropy=cfg.seed, spawn_key=(ci, ch))
    paths = simulate_chunk(model, cfg, spot0, n_here, seed_seq)
    if cfg.mode == "family" and children:
        _, total = run_family(model, cfg, strategy, children, paths, ladder, dyn=dyn)
        trials = [TrialResult(entered=True, entry_minute=-1, exit_minute=-1,
                              exit_reason="expired", short_strike=0, long_strike=0,
                              width=0, qty=1, fill_credit=0, exit_debit=0,
                              pnl=float(total[p])) for p in range(n_here)]
    else:
        trials = run_cell(model, cfg, strategy, paths, ladder,
                          sl_multiplier=cell["sl_multiplier"], k=cell["k"], dyn=dyn)
    return dict(ci=ci, ch=ch, trials=trials,
                spots=paths.spots if ci == 0 else None)


def chunk_payloads(cfg: SimRunConfig, model: CalibratedModel, strategy: Strategy,
                   children: List[Strategy], ladder: np.ndarray,
                   dyn: SmileDynamics, cells: List[dict], n_chunks: int,
                   spot0: float) -> List[tuple]:
    """Cartesian (cell, chunk) task list in the serial execution order."""
    out = []
    for ci, cell in enumerate(cells):
        for ch in range(n_chunks):
            n_here = min(cfg.chunk_size, cfg.n_paths - ch * cfg.chunk_size)
            out.append((cfg, model, strategy, children, ladder, dyn, cell,
                        ci, ch, n_here, spot0))
    return out


def spawn_pool(workers: int):
    """Spawn-context worker pool.

    Explicit spawn on every platform: the sim runs inside the server's threaded
    process, and fork would inherit its locks and IB sockets.
    """
    return mp.get_context("spawn").Pool(workers)
