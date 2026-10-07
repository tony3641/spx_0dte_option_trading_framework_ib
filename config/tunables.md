# Tunables Inventory

Generated: 2026-04-16
Scope: backend + frontend runtime/configuration knobs in the current workspace.

## 1) Environment-driven settings (primary)

Source: spx_trade_desk/core/config.py

- IB_HOST = "127.0.0.1"
- IB_PORT = 7497
- IB_CLIENT_ID = 1
- CHAIN_REFRESH_SECONDS = 10
- DASHBOARD_CHAIN_REFRESH_SECONDS = 300
- CHAIN_TAB_FULL_REFRESH_SECONDS = 300
- PRICE_PUSH_INTERVAL = 1.0
- PRICE_BARS_KEEP_UP_TO_DATE = true (price chart bars come from an IB keepUpToDate request; false = one-shot backfill at boot, 09:30 and reconnect, with the forming bar aggregated from the live SPX last price; after 3 failed keepUpToDate starts in a row the server switches to false for the rest of the process). A blank value (`NAME=`) of a numeric or boolean setting means its default
- SERVER_HOST = "0.0.0.0"
- SERVER_PORT = 8000
- MARKET_DATA_LINES = 100 (account IB line allowance; split into fixed/order/poll/stream shares at startup)
- IB_REQUEST_RATE = 30 (messages per second the data lane may send to TWS; orders and cancels are never paced; 0 disables the pacer)
- IB_REQUEST_BURST = 5 (messages that may go out back to back before the rate applies)
- ORDER_USE_CONTRACT_CACHE = true (order path reuses cached contract ids; false forces a live lookup for every order)
- ORDER_MID_MAX_AGE_S = 2.0 (dynamic fill takes its mid from the quote book only if the quote is at most this old)
- PERF_LOG_SECONDS = 60 (one perf summary log line per interval; 0 disables; the same data is at /api/perf on localhost)
- CHAIN_STREAM_UPDATE_INTERVAL = 0.5
- VIEWPORT_CENTER_MIN_INTERVAL = 0.2
- PUSH_ORDERED_BACKLOG_MAX = 1000, minimum 10 (a lower value is raised to 10) (unsent critical messages, such as order status, one browser may queue before it is dropped and reconnects to a fresh init; raise it only if a slow but healthy client gets dropped during order bursts)
- PUSH_SEND_TIMEOUT_S = 5.0, minimum 0.5 (a lower value is raised to 0.5) (one WebSocket send slower than this drops that browser only; raise it for clients on a slow remote link, lower it to shed a stuck tab sooner)

## 2) Backend hardcoded tunables (not env-wired today)

### spx_trade_desk/server.py
- Reconnect endpoint valid port range: 1..65535

### spx_trade_desk/core/app_state.py
- price_history maxlen: 28800
- price_overnight maxlen: 1440 (ES-derived SPX points, one per minute outside RTH)
- annual_vol default: 0.20
- risk_free_rate default: 0.043

### spx_trade_desk/market/chain_fetcher.py
- BATCH_SIZE: 200
- QUALIFY_BATCH_SIZE: 150 (only sizes the single-qualification fallback batches)
- DEFAULT_ANNUAL_VOL: 0.20
- TRADING_DAYS_PER_YEAR: 252
- fetch_option_chain std_dev_range default: 5.0
- _snapshot_batch timeout default: 12.0 s
- qualify batch delay: 0.1 s
- snapshot inter-batch delay: 0.5 s

### spx_trade_desk/market/chain_manager.py
- build_chain_quotes annual_vol default: 0.20
- same-day minimum minutes-left floor: 1.0 min
- annualization basis: 390 minutes/day, 252 days/year
- chain stream startup delay: 2 s
- chain stream no-data sleep: 5 s
- chain stream no strikes sleep: 10 s
- chain contract lookup: one shared ContractRegistry (ib/contracts.py): one bulk reqContractDetails per (expiry, trading class), single-qualification fallback in QUALIFY_BATCH_SIZE batches through the request pacer (ib/pacing.py); strikes missing from the listing are retried after CHAIN_STREAM_UNKNOWN_RETRY_SECS with one re-list
- chain stream quote-book writes: only rows whose stream ticked since the previous pass
- chain stream tick-log cadence: 10.0 s
- chain stream update cadence: CHAIN_STREAM_UPDATE_INTERVAL (from spx_trade_desk/core/config.py); each cycle sends one `chain_tick` (with its `expiration_raw`) with only the fields that changed per (strike, right), nothing when nothing changed; the first cycle after a browser connects sends every field of every streamed row (the full `chain_quotes` comes from the publisher every CHAIN_REFRESH_SECONDS)
- monthly cache TTL: 600 s
- monthly fetch std_dev_range: 8.0

### spx_trade_desk/market/chain_poller.py
- CHAIN_STD_DEV_RANGE: 8.0 daily sigmas, strikes that are multiples of 5
- ANNUAL_VOL_REFRESH_S: 300 s (compute_annual_vol lookback_days: 30)
- POLL_PACE_S: 1.0 s between productive cycles; POLL_IDLE_S: 10 s after a cycle that wrote nothing
- POLL_START_DELAY_S: 1.0 s
- connection/expiration retry sleep: 10 s; missing spot retry sleep: 30 s; CBOE daily gap sleep: 10 s
- snapshot timeout: 6.0 s per batch; batch size: the 'poll' line share

### spx_trade_desk/market/chain_publisher.py
- publish cadence: CHAIN_REFRESH_SECONDS (10 s); heartbeat touched on every successful tick

### spx_trade_desk/market/capture.py
- HEARTBEAT_STALE_S: 180 s; IDLE_CHECK_S: 60 s; STEP_S: 5 s
- MAX_FAILURES: 5 (a failed sweep waits a record interval, 60-120 s, before the next attempt)

### spx_trade_desk/ib/connection.py
- connectAsync timeout: 15 s
- SPX generic ticks: "233"
- ES baseline end time: 16:20:00 ET
- ES baseline history duration: 600 s
- ES baseline bar size: 1 min

### spx_trade_desk/market/hours.py
- RTH open: 09:30 ET
- RTH close: 16:15 ET
- SPXW cease (expiration day): 16:00 ET
- Daily options gap: 17:00..20:15 ET

### spx_trade_desk/ib/account.py
- Invalid IB sentinel for prices: 1.7976931348623157e+308
- FORCE_REFRESH_INTERVAL: 10.0 s
- account push loop base sleep: 1.0 s
- account push error backoff: 2 s

### spx_trade_desk/ib/orders.py
- await_order_status timeout default: 5.0 s
- await_order_status poll sleep: 0.1 s
- watch_and_push_status timeout: 30.0 s
- watch_and_push_status poll sleep: 0.5 s
- watch_parent_and_cancel_child timeout: 86400.0 s
- watch_parent_and_cancel_child poll sleep: 0.5 s
- default repriceIntervalSec fallback: 0.3 s
- midpoint quote attempts: 8
- midpoint quote poll sleep: 0.1 s
- post stop attach settle sleep: 0.05 s
- dynamic fill ack wait timeout: 3.0 s
- normal ack wait timeout: 10.0 s
- pending recheck sleep after reqOpenOrders: 0.5 s
- dynamic fill max runtime: 300.0 s
- dynamic fill max iterations: 10
- dynamic fill min reprice sleep clamp: 0.05 s
- combo pending recheck sleep: 0.5 s
- cancel order settle sleep: 0.1 s

### spx_trade_desk/market/bars.py
- compute_annual_vol lookback_days default: 30
- minimum bars needed for vol calc: 5
- annualization trading days: 252
- historical bars duration: 1 D
- historical bars size: 1 min
- historical fetch off-hours end time: 16:30:00 ET

### spx_trade_desk/market/price_bars.py
- price_bars_loop cadence: PRICE_PUSH_INTERVAL (live merge of the forming bar, overnight line)
- price_bars_loop error backoff: 5 s
- IB request: 1 D, 1 min, TRADES, regular hours, keepUpToDate (PRICE_BARS_KEEP_UP_TO_DATE)
- PRICE_BARS_STALL_S: 180 s (RTH: no IB update for this long cancels and re-requests the bars)
- re-request backoff after an IB error or stall: 5 s, 15 s, 60 s (cap)
- LIVE_FAILURES_BEFORE_FALLBACK: 3 failed keepUpToDate starts in a row switch the feed to the one-shot backfill for the rest of the process (cleared by a start that returns bars or by an IB update)
- overnight (ES-derived) line: one point per minute outside RTH, at most 1440 points

### spx_trade_desk/core/rates.py
- SGOV source URL: https://finance.yahoo.com/quote/SGOV?p=SGOV
- DEFAULT_RISK_FREE_RATE: 0.038
- fetch_sgov_7_day_yield timeout default: 5.0 s

### spx_trade_desk/web/ws.py
- status_push_loop cadence: 5 s
- keepalive ib.sleep cadence: 0.1 s
- keepalive asyncio sleep cadence: 0.1 s
- keepalive error sleep: 0.5 s
- websocket receive timeout: 30 s

## 3) Frontend hardcoded tunables (static/js)

### static/js/state.js
- TAB_KEY: "spx0dte.activeTab"
- VALID_TABS: dashboard, chain, account
- CHAIN_VIEWPORT_SEND_THROTTLE_MS: 200
- CHAIN_VIEWPORT_CENTER_THRESHOLD: 30
- state.gexWindowStrikes: 40 (the GEX and smile charts draw this many strike steps each side of spot; the step is the median gap between strikes)
- state.gexShowAll: false (true draws every strike; the Near / All toggle sets it)
- state.smileMinIntervalMs: 5000 (a changed IV smile redraws at most this often, with a trailing draw)
- state.chainFlashMax: 40 (most tick flashes started per chain flush; 0 turns the flash off)
- Theme storage key: `spx-theme` (`light` or `dark`; absent = follow the OS), set from static/js/theme.js

### static/js/ws.js
- Initial viewport report delay after open: 120 ms
- Reconnect delays: 500, 1000, 2000 ms, then every 3000 ms (the attempt counter resets when a socket opens)

### static/js/main.js
- `--shell-h` CSS variable: measured height of the header, level strip and tab bar (`syncShellHeight()`, re-run on resize); the dashboard grid and chain container fill the rest of the viewport (CSS fallback 134px)
- Chain age update interval: 1000 ms (also toggles the chain table's quote-stale marks)

### static/js/tabs.js
- (the 50 ms Dashboard resize timer is gone: a ResizeObserver in main.js sizes the GEX and smile charts)
- Chain viewport recenter delay after switch: 80 ms

### static/js/render-loop.js
- Frame batching: one requestAnimationFrame flush per frame for light jobs
- HEAVY_MAX_WAIT_FLUSHES: 4 (a heavy job, such as a GEX or smile Plotly draw, runs one per frame after the light jobs; after waiting behind this many busy flushes it runs in a frame that also has light jobs)

### static/js/perf.js
- PERF_REPORT_MS: 10000 (browser perf_report interval)
- PERF_MAX_SAMPLES: 200 (per span name and report; the server keeps at most 50 names and 200 samples per name, and drops samples outside 0..60000 ms)

### static/js/price-chart.js
- Level price lines: Call Wall, Put Wall (solid), Gamma Flip, Max Pain (dashed)
- Candle and line colors come from `themeColors()` (tokens in static/css/tokens.css), re-applied on `themechange`
- A reconnect snapshot of the same session keeps the visible range (shifted by the number of new bars when the viewer was at the live edge); only the first snapshot and a new session fit the content
- Times: ET wall-clock encoded as UTC seconds (Date.UTC)

### static/js/chain-table.js
- Visible strike window: +/-5 sigma plus +/-60 points
- Gamma flip row highlight tolerance: 2.5 points

### static/js/charts.js
- Mobile breakpoint: 600 px
- state.spotLineMinIntervalMs: 2000 (at most one spot-line relayout per GEX / smile chart in this window; set in state.js)
- Chart colors come from `themeColors()` (static/js/theme-colors.js), which reads the tokens in static/css/tokens.css; `chartTheme()` / `patchChartTheme()` re-theme the Plotly charts on `themechange`
- Axis/title legend default font sizes: 9, 10, 11
- Common line widths used for overlays/traces: 1.5, 2

### static/js/badges.js
- GEX display scaling thresholds: 1e3, 1e6, 1e9
- Reconnect IB prompt default port: 7497

### static/js/order-entry.js
- Toast auto-hide timeout: 5000 ms
- CANCEL_PENDING_TTL_MS: 60000 (a cancel IB has not confirmed after this long is reported as lost; a second click inside it sends nothing)

### static/js/strategy-builder.js
- SPX tick rule: >2 uses 0.10, otherwise 0.05
- Payoff scan range buffer: minStrike-100 to maxStrike+100
- Payoff scan step: 0.5
- Unlimited PnL threshold sentinel: 1e8

### spx_trade_desk/sim/pricing_tables.py, pricing_model.py, validate.py
- z grid: -6..+3, step 0.25 (37 nodes)
- tau bucket edges (minutes to the close): 300, 180, 120, 60, 30, 15
- put-mid spread bucket edges: 0.5, 1, 2, 3, 5, 10, 20
- node validity: >= 3 points from >= 2 records; spread cell: >= 5 points
- extrapolation slope cap |d(r^2)/dz|: 2.0; IV/ATM floor: 0.3; Lee cap applies from |z| >= 1
- tiers: library needs >= 5 days in the VIX1D regime and >= 10 days in total; a bucket falls back below 20 sweeps
- VIX1D regimes (prior close): < 12, 12-18, 18-25, >= 25
- stale after 10 trading days without a capture
- level link L clip: [0.5, 3]; sim sigma clip: [1e-4, 5]
- spread floor: 0.025 below a $3 mid, 0.05 at or above
- harness: deltas 0.05/0.10/0.15/0.20, width 10, real credit >= 0.15, delta tolerance 0.03, credit bar 25% before 15:00 and 40% after, strike within 5 points in >= 80% of pairs, >= 5 pairs per bucket

## 4) Duplicate/overlap notes

- There are existing env keys in spx_trade_desk/core/config.py that appear to be legacy/unused in current runtime path:
  - DASHBOARD_CHAIN_REFRESH_SECONDS
  - CHAIN_TAB_FULL_REFRESH_SECONDS
- The chain publisher keys off CHAIN_REFRESH_SECONDS; the wing poller runs continuously on the 'poll' line share.

## 5) Suggested normalization path (optional next step)

If you want every tunable centralized and runtime-editable, the next pass should:

1. Add remaining hardcoded backend values into spx_trade_desk/core/config.py as env-backed constants.
2. Add a frontend settings object (single static/js config module).
3. Replace in-file literals with imports/references from those central modules.
