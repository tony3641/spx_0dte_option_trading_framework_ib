# SPX 0DTE Option Dashboard

A real-time Gamma Exposure (GEX) dashboard for SPX 0DTE options, powered by Interactive Brokers TWS and served as a browser app.

## Stack

| Layer | Technology |
|---|---|
| Broker API | native `ibapi` → IB TWS/Gateway (port 7497) |
| Backend | Python 3.10, FastAPI, uvicorn |
| Real-time push | WebSocket, one send channel per browser (changed-field chain ticks, latest-wins snapshots) |
| Frontend | Vanilla JS, no build step: Plotly 2.32 (GEX, IV smile) and TradingView Lightweight Charts 5.2.1 (SPX price chart), both from a CDN |

## Features

- **GEX dashboard with 0DTE / Monthly toggle** — switch the GEX calculation between the current 0DTE SPXW expiry and the monthly SPX expiry.
- **SPX price chart** — today's session from the 09:30 ET open as 1-minute bars from IB with the current minute following the live SPX price; outside regular hours the last session plus a dotted ES-derived line.
- **Real-time option chain streaming** — live 0DTE chain with greeks, wall/flip markers, and a strike filter; only changed fields stream and the table is patched in place.
- **Account / Order Management tab** — account summary, portfolio positions (legs filled together as one combo order today are grouped into one expandable spread row, here and on the Dashboard), today's executions, and order placement with **Stop Limit** support (also accessible as an order-entry widget on the dashboard).
- **Strategies tab** — define automated multi-leg strategies and let the server watch the market for you:
  - Conditions (entry window, short delta, spread width, credit, trend, volatility), triggers, and a candidate scanner.
  - **Budget-based position sizing** with total-credit preview per candidate.
  - Live candidates ranked best-first, each with a one-click **Place** button.
  - **Take-profit** (idempotent close loop, per-leg limit prices) and **stop-loss** as a single credit multiplier.
  - One-shot eval loop with re-entry guards, margin checks, and a kill switch.
  - **Subsequent strategies** — trigger children off a parent trade's state (parent close / time window), with acyclic tree validation.
- **Simulation tab** — intraday Monte Carlo stress-testing for 0DTE strategies: GJR-GARCH + Student-t paths with U-shape volatility, option marks from a z-model fitted to recorded 0DTE chains, tick-rule fills, family re-entry, SL/strike sweeps and stress dials (ν, γ, ATM-IV anchor, pricing tier), PnL/CVaR/max-DD/ruin analytics.
- **Logging tab** — server-side framework log streamed to the browser.
- **Light and dark themes** — a terminal-style dense layout with an amber accent and tabular monospace numerals; see Themes below.

## Quick Start

1. **Install the Interactive Brokers TWS API.** The native broker client (`ibapi`) is
   not on PyPI — it ships with the TWS API distribution. Download and install it from
   IBKR first:

   ```
   pip install -e "C:\TWS API\source\pythonclient"
   ```

   The path above is machine-specific. Edit it to match your local TWS API installation.

2. **Install the Python dependencies.**

   ```
   pip install -r requirements.txt
   ```

   Do this *after* step 1. `requirements.txt` contains an editable install pointing at
   the same local TWS API path, so it fails if the TWS API is not present.

3. **Install this project in editable mode.**

   ```
   pip install -e . --no-deps
   ```

   `--no-deps` is correct here: dependencies are already installed by step 2, and the
   project declares none of its own.

4. **Open IB TWS / Gateway** and enable API access on port 7497.

5. **Start the server.**

   ```
   python -m spx_trade_desk.server
   ```

6. Open `http://localhost:8000` in a browser.

### Tests

```
python tests/run_tests.py --pretty     # structured JSON results
pytest tests/ -v --tb=long             # standard pytest flow
```

## Network Access

The server binds to all network interfaces (`0.0.0.0`), so it's accessible from both localhost and your local IP address:

- **Localhost only**: `http://localhost:8000`
- **Local network**: `http://<your-local-ip>:8000`

The exact URLs are printed to the console when the server starts. You can override the listening address with the `SERVER_HOST` environment variable if needed.

## Screenshot

![Dashboard Screenshot](docs/screenshot.png)
![Option Chain w/ Strategy Builder](docs/screenshot2.png)
![Account Management](docs/screenshot3.png)

## Dashboard Tabs

- **Dashboard** — a four-panel overview: SPX intraday chart (1-minute bars with an RSI(14) pane below), IV smile, GEX bars and Positions & P&L, plus level badges and the status bar. The panels fill the window below the header and stack into one column at 1100 px and below.
- **Option Chain** — full streaming chain table with greeks and order entry. The Strategy Builder is a collapsible order dock at the bottom: collapsed (the default) it shows one line (legs, net price, max loss) and the order row; the arrow button expands the legs table and the combo numbers. The order row shows the order's Bid / Mid / Ask (spreads as a signed net, negative for a credit); the limit price defaults to the mid on the SPX tick (0.05, or 0.10 above $2.00) and follows it until you type a price, and clicking Bid, Mid or Ask puts that price in the limit.
- **Account** — account summary, positions, executions, order placement.
- **Strategies** — strategy list/editor, live candidates, triggers, and arm/disarm controls.
- **Simulation** — run intraday MC stress tests, sweep stop-loss multipliers and dynamic strike distances, A/B stress dials, read the report (SPX percentile fan + per-cell charts), force-clear it between runs, and export a full AI-readable JSON report.
- **Log** — real-time framework log.

## Simulation

The Simulation tab runs an intraday Monte Carlo stress test of a saved strategy (or a
parent + children family) against synthetic GJR-GARCH + Student-t paths. It only **reads**
`Strategy` objects — it never places orders or touches live trading state.

Here's what the report it produces looks like:

![Simulated SPX percentile fan, run tiles, and sweep table](docs/simulation1.png)
![Day PnL distribution and spread mark-to-market through the day](docs/simulation2.png)
![Bootstrap max-drawdown histogram over 60-day sequences](docs/simulation3.png)

### Parallel execution

A run is split into `(sweep cell, chunk)` tasks and executed on a **process pool**
(`sim_parallel.py`), so a full-size run uses every CPU core. Processes rather than
threads: the exit scan is a per-path Python loop, which the GIL would serialize.

- **Workers**: set in the gear menu (Settings → Simulator → *Worker processes*), or
  `SIM_WORKERS` in `.env`. `0` = auto = CPU count; `1` = serial.
- **Auto stays serial for small runs** (< ~1000 paths): spawning workers costs more
  than the run itself. Explicit `n_workers`/`SIM_WORKERS` overrides that.
- **Results are identical either way** — the RNG stream is keyed by
  `(seed, cell, chunk)` and results are reassembled by index, never by completion
  order, so parallel and serial runs are bit-for-bit the same.
- **Cancel** stops submitting new chunks and kills the in-flight ones; cells whose
  chunks did not all finish are dropped, as before.
- First-run cost on Windows: each worker re-imports the app (~1–3 s per worker) when
  the pool is created. The pool is per run — no idle worker processes between runs.

### Known limitations

- **Family mode** uses placeholder per-path stats (`mtm=None`) and does **not** support
  SL/k sweeps — a family-mode config with `sl_multipliers` or `dynamic_k_values` is rejected.
- **IB live bars are not yet implemented** in the simulator — use `csv` or `yfinance`
  (`source="ib"` is rejected).
- **trend / pmove / RSI / atm_iv entry gates are not supported.** A strategy that enables one
  is rejected with a validation error rather than silently simulated without the gate
  (`vix_enabled` volatility conditions are supported).
- **`bear_call` is not simulated** — a non-`bull_put` strategy is rejected rather than
  mis-simulated with bull-put geometry.
- **Single-mode day-PnL stats count entered days only**; family-mode totals include zero-PnL
  days for never-entered children paths (a definition mismatch between the two modes).
- **Engine-mode exit scans are per-path Python loops** — the reason runs are CPU-bound.
  They are parallelized across processes (see "Parallel execution"), so wall-clock scales
  with cores, but a single huge sweep still takes minutes.
- **Sweep cells use independent RNG streams** (no common random numbers), so cross-cell
  differences include sampling noise — an experiment-quality tradeoff, not a paired A/B.
- **Implied vs realized vol are tied only through the ATM level.** Options are priced
  from the chain-library tables at an opening ATM level (the ATM IV % dial, else the
  VIX1D prior close x the library's ATM/VIX1D ratio, else the GARCH level), and the
  level follows the path's GJR state from there. The underlying still moves at the
  data's realized vol; see "Reading results: win-rate sanity" below.
- **Bars are cached per `(source, csv_path, bar size, lookback)`; the fitted model also
  per pricing tier, pricing-model file and session date** — not per CSV content. If you
  replace a CSV's contents on disk, restart the server.

### How the pricer marks options (z-model)

- Every contract is a **0DTE put** expiring at the session close (13:00 ET on half
  days). Two clocks meet in one place (`sim/clock.py`): stored IVs (IB quotes, VIX1D,
  the chain library) are calendar-time; the simulated path runs on trading time
  (252 x 6.5 h). Total variance is the same on both, so BSM gets
  `sigma_sim = IV_cal x 0.43242` and the rate scaled the same way; the price equals the
  calendar-clock price exactly.
- **IV** = `ATM(t) x f(z, tau)`, with `z = ln(K/S) / (ATM x sqrt(T_cal))`.
  - `f` is a table over z (-6..+3, step 0.25) and seven time-to-close buckets
    (>300, 300-180, 180-120, 120-60, 60-30, 30-15, <15 min). It is fitted from the
    recorded chain (`data/chain_library/`, SP1).
  - Outside the quoted z range, `f` extrapolates linearly in total variance, with the wing
    capped at Lee's moment bound.
- **ATM(t)** = `ATM_open x g(tau) x L(t)`.
  - `g` is the library's intraday ATM curve, normalized at 09:45.
  - `L` links the level to the path's GJR state: `L = 1` at the unconditional variance,
    clipped to [0.5, 3]. `budget_beta` scales its sensitivity.
  - `skew_beta > 0` also steepens the wing as `L` rises, by at most 1.25x the table's skew
    (a larger tilt makes wing put prices fall with strike). The tilt is applied before
    the Lee cap.
  - `ATM_open` is the ATM IV % dial if set, else the VIX1D prior close x the library's
    ATM/VIX1D ratio, else the GARCH level.
- **Half-spread** comes from a table by bucket and put mid, floored at half a tick. The
  entry fill is the tick-floored conservative side (never better than the natural).
  Expiry settles at intrinsic value. Stops trigger at mark >= multiplier x collected
  credit (+ slippage).
- **Flat IV** prices every strike at the ATM level (no smile), as a sanity check.

### Pricing tables, tiers and the library

- **Tiers** (`pricing_tier`, default `auto`):
  - `library`: the run's VIX1D-prior-close regime (<12, 12-18, 18-25, >=25) has 5+ captured
    days and the library 10+.
  - `thin`: pooled tables from the days you have.
  - `cold`: the tracked default `config/sim_pricing_default.json`.
  - A time bucket with fewer than 20 chain sweeps falls back one tier.
  - The run's warnings (and `meta.pricing`) show the tier, the fallen-back buckets, the
    library age (stale after 10 trading days without a capture) and the last harness
    score, with its day count and an in-sample flag (e.g. "harness 4/4 buckets on 1 day
    (in-sample)").
  - With no VIX1D prior close (yfinance down or no history), the run warns "no VIX1D prior
    close; regime tiers unavailable" (plus "ATM anchor = GARCH level" if no ATM IV % dial
    is set). Such a calibration is not cached, so the next run retries the lookup.
- **Building:**
  - The standalone capture rebuilds `data/chain_library/pricing_model.json` after
    each session.
  - The Sim tab's **Rebuild pricing library** button does it on demand
    (`POST /api/sim/pricing/rebuild`; `GET /api/sim/pricing` returns the tier summary).
  - Or from the command line: `python -m spx_trade_desk.sim.library build` (add
    `--vix1d-prev YYYYMMDD=VALUE` for a day yfinance has no VIX1D close for, and
    `--write-default` to regenerate the Cold default).
- **Validation harness:**

  ```bash
  python -m spx_trade_desk.sim.validate data/chain_library/YYYYMMDD.jsonl.gz --tier auto --store
  ```

  For each recorded snapshot it prices the chain the way a sim run would (opening
  anchor, never the real ATM). It then compares the 10-wide bull-put credit at
  5/10/15/20 delta, the short strike the sim's delta picks, and the half-spread.
  - Bar: median credit error within ±25% before 15:00 (±40% after); short strike
    within 5 points in 80% of pairs.
  - Tier tables are built leave-one-out (except the Cold default: a day it was built
    from scores in-sample, and the harness says so).
  - `--store` saves the score under each tier the scored days resolved to, with the day
    count and an in-sample flag; a run that scored nothing stores nothing.
  - A second column reprices with the snapshot's real ATM (shape error only).
  - Reports go to `reports/output/`.
- **Tuning results from before this pricer** (SVI smile, `vol_beta`) are not comparable:
  re-run them. Old configs and tuning specs that still carry `vol_beta`, `skew_t_gamma`
  or `atm_budget` load with a warning (unless the value already matches the new behaviour:
  `skew_t_gamma` 0, `atm_budget` true), and the key is ignored.

### Reading results: win-rate sanity

The engine is deterministic (same seed + same inputs = identical results) and covered by
tests, but absolute win rates can still mislead:

- **Stale calibration** (above) silently simulates old data after a file change — restart
  the server when in doubt.
- **A calm CSV + a market-level IV** makes far-OTM strikes unreachable: the short put sits
  where the recorded chain's delta puts it, but data moving only ~0.4%/day rarely gets
  there, producing near-100% win rates that reflect the vol-world mismatch, not strategy
  edge.
- Mitigations: **keep the chain library current** (the capture rebuilds it; check the tier and staleness in the run warnings), use
  data whose realized vol is realistic, and compare sweep rows *relatively* rather than
  trusting absolute levels.
- **Set the ATM IV % dial** to anchor the SPX fan to the market's current implied vol.
  A GARCH fit on calm data has low *conditional* vol but can be *near-integrated*
  (`α + γ/2 + β ≈ 0.99`), which lets a small subset of simulated paths ratchet up to
  5–10× the fitted vol — a 1-day fan that closes p0 at −25% or worse. The ATM IV anchor
  caps each per-bar move so the median session still resembles your data while the tails
  match what the options market prices. The dial takes the calendar-unit annual IV (the
  IB / VIX-style number); the cap is `vol_cap_mult x ATM IV x sqrt(390/525600)` per RTH
  day, spread over the day's bars.

### Stress dials & experiments reference

| Control | Meaning |
| --- | --- |
| ν override | Student-t dof for per-bar shocks (blank = fitted; lower = fatter tails; must be > 2) |
| γ × | GJR leverage multiplier — extra vol after *negative* returns (1.0 = as fitted) |
| Flat IV | Sanity mode: price every strike at the ATM level, ignoring the smile shape |
| ATM IV % | Anchor the SPX path vol to today's at-the-money IV, as a calendar-unit annual % (the IB / VIX-style number). Blank = the GARCH level fitted from your data, which can diverge from the live market — see below |
| Vol-cap × | Per-bar sigma cap as a multiple of the IV-implied per-bar vol (default 2; the IV-implied daily vol is ATM IV x sqrt(390/525600) per RTH day). Effective only when ATM IV is set |
| SL multipliers | Sweep stop-loss = mult × credit, one table row per value; `inf` holds past the stop |
| Strike mode | `engine` = strategy's delta/width/credit gates; `dynamic_k` = short strike at k·σ below spot |
| k values | Short-strike distance in daily σ (`dynamic_k` mode only) |

Every run-form control also carries an inline “?” tooltip in the UI with the same
guidance.

### Reading the report

- **Simulated SPX paths (percentile fan, top)** — the market simulation itself: twenty
  SPX price curves at every 5th percentile (p0…p95, gold median) through the session.
  Strategy-independent, so it is identical for every sweep row.
- **Tiles, sweep table, per-cell charts** — the selected row's outcome distribution:
  day-PnL histogram, spread MTM quantile fan, and bootstrap max-drawdown.
- **Clear** — wipes the report back to its initial blank state (form inputs are kept; a
  run in flight keeps running server-side). Every new Run also starts from a blank
  report automatically, so a re-run can never mix with the previous result.
- **Export report** — downloads `sim_report.json`, a self-contained, AI-agent-readable
  snapshot of everything the page shows: the exact run config, run meta (source, bar
  size, GARCH fit + warnings, pricing tier, dials), all per-cell stats and plotted series
  (histograms, MTM fan, bootstrap DDs, SPX fan), and a glossary reusing the UI's "?"
  explanations.

### Strategy tuning (agent workflow)

`spx_trade_desk.sim.tune` executes **named knob variants** of one live strategy from
`config/strategies.json` through the simulator — deterministically, without ever
writing the config file. It backs the agent-driven tuning methodology in
`.claude/skills/strategy-tuning/` (baseline → diagnose → one-knob-at-a-time
rounds → multi-seed robustness gate → propose-only JSON diff), where the agent
picks which knob to probe by judgment rather than sweeping grids. Variant specs
and results land in `docs/experiments/<slug>/` (`variants.json`, `results.csv`,
`results.json`, `report.md`). All variants in a spec share the same seed,
dataset, and `n_paths`, so the simulator replays identical spot paths per variant
(common random numbers) and metric deltas are attributable to the knobs alone.

```
python -m spx_trade_desk.sim.tune --strategy MyStrategy --spec docs/experiments/<slug>/variants.json --smoke  # wiring check
python -m spx_trade_desk.sim.tune --spec docs/experiments/<slug>/variants.json --seeds 42,43,44                 # robustness gate
```

### Chain library (input for the sim pricing tables)

While the dashboard runs, the merged 0DTE chain is appended every 60 s (09:31-10:00 and the last hour) or 120 s (otherwise) to `data/chain_library/YYYYMMDD.jsonl.gz`. Each line is one snapshot: spot, VIX, VIX1D, and per strike and side bid/ask/last/IV (IB's raw calendar-clock decimal)/delta/gamma/OI/volume, plus the quote's age and source. The folder is local market data, gitignored and blocked by the pre-commit hook.

When the dashboard is not running, `python -m spx_trade_desk.market.capture` writes the same files. It idles while the dashboard's heartbeat is fresh and takes over within about four minutes of the dashboard stopping (the dashboard refreshes its heartbeat on every successful publish tick, so it also stays quiet from the 09:20 start until the first 09:31 record). A failed or empty sweep is logged and retried only after the next record interval (60-120 s), so a short outage does not end the run; the capture exits after 5 consecutive failed sweeps (an idle spell while the dashboard is alive resets the count). While SPX has no price yet it waits instead of sweeping. To run it every trading day, create a Windows Task Scheduler task yourself (weekdays, 09:20 ET; working directory = repo root; action = `python -m spx_trade_desk.market.capture`). When the dashboard restarts while the capture is sweeping, both can briefly hold lines until the dashboard writes its first record, so with a 100-line account expect a short overlap at the line cap. Stopping the capture before restarting the dashboard avoids it.

## Trade Log Analysis

A statement-facing analysis engine (`spx_trade_desk/tradelog/`, ported from the
`trade_pnl_dashboard` project) that measures what **actually happened** in the account,
from broker statements rather than from live state. It never places orders and never
touches the strategy runtime or the IB connection.

Supported statement formats:

| Format | Broker | Notes |
|---|---|---|
| `.qfx` | IBKR | Perf & Reports → 3rd Party Reports → Quicken Web Connect. The only format that preserves intraday trade timestamps. |
| `.csv` | IBKR | Perf & Reports → Transaction History, and E*Trade's trades download; the format is auto-detected from the header. |
| `.pdf` | E*Trade (Morgan Stanley) | Monthly client statement. |
| `.xlsx` | IBKR + E*Trade | A consolidated trade-log workbook: tabs `Index Options` / `Other Options` / `Stock & ETF` / `Other Transactions`, each with the header `Date, Source, Account, Underlying, Symbol, Description, Order Type, Quantity, Price, Commission, Net Amount`. Pass it by path, or as `data_base64` in `file_contents`. |

The `.xlsx` workbook is read as a standalone ledger, so load it on its own. Its IBKR
account id is masked (`U***12345`) and cannot be matched to a QFX statement's account, so
loading both would double count; the server adds a warning when it sees that combination.
E*Trade rows take the same account id as an E*Trade CSV (or the real id of a loaded E*Trade
PDF, so those still dedup). The workbook carries no balances, so capital falls back to
`initial_capital` or the $100,000 default, exactly as with an E*Trade CSV. Columns are
matched by header name, so dashboard cells beside the ledger are ignored, and each tab's
data ends at its first blank `Date`. The workbook has dates but no intraday timestamps, so
`analyze_strategy_compliance` takes QFX statements only; `generate_monthly_report` accepts the
workbook with the reduced content described below.

What it computes: realized P&L per contract and per day, risk metrics (Sharpe, Sortino,
drawdown, Net EV, commission drag, SPX/VIX benchmarks), TWR/MWR account returns,
the daily calendar matrix, and — for bull put spread books — spread reconstruction,
per-leg win rates, bootstrap significance, spread-capped tail stress, Monte Carlo,
Kelly sizing, and stop/re-entry behaviour. `generate_monthly_report` renders all of it
as a self-contained HTML report under `reports/output/`.

`generate_monthly_report` accepts a QFX statement or the `.xlsx` trade-log workbook (any other type is rejected
with a clear message). A QFX report is the full analysis. A workbook report is day-level: it covers the
SPX/SPXW option rows (stock, dividend and non-SPX rows are excluded) with the executive summary, daily and
weekly P&L, risk-adjusted metrics, VIX regimes, SPX benchmark, bootstrap significance and Monte Carlo, and it
shows a "Needs QFX timestamps" notice in place of strategy structure, stops/re-entry and Kelly, because a
contract that was both bought and sold on one day cannot be classified as a short or a long from dates alone.
`unavailable_sections` in the result lists them. Extra parameters for a workbook: `month="YYYY-MM"` slices one
month (earlier months in the same file feed the cross-month table and pooled significance automatically),
`account_filter` (default `"All"`, the combined book) and `initial_capital` (default 100,000, shown on the page
as assumed, since the workbook has no balances). The bundled SPX/VIX cache ends 2026-07-31; later months need
`offline=False`, which refreshes and rewrites that cache.

### ITM expiry settlement

An IBKR QFX records a worthless 0DTE expiry as a `$0` closing trade but omits the cash
settlement of an in-the-money one (only the transactions CSV carries it), so P&L built
straight from a QFX overstates any month with an ITM expiry. The QFX paths (the MCP tools,
`generate_monthly_report`, `strategy_analysis`) now infer the missing `Cash Settlement`
rows: for each SPXW contract whose whole history sits on its expiry day and whose net
quantity is non-zero, the cash flow is `net signed quantity × intrinsic × 100` at the official
SPX close from `reports/data/spx_closes.csv`. A real settlement row (from a transactions CSV)
always wins, an expiry with no close on file is listed in a warning instead of being guessed,
and the day's own close is never trusted until the day is over. Inferred rows are labelled
"Cash settlement (inferred ...)". The inference also keeps the balance-anchored starting
capital consistent (ending balance minus all cash flows, settlement included). An ITM expiry
is not counted as a stop in the stops/re-entry section: nothing was closed.

### Strategy compliance tagging

`analyze_strategy_compliance` scores each reconstructed spread against the entry
conditions of the strategies in `config/strategies.json`. Every condition resolves to
**passes**, **fails**, or **unverifiable** — a condition the statement cannot answer
(missing timestamp, ATM-IV gate, RSI trend gate) is reported as unverifiable and never
counted as a failure.

Only **QFX** statements are accepted: they are the one format carrying the intraday
entry timestamps the `entry_window` check needs. The VIX close for the statement's
date range is loaded alongside the trades — fresh from Yahoo Finance by default, or the
cached CSVs under `reports/data/` with `offline=True` — so the VIX gate is decidable.
The credit-implied delta needs the SPX **spot at the entry time**, not the daily close
(which can sit hundreds of points from where SPX traded at 10:00). Pass
`intraday_spot_path` (a CSV with `ts` and `close` columns, naive ET, e.g. 1-minute bars)
or, online, recent days come from yfinance 1-minute bars (about a week of history).
Without a bar within 5 minutes of the entry, the delta check is unverifiable rather than
scored against the close. Where no VIX close exists for an entry date, that gate comes
back unverifiable rather than failing.

Note that the default (`offline=False`) **rewrites** `reports/data/*.csv` as it merges
the freshly fetched rows in — that is how the offline cache stays current, and it means
a default run modifies tracked files. Pass `offline=True` to leave them alone.

The short delta is not recorded in any statement. It is inferred: the observed spread
credit is used to back out the BSM implied volatility, and that vol gives the delta —
so the `short_delta` band can be checked against real fills. The inference uses the SPX
spot at the entry time (see above) and, where a statement has no timestamp, the delta is
left unverifiable instead of assuming a 12:00 ET entry.

`exit_audit` measures each trade's realized loss ratio against the strategy's stop
multiple. For a paired spread it divides the **spread's** P&L by its net credit, which
is what the live engine stops on; the `basis` column says so explicitly, and falls back
to `short_leg` for an unpaired trade. Rows carry a `source` column, because spread ids
restart at 0 in every statement file.

Tagging never writes `config/strategies.json`.

### MCP server

Eleven tools over **stdio**:

| Tool | Description |
|------|-------------|
| `get_transaction_summary` | Load files; row counts, date range, accounts, balances |
| `compute_daily_pnl` | Realized-P&L pipeline — daily series, cumulative, top contracts |
| `compute_risk_metrics` | Sharpe, Sortino, drawdown, Net EV, SPX/VIX benchmarks, VIX regimes |
| `get_calendar_data` | Weekly calendar heatmap matrix |
| `get_market_data` | SPX or VIX daily OHLC/returns from Yahoo Finance |
| `parse_occ_symbol` / `build_occ_symbol` | Decompose / assemble an OCC option symbol |
| `get_contract_details` | All trades and P&L for one contract |
| `compute_account_return` | SPX/SPXW-only TWR/MWR with non-SPX activity as external flows |
| `generate_monthly_report` | Full monthly report (HTML + JSON) |
| `analyze_strategy_compliance` | Score realized spreads against `config/strategies.json` |

```powershell
python -m spx_trade_desk.mcp.server
```

Claude Code picks it up from a repo-root `.mcp.json` and asks for approval on first
use. That file holds an absolute interpreter path, so it is machine-specific and is not
tracked: copy `.mcp.json.example` to `.mcp.json` and edit `command` and `cwd`.

### Known limitations

- **A year-less E*Trade PDF statement is dated with the current year.** The parser reads
  the year from the statement's period line and falls back to `datetime.now().year`, so a
  prior-year statement whose header omits the year parses with wrong dates.
- **Compliance measures the current config against historical fills.** The config armed
  when a trade was placed is not recoverable from a statement, so every result carries
  the config's path, mtime, and SHA-256. Band/bucket and run-day semantics are taken from
  the live engine's own helpers, so "compliant" means exactly what the engine enforces.
- **`bear_call` strategies can never match.** Spread reconstruction pairs shorts with
  lower-strike longs, which only builds bull puts.
- **Entry delta is approximate.** It comes from the daily close and a credit-implied vol,
  not the intraday entry spot; unpaired shorts and credits outside the no-arbitrage band
  yield no delta at all (unverifiable).
- **The E*Trade PDF parser has no automated test** — it needs a real statement. Its
  year-extraction helper is tested; the body is exercised only by a real run.

New runtime dependencies: `pandas`, `pdfplumber`, `mcp`.

## Charts

### 1. SPX Intraday (top)
Candlestick chart of 1-minute SPX bars on TradingView Lightweight Charts. Times are Eastern (ET) whatever the PC's time zone.
- **Regular hours:** today's session from the 09:30 ET open. The completed bars come from IB, which keeps a 1-minute historical-bars request open (`keepUpToDate`). The current minute also follows the live SPX **last** price once a second (never bid or ask) until IB's own update for that minute replaces it. The server builds the series, so no minute is lost while the page sits on another tab or reconnects.
- **Outside regular hours:** the last regular session's bars plus a dotted ES-derived SPX line, one point per minute (see Data Modes). At 09:30 ET the chart resets to the new day and the dotted line is cleared.
- **Levels:** Call Wall, Put Wall, Gamma Flip and Max Pain are price lines, redrawn only when GEX publishes a different value.
- **When IB fails:** an error on the bar request, or no update for 3 minutes in regular hours, cancels the request and asks again after 5 s, then 15 s, then every 60 s, with the reason on the Log tab; the chart keeps its last bars meanwhile. After 3 failed `keepUpToDate` starts in a row (the request raised or IB reported it dead before any bars came back) the server stops using `keepUpToDate` for the rest of the process, logs a warning on the Log tab and carries on as if `PRICE_BARS_KEEP_UP_TO_DATE=false`; restart the server to try `keepUpToDate` again. `PRICE_BARS_KEEP_UP_TO_DATE=false` is the fallback source: one backfill at start-up, at the open and on reconnect, with the forming bar built from the live SPX last price. The `keepUpToDate` request has not been run against a live TWS yet (see `docs/progress.md`); `python -m spx_trade_desk.ib.bars_probe` checks it on paper.
- If the Lightweight Charts script cannot be loaded (CDN down) the chart area shows a short error text and the rest of the page keeps working. The pinned Lightweight Charts and Plotly scripts carry Subresource Integrity hashes, so a CDN file that differs from the pinned version is refused the same way.

### 2. Gamma Exposure (GEX) by Strike (middle)
Bar chart showing Call GEX (green) and Put GEX (red) by strike with:
- **Net GEX line** (violet, the theme's `alt` color) — cumulative gamma exposure
- **Spot price indicator** (dotted line in the text color)
- **Near / All toggle** — by default the chart draws the strikes within 40 steps of spot (`state.gexWindowStrikes`); "All" draws every strike. The IV smile follows the same toggle.
- **Key level markers** — visual anchors for walls and flip points
- **Annotation box** — Net GEX value, MM regime (CONVERGING/DIVERGING), Call OI, Put OI, P/C OI Ratio, Call GEX % skew

### 3. IV Smile & Delta-Decay Efficiency (bottom)
Two-row subplot showing:
- **Top (Calls):** Call IV curve (green) + Call efficiency (violet, dotted)
- **Bottom (Puts):** Put IV curve (red) + Put efficiency (violet, dotted)
- All subplots share synchronized x-axes (strike ranges aligned)
- Hover displays: Strike, IV %, Delta, Charm (delta decay rate), Efficiency metric

**Real-time zoom sync:** Pan/zoom either the GEX or Smile chart → both charts update their x-axis range simultaneously.

### 4. Real-time Option Chain Streaming
- Live streaming 0DTE option chain with greeks
- Markers on Put Wall, Call Wall, and Gamma Flip location
- Every 0.5 s (`CHAIN_STREAM_UPDATE_INTERVAL`) the server sends one `chain_tick` holding only the fields that changed for each strike and side, and nothing when nothing changed. The tick names its expiry (`expiration_raw`, the same value `chain_quotes` carries) and the page ignores a tick whose expiry is not the table's. The full chain (`chain_quotes`) goes out every `CHAIN_REFRESH_SECONDS` (10 s).
- The table is built once and patched in place: a changed cell is rewritten and flashed once per animation frame, rows are added or removed only when the visible strike range changes, and hover, selected legs and the scroll position survive updates. One delegated click handler serves the whole table.
- Dimming follows when each side last ticked in your browser; a full payload re-seeds it from the quote ages. A browser that connects between two full refreshes starts from the last full payload, but the next stream cycle (about 0.5 s later) sends it every field of every streamed strike, so the streamed strikes are current almost at once; the strikes on the poller's share are only as fresh as the last full refresh (up to 10 s).

The strikes nearest spot (or nearest your scroll position in the chain tab) stream live on the chain stream's line share. Every other strike in the +-8 sigma range is polled continuously in small batches on the poller's share, so the live stream never pauses. Both feed one quote book. Every `CHAIN_REFRESH_SECONDS` it is published as GEX, the chain table and the strategy engine's chain cache. Each side carries its quote age; sides older than `CHAIN_QUOTE_MAX_AGE_S` are dimmed and are never used as a candidate leg. A streamed row ages from its stream's last tick, so a silent data farm ages out the same way. Contract ids come from one bulk listing per expiry, requests to TWS are paced, and if IB refuses a market-data line (error 101) the app shrinks its line budget to the lines IB actually granted and logs the value to set in MARKET_DATA_LINES.

## Push and Rendering

**Server push** (`web/push.py`). Every browser has its own send channel and writer task, so a slow tab never delays the server's loops, order feedback or another browser. `broadcast` stamps the message with the server time, serializes it once and enqueues it for each browser; it never waits on a socket.

| Kind | Messages | Behaviour |
|---|---|---|
| critical | `init`, `order_status`, `ib_error`, `strategy_*`, replies to a browser's own messages | ordered, never dropped, sent first |
| log | `log` lines | ordered, at most 500 unsent; on overflow the oldest are dropped and one line says how many |
| merge | `chain_tick` | pending ticks are merged per strike and side, field by field; the message carries its `expiration_raw` and unsent ticks of another expiry are discarded |
| latest | `status`, `vix_update`, `gex`, `chain_quotes`, `account_update`, `price_snapshot`, `price_bar` (per minute), `price_overnight`, monthly GEX | a newer message replaces an unsent one with the same key |

A browser with more than `PUSH_ORDERED_BACKLOG_MAX` unsent critical messages, or whose send takes longer than `PUSH_SEND_TIMEOUT_S`, is dropped; the others are unaffected. The page reconnects (after 0.5 s, 1 s, 2 s, then every 3 s) and receives a fresh `init`.

**Orders.** A cancel runs off the browser's receive loop: the page keeps working while IB confirms (up to 2 s) and gets the reply when it settles. An order IB reports as `PendingCancel` shows as "Cancel pending" until the next order status or account update resolves it. Placing an order is unchanged.

**Rendering** (`static/js/render-loop.js`). Incoming messages update a model, and drawing happens once per animation frame:
- Light jobs (price bars, chain cells, badges, account) run first in the frame.
- Heavy jobs (the GEX and IV smile Plotly draws, tens of milliseconds each) run one per frame, after the light jobs, so a price bar or chain tick that arrives together with a GEX publish is painted before the chart draw starts. A heavy job that has waited behind four busy frames runs anyway.
- The spot line on the GEX and smile charts moves with a light relayout at most once every 2 s per chart.
- A chart is drawn only while its tab is visible and its container has a size (`ResizeObserver`); what changed while a tab was hidden is painted once when the tab is shown.

## Themes

The page has a dark and a light theme. It follows the OS (`prefers-color-scheme`) until you choose: the sun/moon button in the header flips the theme and remembers it in the browser (`localStorage` key `spx-theme`); Shift+click on it goes back to following the OS. The Plotly charts and the price chart re-theme in place when it changes, with no reload.

Every color lives in `static/css/tokens.css` (dark in `:root`, light in `:root[data-theme="light"]`), together with the radius, spacing, shadow and motion tokens; CSS reads them as `var(--name)`. JavaScript that has to hand a color to Plotly or Lightweight Charts (they cannot parse `var()` or `color-mix()`) calls `themeColors()` from `static/js/theme-colors.js`. `tests/test_ui_tokens.py` checks that both themes define the same tokens, that text and background pairs meet WCAG AA, that no `var(--x)` is undefined, and that no color literal appears outside those two files, so a new color goes into `tokens.css`.

Motion is short and quiet: tabs cross-fade, loading states are shimmer bars, and the chain's tick flash is an opacity fade on a cell overlay, capped at 40 cells per update (`state.chainFlashMax`). With `prefers-reduced-motion: reduce` the animations and the flash are off.

## Status Bar

Real-time status indicators:
- **IB Connection** — green dot when connected
- **Market Status** — RTH (green) / GTH (yellow) / closed (gray)
- **Expiration** — target SPXW expiration date
- **Last GEX Update** — timestamp of most recent chain fetch

## Level Badges

Key strike prices and conditions:
- **SPX** — current spot price
- **Call Wall / Put Wall** — highest gamma-OI strikes
- **Gamma Flip** — price level where net gamma crosses zero
- **Max Pain** — strike minimizing option holder payoff
- **Net GEX** — total gamma exposure (with regime label)
- **Data Mode** — LIVE, HISTORICAL, or ES-DERIVED

## Files

All application modules live in the installable `spx_trade_desk` package, grouped by
domain. `config/`, `static/` and `tests/` stay at the repository root as data and tests.

| File | Purpose |
|---|---|
| `spx_trade_desk/server.py` | FastAPI app, IB connection, state management, WebSocket endpoint |
| `spx_trade_desk/web/ws.py` | WebSocket message routing (tabs, GEX mode, strategies, orders, viewport sync) |
| `spx_trade_desk/web/push.py` | Per-browser send channels: `broadcast` enqueues and returns, one writer task per socket coalesces and sends |
| `spx_trade_desk/ib/client.py` / `ib/connection.py` | Native `ibapi` wrapper: contract resolution, streaming quotes, connection lifecycle |
| `spx_trade_desk/market/chain_fetcher.py` | Batched SPXW option chain fetcher (streaming mode, ±8σ strike filter) |
| `spx_trade_desk/ib/line_budget.py` | Market-data line accounting: splits the account allowance into fixed/order/poll/stream shares and refuses over-cap requests locally |
| `spx_trade_desk/market/chain_manager.py` | Chain stream loop (live near-money subscriptions into the quote book), `chain_quotes` payload builder, monthly GEX fetch and 0DTE coordination |
| `spx_trade_desk/ib/contracts.py` | Contract registry: one bulk listing per (expiry, trading class), exact-match single qualification fallback, order-path lookups from cached contract ids |
| `spx_trade_desk/ib/pacing.py` | Token-bucket pacer for the data lane (orders and cancels bypass it) |
| `spx_trade_desk/ib/session.py` | One boot for the server start and the manual IB reconnect, plus the list of background loops |
| `spx_trade_desk/core/perf.py` | Timing recorder behind `/api/perf` and the periodic perf log line |
| `spx_trade_desk/ib/line_probe.py` | Calibration tool for the market-data line allowance (`python -m spx_trade_desk.ib.line_probe`) |
| `spx_trade_desk/ib/latency_probe.py` | Paper-only acceptance probe for order, cancel, qualification and boot latency (`python -m spx_trade_desk.ib.latency_probe`) |
| `spx_trade_desk/ib/bars_probe.py` | Read-only paper probe for SPX 1-minute `keepUpToDate` bar updates (`python -m spx_trade_desk.ib.bars_probe`) |
| `spx_trade_desk/market/qualification.py` | Key helpers for the chain loops (`Key`, `norm_key`, `unknown_retry_due`); the cache itself moved to `ib/contracts.py` |
| `spx_trade_desk/market/quote_book.py` | The merged 0DTE quote book fed by the stream and the poller; every row carries its source and quote age |
| `spx_trade_desk/market/chain_poller.py` | Wing poller: continuous batched snapshots of every in-range strike the stream does not hold |
| `spx_trade_desk/market/chain_publisher.py` | Publish loop: quote book to GEX, chain table and the strategy engine's chain cache every `CHAIN_REFRESH_SECONDS`; refreshes the dashboard heartbeat |
| `spx_trade_desk/market/chain_recorder.py` | Appends the merged chain to the daily gzipped chain library and maintains the heartbeat file |
| `spx_trade_desk/market/capture.py` | Standalone chain capture (`python -m spx_trade_desk.market.capture`): records when the dashboard is not running |
| `spx_trade_desk/market/gex.py` | GEX computation: Call/Put Wall, Gamma Flip, Max Pain, Net GEX, MM regime |
| `spx_trade_desk/market/hours.py` | Market-hours helpers, ET timezone, expiration, FOMC/NFP day utilities |
| `spx_trade_desk/market/price_bars.py` | The SPX 1-minute bar series for the price chart: IB `keepUpToDate` request, forming-bar merge from the live SPX last, ES-derived overnight line, 09:30 reset, re-request with backoff, `price_snapshot` / `price_bar` / `price_overnight` messages |
| `spx_trade_desk/market/bars.py` / `core/rates.py` | One-shot historical bars (the chain poller's spot fallback), annualized-vol helper and risk-free-rate (SGOV) helpers |
| `spx_trade_desk/ib/orders.py` | Order placement, take-profit close loop, stop-loss handling |
| `spx_trade_desk/ib/account.py` | Account values, portfolio positions, executions serialization |
| `spx_trade_desk/strategy/models.py` | Strategy/Condition/Trigger/TakeProfit/StopLoss/RuntimeState dataclasses |
| `spx_trade_desk/strategy/engine.py` | Candidate generation, condition eval, sizing, entry payloads, triggers, parent/child logic |
| `spx_trade_desk/strategy/store.py` | Strategy persistence to `config/strategies.json` |
| `spx_trade_desk/core/app_state.py` | Shared AppState runtime, day key, kill switch |
| `spx_trade_desk/core/log_buffer.py` | Ring-buffer framework log for the Log tab |
| `spx_trade_desk/core/config.py` | Centralized settings: env var → repo-root `.env` → `config/params.yaml` → defaults |
| `spx_trade_desk/resources.py` | Repository-relative path anchors (`config/`, `static/`, `.env`, `docs/experiments`) |
| `spx_trade_desk/sim/config.py` | Simulation run config: validation, JSON round-trip, sweep cells |
| `spx_trade_desk/sim/data.py` | Layered intraday bar loaders: CSV → yfinance → IB |
| `spx_trade_desk/sim/calibrate.py` | GJR-GARCH(1,1)-t MLE, U-shape profile, pricing tables by tier, VIX mapping |
| `spx_trade_desk/sim/paths.py` | Chunked vectorized path generation (stress dials: ν, γ×; ATM-IV anchored per-bar sigma cap) |
| `spx_trade_desk/sim/pricing.py` | Vectorized BSM, strike ladder, tick fill rules |
| `spx_trade_desk/sim/clock.py` | The one calendar-to-sim clock conversion (IV and rate), minutes to the close |
| `spx_trade_desk/sim/library.py` | Chain-library reader, per-day extraction cache, pricing-model builder CLI, run tier resolution |
| `spx_trade_desk/sim/pricing_tables.py` | z-model tables (f, g, spreads), builder, tier selection, Cold default loader |
| `spx_trade_desk/sim/pricing_model.py` | Per-run pricer: table lookups, extrapolation, GJR level link, opening anchor |
| `spx_trade_desk/sim/validate.py` | Pricing validation harness against recorded chains (`python -m spx_trade_desk.sim.validate`) |
| `spx_trade_desk/sim/engine.py` | Entry/exit scans, single + family simulation, experiment modes |
| `spx_trade_desk/sim/risk.py` | CVaR/exit breakdown/max-DD/bootstrap ruin metrics, SPX path fan |
| `spx_trade_desk/sim/jobs.py` | Background job registry, progress, cancel, memoized calibration |
| `spx_trade_desk/sim/tune.py` | Offline knob-tuning runner (`python -m spx_trade_desk.sim.tune`) |
| `spx_trade_desk/tradelog/io/` | Statement parsers (IBKR QFX/CSV, E*Trade CSV/PDF, consolidated `.xlsx` trade-log workbook) and the SPX/VIX market-data loaders |
| `spx_trade_desk/tradelog/domain/` | Realized-P&L engine, merge/dedup, SPX/SPXW filter, OCC symbol parse, risk metrics, TWR/MWR, calendar matrix |
| `spx_trade_desk/tradelog/analysis/` | Spread reconstruction, edge/tail/Monte-Carlo/Kelly analysis, strategy-compliance tagging |
| `spx_trade_desk/tradelog/report/` | Self-contained HTML monthly report builder |
| `spx_trade_desk/mcp/` | FastMCP stdio server (11 tools) and its DataFrame→JSON adapter |
| `reports/data/`, `reports/output/` | Seed SPX/VIX market data (committed) and generated reports (gitignored) |
| `static/` | Browser app: `index.html`, `css/`, `js/` (charts, price chart, render loop, chain table, order entry, strategy UI, tabs, WS) |
| `static/css/tokens.css` | Design tokens: every color (dark and light), radius, spacing, shadow and motion value; the only CSS file with color literals |
| `static/css/components.css` | Shared pieces: `.num` tabular numerals, chips, KPI blocks, the Positions & P&L panel |
| `static/js/theme.js` | Blocking script in `<head>`: sets `data-theme` from `spx-theme` or the OS before first paint, `getTheme` / `setTheme` / `toggleTheme`, fires `themechange` |
| `static/js/theme-colors.js` | `themeColors()` (token values for Plotly and Lightweight Charts, cached until `themechange`), `withAlpha()` |
| `static/js/dash-positions.js` | The Dashboard's Positions & P&L panel, drawn from `account_update` |
| `static/js/price-chart.js` | SPX price chart on Lightweight Charts: snapshot, per-bar updates, overnight line, level price lines, ET time axis |
| `static/js/render-loop.js` | Frame batching: light and heavy job lanes, hidden-tab parking |
| `static/js/perf.js` | Browser-side timings (server stamp to receipt, receipt to paint, long tasks), sent to the server as `perf_report` every 10 s |
| `tests/` | Pytest suite + `run_tests.py` structured runner |
| `tests/e2e/` | Playwright tier against the real server with IB pointed at a closed port (`hermetic_server.py`): price chart, chain table and dashboard render tests, and the render benchmark (`render_bench.py` / `render_bench.js`, `test_render_perf_playwright.py`) |

## Configuration

Settings are resolved in order: **environment variable → repo-root `.env` → `config/params.yaml` → hardcoded default**.

| Variable | Default | Description |
|---|---|---|
| `IB_HOST` | `127.0.0.1` | TWS host |
| `IB_PORT` | `7497` | TWS API port |
| `IB_CLIENT_ID` | `1` | IB client ID |
| `CHAIN_REFRESH_SECONDS` | `10` | How often the merged quote book is published (GEX, chain tab, strategy engine) |
| `DASHBOARD_CHAIN_REFRESH_SECONDS` | `300` | Dashboard GEX chain refresh cadence |
| `CHAIN_TAB_FULL_REFRESH_SECONDS` | `300` | Chain tab full refresh cadence |
| `MARKET_DATA_LINES` | `100` | Your IB account's market-data line allowance (all API clients). Split at startup into underlyings 4, order entry 4, wing poller 12, chain stream (rest, up to `CHAIN_STREAM_MAX_LINES_CAP`), 2 spare |
| `CHAIN_STREAM_MAX_LINES_CAP` | `160` | Most lines the live chain stream may use; lines beyond it go to the poller |
| `IB_REQUEST_RATE` | `30` | Messages per second the data lane (qualification, snapshots, stream subscribes) may send to TWS. TWS handles one request queue per connection, so an unpaced burst delays an order behind it; orders and cancels are never paced. `0` disables |
| `IB_REQUEST_BURST` | `5` | Messages that may go out back to back before the rate applies |
| `ORDER_USE_CONTRACT_CACHE` | `true` | Order placement reuses the contract ids the chain service already listed (every cache miss still does a live lookup). `false` looks every order up live |
| `ORDER_MID_MAX_AGE_S` | `2.0` | A dynamic-fill order takes its starting mid from the chain's quote book when the quote is at most this old and two-sided; otherwise it subscribes to the contract |
| `PERF_LOG_SECONDS` | `60` | Seconds between perf summary log lines (`0` disables). Same data: `GET /api/perf` on localhost |
| `CHAIN_QUOTE_MAX_AGE_S` | `180` | Quotes older than this are dimmed in the chain tab and ignored by the strategy engine |
| `PRICE_BARS_KEEP_UP_TO_DATE` | `true` | Price chart bars come from an IB `keepUpToDate` request. `false` falls back to one backfill at start-up, at the 09:30 open and on reconnect, with the forming bar built from the live SPX last price. After 3 failed `keepUpToDate` starts in a row the server switches to this fallback by itself until the next restart. A blank value (`PRICE_BARS_KEEP_UP_TO_DATE=`) means the default, as for the other numeric and boolean settings. The live `keepUpToDate` check on a paper TWS is still open (see `docs/progress.md`) |
| `PUSH_ORDERED_BACKLOG_MAX` | `1000` | Unsent ordered messages (order status, IB errors, replies) one browser may hold before the server drops it; the page reconnects and gets a fresh `init`. Values below 10 are raised to 10 |
| `PUSH_SEND_TIMEOUT_S` | `5.0` | One WebSocket send longer than this drops that browser only (the others keep streaming). Values below 0.5 are raised to 0.5 |
| `CAPTURE_CLIENT_ID` | `97` | IB client id of the standalone chain capture |
| `CHAIN_LIBRARY_DIR` | `data/chain_library` | Where daily 0DTE chain files are written (env var only; gitignored) |
| `SERVER_HOST` | `0.0.0.0` | Server listen address (all interfaces) |
| `SERVER_PORT` | `8000` | Server listen port |
| `DEFAULT_ANNUAL_VOL` | `0.20` | Assumed annualized volatility for unlisted IV |
| `MONTHLY_CACHE_TTL` | `600` | Monthly chain cache TTL (seconds) |
| `SGOV_TICKER` | `SGOV` | Ticker used for risk-free rate |
| `DEFAULT_RISK_FREE_RATE` | `0.043` | Fallback risk-free rate |
| `SIM_WORKERS` | `0` | Sim worker processes: `0` = auto (CPU count), `1` = serial |
| `RTH_OPEN` / `RTH_CLOSE` | `09:30` / `16:15` | Regular trading hours window (ET) |
| `FOMC_DATES` | `[]` | FOMC meeting dates (via `params.yaml`) |

Additional tunables (chain streaming, batch sizes, viewport sync, SPXW cease/gap windows) live in `config.py` and `config/params.yaml`.

`numpy` and `scipy` are new runtime requirements; `requirements-dev.txt` adds `pytest-playwright` for the UI E2E tier.

### Measuring the IB layer

- `GET /api/perf` (localhost only) shows n / p50 / p95 / max / last for connect, contract lookups, bulk listing, order place-to-ack, cancel-to-terminal, pacer wait, chain stream cycle and startup, plus the error-101 and registry counters (hit, miss, rejected non-SMART rows). The log line every `PERF_LOG_SECONDS` carries n / p50 / p95 and the counters; `max` is on `/api/perf` only.
- The browser push shows up on `/api/perf` too: `push.broadcast` (enqueueing one message for every browser), `push.queue_wait` (time a message waited in a browser's queue) and `push.send` (one socket send). Render timings a page sends back in a `perf_report:` WebSocket message are recorded as `client.*` spans, up to 50 names and 200 samples per name per report, and at most 200 distinct `client.*` names per server process (a new name beyond that is dropped): `client.<type>.recv` (server stamp to the browser's receipt, for example `client.chain_tick.recv`), `client.<type>.paint` (receipt to the end of the frame job that drew it, for example `client.price_bar.paint`) and `client.longtask` (main-thread tasks over 50 ms). Malformed names (anything but lowercase dotted words) and samples outside 0 to 60000 ms are dropped.
- The price bar feed adds `price_bars.update` (count of IB bar updates), `price_bars.update_gap` (ms between IB updates) and `price_bars.restart` (count of requests made).
- `python -m spx_trade_desk.ib.line_probe` finds how many market-data lines your account really has (stop the dashboard and close TWS watchlists first; it only reads market data) and prints a `MARKET_DATA_LINES` value with 10% head-room. Setting it above 100 lets the chain stream cover more strikes, up to `CHAIN_STREAM_MAX_LINES_CAP` lines (lines beyond the cap go to the poller, whose snapshot batches stay at 50 lines or fewer); keep it opt-in until you have watched the Log tab for error 101.
- `python -m spx_trade_desk.ib.latency_probe` is the acceptance run on a **paper** account (it refuses any other): it places and cancels non-fillable orders and prints each latency target as PASS / NEAR / MISS. It refuses live ports and exits non-zero on a MISS or an unmeasured target.
- `python -m spx_trade_desk.ib.bars_probe` checks how IB keeps SPX 1-minute bars up to date (`keepUpToDate`): it prints the initial bars and how often updates arrive over `--seconds` (default 180); it is read-only, refuses live ports (run it on paper TWS) and needs regular hours, since IB sends no updates outside them.
- `python -m tests.e2e.render_bench --protocol v2 --seconds 60 [--out result.json]` is the render benchmark (needs Playwright and Chromium). It starts the real server with IB pointed at a closed port and feeds the page invented messages: 120 strikes, a `chain_tick` for about 30 changed contracts every 0.5 s, a price-bar update every second, a full `chain_quotes` and `gex` every 10 s, for `--seconds` on the Dashboard tab and then on the Chain tab. It prints, per tab, p50 / p95 / max of inject-to-painted-frame for the stream cycle, the price bar and the full publish, plus the long tasks over 50 ms. `--protocol v1` replays the old message mix and is valid only on commit `577db68`. Timings vary by 2 to 3 times between runs on a laptop, so compare runs made on the same machine in the same power state. `--no-flash` turns the chain's tick flash off (`state.chainFlashMax = 0`) so its cost can be read on its own; in a headless software-rendered Chromium it was about 15 to 20 ms per chain update. `python -m pytest tests/e2e/test_render_perf_playwright.py` checks the run for errors and against generous ceilings (`RENDER_BENCH_SECONDS=12` for a smoke run); `RENDER_BENCH_STRICT=1` also asserts the design targets (stream cycle p95 at most 50 ms, price bar p95 at most 20 ms, no long tasks). Results are in `docs/progress.md`.

## Discord Bot

Optional in-process bot that lets an allowlisted user query the dashboard and
control strategies from Discord. **Orders are never placed from Discord** —
the `/place` command shows the candidate and directs you to confirm in the web UI.

### Settings UI

Configure Discord — and the IB Gateway port — from the dashboard: click the
gear icon in the header. Apply hot-applies the change (the Discord bot restarts
in place; the IB port reconnects immediately) and persists it to the repo-root
`.env`, which overrides `config/params.yaml` but loses to real environment
variables. Settings endpoints only accept localhost connections. The header
connection badge is display-only now.

### Manual setup (headless alternative)

1. Create a bot in the Discord Developer Portal, copy its token, and add the
   `applications.commands` scope. Invite it to your server.
2. Provide the token and options via env var or `config/params.yaml` (never
   commit a real token):

| Variable | Example | Notes |
|---|---|---|
| `DISCORD_TOKEN` | `abc...` | Bot token. Presence enables the bot. |
| `DISCORD_GUILD_ID` | `123456789` | Reserved for alerts; slash commands are registered globally (available in any server the bot is in). New global commands can take up to ~1 hour to appear after first sync. |
| `DISCORD_CHANNEL_ID` | `987654321` | Optional; alert-stream channel. |
| `DISCORD_ALLOWED_USER_IDS` | `111,222` | Allowed Discord user IDs. |
| `DISCORD_ALLOWED_ROLE` | `trader` | Optional role name or ID that is also allowed. |

3. Restart the server.

### Commands

`/status` `/account` `/positions` `/orders` `/strategy` `/candidates <name>`
`/arm <name>` `/disarm <name>` `/killswitch on|off` `/place <name> <index>`

- Read commands query live dashboard state.
- `/arm` warns when the strategy `auto_execute`s, and honors the global kill switch.
- `/place` intentionally refuses — place orders in the browser.
- A curated stream (fills, strategy exits, take-profit closes, IB errors,
  connection and kill-switch changes) is posted to `DISCORD_CHANNEL_ID`.

## Data Modes

- **LIVE** — SPX streaming quote from IB during RTH (09:30–16:15 ET). The price chart shows today's session from 09:30 with live 1-minute bars.
- **ES-DERIVED** — Off-hours SPX price inferred from ES front-month futures movement relative to the last SPX close. The price chart shows the last regular session's bars and adds a dotted line with one derived SPX point per minute; at 09:30 ET the line is cleared and the chart switches to today's session.
- **HISTORICAL** — Last available historical bars when markets are closed and ES is unavailable. The price chart shows the last regular session and no dotted line.

**Trend conditions and the bar series.** A strategy's trend condition (RSI, percent change) reads the same 1-minute series as the price chart, including the still-forming current minute, whose close follows the live SPX last price once a second. The RSI or percent change therefore moves inside the minute, and an entry can pass or fail on the forming bar (before this change the forming bar was never in the series, so the value changed only when a minute completed). The Monte Carlo simulator does not support trend conditions, so there is no sim parity break. In regular hours that series holds only today's bars from 09:30; a server that was running across the open no longer keeps the previous session's bars in front of them. Until enough bars exist (about 15 for RSI(14), the number of minutes plus 1 for percent change) the condition cannot be evaluated: it counts as not met and blocks the entry rather than passing on missing data. The wait after the open equals the indicator's lookback: about a quarter hour for RSI(14), the configured number of minutes for percent change.
