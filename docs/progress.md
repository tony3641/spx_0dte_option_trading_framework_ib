# Progress & Change Log

All significant feature additions and bug fixes made to the SPX 0DTE GEX Dashboard.

---

## Session: October 5, 2026 - Sim option pricing z-model (SP2)

The simulator's SVI smile is replaced by a z-model fitted from the recorded 0DTE chain
library (SP1).

- **One clock** (`sim/clock.py`): stored IVs are calendar-time, the sim path is
  trading-time. `sigma_sim = IV_cal x 0.43242` and the rate is scaled the same way, so
  sim-clock BSM reproduces the calendar price. This also fixes the ATM IV % fan cap,
  which read a calendar IV as trading-time (the dial now takes the IB/VIX-style number).
  The cap is `atm_iv x sqrt(390/525600)` per RTH day instead of `atm_iv/sqrt(252)`, so
  saved configs or tuning specs that set `atm_iv` in the old trading-time units now get a
  fan about 0.43x as wide; re-enter the IB/VIX-style IV.
- **Pricing tables** (`pricing_tables.py`, `pricing_model.py`): `IV = ATM(t) x f(z, tau)`.
  - Seven time-to-close buckets, total-variance extrapolation with a Lee cap.
  - Intraday ATM curve `g`; the GJR level link `L` is always on (the old `atm_budget`).
  - Tiers: library / thin / cold with per-bucket fallback.
  - `vol_beta`, `skew_t_gamma` and `atm_budget` are removed; old configs load with a warning.
- **Library builder** (`library.py`): rebuilt by the capture after each session, by the
  Sim tab button, or by `python -m spx_trade_desk.sim.library build`. The tracked Cold
  default `config/sim_pricing_default.json` holds aggregate tables only.
- **Validation harness** (`validate.py`) on the first captured day (in-sample, Cold tier):
  - legacy SVI pricer: 0/4 scored time buckets within the bar;
  - z-model: 4/4.
  - Out-of-sample check: pending (needs a second captured day).
- Regression baselines re-pinned (`sim_baseline_z*.npz`); the legacy/A/AB/ABC chain is
  retired. Tuning results from before this change must be re-run.
- Known failure resolved: sim_regression baseline drift (re-pinned).

---

## Session: October 5, 2026 - Chain service: line budget, no stream pause, chain library

- **Why:** the sim's option prices were tested against a live SPXW 0DTE chain (one afternoon, local analysis). They were not usable as wired: calendar- vs trading-clock IV units, SVI guards that cannot fit a 0DTE smile, and one frozen smile per day. The planned fix (SP2) needs a daily chain library, so this session builds the capture first (SP1).
- **Market-data line budget** (`ib/line_budget.py`): `MARKET_DATA_LINES` (default 100) is split at startup into underlyings 4 (SPX, ES, VIX, VIX1D), order entry 4, poller 12 and stream 78, with 2 spare. Every subscribe goes through it, so the process cannot trigger IB error 101. Order-entry mid lookups can no longer be starved by the chain.
- **No more stream pause:** the 5-minute full sweep (`chain_fetch_loop`) is gone. A wing poller cycles the strikes the stream does not hold. Stream and poller both write a `QuoteBook`, which is published every `CHAIN_REFRESH_SECONDS`. The strategy engine previously read a cache that could be about 5 minutes old; it now reads one at most a cycle old, and skips legs older than `CHAIN_QUOTE_MAX_AGE_S`.
- **Bug fixed in passing:** concurrent `IBClient.fetch_snapshot` calls shared one pending set; each call now has its own batch.
- **Chain library:** `market/chain_recorder.py` appends the book to `data/chain_library/YYYYMMDD.jsonl.gz`; `python -m spx_trade_desk.market.capture` is the fallback when the dashboard is down.
- **Pre-commit hook:** now also blocks anything under `data/` (local market data). The hook's second case block gained a `data/*)` case that blocks anything under the repo-root `data/` directory. `reports/data/` stays tracked and is not blocked. The full rule list is in the "Follow-up" bullet of the October 1, 2026 public-repo cleanup entry below.
- Removed settings: `CHAIN_STREAM_MAX_LINES` (now derived from the budget), `SNAPSHOT_REFRESH_SECONDS`.
- **Final-review fixes:** the dashboard refreshes its heartbeat on every successful publish tick (not only when a record is written), so the standalone capture no longer overlaps it at the 09:31 first record; a heartbeat touch failure is logged and cannot repeat a record. A streamed row now ages from its stream's last tick (`TickStream.last_tick_mono`), so a silent data farm ages out of `CHAIN_QUOTE_MAX_AGE_S`; a stream's missing bid/ask clears the book's value (poll rows still never overwrite with a missing field). The published chain cache carries `built_mono` and the engine adds the cache's age to each side's age; the cache is cleared on IB reconnect. `subscribe_tick` releases its line when the request raises, a qualification overtaken by an expiry roll writes nothing, the recorder skips records while SPX has no price, and a failed capture sweep waits a record interval before retrying (the failure count resets after an idle spell). Known deviations kept: the monthly GEX fetch and the capture sweep still use `chain_fetcher`'s own qualification cache, and quote ages are per row (`ts`/`source`), not per field group.
- Live check: pending (paper TWS, 30 min of RTH: error-101 count, stream subscriptions never 0, poller cycle time <= 120 s, GEX walls vs a full sweep within one strike, recorder cadence, standalone take-over within 4 min, one paper order's mid lookup).

---

## Session: October 1, 2026 - Trade-log review fixes

Fixes found while reviewing a live month of manual SPX put-spread trades.

- **QFX ITM settlement** (`tradelog/domain/settlement.py`): an IBKR QFX omits the cash
  settlement of an in-the-money expired SPXW leg, so P&L, daily series and the balance-anchored
  capital were wrong for any month with an ITM expiry. QFX loads now infer the missing
  `Cash Settlement` rows from the official SPX close (`reports/data/spx_closes.csv`), only for
  contracts whose whole history is on their expiry day; a real settlement row wins, unpriced
  expiries are warned about, and an ITM expiry is not counted as a stop. `reconstruct_spreads`
  now takes the long leg's cost from what was paid (it used `abs(total_pnl)`, wrong once the long
  is sold back or settled).
- **Compliance delta** (`tagging`, `analyze_strategy_compliance`): the credit-implied delta used
  the daily close as the spot; it now needs the spot at the entry time (`intraday_spot_path` CSV
  or yfinance 1-minute bars) and is unverifiable otherwise.
- **Engine** (`strategy/engine.py`): bracket stop-limit gets a cushion beyond the trigger so a
  gap still fills; `classify_parent_close` reports `stop_loss` only when the stop order executed
  (manual closes are `manual`, post-session closes `expire`); `parent_unrealized_pnl` water marks
  are dollars over `credit x 100 x spreads` (they were dollars over the per-share credit).
- **Simulator**: `parent_unrealized_pnl` now matches live (latches while the parent is open,
  supports `gain_multiple`, scales by quantity, child starts after the parent exits); removed the
  dead `_spread_rows`. A `time_of_day` child still starts without waiting for the parent.

---

## Session: September 10, 2026 - Parallel sim execution (process pool)


- Added `sim_parallel.py`: the sim's `(sweep cell, chunk)` work now runs on a
  spawn-context process pool instead of one core. Processes, not threads — the
  exit scan is a per-path Python loop, which the GIL serializes. `compute_chunk`
  is shared by both the serial and parallel paths, so the parallel result is
  bit-identical to the serial one (RNG keyed by `(seed, cell, chunk)`, results
  reassembled by index, never by completion order).
- `sim_jobs.execute_pipeline`: per-cell aggregation as a cell's chunks land
  (raw trials freed immediately, so peak memory does not grow with the sweep
  size); cancel terminates the pool and drops cells whose chunks did not all
  finish, matching the previous semantics. `meta.workers` records the pool size.
- Worker policy (`sim_parallel.resolve_workers`): `cfg.n_workers` → `SIM_WORKERS`
  env/.env → auto. Auto = CPU count, capped by the task count and by run size
  (one worker per 250 paths), so smoke runs (< ~1000 paths) stay in-process where
  pool startup would dominate. Explicit settings always win.
- Measured (8-core box, 2000 paths, 5m bars, fixture): 31.2s serial → 10.7s with
  4 workers → 9.1s with 8 workers; day-PnL mean identical across all three.
- Settings UI: new "Simulator" section in the gear modal (top-right) with a
  worker-process field and apply, plus `GET/POST /api/settings/sim` (localhost
  only, like the Discord/IB sections). The POST writes `os.environ` first so the
  next run picks it up without a restart, then persists `SIM_WORKERS` to `.env`.
- `config.py`: `SIM_WORKERS` (default 0 = auto). `SimRunConfig.n_workers` (0 =
  auto, 1 = serial), validated `>= 0`.
- Tests: `tests/test_sim_parallel.py` (13) — serial/parallel bit-equality incl.
  sweep grids, worker resolution + size floor, pool cancel, chunk determinism;
  `tests/test_settings_api.py` +4 for the new endpoints; e2e
  `test_settings_modal_exposes_sim_workers` (Playwright, POST intercepted so the
  repo `.env` is never written by a test).
- E2E harness fix: the child server now boots with an empty `DOTENV_PATH` — the
  repo `.env` carries `DISCORD_TOKEN`, which the fixture's env-var strip did not
  cover, so boot spent ~15s on a Discord login and blew the 30s deadline (the
  pre-existing "e2e server-start error"). Deadline raised to 60s.
- Docs: README "Parallel execution" subsection, `SIM_WORKERS` in the config
  table, known-limitations entry updated.
- Full suite: 565 passed; the 8 failures are all pre-existing on pristine HEAD
  (6 sim_regression baseline drift, chain_fetcher GEX tolerance, FOMC date) —
  verified by stashing this change and re-running.

---

## Session: September 7, 2026 - Strategy-tuning skill + sim_tune.py runner

- Added `sim_tune.py` (repo root): deterministic variant runner for agent-driven
  strategy tuning. Takes a `variants.json` spec (global dataset/run/stress blocks +
  named knob variants), injects overridden `Strategy` objects in memory via the
  `state` stub (never writes `config/strategies.json`), runs one `execute_pipeline`
  call per variant, and writes `results.csv` / `results.json` into an experiment
  folder. Always runs the live config as the `baseline` control first; validates the
  knob whitelist up front and refuses sim-unsupported gates (trend, atm_iv,
  non-bull_put) before any compute; warns if `spx_fan` differs across variants
  (common-random-numbers check).
- Added project skill `.claude/skills/strategy-tuning/` (SKILL.md + references):
  a five-phase methodology — scope/freeze, baseline + noise floor, diagnose from
  the exit-reason breakdown, one-knob-at-a-time rounds with keep/revert decision
  log, multi-seed robustness gate, then a propose-only JSON diff report. Judgment
  over brute force: the agent picks the binding knob per round; the runner just
  executes deterministically. Includes the knob taxonomy (sim-consumed subset,
  effect directions, quantization traps) and the future family-mode extension
  design.
- Reproducibility property relied on by the methodology: for identical
  (seed, dataset, n_paths, single sweep cell) the sim replays bit-identical spot
  paths across strategy-knob variants (`SeedSequence(entropy=cfg.seed,
  spawn_key=(ci, ch))`; no RNG in the strategy engine), so per-round deltas are
  attributable to knobs alone.
- Tests: `tests/test_sim_tune.py` (10 tests) — knob whitelist/unset semantics,
  fail-fast on unsupported strategies, CRN `spx_fan` equality, byte-identical
  results across invocations, config-file-untouched guarantee, CSV/JSON schemas,
  multi-seed rows, CLI smoke end-to-end. Full suite: 551 passed; the 2 pre-existing
  failures (chain_fetcher GEX tolerance, FOMC date) and the e2e server-start error
  are unrelated to this change.
- README: new "Strategy tuning (agent workflow)" subsection under Simulation.

---

## Session: April 10, 2026 - Current update

- Added a broad backend refactor and new core service modules: `account_manager.py`, `app_state.py`, `chain_manager.py`, `config.py`, `ib_connection.py`, `order_manager.py`, `price_bars.py`, `risk_free.py`, `ws_handler.py`.
- Introduced a full frontend asset refresh in `static/css/` and `static/js/`, including updated option chain behavior, charts, order entry, strategy builder, session handling, and websocket integration.
- Added test coverage under `tests/` for account manager, chain fetcher, config, market hours, order placement, and risk-free functionality.
- Updated tracked code in `.gitignore`, `chain_fetcher.py`, `gex_calculator.py`, `requirements.txt`, `server.py`, and `static/index.html`.
- `.gitignore` now ignores `_old.*` and `*todo*` artifacts.
- Refreshed `requirements.txt` for the revised backend and new test dependencies.

---

## Bug Fixes

### Session: April 7, 2026 - RTH/GTH Mode, Refresh Cache, Graceful Shutdown, Viewport-Centered Chain Stream

#### GTH incorrectly showing LIVE mode and updating SPX intraday bars
**Problem:** During Global Trading Hours (GTH), the dashboard still showed LIVE mode and continued intraday bar updates as if SPX were in-session.

**Root cause:** SPX ticker updates unconditionally flipped `data_mode` to `"live"`, and the bar push loop did not strictly gate updates to RTH-only behavior.

**Fix (`server.py`):**
- SPX tick processing now sets live mode only during RTH (`is_within_rth()`).
- Outside RTH, mode is forced/kept as historical.
- Intraday bar generation now runs only when `data_mode == "live"` and during RTH.
- Status loop includes a safety guard to keep non-RTH mode historical.

Result: in GTH, dashboard mode remains historical and intraday chart stays on the last completed regular session.

#### Off-hours GEX spot did not consistently reflect ES-derived SPX regime
**Problem:** In off-hours contexts, behavior could drift back toward SPX-live assumptions.

**Fix (`server.py`):**
- Preserved ES-derived spot path for non-live mode and prevented SPX off-hours ticks from forcing live mode.

Result: GEX off-hours spot behavior stays aligned with ES-derived SPX logic.

#### Manual option-tab refresh did not guarantee full qualification reset
**Problem:** Manual refresh from the Option Chain tab did not explicitly clear qualification cache state before re-fetching.

**Fix (`chain_fetcher.py`, `server.py`):**
- Added `clear_qualification_cache(reason=...)` helper.
- On `refresh_chain` WebSocket command, server now clears qualification cache before triggering refresh.

Result: manual refresh now forces clean re-qualification from scratch.

#### Ctrl+C / terminate shutdown handling
**Problem:** Server shutdown relied mainly on default behavior and could be less explicit under different termination paths.

**Fix (`server.py`):**
- Added signal handlers for `SIGINT`, `SIGTERM`, and `SIGBREAK` (Windows where available).
- First signal requests graceful exit (`server.should_exit = True`), second signal forces exit.
- Added final loop teardown path: cancel pending tasks, await cancellation, shutdown async generators, close event loop.

Result: cleaner and more deterministic service shutdown on Ctrl+C / terminate.

---

## Features

### Session: April 7, 2026 - Viewport-Centered Option Chain Livestream

**Goal:** Stream live option quotes around the strikes the user is currently viewing, not only around current SPX.

**Backend (`server.py`):**
- Added viewport center state: `viewport_center_strike`, `viewport_center_last_ts`.
- Added WebSocket message handling for `viewport_center:<strike>`.
- Added server-side throttling (`VIEWPORT_CENTER_MIN_INTERVAL`, default 0.2s).
- `chain_stream_loop()` selection center now uses viewport center when available on chain tab, otherwise falls back to spot.

**Frontend (`static/index.html`):**
- Added center-strike detection from `#chainTableWrap` visible midpoint.
- Added throttled center reporting to server (200ms client throttle).
- Sends center updates on:
  - option-chain scroll,
  - chain tab activation,
  - chain table rerender,
  - ATM auto-scroll,
  - WebSocket reconnect,
  - window resize (while on chain tab).

Result: as users scroll the option chain, live subscriptions migrate to strikes near the visible center of the screen.

### OI Always Zero
**Problem:** Server logged "OI is all zeros" despite real open-interest data being visible in TWS.

**Root cause:** `ib.reqMktData(..., snapshot=True)` silently ignores `genericTickList`, so tick type 101 (open interest) was never requested. Additionally the code was reading `ticker.openInterest`, a field that does not exist on ib_insync `Ticker`.

**Fix (`chain_fetcher.py`):**
- Switched `_snapshot_batch` to `snapshot=False` with `genericTickList='101'` and manual `cancelMktData` after a 12-second timeout.
- `_ticker_to_option_data` now reads `ticker.callOpenInterest` (calls) / `ticker.putOpenInterest` (puts) — both are `float`, `nan` when not yet received.
- Added OI summary log line: `"Fetched data for N options — OI: X/N contracts with non-zero OI, total OI=..."`.

---

### Hotfix: Intraday bars gap during RTH
**Problem:** When the server started during Regular Trading Hours (RTH) the price chart only showed a few recent live bars (gap between historical data and now), leaving most of the session empty.

**Root cause:** Historical fetch used a fixed future `endDateTime` (e.g. 16:30), which causes IB to return only fully processed bars (30–60 minute lag) and not the most-recent intraday minutes. Also, startup historically-only logic skipped seeding today's bars when starting during RTH.

**Fixes (server.py / static/index.html):**
- Always fetch intraday bars at startup so the chart is seeded with today's session (or the last session when outside RTH).
- During RTH, call IB historical with `endDateTime = ""` so IB returns bars up to the current minute.
- Preserve `data_mode = "live"` if live ticks have already been detected (avoid regressing to historical mode).
- Prevent duplicate minute bars at the historical/live seam: live push now dedupes before appending and inherits partial last-bar OHLC when appropriate.
- Frontend: fully filled candle bodies (no alpha transparency) and a clear vertical session-start marker with a date label when prior-day bars are present.
- UI accessibility: added IV Smile title help indicator with hover/focus tooltip for delta-decay efficiency definition and fixed literal `\n` to actual line breaks.
- Added GEX chart title help tooltip for GEX meaning, and hover titles for Call Wall/Put Wall/Gamma Flip/Max Pain/Net GEX badges; MM regime badge hover text also present.

Result: the price chart now fills the full session from 09:30 through the current minute immediately after startup, with no 12:30→13:30 hole.

---

### Dashboard Snapshot First, Chain Stream Second
**Problem:** Chain streaming could begin before the initial dashboard snapshot was fully loaded, causing the dashboard to remain empty until the next refresh.

**Fix:** Startup now forces one full chain snapshot before starting option-chain live streaming. The dashboard snapshot refreshes on a fixed 5-minute cadence independent of the option-chain stream.

**Implementation:**
- `server.py` startup now triggers an immediate snapshot and waits for `latest_gex` + `chain_data` before starting `chain_stream_loop()`.
- The snapshot loop uses `SNAPSHOT_REFRESH_SECONDS` (default 300) and is no longer tied to active tab state.
- `gex` messages are broadcast for dashboard updates regardless of whether the chain tab is active.

---

### Error 321 on Snapshot Request
**Problem:** IB returned Error 321 when `genericTickList` was set with `snapshot=True`.

**Fix:** `snapshot=False` is required for any non-empty `genericTickList`. The batch function now streams data and cancels subscriptions manually.

---

### ES Contract Ambiguity on Qualify
**Problem:** Calling `ib.qualifyContracts()` on a generic `Future('ES', 'CME', 'USD')` returned multiple contracts, causing an exception.

**Fix (`server.py` — `setup_es_subscription`):** Use `reqContractDetails` on the generic Future, filter results to unexpired contracts, sort ascending by expiry, and use `details[0].contract` directly — no re-qualification needed.

---

### Historical Bars Duration Format
**Problem:** IB rejected `'10 mins'` as a duration string, causing Error 321 on historical data requests.

**Fix (`server.py`):** Use IB's required format: `'600 S'` (integer + space + unit).

---

## Features

### ±8σ Strike Range Filter
Chain fetch now filters strikes to within **±8 daily standard deviations** of spot (previously ±10σ), using the runtime-computed annualised vol. This reduces the number of contracts fetched while still covering all practically relevant strikes.

**File:** `chain_fetcher.py` — `_strike_range_for_std_devs`, `fetch_option_chain(std_dev_range=8.0)`

---

### Net GEX Value + MM Hedging Regime
The GEX chart and levels strip now display:

- **Net GEX** badge: total net gamma exposure formatted as `+1.23B` / `-450M` / `+12.3K`.
- **MM Regime** badge: `CONVERGING ▼` (green, Net GEX > 0 — market makers are short gamma, hedging acts as a stabiliser) or `DIVERGING ▲` (red, Net GEX < 0 — hedging amplifies moves).
- **Annotation box** (top-right of GEX chart): shows Net GEX value + regime label with a colour-coded border (green/red).

**Files:** `gex_calculator.py` (`GEXResult.net_gex`), `static/index.html` (badges + Plotly annotation).

---

### ES-Derived Off-Hours SPX Price
When markets are outside RTH (09:30–16:15 ET), the dashboard derives a synthetic SPX price from the ES front-month futures move:

```
spx_derived = spx_last_close × (1 + (es_now − es_baseline) / es_baseline)
```

- `es_baseline` = ES price at the last SPX close (~16:15 ET), fetched from 600 seconds of 1-minute TRADES bars.
- ES front-month contract is selected via `reqContractDetails`, filtered to unexpired, sorted by expiry (nearest first).
- The GEX chart spot line label changes to **"ES derived SPX: XXXX"** (yellow) during off-hours and **"SPX: XXXX"** (white) during live.

**Files:** `server.py` (`AppState.es_*`, `setup_es_subscription`, `fetch_es_baseline`, `on_pending_tickers`), `static/index.html` (spot line label colour).

---

### Strategy Builder Delta + Strike Sigma UX
- Added combined strategy delta in the Strategy Builder summary and per-leg delta display in the strategy table.
- Backend now includes expiry-based sigma metadata in `chain_quotes` (`expiration_raw`, `tte_years`, `sigma_move`) plus per-strike `sigma_distance_abs` and `sigma_distance_signed`.
- Frontend renders strike-cell sigma buckets and hover tooltips, and updated wall marker borders so Call Wall has a green left border, Put Wall has a red right border, and the ATM strike is marked with white borders on both sides.

**Files:** `server.py`, `static/index.html`.

---

### Chain Fetch Progress — Startup Spinner Overlay
During the initial chain fetch (before any GEX data is available), the GEX chart panel shows a centred rotating loading circle with a darkened background overlay.

**Behaviour:**
- Spinner appears on startup/reconnect only — recurring background refreshes run silently.
- Phase text updates: *Qualifying contracts…* → *Streaming market data…* → *Computing GEX…*
- Sub-text shows batch progress: *Batch N of M* during the streaming phase.
- Server broadcasts `chain_progress` WebSocket events with `phase` / `batch` / `total_batches` / `pct`.

**Files:**
- `server.py`: broadcasts `chain_progress` events at start, each batch, and completion.
- `chain_fetcher.py`: `fetch_option_chain(progress_callback=...)` — async callback called after each batch.
- `static/index.html`: `.gex-loading` CSS overlay, `#gexLoading` HTML element, `handleChainProgress(data)` JS function.

**Suppression logic (`index.html`):**
```js
// only show overlay if no GEX data received yet
if (state.gex) return;
overlay.classList.remove('hidden');
```

---

## Session: April 1, 2026 — IV Smile & Synchronized Charts

### Added 3rd Chart with IV Smile & Delta-Decay Efficiency
**New dashboard chart:** IV Smile & Delta-Decay Efficiency — displays implied volatility curves and delta-decay efficiency metrics across strikes.

**UI Layout:**
- **Top subplot (Calls):** Call IV curve (green) + Call efficiency = |charm| / |delta| (yellow, dotted)
- **Bottom subplot (Puts):** Put IV curve (red) + Put efficiency (yellow, dotted)
- **Key lines:** Spot price (white dotted), Call Wall, Put Wall, Gamma Flip (dashed)
- **Hover tooltip:** Strike, IV %, Delta, Charm, Efficiency
- **Loading spinner:** Synced with GEX chart during initial option chain fetch

**Data Computation (`gex_calculator.py`):**
- Added **Black-Scholes delta calculation** — no scipy dependency (uses `math.erf` for norm CDF)
  - `_norm_cdf()` — standard normal CDF
  - `_bsm_delta()` — European option delta at any S, K, T, σ, r
  - `_compute_charm_fd()` — delta decay rate via 15-minute finite-difference

- Extended `GEXResult` dataclass to hold per-strike IV, delta, charm, and efficiency data
- Updated `compute_gex()` signature to accept `time_to_expiry_years` and `risk_free_rate` parameters
- Added `_build_smile_data()` function — formats smile data for frontend (strike-level IV, delta, charm, efficiency)

**Time-to-Expiry Calculation (`server.py`):**
- For 0DTE: minutes remaining until 16:00 ET close, converted to trading-year fractions
- For multi-day expirations: calendar days remaining × (390 min/day) / (390 × 252 trading days/year)
- Passed to `compute_gex()` for charm calculation

**Frontend (`static/index.html`):**
- New `initSmileChart()`, `updateSmileChart()` functions
- Grid layout: 3 rows (price:gex:smile = 1:1:1)
- Responsive margins and font sizing for mobile/desktop

### Synchronized Horizontal Zoom Between GEX & Smile Charts
Both charts compute a **common x-axis range** from the smile data strikes and apply it to their respective x-axes.

**Sync Logic:**
- GEX chart zoom event → updates Smile chart's xaxis + xaxis2 range
- Smile chart zoom event → updates GEX chart's xaxis range
- Both charts always show identical strike price positions (pixel-perfect alignment)
- Example: strike 5600 is now at the same horizontal pixel location in both charts

**Implementation:**
- Event listeners on `plotly_relayout` for both charts
- Sync applies to both zoom and auto-range operations
- Common range calculated as `[min(strikes) - 1%, max(strikes) + 1%]` for padding

### Enhanced GEX Chart Metrics
**Updates to GEX chart:**
- Added **Net GEX line** (yellow) overlaid on call/put bars for visual clarity
- Enhanced top-right annotation box with:
  - **Call OI** (green) / **Put OI** (red) — total for all strikes
  - **P/C OI Ratio** — put OI / call OI (green if ≤1, red if >1)
  - **Call GEX %** — call GEX / gross GEX (green if ≥50%, red if <50%)
- Hover tooltips now include:
  - Call/Put GEX values
  - Call/Put OI per strike
  - Call/Put volume per strike

**Extended `GEXResult` dataclass:**
- Added `call_oi_by_strike`, `put_oi_by_strike`, totals
- Added `call_vol_by_strike`, `put_vol_by_strike`, totals
- Added IV, delta, charm maps: `call_iv_by_strike`, `put_iv_by_strike`, etc.

### Tightened Strike Filtering
Changed default `std_dev_range` from **8.0 to 5.0** in `chain_fetcher.py`:
- Covers ±5 daily standard deviations (~99.99% of probability mass)
- Reduces fetch time by ~30-40% compared to ±8σ
- Lower memory footprint, faster computation

### Minor Updates
- Updated FastAPI title: "SPX 0DTE GEX Dashboard" → "SPX 0DTE Option Dashboard" (reflects broader feature set)
- Updated [README.md](README.md) title to match

---

## Session: April 14-15, 2026 — Monthly SPX GEX Toggle

### 0DTE ↔ Monthly SPX GEX Mode Toggle on Dashboard

**Goal:** Add a segmented toggle ("0DTE | Monthly") on the Dashboard GEX chart panel so both the GEX-by-strike chart and IV Smile chart can display either the current 0DTE SPXW chain or the monthly SPX option chain (3rd Friday expiry). The price chart and header badges remain fixed to 0DTE data.

**Implementation:**

#### `chain_fetcher.py`
- `fetch_option_chain()` now accepts a `trading_class: str = 'SPXW'` parameter; the IB `Option` contract constructor uses the passed value instead of hardcoded `'SPXW'`.
- Added `_monthly_qualification_cache = QualificationCache()` — a separate qualification cache for SPX contracts to prevent collisions with the SPXW cache. Cache selection is driven by `trading_class`.
- `clear_qualification_cache(monthly: bool = False)` — clears either cache.
- Added `get_monthly_chain_params(ib, underlying)` — calls `reqSecDefOptParamsAsync`, filters for `tradingClass='SPX'` and `exchange='SMART'`, returns expirations/strikes.
- Added `find_monthly_expiration(expirations)` — calculates the 3rd Friday of the current month using `timedelta`. If that date is already past, returns next month's 3rd Friday. Falls back to the first expiration ≥ today. Verified: April 2026 → `20260417`.

#### `app_state.py`
- Added 8 new fields: `gex_mode`, `monthly_expiration`, `monthly_expirations`, `monthly_strikes`, `monthly_gex_result`, `monthly_latest_gex`, `monthly_chain_data`, `monthly_last_fetch_ts`.

#### `ib_connection.py`
- Added `setup_monthly_chain_info(ib, state)` function — calls `get_monthly_chain_params()` and `find_monthly_expiration()` at startup to populate `state.monthly_expirations`, `state.monthly_strikes`, `state.monthly_expiration`.

#### `chain_manager.py`
- Added `MONTHLY_CACHE_TTL = 600` (10 minutes).
- Added `monthly_gex_fetch(ib, state, broadcast_fn)` — on-demand async function that:
  - Returns cached data immediately if fresh (< TTL).
  - Otherwise calls `fetch_option_chain(..., trading_class='SPX')`, handles OI=0 fallback, computes `tte_years` (minutes-to-close for same-day expirations, else calendar days/252), calls `compute_gex()`, stores in `state.monthly_*` fields.
  - Broadcasts `{"type": "monthly_gex", ...}` and `{"type": "monthly_gex_progress", ...}`.

#### `ws_handler.py`
- `init` payload extended with `"gex_mode"`, `"monthly_gex"`, and `"monthly_expiration"`.
- New message handlers:
  - `set_gex_mode:monthly` → sets `state.gex_mode = "monthly"`, fires `asyncio.create_task(monthly_gex_fetch(...))`.
  - `set_gex_mode:0dte` → sets `state.gex_mode = "0dte"`, re-broadcasts cached 0DTE GEX if available.

#### `server.py`
- Startup lifespan and `/api/reconnect_ib` endpoint call `await setup_monthly_chain_info(ib, state)` after `setup_chain_info`.
- `/api/state` response includes `"gex_mode"`, `"monthly_gex"`, `"monthly_expiration"`.

#### `static/index.html`
- GEX chart title replaced with `<span id="gexChartLabel">` + segmented toggle:
  ```html
  <span class="gex-mode-toggle" id="gexModeToggle">
      <button class="gex-mode-btn active" data-mode="0dte" onclick="setGexMode('0dte')">0DTE</button>
      <button class="gex-mode-btn" data-mode="monthly" onclick="setGexMode('monthly')">Monthly</button>
  </span>
  ```
- IV Smile chart title wrapped in `<span id="smileChartLabel">` for dynamic text updates.

#### `static/js/state.js`
- Added 3 fields: `gexMode: '0dte'`, `monthlyGex: null`, `monthlyExpiration: ''`.

#### `static/js/charts.js`
- `updateGexChart()` and `updateSmileChart()` now resolve `gexData = state.gexMode === 'monthly' ? state.monthlyGex : state.gex` before rendering — all `state.gex.*` references replaced with `gexData.*`.
- Added `setGexMode(mode)` — validates mode, updates `state.gexMode`, calls `updateGexModeToggle()`, sends `set_gex_mode:<mode>` over WS, re-renders charts.
- Added `updateGexModeToggle()` — syncs `.active` class on toggle buttons; updates `gexChartLabel` and `smileChartLabel` text to include monthly expiration date when in Monthly mode.
- Added `handleMonthlyGexProgress(data)` — shows/hides `gexLoading`/`smileLoading` overlays during monthly fetch; suppressed if `monthlyGex` data already cached.

#### `static/js/strategy-builder.js`
- Added `monthly_gex` message case — stores `state.monthlyGex`, formats expiration display string, calls `updateGexModeToggle()`, triggers chart re-renders if in Monthly mode.
- Added `monthly_gex_progress` message case — calls `handleMonthlyGexProgress(msg.data)`.
- `handleInit()` restores `gex_mode`, `monthlyGex`, `monthlyExpiration` from init payload and calls `updateGexModeToggle()`.

#### `static/css/charts.css`
- Added `.gex-mode-toggle` and `.gex-mode-btn` styles — segmented button control with dark theme (`#1e293b` background, `#22c55e` active state with `#0f172a` text).

**Design Decisions:**
- Monthly data is fetched **on-demand** (not looped) to conserve IB market data lines.
- 10-minute TTL prevents redundant full chain fetches while keeping data reasonably fresh.
- Separate qualification caches prevent SPXW/SPX contract metadata from colliding.
- Price chart (1st graph) and header badges are always driven by 0DTE data regardless of mode.

**Files:** `chain_fetcher.py`, `chain_manager.py`, `app_state.py`, `ib_connection.py`, `ws_handler.py`, `server.py`, `static/index.html`, `static/js/charts.js`, `static/js/strategy-builder.js`, `static/js/state.js`, `static/css/charts.css`

**Result:** Toggle appears in the GEX chart title bar. Clicking "Monthly" triggers an on-demand fetch of the SPX monthly chain (3rd Friday expiry), renders GEX-by-strike and IV Smile for that contract, and labels both chart titles with the expiration date. Clicking "0DTE" instantly reverts to the live 0DTE SPXW data. 91/92 tests pass (1 pre-existing BSM precision failure unrelated to this feature).

---

## Architecture Notes

- **IB data flow:** `ib.pendingTickersEvent` (async) → `on_pending_tickers` → updates `state.spx_price` / `state.es_price` → derives ES-based SPX when not in RTH.
- **Chain loop cadence:** every `CHAIN_REFRESH_SECONDS` (default 60 s); skipped during the CBOE daily maintenance gap (17:00–20:15 ET).
- **Vol estimate:** `compute_annual_vol()` fetches 30 days of daily bars from IB and computes annualised historical vol; falls back to 20% if unavailable.
- **Mode labels:** `state.data_mode` = `"live"` | `"historical"` | `"initializing"` — broadcast in every `status` and `gex` WebSocket message.

---

## Native API spike

Task 1 of the native ibapi migration: spike latency probe comparing native `ibapi` vs `ib_insync` against TWS paper (`127.0.0.1:7497`). Throwaway probe lives in `tests/spikes/spike_native_latency.py` (not wired into the app). Run with `python tests/spikes/spike_native_latency.py` (native) or `python tests/spikes/spike_native_latency.py --insync`.

**Method:** The brief's far-OTM SPXW call (strike 100, exp 20260918) is **not listed** — TWS returns error 200 "No security definition has been found". Per the controller ruling, the probe falls back to qualifying AAPL and placing a resting GTC buy-limit at $5.00 (AAPL ~$200 — can never fill), measuring orderStatus latency identically, then cancelling. Every run exercised the fallback. `details_ms` times the successful `reqContractDetails` request itself (native from `req_t0`, insync from the `reqContractDetailsAsync` call), so both sides measure the same round trip. The brief's `time.sleep(8)` was raised to a `TOTAL_TIMEOUT = 12 s` cap; runs complete in ~1 s (insync), ~1 s (native, immediate error path) or ~5.5 s (native, watchdog path when the error-200 response took >5 s).

**Latencies (single-shot, 1 order each, paper):**

| metric | native ibapi | ib_insync |
|---|---|---|
| connect → nextValidId | 5.8 / 7.8 / 9.6 ms | 117.1 / 120.2 / 313.4 ms |
| reqContractDetails (AAPL) | 80.8 / 81.2 ms | 73.9 / 75.0 / 83.7 / 84.0 ms |
| placeOrder → orderStatus(PreSubmitted) | 115.7 / 121.2 / 121.5 ms | 108.0 / 112.4 / 226.6 ms |

**Result:** premise validated. Native `ibapi` is dramatically faster at connection establishment — `connect→nextValidId` ~6–10 ms vs ~117–320 ms for ib_insync (roughly 15–50×). `reqContractDetails` and `placeOrder→orderStatus` latencies are on par (~80 ms and ~115–120 ms native vs ~75–84 ms and ~108–227 ms insync); both are dominated by TWS/SMART network round-trip time, with ib_insync showing noticeably higher variance. No latency penalty in native ibapi that would block the migration; the one caveat is that an unlisted-contract `reqContractDetails` produces error 200 and can take up to ~5 s to respond, which the client wrapper will need to treat as a "no contract found" condition.

*Note (Task 19):* the throwaway probe `tests/spikes/spike_native_latency.py` was deleted per the Task-19 controller ruling — its purpose was served and it was the last ib_insync dependency besides the explicitly manual `tests/manual_spread_probe.py`.

---

## Native API migration — snapshot latency

Task 19 (full-suite parity + extended paper smoke, 2026-08-21). Paper TWS on `127.0.0.1:7497`, expiry `20260821`, 720 strikes available, 191 quote rows broadcast.

### Full suite
`python -m pytest -v` → **165 passed, 1 failed** (~10 s). The single failure is the pre-existing `test_chain_fetcher.py::test_compute_gex_uses_bsm_gamma_when_ib_gamma_missing` — a BSM-gamma math-tolerance failure (`abs(0.5924) < 0.01`; off by ~0.59). Confirmed pre-existing: the test and `gex_calculator.py` both exist at the migration baseline (`62ab676`) and `gex_calculator.py` is untouched by the migration.

### ib_insync grep
No `ib_insync` import remains in production source. Remaining matches after deleting the spike are docstring/comment references ("No ib_insync." in `chain_fetcher.py`/`ib_connection.py`) and one real import in `tests/manual_spread_probe.py:27` (`from ib_insync import ...`) — an explicitly manual probe (docstring: "not part of the automated pytest suite") that `test_manual_spread_probe.py` imports; it is the last live ib_insync dependency to resolve in Task 20 (hard cut).

### Chain snapshot latency (native bridge)
| metric | measured |
|---|---|
| full snapshot `starting` → `done` | **~30 s** |
| `qualifying` → `done` | ~20 s |
| 8 fetch batches (`fetching` 1/8→8/8) | ~17.5 s (~2.2 s/batch) |
| `computing` → `done` | ~0.02 s |
| post-reconnect snapshot | ~31 s |

vs the pre-migration ~60 s, the native bridge roughly **halves** full-snapshot time. The 6 s per-batch timeout ceiling is **not** the binding constraint — batches complete in ~2.2 s each. The bottleneck is the contract count (8 batches × ~50 contracts) plus ~10 s in the pre-qualify phase (`compute_annual_vol` over 30 days of daily bars + SPXW contract qualification).

### Order path (paper, driven over `ws://127.0.0.1:8000/ws`)
- **place** AAPL GTC buy-limit $5 → `order_status PreSubmitted` id 263, ack in ~0.43 s.
- **cancel** → `Cancelled` in ~0.10 s (IB code 202 "Order Canceled").
- **bracket stop-loss attach** (stopLoss 4.00/4.00) → parent id 264 `PreSubmitted` with message referencing stop child `orderId=265` (STP LMT). Child held at PreSubmitted (parent transmit=False, matching unit-test behavior `test_stop_order_stays_presubmitted_until_parent_fills`); on parent cancel the stop child cascaded to `Cancelled` ("Stop order cancelled (parent Cancelled)"). One benign race observed: IB code 10148 ("OrderId 265 ... cannot be cancelled, state: Cancelled") — the watcher tried to cancel the child just after IB had already cancelled it.

### Reconnect
`POST /api/reconnect_ib {"port": 7497}` → **HTTP 200** `{"status":"ok","port":7497}` in ~14.6 s. Streaming resumed (status / gex / chain_progress / chain_quotes all flowing; 0 actionable IB errors in the observation window) and a fresh snapshot completed in ~31 s.

### Shutdown
CTRL_BREAK_EVENT (SIGBREAK) → `Shutdown signal received (SIGBREAK); stopping server gracefully...` → `Shutting down...` → `Disconnected from IB` → `Server shutdown complete`; **exit rc=0**.

### Environment note (not a migration regression)
At startup the TWS data farms were mid-reconnect: historical bars and live ticks returned error 162 ("Historical Market Data Service ... connected from a different IP address"), 10197 ("No market data during competing live session") and 2103 ("Market data farm connection is broken") for the first ~3 min, so the startup snapshot wait timed out with `spx_price=0`. The app's retry loop recovered once a live SPX tick landed (spot 7676.40) and full snapshots then completed normally. The native bridge surfaced and logged every error; the app followed its documented retry path.

---

## Task 20 — hard cut: ib_insync removed (2026-08-21)

`ib_insync` is fully removed from the dependency surface. `requirements.txt` now installs the native `ibapi` client from the local TWS API source (`-e file:///C:/TWS%20API/source/pythonclient`, equivalent `pip install -e "C:\TWS API\source\pythonclient"`); `README.md` documents `ibapi` in the stack table and Quick Start.

**Probe removal:** `tests/manual_spread_probe.py` and its test `tests/test_manual_spread_probe.py` were deleted. The probe was an explicitly manual tool ("not part of the automated pytest suite"), built entirely on ib_insync's async object model (`IB()`, `connectAsync`, `qualifyContractsAsync`, `reqMktData`, `placeOrder`→`Trade`, `errorEvent +=`), and passed an ib_insync `IB()` into native `handle_place_order`/`handle_cancel_order`/`get_chain_params` — object models that no longer line up after Tasks 2–19. Its combo-construction logic (`build_payload`) is now implemented natively in `order_manager._place_multi_leg`; the other tested helpers were consumed only by the probe itself. Nothing in production referenced it.

**Verification:** `python -m pytest -v` → all pass except the pre-existing `test_chain_fetcher.py::test_compute_gex_uses_bsm_gamma_when_ib_gamma_missing` gex-math failure. `grep -rn "ib_insync" --include="*.py" .` → **0 matches** (docstring/comment references in `chain_fetcher.py`, `ib_connection.py`, and `tests/spikes/smoke_bridge_mock.py` were reworded). `python -c "import ibapi"` OK. `python -c "import server"` boots cleanly with ib_insync absent from the code path.

- Discord support: in-process bot (query / arm-disarm / live candidates, curated alert stream). Orders remain web-only — see `.superpowers/sdd/2026-08-25-discord-support/`.

## Session: September 30, 2026 - Trade log analysis port

- Ported the trade-log analysis engine from `trade_pnl_dashboard` into
  `spx_trade_desk/tradelog/` (`io/`, `domain/`, `analysis/`, `report/`), moving
  modules rather than rewriting them; the source repo's own 103-test suite passes
  with import-path changes only.
- Extracted `_build_calendar_matrix` out of the Streamlit UI module into
  `tradelog/domain/calendar.py`, so the MCP server no longer needs streamlit or
  plotly to build calendars (asserted by a subprocess isolation test).
- Added `spx_trade_desk/mcp/` — a FastMCP stdio server exposing 11 tools — and
  registered it in `.mcp.json` (Claude Code prompts for approval on first use).
- Added `tradelog/analysis/tagging.py`: scores reconstructed spreads against
  `config/strategies.json` entry conditions as pass / fail / unverifiable, with
  the short delta implied from the spread's own credit. Read-only.
- Added `pandas`, `pdfplumber`, `mcp` to `requirements.txt`; committed the SPX/VIX
  seed CSVs under `reports/data/` and gitignored `reports/output/`.
- Removed the duplicated OCC symbol builder in the PDF loader in favour of the
  shared `parse_option_symbol.build_occ_symbol`.
- Whole-branch review fix pass: the compliance tool now loads SPX/VIX closes so the
  delta gate is decidable (it previously reported every strategy as matching 0 fills,
  because a missing market frame left the delta permanently unverifiable); unpaired
  shorts are unverifiable rather than false failures; `exit_audit` measures a paired
  spread against its net credit (the live engine's stop rule) and labels the basis;
  outputs carry a `source` column because spread ids restart per file.
- Known minor gaps, deferred deliberately: `strategy_config_fingerprint(<directory>)`
  raises instead of reporting "no strategies configured"; the summary's
  `failure_counts` counts only failures, so a row with 0 failures and N unverifiable can
  read as clean to a skimmer; and no test pins a realistic credit/delta pair against a
  realistic band (the solver's own round-trip test covers its correctness).

## Session: September 30, 2026 - Trade log: `.xlsx` workbook loader

- Added `tradelog/io/load_xlsx.py`: reads the consolidated IBKR + E*Trade trade-log
  workbook (tabs `Index Options` / `Other Options` / `Stock & ETF` / `Other Transactions`)
  into the standard 11-column transaction frame, and routed `.xlsx` / `.xlsm` through the
  MCP server (`_load_file_from_path`, `_load_file_from_bytes`, pass 2 of `_load_and_merge`).
  Pass it by path, or as `data_base64` in `file_contents`.
- The sheet's conventions differ from every other loader's input, so the loader converts
  them: quantity is positive in the sheet and is signed here (Buy +, Sell -), because the
  engine's expire-inference only fires on a negative Sell quantity; `Net Amount` is already
  signed and is taken verbatim; `Buy*` / `Sell*` order types (Buy Open, Sell To Open,
  Buy To Close, ...) collapse to Buy / Sell while Cash Settlement, Dividend and Other Fee
  pass through by name.
- Commission is taken as recorded, which means different things per broker: IBKR's column
  includes all fees, while E*Trade's is only the broker's own charge (exchange fees are
  already in the recorded fill price). So commission is the column's magnitude and
  `gross_amount` is net with it added back, for both. The E*Trade CSV loader instead
  reports the whole gap between `qty x price` and the net as commission, so commission
  totals (and commission drag) differ between loading the same E*Trade trades as CSV and
  as workbook; that is known and accepted. Realized P&L uses `net_amount` and is the
  same either way.
- E*Trade option symbols (`SPXW MAR 10 '25 $5100 PUT`) are rebuilt as OCC from
  `Description` with the E*Trade CSV loader's own pattern. E*Trade rows take the same
  account id as an E*Trade CSV (the real id of a loaded E*Trade PDF, so those still dedup).
- Columns are matched by header name, so dashboard cells beside the ledger are ignored; a
  tab without the ledger header is skipped; each tab's data ends at its first blank `Date`.
- Decision: the workbook is a standalone ledger. Its IBKR account id is masked
  (`U***12345`), so it cannot be matched to a QFX statement's account; loading both would
  double count, and `_load_and_merge` now warns when it sees that combination. Mapping the
  masked id onto a real QFX account was considered and deliberately not built.
- The workbook carries no balances, so capital falls back to `initial_capital` or the
  $100,000 default, as with an E*Trade CSV. `analyze_strategy_compliance` stays QFX-only (no
  intraday timestamps in the workbook); `generate_monthly_report` accepts the workbook, see the
  next entry.
- Added `openpyxl>=3.1` to `requirements.txt` (it was installed but undeclared).
- Tests: `tests/test_tradelog_io_xlsx.py` (37), built from a synthetic workbook, no real
  statement data. Covers the shape, every conversion above, input forms (path / bytes /
  buffer / base64), bad input becoming a warning, the QFX double-count warning,
  `get_transaction_summary` / `compute_daily_pnl` end to end, and that the same E*Trade
  trades load with identical identity columns (symbol, quantity, price, net) from the
  workbook and from the E*Trade CSV, with commission deliberately not compared.
  Each conversion rule was mutation-checked: breaking it makes a test fail.
- Known, pre-existing, not changed: the E*Trade CSV loader has no `Buy To Close` mapping,
  so it would load such a row with zero P&L (the workbook loader handles it). (The bare
  `KeyError: 'is_option'` that `generate_monthly_report` raised for any non-QFX input is fixed
  in the next entry.)

## Session: October 1, 2026 - Repository hygiene audit (public repo)

- Scope: the working tree, ignored files, every commit on every ref, PR/issue text, and the
  repo's forks. Method: path sweep, secret-pattern scan of each history diff, and a search
  for every `.env` value across all revisions.
- No credential ever reached git. The Discord token and IDs live only in the git-ignored
  `.env`; none appears in any revision, PR or issue.
- Found and fixed: the live parameters of one strategy were quoted in the strategy-tuning
  skill docs (knobs table, eval prompts, a BEFORE/AFTER example) and a real strategy name
  was used as test data; a partially masked real broker account id and real commission
  totals were in the in-progress `.xlsx` docs and tests. All replaced with placeholders
  (`MyStrategy`, "see config", the synthetic account id `U***12345`).
- Policy: tracked docs, tests and skills never quote values read from
  `config/strategies.json`, a broker statement or the account panel. Test account ids are
  synthetic. A screenshot of the live Account tab is acceptable only for a paper account.
- `.gitignore` now also covers `.env.*`, `*.tmp` (the stores write `config/strategies.tmp`
  and `.env.tmp` while saving), `config/strategies.*`, statement types (`*.qfx *.ofx *.qbo
  *.xlsx *.xlsm *.xls *.pdf`) and directories, key material, `.mcp.json`,
  `.claude/settings.local.json`, `.playwright-mcp/`. `.mcp.json` pins a machine-specific
  interpreter path, so it is untracked; copy `.mcp.json.example`. `docs/superpowers/` was
  already ignored but still tracked; it is now untracked (files stay on disk).
- GitHub: history was rewritten with `git filter-branch` (index-filter, 225 commits, five
  branches force-pushed; a full bundle backup was taken first) to drop the values and
  `.mcp.json` from every commit. Limits a push cannot fix: GitHub's own `refs/pull/*` heads
  for three merged PRs still hold the pre-rewrite commits, and one third-party fork copied
  the skill docs before the rewrite. The first needs a GitHub Support request.
- Follow-up: `*.csv` is now ignored except `reports/data/` and `tests/fixtures/` (IBKR and
  E*Trade exports are CSV). A local `.git/hooks/pre-commit` (POSIX sh, not versioned, not
  pushed) rejects staged statement/data files (`csv tsv pdf xlsx xlsm xls qfx ofx qbo`
  outside those two folders), anything under the repo-root `data/` directory (local market data,
  added October 5, 2026; `reports/data/` is unaffected) and secrets (`.env*`, `config/strategies.*`,
  `config/sim_smile.json`, `.mcp.json`, key files) even after `git add -f`. Deliberate
  override: `git commit --no-verify`. A fresh clone does not have the hook.

## Session: October 1, 2026 - Monthly report from the `.xlsx` trade log

- `generate_monthly_report` / `build_report` now accept the trade-log workbook as well as a QFX statement.
  Any other file type is rejected up front with a message naming the file and the accepted types (this
  replaces the bare `KeyError: 'is_option'` a CSV used to produce). Design:
  `docs/superpowers/specs/2026-10-01-xlsx-monthly-report-design.md` (local planning doc).
- Decision: **position direction is not inferred.** A date-only ledger cannot say whether a contract that was
  both bought and sold in one day was a short bought back or a long sold. Two structural rules were tried on a
  real year-long ledger and rejected: the "higher strike of a matched spread is the short" rule disagreed with
  an independent net-position signal more often than not, and anchoring on one-sided legs gave clear evidence
  for only a minority of the fully closed contracts. Those contracts are exactly the stops and early exits, so
  a wrong label would corrupt the analysis that needs direction. A workbook report is therefore day-level.
- Workbook report: SPX/SPXW option rows only (stocks, dividends, fees and other underlyings are excluded);
  sections 1-6, the day-level bootstrap and Monte Carlo are exact; sections 7 (structure), 9 (stops/re-entry)
  and 11 (Kelly) show a "Needs QFX timestamps" notice and the gap-stress table is omitted;
  `report_data` carries `source` and `unavailable_sections` and nulls for the unavailable blocks. A provenance
  box states the source, accounts, scope, capital (assumed unless `initial_capital` is passed) and coverage
  (contract-days and how many closed within the same day).
- New tool parameters: `account_filter` (default `"All"`) and `initial_capital` (default 100,000, workbook
  only; ignored with a warning for a QFX). `month` slices one month and the earlier months in the same
  workbook feed the cross-month table and pooled significance. `ytd_*` is rejected with a workbook.
- Structure: `tradelog/report/report_inputs.py` holds `ReportInputs` and the QFX / workbook loaders;
  `build_report` consumes the bundle and the position-dependent computation moved verbatim into
  `_position_analytics`. The QFX HTML and `report_data` are unchanged, pinned by a golden snapshot
  (`tests/test_tradelog_report_characterization.py`, written before the refactor, market data faked).
- Tests: `tests/test_tradelog_report_inputs.py`, `tests/test_tradelog_report_workbook.py`, shared invented
  workbook in `tests/ledger_fixture.py` (no real data). Full suite (browser e2e excluded): 853 passed, 2 failed. Both failures are pre-existing and identical on
  pristine master (`test_chain_fetcher::test_compute_gex_uses_bsm_gamma_when_ib_gamma_missing`,
  `test_market_hours::TestIsFomcDay::test_known_fomc_date`: `params.yaml` lists only 2026-01-28 for the January
  meeting while the test expects both days).
- Known and unchanged: the bundled SPX/VIX cache ends 2026-07-31, and an online run (`offline=False`)
  rewrites it; the E*Trade CSV loader's missing `Buy To Close` mapping.

## Session: October 1, 2026 - Review of the workbook loader and report

- A line-by-line review of the `.xlsx` loader and the workbook report found four defects, fixed with tests
  (the regression tests fail against the old code):
  - the caller-supplied `label` was interpolated into the section-8 HTML unescaped (workbook branch, and the
    same pre-existing line on the QFX branch); it now goes through `esc()`;
  - a header repeated further right on the sheet (a dashboard) shadowed the ledger column because the last
    occurrence won; the first occurrence now wins;
  - rows whose Date is not a valid date (an Excel serial number, malformed text) were dropped with no trace;
    they are now counted, logged, returned as a warning by the MCP loaders and `generate_monthly_report`, and
    stated in the report's provenance box (`source.skipped_rows`);
  - the `generate_monthly_report` tool tests wrote into the real `reports/output/`; they now use a temp dir.
- Open, not fixed (low): a blank `Date` still ends a tab's data block by design; `build_report`'s `ValueError`
  is reported to the MCP client as a plain error (a dedicated exception type would not mask internal bugs); the
  CLI prints a traceback instead of the message and has no `--account` / `--initial-capital`; `.ofx` is
  rejected by the extension gate; the loader reads each whole sheet before slicing; ytd validation exists in
  both the tool and `load_report_inputs`; the older server tests still write into `reports/output/`.

## Session: October 1, 2026 - `analyze_strategy_compliance` hung over MCP on Windows

- Symptom: the first `analyze_strategy_compliance` call from Claude Code never returned, while the same
  function called in-process finished in under 2 s. Every other tool answered normally.
- Root cause (reproduced with a stdio client and a `faulthandler` stack dump of the server): the tool
  imported `tradelog.analysis.tagging` lazily, and tagging imports `scipy.optimize`. The server's main
  thread was stuck loading scipy's BLAS extension (`scipy.linalg.blas`, `create_module`). On Windows,
  FastMCP's stdio transport keeps a worker thread in a synchronous `ReadFile` on the stdin pipe while a
  tool runs; the DLL's runtime initialisation queries the std handles and waits behind that read, which
  only returns when the client sends another message. The client was waiting for the reply, so neither
  side moved. The server was not slow; it was deadlocked.
- Fix: `spx_trade_desk/mcp/server.py` imports `strategy_analysis`, `tagging` and `REPORT_DATA_DIR` at
  module load, before `mcp.run()` starts the reader thread, with a comment saying why imports must never
  move back into a tool body. Pre-importing scipy alone made the same call return in 1.4 s.
- Tests: `tests/test_tradelog_mcp_stdio.py` (2). One pins that importing the server already loads
  `scipy.optimize` and the tagging module (cross-platform); the other drives a real stdio session and
  calls the tool with a 60 s timeout. Both failed before the fix (the stdio one by timing out) and pass
  after it. Scoped run (MCP stdio, tagging, server tools, workbook report, characterization): 111 passed.
