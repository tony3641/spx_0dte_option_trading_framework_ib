"""Frontend foundation in a real browser: the Lightweight Charts price chart (snapshot, live bar,
older-bar revision, ET axis, levels), frame batching, client perf reports and reconnect backoff.

Times are invented (2099 dates) and nothing depends on the wall clock or on market hours.
"""
import calendar
import datetime
import json
import re
import time
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.hermetic_server import hermetic_server            # noqa: E402
from tests.e2e.render_bench import BENCH_JS, _proactor_policy, open_page    # noqa: E402

S = "2099-01-05"
FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


def _utc_seconds(y, mo, d, h, mi):
    return calendar.timegm(datetime.datetime(y, mo, d, h, mi).timetuple())


def _bar(hhmm, close):
    return {"time": f"{S}T{hhmm}:00-05:00", "time_short": hhmm, "open": close, "high": close + 1,
            "low": close - 1, "close": close}


@pytest.fixture(scope="module")
def browser_env():
    import asyncio
    from playwright.sync_api import sync_playwright
    prev = _proactor_policy()
    with hermetic_server() as url, sync_playwright() as p:
        yield p, url
    if prev is not None:
        asyncio.set_event_loop_policy(prev)


@pytest.fixture(scope="module")
def page(browser_env):
    p, url = browser_env
    browser, pg = open_page(p, url, "dashboard")
    yield pg
    browser.close()


def _candles(page):
    return page.evaluate("() => priceChart.candles.data().map(c => [c.time, c.close])")


def _inject(page, msg_type, data):
    page.evaluate("m => window.__benchInject(m)", {"type": msg_type, "data": data})


# --- price chart -------------------------------------------------------------------------------

def test_library_loaded_and_et_axis(page):
    assert page.evaluate("() => typeof LightweightCharts !== 'undefined' && priceChart.candles !== null")
    assert page.evaluate(f"() => etIsoToChartTime('{S}T09:30:00-05:00')") == _utc_seconds(2099, 1, 5, 9, 30)
    # The wall-clock reading is what counts: the UTC offset in the string never shifts the axis.
    assert page.evaluate("() => etIsoToChartTime('2099-07-06T09:30:00-04:00')") == _utc_seconds(2099, 7, 6, 9, 30)
    assert page.evaluate("() => etIsoToChartTime('not a time')") is None


def test_snapshot_then_live_bar(page):
    snap = {"session_date": S, "mode": "live", "bars": [_bar("09:30", 100), _bar("09:31", 101)], "overnight": []}
    _inject(page, "price_snapshot", snap)
    assert [c[1] for c in _candles(page)] == [100, 101]
    _inject(page, "price_bar", {"session_date": S, "bar": _bar("09:31", 102)})
    _inject(page, "price_bar", {"session_date": S, "bar": _bar("09:32", 103)})
    assert [c[1] for c in _candles(page)] == [100, 102, 103]


def test_spot_badge_follows_the_latest_bar(page):
    assert page.evaluate("() => document.getElementById('spotBadge').textContent") == "103.00"


def test_price_chart_older_bar_revision_resets_without_error(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    _inject(page, "price_bar", {"session_date": S, "bar": _bar("09:30", 99)})
    assert [c[1] for c in _candles(page)] == [99, 102, 103] and errors == []


def test_bar_for_another_session_is_ignored(page):
    _inject(page, "price_bar", {"session_date": "2099-01-06",
                                "bar": {**_bar("09:33", 500), "time": "2099-01-06T09:33:00-05:00"}})
    assert 500 not in [c[1] for c in _candles(page)]


def test_overnight_line_and_levels(page):
    _inject(page, "price_overnight", {"point": {"time": f"{S}T20:00:00-05:00", "value": 104.5}})
    assert page.evaluate("() => priceChart.overnight.data().length") == 1
    page.evaluate("() => updatePriceLevels({call_wall: 110, put_wall: 90, gamma_flip: 100, max_pain: null})")
    assert page.evaluate("() => Object.keys(priceChart.levels).sort()") == ["call_wall", "gamma_flip", "put_wall"]
    page.evaluate("() => updatePriceLevels({call_wall: 111, put_wall: null, gamma_flip: 100, max_pain: null})")
    assert page.evaluate("() => Object.keys(priceChart.levels).sort()") == ["call_wall", "gamma_flip"]


def test_updates_while_hidden_render_once_on_show(page):
    page.evaluate("() => switchTab('chain')")
    _inject(page, "price_bar", {"session_date": S, "bar": _bar("09:34", 105)})
    assert [c[1] for c in _candles(page)][-1] == 103          # nothing drawn while the tab is hidden
    page.evaluate("() => switchTab('dashboard')")
    page.wait_for_timeout(100)
    assert _candles(page)[-1][1] == 105


def test_init_message_carries_the_snapshot_and_the_title_keeps_its_help(page):
    price = {"session_date": S, "mode": "live", "bars": [_bar("09:30", 200)], "overnight": []}
    _inject(page, "init", {"connected": True, "data_mode": "live", "historical_date": S, "price": price})
    assert [c[1] for c in _candles(page)] == [200]
    title = page.evaluate("""() => {
        const t = document.getElementById('priceChartTitle');
        return {text: t.textContent, children: t.children.length,
                help: !!t.parentElement.querySelector('.help'), tag: t.parentElement.className};
    }""")
    assert title == {"text": "SPX Intraday — Live", "children": 0, "help": True, "tag": "chart-title"}


def test_overnight_duplicate_and_unsorted_times_resolve_the_same_way_in_setdata_and_update(page):
    def point(hhmm, value):
        return {"time": f"{S}T{hhmm}:00-05:00", "value": value}

    def line():
        return page.evaluate("() => priceChart.overnight.data().map(p => [p.time, p.value])")

    def t(hh, mm):
        return _utc_seconds(2099, 1, 5, hh, mm)

    snap = {"session_date": S, "mode": "historical", "bars": [_bar("09:30", 100)],
            "overnight": [point("20:03", 1), point("20:01", 2), point("20:03", 9)]}
    _inject(page, "price_snapshot", snap)
    assert line() == [[t(20, 1), 2], [t(20, 3), 9]]                   # sorted, the last duplicate wins
    _inject(page, "price_overnight", {"point": point("20:03", 10)})
    assert line() == [[t(20, 1), 2], [t(20, 3), 10]]                  # update() replaces the same time
    _inject(page, "price_overnight", {"point": point("20:02", 5)})
    assert line() == [[t(20, 1), 2], [t(20, 2), 5], [t(20, 3), 10]]   # an older point rebuilds the line
    page.evaluate("() => switchTab('chain')")
    _inject(page, "price_overnight", {"point": point("20:03", 11)})
    page.evaluate("() => switchTab('dashboard')")
    page.wait_for_function("() => priceChart.overnight.data().some(p => p.value === 11)", timeout=5000)
    assert line() == [[t(20, 1), 2], [t(20, 2), 5], [t(20, 3), 11]]   # a redraw after a hidden period agrees


# --- frame batching ----------------------------------------------------------------------------

def test_scheduled_renders_coalesce_per_key_and_survive_a_failing_job(page):
    out = page.evaluate("""async () => {
        const ran = [];
        scheduleRender('t.a', () => ran.push('a1'));
        scheduleRender('t.a', () => ran.push('a2'));
        scheduleRender('t.bad', () => { throw new Error('boom'); });
        scheduleRender('t.b', () => ran.push('b'));
        const pending = isRenderPending('t.a');
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        return {ran, pending, after: isRenderPending('t.a')};
    }""")
    assert out == {"ran": ["a2", "b"], "pending": True, "after": False}


def test_hidden_tab_work_is_parked_until_the_tab_is_shown(page):
    out = page.evaluate("""async () => {
        const frames = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        switchTab('chain');
        const seen = [];
        renderWhenVisible('dashboard', 't.park', () => seen.push('first'));
        renderWhenVisible('dashboard', 't.park', () => seen.push('second'));
        await frames();
        const whileHidden = seen.slice();
        switchTab('dashboard');
        await frames();
        return {whileHidden, afterShow: seen};
    }""")
    assert out == {"whileHidden": [], "afterShow": ["second"]}


# --- the chart is sized correctly when the page starts on another tab ---------------------------

def test_chart_fits_its_bars_when_the_page_starts_on_another_tab(browser_env):
    p, url = browser_env
    browser = p.chromium.launch()
    try:
        pg = browser.new_page(viewport={"width": 1600, "height": 1000})
        problems = []

        def on_console(m):
            if m.type == "error" and not m.location.get("url", "").endswith("favicon.ico"):
                problems.append(f"console: {m.text}")

        pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        pg.on("console", on_console)
        pg.goto(f"{url}/#chain")
        pg.wait_for_function("() => typeof state !== 'undefined' && state.wsConnected === true", timeout=30000)
        pg.wait_for_timeout(1000)
        pg.add_script_tag(path=BENCH_JS)
        bars = [_bar("09:30", 100), _bar("09:31", 101), _bar("09:32", 102)]
        _inject(pg, "price_snapshot", {"session_date": S, "mode": "live", "bars": bars, "overnight": []})
        assert pg.evaluate("() => priceChart.candles.data().length") == 0       # parked: the tab is hidden
        pg.evaluate("() => switchTab('dashboard')")
        pg.wait_for_function("() => priceChart.candles.data().length === 3", timeout=5000)
        pg.wait_for_timeout(200)
        view = pg.evaluate("""() => {
            const r = priceChart.chart.timeScale().getVisibleLogicalRange();
            return {width: priceChart.chart.timeScale().width(), span: r.to - r.from};
        }""")
        assert view["width"] > 100                  # laid out at its real size, not at zero width
        assert view["span"] < 10                    # three bars fitted, not a squashed 0.5 px/bar view
        assert problems == []
    finally:
        browser.close()


# --- the chart library is missing or broken: the rest of the page still works --------------------------

LC_URL = "**/lightweight-charts*"
LC_ERROR_TEXT = "Price chart library failed to load (check the network connection)."
LC_STUBS = {
    "createChart throws": "window.LightweightCharts = {CrosshairMode: {Normal: 0}, "
                          "createChart() { throw new Error('createChart failed'); }};",
    "addSeries throws": "window.__chartRemoved = false; window.LightweightCharts = {CrosshairMode: {Normal: 0}, "
                        "createChart() { return {addSeries() { throw new Error('addSeries failed'); }, "
                        "remove() { window.__chartRemoved = true; }}; }};",
}


def _mini_gex():
    strikes = [6100 + 5 * i for i in range(4)]
    return {"gex_bars": [{"strike": s, "call_gex": 1e6, "put_gex": -1e6, "net_gex": 0.0} for s in strikes],
            "call_wall": 6110, "put_wall": 6100, "gamma_flip": 6105, "max_pain": 6105, "spot_price": 6107,
            "total_net_gex": 0.0, "smile_data": [], "es_derived": False}


def _assert_page_works_without_the_price_chart(pg, problems):
    assert pg.evaluate("() => document.getElementById('priceChart').textContent") == LC_ERROR_TEXT
    assert pg.evaluate("() => document.getElementById('priceChart').className") == "price-chart-error"
    # the error text clears the chart title above it
    assert pg.evaluate("() => getComputedStyle(document.getElementById('priceChart')).paddingTop") == "40px"
    assert pg.evaluate("() => [priceChart.chart, priceChart.candles, priceChart.overnight, state.priceChartReady]")         == [None, None, None, False]
    # the rest of the page started: GEX chart, smile chart, WebSocket
    assert pg.evaluate("() => [state.gexChartReady, state.smileChartReady, state.wsConnected]") == [True, True, True]
    _inject(pg, "price_snapshot", {"session_date": S, "mode": "live", "bars": [_bar("09:30", 100)],
                                   "overnight": [{"time": f"{S}T20:00:00-05:00", "value": 100.5}]})
    _inject(pg, "price_bar", {"session_date": S, "bar": _bar("09:31", 101)})
    _inject(pg, "price_overnight", {"point": {"time": f"{S}T20:01:00-05:00", "value": 100.6}})
    _inject(pg, "gex", _mini_gex())
    assert pg.evaluate("() => document.getElementById('gexChart')._fullData[0].x.length") == 4
    assert pg.evaluate("() => document.getElementById('spotBadge').textContent") == "101.00"
    assert [p for p in problems if "pageerror" in p] == []


def test_price_chart_shows_an_error_text_when_the_library_does_not_load(browser_env):
    p, url = browser_env
    problems = []
    browser, pg = open_page(p, url, "dashboard", problems=problems,
                            before_goto=lambda page: page.route(LC_URL, lambda route: route.abort()))
    try:
        assert pg.evaluate("() => typeof LightweightCharts") == "undefined"
        _assert_page_works_without_the_price_chart(pg, problems)
    finally:
        browser.close()


@pytest.mark.parametrize("stub", sorted(LC_STUBS))
def test_price_chart_failing_to_build_does_not_stop_the_page_from_starting(browser_env, stub):
    p, url = browser_env
    problems = []

    def serve_stub(page):
        # The page pins the real library with an integrity hash, which a stub can never match: drop the
        # attributes from the served page so the stub is what runs.
        def without_sri(route):
            resp = route.fetch()
            page_html = re.sub(r'\s+(integrity|crossorigin)="[^"]*"', "", resp.text())
            route.fulfill(status=resp.status, content_type="text/html; charset=utf-8", body=page_html)

        page.route(re.compile(r"^http://127\.0\.0\.1:\d+/$"), without_sri)
        page.route(LC_URL, lambda route: route.fulfill(status=200, content_type="application/javascript",
                                                       body=LC_STUBS[stub]))

    # A document served through a route loses Chromium's loopback address space, which blocks its WebSocket.
    browser, pg = open_page(p, url, "dashboard", problems=problems, before_goto=serve_stub,
                            launch_args=["--disable-features=LocalNetworkAccessChecks"])
    try:
        _assert_page_works_without_the_price_chart(pg, problems)
        assert any("Price chart unavailable" in m for m in problems)             # the cause is logged, once
        if stub == "addSeries throws":
            assert pg.evaluate("() => window.__chartRemoved") is True            # no half-built chart is left behind
    finally:
        browser.close()


# --- client perf reports -------------------------------------------------------------------------

def test_pinned_cdn_scripts_carry_integrity_hashes_and_still_load(page):
    got = page.evaluate("""() => [...document.querySelectorAll('script[src^="https://"]')]
        .map(s => [s.src.split('/')[2], s.integrity.slice(0, 7), s.crossOrigin])""")
    assert sorted(got) == [["cdn.jsdelivr.net", "sha384-", "anonymous"], ["cdn.plot.ly", "sha384-", "anonymous"]]
    assert page.evaluate("() => [typeof LightweightCharts, typeof Plotly, state.priceChartReady]") == [
        "object", "object", True]


def test_perf_ignores_a_server_timestamp_that_is_not_plausible(page):
    spans = page.evaluate("""() => {
        perfOnMessage({type: 'perf_probe_old', ts: Date.now() - 120000});
        perfOnMessage({type: 'perf_probe_future', ts: Date.now() + 120000});
        perfOnMessage({type: 'perf_probe_ok', ts: Date.now() - 5});
        return Object.keys(_perfSpans).filter(n => n.startsWith('perf_probe'));
    }""")
    assert spans == ["perf_probe_ok.recv"]


def test_perf_job_samples_stay_bounded_when_the_job_never_runs(page):
    out = page.evaluate("""() => {
        for (let i = 0; i < 1000; i++) {
            perfOnMessage({type: 'perf_probe_hidden', ts: Date.now()});
            perfNoteJob('t.perf.never_runs');
            perfAfterHandle();
        }
        const n = _perfJobSamples.get('t.perf.never_runs').length;
        perfJobDropped('t.perf.never_runs');
        return n;
    }""")
    assert out == 200


def test_perf_report_reaches_the_server_under_accepted_names(page, browser_env):
    _, url = browser_env
    page.evaluate("""async () => {
        switchTab('dashboard');
        // A paint span exists only for a message whose handling scheduled a render job.
        const handled = (type, ageMs, jobKey) => {
            perfOnMessage({type, ts: Date.now() - ageMs});
            if (jobKey) scheduleRender(jobKey, () => {});
            perfAfterHandle();
        };
        handled('price_bar', 3, 't.perf.bar');
        handled('status', 4, 't.perf.status');
        handled('chain_tick', 5, null);                  // Chain tab hidden: parked, nothing scheduled, no paint span
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        perfSendReport();
    }""")
    want = {"client.price_bar.recv", "client.price_bar.paint", "client.status.recv", "client.status.paint",
            "client.chain_tick.recv"}
    deadline = time.time() + 5
    seen = set()
    while time.time() < deadline and not want <= seen:
        with urllib.request.urlopen(f"{url}/api/perf", timeout=2) as r:
            seen = set(json.load(r)["metrics"])
        time.sleep(0.1)
    assert want <= seen
    assert "client.chain_tick.paint" not in seen


# --- reconnect ---------------------------------------------------------------------------------

def test_reconnect_backs_off_and_resets_when_the_socket_opens(page):
    out = page.evaluate("""() => {
        const delays = [];
        const realSetTimeout = window.setTimeout;
        window.setTimeout = (fn, ms) => { delays.push(ms); return 1; };
        try {
            reconnectAttempt = 0;
            for (let i = 0; i < 6; i++) { reconnectTimer = null; scheduleReconnect(); }
        } finally {
            window.setTimeout = realSetTimeout;
            reconnectTimer = null;
        }
        const beforeOpen = reconnectAttempt;
        ws.onopen();
        return {delays, beforeOpen, afterOpen: reconnectAttempt};
    }""")
    assert out == {"delays": [500, 1000, 2000, 3000, 3000, 3000], "beforeOpen": 6, "afterOpen": 0}


# --- RSI(14) pane --------------------------------------------------------------------------------

def test_rsi_uses_wilder_smoothing(page):
    closes = [100 + i for i in range(15)] + [113]          # 14 rises of 1, then a fall of 1
    vals = page.evaluate("c => rsiValues(c, 14)", closes)
    assert vals[:14] == [None] * 14 and vals[14] == 100
    assert round(vals[15], 3) == round(100 - 100 / (1 + 13), 3)   # avg gain 13/14, avg loss 1/14
    assert page.evaluate("() => rsiValues([1, 1, 1, 1], 3)") == [None, None, None, 50]   # a flat market is 50


def test_the_rsi_pane_follows_the_bars(page):
    bars = [_bar(f"10:{m:02d}", 100 + (m % 3)) for m in range(20)]
    _inject(page, "price_snapshot", {"session_date": S, "mode": "live", "bars": bars, "overnight": []})
    page.evaluate(FRAMES)
    assert page.evaluate("() => priceChart.chart.panes().length") == 2
    assert page.evaluate("() => priceChart.rsi.data().length") == 6            # 20 bars, the first 14 warm up
    assert page.evaluate("() => priceChart.rsi.data()[0].time") == _utc_seconds(2099, 1, 5, 10, 14)
    assert sorted(page.evaluate("() => priceChart.rsiGuides.map(l => l.options().price)")) == [30, 70]
    before = page.evaluate("() => priceChart.rsi.data().slice(-1)[0].value")
    _inject(page, "price_bar", {"session_date": S, "bar": _bar("10:19", 140)})   # a jump on the live bar
    page.evaluate(FRAMES)
    last = page.evaluate("() => priceChart.rsi.data().slice(-1)[0]")
    assert last["time"] == _utc_seconds(2099, 1, 5, 10, 19) and last["value"] > before
    _inject(page, "price_bar", {"session_date": S, "bar": _bar("10:20", 139)})
    page.evaluate(FRAMES)
    assert page.evaluate("() => priceChart.rsi.data().length") == 7
