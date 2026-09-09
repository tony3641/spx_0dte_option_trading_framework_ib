---
name: strategy-tuning
description: >
  Systematic, reproducible workflow for tuning ONE trading strategy's knobs
  against this repo's Monte Carlo stress simulator using sim_tune.py. Use this
  skill whenever the user wants to improve, optimize, or tune a strategy from
  config/strategies.json — better win rate, risk profile, stop-loss multiplier,
  delta/width/credit bands, entry window, take profit — or asks why a strategy
  is losing money, wants "better parameters", wants a what-if experiment on a
  knob, or wants tuning results turned into a config-change proposal. Also use
  it when the user reports a strategy underperforming and the cause is unknown.
  Do NOT use it for live-order debugging, IB connectivity, or dashboard bugs.
---

# Strategy tuning (MC-simulator knob methodology)

Tuning here means: a judgment-driven, one-knob-at-a-time experiment loop run
through the deterministic MC simulator, producing an auditable experiment
record and a propose-only config diff. It is NOT a brute-force grid sweep —
the agent's judgment decides which knob is worth probing and by how much;
`sim_tune.py` only executes the chosen variants deterministically.

## Why the hard rules exist

1. **Never edit `config/strategies.json`.** That file drives live order
   execution (some strategies have `auto_execute: true`). You produce an exact
   JSON diff proposal in `report.md`; the user applies it. There is no
   exception, even when the winning config is obvious.
2. **One knob per round.** With one knob changed at a time, each metric delta
   is attributable to that knob. Two simultaneous changes can cancel or mask
   each other and the experiment record becomes uninterpretable.
3. **Same seed, same dataset, same n_paths within a round.** The simulator
   replays bit-identical spot paths for identical cfg (seeded per
   `SeedSequence(entropy=cfg.seed, spawn_key=(cell, chunk))`), so variant
   deltas are apples-to-apples. The runner enforces this by design and checks
   `spx_fan` equality; a CRN warning means the round is invalid — stop and
   re-read `references/runner.md`.
4. **Propose, never auto-apply** (rule 1 applied to the end of the workflow).
5. **English only in all files you write** (experiment records, reports, diffs).

## The five phases

Work through these in order. Phases 0–1 and 5 always apply; iterate 2–3 as
many rounds as the evidence justifies; never skip 4.

**Phase 0 — Scope & freeze.** Confirm with the user: target strategy name,
objective (default: rank by mean PnL/day, subject to `ruin_prob` ≤ cap and
`never_entered_pct` sanity, tie-break on CVAR5), dataset (one frozen CSV, bar
size, pinned `spot0`), seed (default 42), n_paths. Note up front which knobs
are sim-UNSUPPORTED (`trend` gates, `atm_iv` gates, non-pct take-profit,
non-bull_put directions) — they can never be tuned via the sim. Write
`docs/experiments/YYYY-MM-DD-<slug>/experiment.md` (template in
`references/methodology.md`).

**Phase 1 — Baseline.** Run the live config unchanged through the runner
(`--smoke` first to sanity-check wiring, then full n_paths). Record the row.
Then re-run the baseline on a second seed — that cross-seed delta of an
UNCHANGED config is your noise floor; every later "win" must exceed it.

**Phase 2 — Diagnose.** Read the baseline breakdown (`stop` / `expired` /
`take_profit` / `never`), win_rate, CVAR5, ruin_prob, and the fan. Form a
hypothesis about the single most binding knob and rank 2–4 candidates.
`references/knobs.md` lists every knob's mechanism, expected effect direction,
current value, and quantization traps (widths snap to the 5-pt ladder step,
entry windows to bars). Pick ONE knob.

**Phase 3 — One-knob rounds.** Write a `variants.json` with 4–7 bracketing
values around the current value of that knob (runner.md has the schema), run
it, read `results.csv`. Keep a change only if: (a) it beats the noise floor on
the working seed, (b) `cvar5` and `ruin_prob` do not worsen, (c) trading
activity stays sane. Log keep/revert + reason in `experiment.md`'s decision
log; the kept value becomes the new baseline for the next round. Identical
stats across a variant means the knob was a no-op at this resolution —
note it and move on. Stop when no knob clears the noise floor. After ≥2 kept
changes, re-run the COMBINED config (interaction check) before Phase 4.

**Phase 4 — Robustness gate.** Baseline-vs-final on fresh seeds
(`--seeds 42,43,44`): the win must hold on ≥2 of 3 seeds. Optionally add one
market-stress perturbation (e.g. `stress.gamma_mult = 1.25`) as a whole-new
baseline run — path-dial comparisons are never paired.

**Phase 5 — Report & propose.** Write `report.md`: decision log, final tables,
the recommended config as an EXACT before/after JSON fragment of
`config/strategies.json`, and caveats. Append a dated entry to
`docs/progress.md` pointing at the experiment folder. Hand the diff to the
user; do not apply it.

## Reading order

- Before Phase 0: `references/methodology.md` (templates, decision rules,
  noise-floor arithmetic, honesty rules).
- Before Phase 2: `references/knobs.md` (knob taxonomy, effect directions).
- Before Phase 3: `references/runner.md` (CLI, variants.json schema, outputs,
  cost planning, CRN failure handling).

## Cost discipline

A full 10k-path run takes minutes (the per-path × per-bar exit loop dominates).
Use `--smoke` (400 paths) to validate wiring, full n_paths for decision rounds.
Budget experiments in rounds: each round is one runner invocation. If the user
needs many rounds, run smoke rounds first to prune the bracketing set.

## Extending to a strategy family (future)

The single-strategy workflow above is the v1 scope. `references/knobs.md`
ends with the family-mode extension design (parent/child triggers, budget
interaction, `--mode family`); the spec schema and the runner's stub-injection
already keep the door open. Do not improvise family tuning before that
extension lands — children trigger off parent exits and need `run_family()`,
which this skill does not yet orchestrate.
