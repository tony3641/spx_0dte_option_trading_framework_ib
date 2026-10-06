"""Run configuration for the intraday Monte Carlo simulator."""
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional

BAR_SECONDS = {"5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300}
_MODES = ("single", "family")
_SOURCES = ("csv", "yfinance", "ib", "auto")
_STRIKE_MODES = ("engine", "dynamic_k")

# Keys removed by the z-model pricer (SP2). A saved config or tuning spec that still
# carries one loads; the key is dropped with a warning.
REMOVED_KEYS: Dict[str, str] = {
    "vol_beta": "vol_beta was removed: the ATM level now follows the path's GJR state",
    "skew_t_gamma": "skew_t_gamma was removed: the tau buckets of the pricing tables carry "
                    "the expiry steepening",
    "atm_budget": "atm_budget was removed: the variance-budget level link is always on",
}


def removed_key_warnings(d: dict) -> List[str]:
    """Warnings for removed keys whose value asked for behavior that no longer exists
    (a zero skew_t_gamma or a true atm_budget is what the sim does now: no warning)."""
    out = []
    for k, msg in REMOVED_KEYS.items():
        if k not in d or (k == "skew_t_gamma" and not d[k]) or (k == "atm_budget" and d[k]):
            continue
        out.append(f"config: {msg}")
    return out


@dataclass
class SimRunConfig:
    strategy_name: str
    mode: str = "single"                # "single" | "family"
    source: str = "auto"                # "csv" | "yfinance" | "ib" | "auto"
    csv_path: str = ""
    bar_size: str = "1m"
    lookback_days: int = 10
    spot0: Optional[float] = None       # day-open spot; None = live / last close
    n_paths: int = 10000
    seed: int = 42
    chunk_size: int = 250
    equity: float = 100_000.0
    ruin_threshold_pct: float = 0.20
    bootstrap_seqs: int = 500
    bootstrap_len: int = 60
    sl_multipliers: Optional[List[float]] = None    # None -> strategy's own; inf = hold past stop
    strike_mode: str = "engine"         # "engine" | "dynamic_k"
    dynamic_k_values: Optional[List[float]] = None  # None -> [0.5] when dynamic_k
    width_points: float = 50.0          # spread width in dynamic_k mode
    nu_override: Optional[float] = None             # stress: Student-t dof
    gamma_mult: float = 1.0             # stress: GJR leverage-term multiplier
    flat_iv: bool = False               # sanity mode: every strike at the ATM level (no smile)
    pricing_tier: str = "auto"          # option pricing tables: auto | cold | thin | library
    atm_iv: Optional[float] = None      # annual ATM IV (decimal) to anchor the SPX fan; None = historical GARCH level
    vol_cap_mult: float = 2.0           # per-bar sigma cap as a multiple of the IV-implied per-bar vol
    stop_extra: float = 0.10            # market-order stop: trigger + this
    tick_size: float = 0.05
    ladder_range_pct: float = 0.15
    skew_beta: float = 0.0              # wing tilt per unit of the level link L - 1 (>= 0); 0 = off
    budget_beta: float = 1.0            # level-link state sensitivity (1.0 = theory)
    n_workers: int = 0                  # worker processes; 0 = auto (SIM_WORKERS env, else CPU count)
    load_warnings: List[str] = field(default_factory=list, compare=False, repr=False)

    def steps_per_day(self) -> int:
        return (390 * 60) // BAR_SECONDS[self.bar_size]

    def validate(self) -> None:
        if not self.strategy_name:
            raise ValueError("strategy_name is required")
        if self.mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}")
        if self.source not in _SOURCES:
            raise ValueError(f"source must be one of {_SOURCES}")
        if self.source == "ib":
            raise ValueError("IB live bar ingestion is not implemented in the simulator — "
                             "use 'csv' or 'yfinance'")
        if self.mode == "family" and (self.sl_multipliers or self.dynamic_k_values):
            raise ValueError("SL/k sweep is not supported in family mode; leave "
                             "sl_multipliers and dynamic_k_values empty")
        if self.bar_size not in BAR_SECONDS:
            raise ValueError(f"bar_size must be one of {sorted(BAR_SECONDS)}")
        if self.n_paths <= 0:
            raise ValueError("n_paths must be > 0")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be > 0")
        if self.n_workers < 0:
            raise ValueError("n_workers must be >= 0 (0 = auto, 1 = serial)")
        if self.equity <= 0:
            raise ValueError("equity must be > 0")
        if not (0 < self.ruin_threshold_pct <= 1):
            raise ValueError("ruin_threshold_pct must be in (0, 1]")
        if self.strike_mode not in _STRIKE_MODES:
            raise ValueError(f"strike_mode must be one of {_STRIKE_MODES}")
        for name in ("nu_override",):
            v = getattr(self, name)
            if v is not None and v <= 2.0:
                raise ValueError(f"{name} must be > 2.0 (Student-t needs finite variance)")
        if self.gamma_mult <= 0:
            raise ValueError("gamma_mult must be > 0")
        if self.skew_beta < 0:
            raise ValueError("skew_beta must be >= 0")
        from spx_trade_desk.sim.pricing_tables import TIERS   # lazy: config stays import-light
        if self.pricing_tier not in TIERS:
            raise ValueError(f"pricing_tier must be one of {TIERS}")
        if self.budget_beta < 0:
            raise ValueError("budget_beta must be >= 0")
        if self.vol_cap_mult <= 0:
            raise ValueError("vol_cap_mult must be > 0")
        if self.atm_iv is not None and not (0 < self.atm_iv < 5.0):
            raise ValueError("atm_iv must be in (0, 5.0) when set (annual decimal)")
        if self.stop_extra < 0 or self.tick_size <= 0:
            raise ValueError("stop_extra must be >= 0 and tick_size > 0")
        for v in (self.sl_multipliers or []):
            if not (v > 0):
                raise ValueError("sl_multipliers entries must be > 0 (use inf for hold)")
        for v in (self.dynamic_k_values or []):
            if not (0 < v < 10):
                raise ValueError("dynamic_k_values entries must be in (0, 10)")
        if self.source == "csv" and not self.csv_path:
            raise ValueError("csv_path is required when source='csv'")

    def to_dict(self) -> dict:
        d = {}
        for f in fields(self):
            if f.name == "load_warnings":
                continue
            v = getattr(self, f.name)
            if isinstance(v, list):
                v = ["inf" if x == float("inf") else x for x in v]
            elif v == float("inf"):
                v = "inf"
            d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SimRunConfig":
        d = dict(d or {})
        known = {f.name for f in fields(cls)} - {"load_warnings"}
        kwargs = {}
        for k, v in d.items():
            if k not in known:
                continue
            if isinstance(v, list):
                v = [float("inf") if x == "inf" else x for x in v]
            elif v == "inf":
                v = float("inf")
            kwargs[k] = v
        cfg = cls(**kwargs)
        cfg.load_warnings = removed_key_warnings(d)
        return cfg


def sweep_cells(cfg: SimRunConfig) -> list:
    """Cartesian product of the SL-multiplier list and the strike-k list.

    sl_multipliers None -> [None] (use the strategy's own stop multiplier).
    engine strike mode ignores k (single [None] column); dynamic_k defaults to [0.5].
    """
    sls = cfg.sl_multipliers or [None]
    ks = [None]
    if cfg.strike_mode == "dynamic_k":
        ks = cfg.dynamic_k_values or [0.5]
    return [{"sl_multiplier": s, "k": k} for s in sls for k in ks]
