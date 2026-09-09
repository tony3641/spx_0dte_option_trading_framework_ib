# Methodology: judgment-driven, one-knob-at-a-time tuning

This document is the protocol behind the SKILL.md phase list. Read it before
starting an experiment. The unit of work is a **round**: one runner invocation
over one frozen dataset + seed, probing ONE knob with a small bracketing set.

## Phase 0 — Scope & freeze

Record everything that must stay fixed for deltas to mean anything. Use this
template for `docs/experiments/YYYY-MM-DD-<slug>/experiment.md`:

```markdown
# Experiment: <slug> (YYYY-MM-DD)

## Objective
Default: rank by mean PnL/day subject to ruin_prob <= <cap, default 5%> and
never_entered_pct <= 30% sanity; tie-break on CVAR5 (tail). User override: <...>

## Frozen settings
- Strategy: <name> (from config/strategies.json at commit <sha>)
- Dataset: <csv path | yfinance>, bar_size=<5m>, spot0=<pinned float>, lookback=<days>
- Seed(s): working 42; robustness 42,43,44
- n_paths: 10000 (smoke: 400 for wiring checks only)
- Stress dials: all neutral (list any deliberately frozen overrides)

## Hypothesis (why this knob, expected direction)
<1-3 sentences, updated per round>

## Decision log (append per round; never erase)
| Round | Knob | Values tried | Kept? | Reason |
|---|---|---|---|---|

## Noise floor
<cross-seed delta of the unchanged baseline, measured in Phase 1>
```

Freeze means freeze: changing the CSV, spot0, bar size, seed, or n_paths
mid-experiment invalidates every comparison made so far. If the user asks for a
different dataset, start a new experiment folder and say why.

Sim-UNSUPPORTED knobs (never tunable via the sim, list them in experiment.md if
relevant to the strategy): enabled `trend` (rsi/pmove) gates, enabled `atm_iv`
volatility gates, non-bull_put directions, take-profit modes other than
`pct_credit` (silently ignored by the sim's exit engine), and in single-strategy
mode: `run_days`, FOMC/NFP flags, `short_day_enabled`, child triggers.

## Phase 1 — Baseline and the noise floor

1. `python sim_tune.py --spec <spec> --smoke` — wiring check only; never decide
   from smoke numbers.
2. Full-n_paths baseline run (the runner always includes a `baseline` variant).
3. Re-run baseline on a second seed (e.g. `--seed 43`). For the unchanged
   config, record `|mean(42) - mean(43)|`, plus how `win_rate`, `cvar5`,
   `ruin_prob` moved. This spread is the **noise floor**: a knob effect smaller
   than what an unchanged config shows across seeds is not evidence.

A useful secondary gauge on the working seed: `2 * std / sqrt(entered)` (the
per-path standard error of the baseline mean). Because variants share paths
(common random numbers), real knob effects often show up larger than this
gauge — but anything BELOW it is noise, full stop.

## Phase 2 — Diagnose before you tune

Read the baseline cell like a doctor reads vitals. The breakdown is the map:

| Symptom | Likely binding knobs (in probe order) |
|---|---|
| High `stop` count, poor win_rate | `exit.stop_multiplier` (wider), `short_delta.max` (lower/farther OTM), `spread_width.min` (narrower = less gamma risk per spread) |
| High `expired` with losses near expiry | `entry_window.end` (earlier = less time pinned), `credit.min` (richer credit = wider breakeven) |
| High `never` (rarely enters) | `credit.min`/`credit.max` (loosen), `entry_window` (widen/shift), `short_delta` band (widen), VIX gate threshold |
| Good mean but bad CVAR5/worst_day | `exit.stop_multiplier` (tighter), `budget` (fewer contracts), `spread_width.max` (narrower worst case) |
| ruin_prob over cap | `budget`, `exit.stop_multiplier`, `spread_width.max` |

Mechanisms and current values per knob are in `references/knobs.md`. Write the
hypothesis in experiment.md BEFORE running: "knob X binds because <symptom>;
moving it <direction> should <effect on which metric>". A round that disproves
the hypothesis is still a result — log it.

## Phase 3 — Rounds

Per round: 4–7 values bracketing the current value (include something close to
the current value so the round contains its own control). Write
`docs/experiments/<slug>/variants.json`, run full n_paths, read `results.csv`.

Keep a variant only if ALL hold:
1. objective improves on the working seed by more than the noise floor;
2. `cvar5` and `ruin_prob` do not worsen beyond their own noise;
3. `never_entered_pct` stays sane (a config that barely trades is not
   comparable — its stats come from a different population of days);
4. the effect survives on the fresh seeds in Phase 4.

On keep: the kept value becomes the working config for the next round (fold it
into every later variant's knobs). On reject: log and leave the baseline alone.
Identical stats across variants = the knob was a no-op at this resolution
(quantization: widths snap to the 5-pt ladder step, windows to bars) — shrink
the bracket or pick another knob.

Interaction check: after 2+ kept knobs, run one variant with ALL kept knobs on
top of the original baseline and confirm the combined effect ≈ the sum of the
rounds. If not, the last knob's keep decision may be conditional — re-run its
bracket on top of the combined config.

Stopping rule: stop Phase 3 when a full round produces no variant that clears
the noise floor, or when remaining knobs are either unsupported or already
probed. Two or three unproductive rounds in a row is evidence the strategy is
near a local optimum for this objective — say so in the report instead of
grinding.

## Phase 4 — Robustness gate

Run baseline and the final combined config on 2–3 fresh seeds in one invocation
(`--seeds 42,43,44`). The win must hold on ≥2 of 3 seeds on the objective metric
with cvar5/ruin not systematically worse. Optional stress check: add a spec with
`stress.gamma_mult = 1.25` (or another path dial) and re-run baseline AND final
— these comparisons are whole-baseline (path dial changes break CRN by design).

Report per-seed rows honestly. A config that wins seed 42 by a mile and loses
43/44 is noise-chasing, not tuning.

## Phase 5 — Report & propose

`report.md` in the experiment folder:

```markdown
# Tuning report: <slug> (YYYY-MM-DD)

## Recommendation
<Adopt / do not adopt, one paragraph. State the objective used.>

## Final vs baseline (robustness seeds)
| Seed | Config | mean | win_rate | cvar5 | ruin_prob | never% |
|---|---|---|---|---|---|---|

## Proposed change to config/strategies.json (PROPOSAL — not applied)
```json
// BEFORE (Experiment_1.short_delta)
{"min": 0.03, "max": 0.042}
// AFTER
{"min": 0.035, "max": 0.05}
```
<diffs must be exact JSON fragments a human can paste>

## Caveats
<win rates are relative (implied vs realized vol not tied); single-day MC
calibrated on <dataset>; n_paths; anything the user should know>
```

Then append to `docs/progress.md` a dated session block (Goal / Experiment /
Result / Proposal-pointer, matching the existing Problem/Fix/Result style) and
commit the experiment folder + code (never the config change).

## Honesty rules (read before writing any conclusion)

- Win rates and PnL levels are **relative** measures: the simulator does not tie
  implied to realized vol, so absolute levels are model-flavored. Compare
  variants, not reality.
- No cherry-picking: report every variant you ran, including the ones that
  looked bad. The decision log exists so the user can audit the path from
  baseline to proposal.
- One dataset is one regime. A config tuned on a calm month is not validated
  for a crash; the Phase 4 stress dial is a smoke test, not proof. Say this in
  every report's caveats.
- Do not re-roll seeds until a config wins ("seed shopping" is overfitting to
  noise). Fresh seeds are drawn BEFORE looking — 42,43,44, declared in
  experiment.md at Phase 0.
