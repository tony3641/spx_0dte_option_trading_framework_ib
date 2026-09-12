# Knob taxonomy (credit-spread engine, sim-consumed subset)

The simulator runs the REAL `Strategy` objects from `config/strategies.json`
(`sim_engine.extract_conditions` mirrors the live engine's bound semantics).
This file lists what the sim actually responds to, what it ignores, and what
each knob does mechanically — so diagnosis in Phase 2 starts from evidence,
not guesswork. Current values below are Experiment_1's live config
(`config/strategies.json`); re-check the file at experiment time.

## Sim-consumed strategy knobs (tunable via sim_tune.py)

| Knob (dotted path) | Exp_1 now | Mechanism | Expected effect of moving it | Traps |
|---|---|---|---|---|
| `short_delta.min` / `.max` | 0.03 / 0.042 | short leg's |abs delta| band; candidates must fit | Wider band (esp. higher max) = closer-to-money shorts = richer credit but more stop-outs and worse tails; lower max = farther OTM = safer, thinner credit, more `never` | Band edges beyond ~0.50 delta change put geometry; deltas come from the sim's own smile, not the live chain |
| `spread_width.min` / `.max` | 45 / 55 | width band in points; long leg = short − width | Narrower = less max loss per spread, more affordable qty for a budget, thinner credit; wider = fatter credit, uglier tails | Widths SNAP to the 5-pt ladder step (values inside a step are no-ops — identical stats are the tell) |
| `credit.min` / `.max` | 0.25 / 0.60 | collected-credit band on the spread mid | Higher min = demands richer fills = fewer, better-priced entries; higher max is usually inert (mids rarely exceed it) | Credit is the sim's conservative tick-floored natural, never better than mid — a min above what the smile offers yields `never` |
| `entry_window.start` / `.end` | 10:45 / 12:30 | bar-index window when entry scanning is active | Earlier start catches morning vol (richer credit, more trend risk); later end = less afternoon time-in-trade; shrinking the window trades opportunity for focus | Quantized to bars (5m → 5-min resolution; 10:45→bar 15). Off-RTH values clamp silently |
| `volatility.vix_enabled` | (no vol condition) | VIX-regime gate: `VIX_t = vix0 * sigma/sigma0`, bucket-tested per bar | Requiring calm (below ~15) skips stressed bars; requiring stress (above) trades only scared tape | Set `vix_enabled`, `vix_op` (`above`/`below`/`range`), `vix_value` TOGETHER; the VIX here is model-implied, not the real index |
| `exit.stop_multiplier` | 6.0 | stop when spread mark ≥ credit × mult (debit = trigger + 0.10) | Smaller mult = tighter stop = smaller losers, more whipsaw stops; larger/`inf`-like = ride to expiry | The stop level scales with entry credit, so richer fills tolerate wider stops; `stop_extra` (+0.10) is fixed slippage |
| `take_profit.pct` | none | TP when mark ≤ credit × pct (mode forced to `pct_credit`) | Adding e.g. 0.5 banks half-credit winners early, converts some `expired` into `take_profit`, cuts tail time | Other TP modes are NOT simulated — only this pct form; `null` removes the TP |
| `budget` | 20000 | qty = floor(budget / (width × 100)) | Lower budget = fewer contracts = smaller absolute PnL AND smaller ruin numbers (per-trade PnL scales with qty) | Sizing changes shift ALL PnL metrics; compare risk RELATIVELY (win_rate, ruin_prob) when budget moves |

Not consumed in single-strategy mode (changing them in a spec is a no-op):
`hold_to_expire`, `run_days`, `short_day_enabled`, `run_on_fomc`, `run_on_nfp`,
`auto_execute`, `armed`, `target_expiry`, `gth`, `subsequent_triggers`.

Loudly UNSUPPORTED (runner + sim refuse): non-`bull_put` direction, enabled
`trend` gates, enabled `atm_iv` gates, non-pct take-profit modes.

## Market stress dials (`stress` block — frozen within a round)

Two classes, and the difference matters for reading results:

- **Path dials** (change the spot/vol paths themselves ⇒ break common random
  numbers vs the baseline): `nu_override` (Student-t dof), `gamma_mult`
  (GJR leverage term), `atm_iv` (fan anchor), `vol_cap_mult` (per-bar sigma
  cap). Use ONLY as whole-new-baseline comparisons in the robustness gate.
- **Pricing dials** (same paths, different option marks): `flat_iv`,
  `vol_beta`, `skew_beta`, `skew_t_gamma`, `atm_budget`, `budget_beta`, and
  fill knobs `stop_extra` / `tick_size` / `ladder_range_pct`. CRN-safe across
  variants, but keep them neutral while tuning strategy knobs — they answer
  model questions, not strategy questions.

## Cost planning

The exit engine is a per-path × per-bar Python loop with a full-ladder BSM per
bar: ~minutes at `n_paths=10000` on 5m bars, ~seconds at 400 (`--smoke`).
Calibration is cached in-process per (source, path, bar_size, lookback); bars
load once per invocation. A 6-variant round ≈ 6 × one run.

## Family-mode extension (future — do not implement ad hoc)

Single-strategy tuning is v1. The natural v2 extends the same loop to a
parent + children tree:

- The runner's stub injection already carries the WHOLE strategy tree
  (`state.strategies`), and `execute_pipeline` supports `mode="family"` via
  `run_family()` — children trigger on `parent_exit_reason` /
  `parent_unrealized_pnl` / `time_of_day` (trigger_logic must be `"any"`).
- New tunable knobs then include child `time_of_day` trigger windows, the
  parent-unrealized-pnl `loss_multiple`/`gain_multiple`, and per-child delta/
  width/credit bands — plus the budget INTERACTION (parent + children draw on
  separate budgets today; family PnL-day accounting differs from single mode).
- Spec schema already reserves a `strategies: [...]` list (single `strategy`
  key now); the runner will grow a `--mode family` flag rather than a new tool.
- Discipline stays identical: one knob per round, CRN discipline, robustness
  gate, propose-only diffs (children included).
