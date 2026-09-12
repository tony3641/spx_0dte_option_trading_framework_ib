# runner.md — sim_tune.py reference

`sim_tune.py` (repo root) executes named knob variants of ONE live strategy
through the MC simulator, deterministically, without touching
`config/strategies.json`. It exists so the methodology's rounds are
reproducible: identical spec + identical data ⇒ identical numbers, every time.

## CLI

```
python sim_tune.py --strategy Experiment_1 --spec docs/experiments/<slug>/variants.json \
    --out docs/experiments/<slug> [--seed 42 | --seeds 42,43,44] [--n-paths 10000] \
    [--csv PATH | --yfinance] [--bar-size 5m] [--spot0 6000.0] [--smoke]
```

- `--spec` (required): variants.json path (schema below).
- `--out` (default `docs/experiments/<slug or strategy-name>`): receives
  `results.csv` + `results.json`.
- `--seed` / `--seeds`: working seed(s). `--seeds 42,43,44` loops the WHOLE
  spec per seed — that one command is the Phase-4 robustness gate.
- `--n-paths` overrides the spec's run block; `--smoke` forces
  `n_paths=400, chunk_size=100, bootstrap_seqs=100, bootstrap_len=30`.
- `--csv PATH` / `--yfinance` / `--bar-size` / `--spot0` override the dataset
  block. Pin `spot0` in the spec: it defaults to the last CSV close, which
  drifts when the file changes.
- Exit codes: 0 = ran, 2 = validation error (message on stderr).
- A `baseline` variant (live config, no knobs) is ALWAYS run first so every
  variant has a paired control.

## variants.json schema

```json
{
  "slug": "exp1-stop-and-delta",
  "strategy": "Experiment_1",
  "dataset": {"source": "csv", "csv_path": "data/spx_5m.csv",
               "bar_size": "5m", "spot0": 6000.0, "lookback_days": 60},
  "run":     {"seed": 42, "n_paths": 10000, "equity": 100000,
               "ruin_threshold_pct": 0.20, "bootstrap_seqs": 500, "bootstrap_len": 60},
  "stress":  {},
  "variants": [
    {"name": "sl4",  "knobs": {"exit.stop_multiplier": 4.0}},
    {"name": "sl5",  "knobs": {"exit.stop_multiplier": 5.0}}
  ]
}
```

- `dataset` / `run` / `stress` are GLOBAL — shared by every variant. That is
  what makes common random numbers hold; per-variant sim overrides are
  rejected by design (if you think you need one, you need two invocations
  compared as whole baselines).
- `variants[].knobs` — dotted paths from the whitelist in
  `sim_tune.KNOB_WHITELIST` (full taxonomy + mechanisms:
  `references/knobs.md`). Unknown knob ⇒ ValueError listing the whitelist;
  `null` unsets (`take_profit.pct`, `credit.max`, `budget`).
- Variant name `baseline` is reserved; names must be unique.
- Unknown keys anywhere in the spec fail loudly — the runner never silently
  ignores a setting.

## Outputs

`results.csv` — one row per (variant, seed), columns:
`variant, seed, mean, median, std, win_rate, cvar5, cvar1, worst_day, n,
entered, never_entered_pct, expired, stop, take_profit, never, ruin_prob,
dd_mean, dd_p95, dd_worst, knobs` (knobs as a JSON string).

`results.json` — `meta` (slug, strategy, frozen blocks, seeds, `spot0`,
`crn_ok`, knob whitelist, generated_at, total elapsed) + per-variant full cell
payloads (`stats`, `breakdown`, `hist`, `dd`, `ruin_prob`, `dd_hist`, `fan`)
and the run meta (GARCH/smile fits, dials). Read the CSV for decisions; dig
into the JSON when a number surprises you.

stdout prints one line per run (mean / win / cvar5 / ruin / never + seconds) —
handy as the round's first read.

## Determinism & the CRN check

Same seed + same dataset + same n_paths ⇒ identical spot paths across variants
(seeded `SeedSequence(entropy=cfg.seed, spawn_key=(cell, chunk))`; the strategy
never draws RNG). After every variant the runner compares its `spx_fan` to the
baseline's; any difference sets `crn_ok: false` and logs a WARNING. If you see
it: something varied the market (different n_paths, stress dial, dataset) —
the round's paired deltas are invalid; re-run as a clean round.

Related reproducibility notes:
- Calibration is cached in-process per (source, path, bar_size, lookback) —
  one invocation is self-consistent; edit `config/sim_smile*.json` between
  invocations, never mid-experiment.
- `tests/fixtures/SPX_1min_10d.csv` is the committed 1-minute fixture (the
  last 10 trading days, 390 RTH bars/day) used by the sim tests and for real
  calibration; `SPX_1min_default.csv` is the longer 13-day source it is cut from.
- The repo's sim tests pin bit-identical baselines; `sim_tune` adds no state to
  that (it writes only into `--out`).

## Errors you may hit

- `unknown strategy: 'X' (have: [...])` — name typo; names come from
  `config/strategies.json`.
- `unknown knob: '...'` — off-whitelist knob; check knobs.md (trend/atm_iv are
  intentionally absent: the sim refuses them).
- `strategy 'X' enables a 'trend' ... not faithfully evaluate` — the LIVE
  strategy itself is sim-unsupported; tuning via sim is impossible until that
  gate is disabled in the proposal (never silently skipped by design).
- `csv_path is required when source='csv'` — fill `dataset.csv_path`.
- A variant with `never_entered_pct` = 1.0 ran but entered nothing — its stats
  are zeros; treat as a failed variant, not a great safe one.
