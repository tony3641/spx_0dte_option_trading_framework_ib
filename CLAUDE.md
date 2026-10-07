# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

One installable package, `spx_trade_desk`, with two halves:

- **Live desk**: FastAPI + WebSocket dashboard on native `ibapi` (TWS/Gateway): SPX 0DTE GEX, streaming option chain, order entry, rule-based credit-spread strategies, an in-process Discord bot (read/arm only, never places orders).
- **Offline analysis**: the Monte Carlo simulator (`sim/`) and broker-statement analysis (`tradelog/`, exposed to Claude as the `spx-trade-desk` MCP server). Neither places orders or touches IB.

The user's real SPX 0DTE bull-put trades are placed **by hand** on the same principles, not by `config/strategies.json`. When reviewing performance, anchor on fills reconstructed from statements; strategy-config compliance is secondary, and config diffs are only the output used to automate improved rules. Conclusions from past reviews live in Claude auto-memory (deliberately not in this public repo): check there before re-deriving.

## Commands

Python 3.10, Windows (Git Bash and PowerShell both available). No linter, formatter or CI is configured.

```bash
pip install -e "C:\TWS API\source\pythonclient"   # ibapi is not on PyPI; install it FIRST (machine-specific path)
pip install -r requirements-dev.txt               # includes requirements.txt (+ pytest-playwright)
pip install -e . --no-deps

python -m spx_trade_desk.server                   # dashboard on http://localhost:8000 (needs TWS/Gateway API enabled)
python -m spx_trade_desk.mcp.server               # trade-log MCP server, stdio (Claude Code loads it from .mcp.json)
python -m spx_trade_desk.sim.tune --spec docs/experiments/<slug>/variants.json --smoke   # sim knob-tuning runner

python -m pytest tests/test_tradelog_settlement.py -q                                   # scoped run: the default
python -m pytest tests/test_tradelog_settlement.py::test_itm_short_is_settled_at_the_official_close   # one test
```

- **Run only the tests in scope of the change.** The full suite (sim, process pool, Playwright e2e) takes minutes; run it only when asked, and say in the report that it was not run. Known pre-existing failures are listed in `docs/progress.md` (chain_fetcher GEX tolerance, FOMC date).
- Prefer `python -m pytest` over `tests/run_tests.py`: the runner rewrites the tracked `tests/test_results.json`.
- Tests use the explicit `MockIBClient` in `tests/conftest.py` (mirrors the real `IBClient` surface), not `unittest.mock`. `tests/e2e/` is skipped unless Playwright is importable.
- Commits use conventional prefixes (`fix(tradelog): ...`, `feat(sim): ...`). The working tree is CRLF (`autocrlf=true`); the index is LF.

## Architecture

**Live runtime.** `server.py` is a thin entrypoint: its FastAPI lifespan builds the `IBClient` (`ib/client.py`, a native `ibapi` wrapper), subscribes SPX/ES/VIX/account, and starts background loops (`price_bars`, `status_push`, `account_push`, `log_push`, `strategy_evaluation`, `take_profit`, `chain_fetch`, `chain_stream`) that all read/write one shared `AppState` (`core/app_state.py`) and push to browsers through `web/ws.py`. The frontend is vanilla JS + Plotly (GEX, smile) + Lightweight Charts (SPX price chart) in `static/`, with no build step. Gotchas:
- If IB boot fails, the lifespan logs and keeps serving with **no retry**: the dashboard shows `connected:false` / `initializing`. First check *which process and port* you are looking at (orphaned background servers and `IB_PORT` overrides have faked "the dashboard is broken"), then the Log tab.
- The Discord bot is owned by `DiscordSettingsManager`, not by `state.background_tasks`, so IB reconnects never kill it.
- Colors live only in `static/css/tokens.css` (dark and light); JS reads them through `themeColors()` in `static/js/theme-colors.js`. `tests/test_ui_tokens.py` fails on any new color literal elsewhere, on an undefined `var(--x)`, and on a token missing from either theme.
- Settings resolve **env var → repo-root `.env` → `config/params.yaml` → default** (`core/config.py`); the gear menu persists to `.env`. Paths come only from `resources.py` (anchored to the repo root, never `__file__` or cwd).
- IB noise that is not a bug: error 2157 (secdef farm) around 23:45 ET is nightly maintenance and overnight zero option quotes are expected (GTH counts as open); error 10197 means a live-account session is competing and blocks live *and* delayed data on the paper gateway; farm names (hfarm, ushmds, secdefhk) are not the failure variable.
- TWS processes one request queue per connection, so IB traffic has two lanes: orders, cancels, order-path lookups, account and the fixed underlyings are never paced; qualification, snapshots and stream subscribes go through `ib/pacing.py` (`IB_REQUEST_RATE`). One `ContractRegistry` (`ib/contracts.py`) serves chain and orders: a bulk listing per expiry prefills it, absence is never trusted on the order path (a miss is a live, exact-match lookup), and `ORDER_USE_CONTRACT_CACHE=false` is the kill switch. IB error 101 shrinks the line budget at runtime (`LineBudget.observe_limit`). `ib/session.py` is the single boot for the lifespan and the reconnect; `/api/perf` shows the spans.

**Simulator** (`sim/`). `jobs.py` runs a job as `(sweep cell, chunk)` tasks on a spawn-context process pool (`parallel.py`). The RNG is keyed by `SeedSequence(seed, spawn_key=(cell, chunk))` and results are reassembled by index, so serial and parallel runs are bit-identical. Path generation is GJR-GARCH + Student-t (`paths.py`, `calibrate.py`), marking is BSM on the z-model tables fitted from the recorded chain library (`pricing_model.py`, `pricing_tables.py`, `library.py`; every calendar-to-sim conversion lives in `clock.py`, and `validate.py` scores the pricer against recorded chains), entries/exits in `engine.py`. `sim/engine.py` deliberately mirrors `strategy/engine.py` candidate ranking, entry-window bounds and child-trigger semantics, so a rule change in the live engine needs a parity check in the sim. `sim/tune.py` injects variant `Strategy` objects in memory with common random numbers (never writes `config/strategies.json`); the `strategy-tuning` project skill drives it. Bars are cached per `(source, csv_path, bar size, lookback)` and calibration additionally per pricing tier, pricing-model file hash and session date, not per CSV content, so restart after replacing a CSV. A near-integrated GARCH fit ratchets tail paths to absurd moves; the `atm_iv` + `vol_cap_mult` per-bar sigma cap is the stable fix (rescaling vol level is not). Regression baselines are `tests/fixtures/sim_baseline_*.npz`. Sim tests never read the local chain library or call yfinance for VIX1D (an autouse fixture in `tests/conftest.py`), so they run on the tracked Cold default.

**Trade-log analysis** (`tradelog/`). Pipeline: `io/` loaders (IBKR QFX/CSV, E*Trade CSV/PDF, consolidated `.xlsx`) → one standard 11-column frame → `domain/merge` dedup → `domain/settlement` → `domain/pnl_engine` → risk / return / calendar → `analysis/` (spread reconstruction, `tagging` compliance) → `report/` self-contained HTML in `reports/output/` (gitignored). `mcp/server.py` is the interface (11 tools, take `paths` or in-memory `file_contents`); `mcp/adapter.py` turns DataFrames into JSON (NaN → null). Invariants:
- An IBKR **QFX omits the cash settlement of ITM expiries**; `settlement.py` infers it from the official close in `reports/data/spx_closes.csv`. P&L from a raw QFX overstates any month with an ITM expiry. A real settlement row always wins, and an unpriced expiry is warned about, never guessed.
- The `.xlsx` workbook is a standalone ledger (masked account id): never load it together with a QFX (double count). It has dates but no intraday timestamps, so its monthly report is day-level and `analyze_strategy_compliance` is QFX-only.
- Compliance delta needs the SPX spot **at entry time**: pass `intraday_spot_path` (CSV with `ts`, `close`) or let yfinance 1-minute bars cover about the last week; otherwise delta is "unverifiable", never scored against the close. Unverifiable is not a failure.
- **Never import scipy-dependent modules lazily inside an MCP tool body.** On Windows the FastMCP stdio reader deadlocks the first call; `mcp/server.py` imports them at module load for that reason (`tests/test_tradelog_mcp_stdio.py` pins it).
- The default `offline=False` market-data path **rewrites the tracked `reports/data/*.csv`**; use `offline=True` to leave them alone. Their showing as modified after an online run is expected. The committed cache ends 2026-07-31, so later months need an online run.
- The report HTML/data for QFX is pinned by `tests/fixtures/report_qfx_golden.json`: change it only deliberately.

## Working rules

- **This repo is PUBLIC (with forks).** Never put these in tracked files, tests, skills, docs, memory or commit/PR text: values from `config/strategies.json` (use `MyStrategy`, "see config"), real strategy names, broker account ids even masked (use `U***12345`), real P&L or commission totals, rows/dates/file names from the user's statements or trade-log workbook. Tests and fixtures use fully invented data (tickers like ACME, built in `tmp_path` or `tests/ledger_fixture.py`). Grep what you authored for real-data literals before reporting done.
- Local-only and gitignored: `.env`, `.mcp.json` (copy `.mcp.json.example`), `config/strategies.json`, `config/sim_smile.json`, `data/` (chain library and pricing model), `docs/experiments/`, `docs/superpowers/` (superpowers specs and plans), `reports/output/`, all statement types (`*.qfx *.xlsx *.pdf`, and `*.csv` outside `reports/data/` and `tests/fixtures/`). A local, unversioned `.git/hooks/pre-commit` blocks them even under `git add -f`; its rules are in `docs/progress.md` if it must be re-created. Keep full analysis reports outside the repo (scratchpad) and put only aggregates and conclusions in repo docs.
- **Docs are the running report.** Land README / `docs/progress.md` / in-UI "?" help updates in the same change as the finding or feature. Everything committed or shown in the product is **English** (chat may be Chinese). New `docs/progress.md` entries go at the top as `## Session: <Month D, YYYY> - <title>`.
- After moving or renaming a module, also grep for the old name **inside quotes** (`monkeypatch.setattr("mod.attr", ...)`, `mock.patch(...)`, `__import__`) and for dangling local bindings; import-statement rewrites miss them and they fail only at runtime.
- Subagents: never override the model to opus/pro; leave `model` unset so they inherit the session model.
