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

### Dealer gamma profile (Phase 1): market/dealer_model.py, levels.py, atm.py, dealer_flows.py, dealer_payload.py
- dealer_model.MULTIPLIER = 100.0 (contract multiplier of the dealer share-equivalent delta)
- dealer_model.IV_FLOOR = 1e-4 (lowest implied vol the greeks use, so a zero IV cannot divide by zero)
- levels.ZERO_GAMMA_BAND = 0.05 (zero gamma is searched on spot +/- 5%; this is the search range, not the sent curve)
- levels.PROFILE_BAND = 0.02 (the curve sent to the browser: spot +/- 2%; also the node band when no straddle exists)
- levels.PROFILE_STEP = 1.0 (search grid step, index points)
- levels.PROFILE_SEND_STEP = 2.5 (sent profile values and hedge step, index points)
- levels.NODES_PER_SIDE = 3 (nodes kept above and below spot: R1..R3, S1..S3)
- levels.NODE_RANGE_EM = 2.0 (node band: spot +/- 2.0 x the live expected move)
- levels.NODE_MIN_PROMINENCE = 0.15 (a node's prominence must reach 15% of the band's largest |GEX|)
- levels.VOID_FRAC = 0.10 (a void strike has |GEX| under 10% of the band's largest)
- levels.VOID_MIN_STRIKES = 2 (a void is at least this many consecutive quiet strikes)
- atm.EM_MAX_GAP = 10.0 points (a straddle or ATM IV bracket wider than this gives no value)
- atm.EM_ANCHOR_TIME = 09:31 ET (the open band anchors at the first eligible publish at or after this time)
- atm.SESSION_CLOSE = 16:00 ET (no anchor at or after the close; GTH publishes for another expiration never anchor)
- dealer_flows.VOL_POINT = 0.01 (one vol point, absolute IV, for vanna)
- dealer_flows.YEAR_MINUTES = 390 x 252 (trading minutes per year, the GEX time unit)
- dealer_flows.T_END_YEARS = 1 / YEAR_MINUTES (the charm-to-close end: one minute before the close)
- dealer_flows.CLOSE_HOUR = 16 (the close used by the charm path, ET)
- dealer_flows charm path marks: every :00 and :30 ET before the close, plus the final minute before the close
- dealer_payload IV change windows: 15 and 30 minutes (`iv_change_15m`, `iv_change_30m`); the ATM IV history is kept per minute and reset on each expiration
- charm_history.DT15_YEARS = 15 / (390 x 252) (existing; the 15-minute charm step that charm_flow_15m_usd uses)
- dealer_payload rounding: prices 2 decimals, USD whole dollars (int), profile values and hedge int, ATM IV in vol points at 2 decimals (not configurable)

## 3) Frontend hardcoded tunables (static/js)

### static/js/state.js
- TAB_KEY: "spx0dte.activeTab"
- VALID_TABS: dashboard, chain, account
- CHAIN_VIEWPORT_SEND_THROTTLE_MS: 200
- CHAIN_VIEWPORT_CENTER_THRESHOLD: 30
- state.gexWindowStrikes: 40 (the IV smile draws this many strike steps each side of spot; the step is the median gap between strikes)
- state.gexShowAll: false (true draws every strike in the smile; the Near / All toggle sets it)
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
- (the 50 ms Dashboard resize timer is gone: a ResizeObserver in main.js sizes the smile chart, and the price chart and GEX profile size themselves)
- Chain viewport recenter delay after switch: 80 ms

### static/js/render-loop.js
- Frame batching: one requestAnimationFrame flush per frame for light jobs
- HEAVY_MAX_WAIT_FLUSHES: 4 (a heavy job, such as the smile's Plotly draw, runs one per frame after the light jobs; after waiting behind this many busy flushes it runs in a frame that also has light jobs)

### static/js/perf.js
- PERF_REPORT_MS: 10000 (browser perf_report interval)
- PERF_MAX_SAMPLES: 200 (per span name and report; the server keeps at most 50 names and 200 samples per name, and drops samples outside 0..60000 ms)

### static/js/price-chart.js
- Level step lines, the entries of `PRICE_LEVELS` in `static/js/price-chart.js`: Call Wall, Put Wall, Zero Gamma and Max Pain (`style` dotted, `width` 2); Expected move high / low (`style` dashed, `width` 1, `profile: false`); the nearest pin above / below as R / S (`style` solid, `width` 1, `alpha` 0.45, `profile: false`). `profile: false` keeps an entry off the GEX profile. Each entry's `alpha` defaults to `LEVEL_ALPHA` (0.7, opaque share of the line and its tag). At most `LEVEL_ROWS_MAX` (2000) change points kept. Axis tags are the codes CW / PW / ZG / MP / EM+ / EM- / R / S.
- static/js/gex-profile.js: `GEX_HEAD_H` (20 px caption strip), `GEX_LABEL_ROOM` (40 px kept free for a bar's value label), `GEX_BAR_FILL` (0.74 of a strike's band) and `GEX_BAR_MAX_H` (18 px), `GEX_ZOOM_BUTTON` (1.5 per `-` / `+` click), `GEX_WHEEL_RATE` (0.0016 zoom exponent per wheel unit), `GEX_MIN_SPAN` (4 index points, the closest zoom; the widest is the strike range x 1.2 or 5% of spot), `GEX_DRAG_PX` (3 px before a press is a drag), `GEX_LABELS_PER_SIDE` (4 value labels when rows are too thin to label all); `GEX_VIEW_KEY` (`spx-gex-profile-view`) remembers Split / Net; `GEX_CURVE_KEY` (`spx-gex-curve`: `'1'` on, the default, or `'0'` off) remembers the Curve button; `GEX_NODE_HALF` (4 px, half the width of a node diamond); column width `--gex-profile-w` (216 px, 150 px under 700 px) in static/css/charts.css
- Charm heatmap: `CHARM_FRAMES_MAX` (1440) frames kept; `CHARM_PREF_KEY` (`spx-charm-heatmap`) remembers the Charm button
- static/js/charm-heatmap.js: `CHARM_MIN_RATIO` (cells under 0.5% of the scale stay clear), `CHARM_ALPHA_MIN` / `CHARM_ALPHA_SPAN` (alpha 0.06 to 0.40), `CHARM_ALPHA_STEPS`, `CHARM_MAX_GAP_S` (a column is at most 5 min wide)
- spx_trade_desk/market/charm_history.py: `CHARM_BAND` (3% of spot), `MAX_FRAMES` (1440)
- Candle and line colors come from `themeColors()` (tokens in static/css/tokens.css), re-applied on `themechange`
- A reconnect snapshot of the same session keeps the visible range (shifted by the number of new bars when the viewer was at the live edge); only the first snapshot and a new session fit the content
- Times: ET wall-clock encoded as UTC seconds (Date.UTC)

### static/js/chain-table.js
- Visible strike window: +/-5 sigma plus +/-60 points
- Gamma flip row highlight tolerance: 2.5 points

### static/js/charts.js
- Mobile breakpoint: 600 px
- state.spotLineMinIntervalMs: 2000 (at most one spot-line relayout of the smile chart in this window; set in state.js; the GEX profile's NOW line is not throttled)
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
