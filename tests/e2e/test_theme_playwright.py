"""Theme runtime and (in later tasks) re-theming, layout and dock behavior in a real browser.
All data is invented (2099 dates, ACME-style positions); nothing depends on the wall clock."""
import asyncio

import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.hermetic_server import hermetic_server            # noqa: E402
from tests.e2e.render_bench import BENCH_JS, _proactor_policy    # noqa: E402

FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


@pytest.fixture(scope="module")
def browser_env():
    from playwright.sync_api import sync_playwright
    prev = _proactor_policy()
    with hermetic_server() as url, sync_playwright() as p:
        yield p, url
    if prev is not None:
        asyncio.set_event_loop_policy(prev)


def _open(p, url, scheme="dark", init_script=None, tab="dashboard", viewport=(1600, 1000), seed=True):
    browser = p.chromium.launch()
    ctx = browser.new_context(color_scheme=scheme, viewport={"width": viewport[0], "height": viewport[1]})
    if init_script:
        ctx.add_init_script(init_script)
    page = ctx.new_page()
    page.goto(f"{url}/#{tab}")
    page.wait_for_function("() => typeof state !== 'undefined' && state.wsConnected === true", timeout=30000)
    page.wait_for_timeout(1500)
    page.add_script_tag(path=BENCH_JS)
    if seed:
        page.evaluate("() => window.__benchSeed()")
    return browser, ctx, page


def _theme(page):
    return page.evaluate("() => document.documentElement.getAttribute('data-theme')")


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_follows_the_os_by_default(browser_env, scheme):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, scheme, seed=False)
    try:
        assert _theme(page) == scheme
        assert page.evaluate("() => localStorage.getItem('spx-theme')") is None
    finally:
        browser.close()


def test_toggle_persists_and_shift_click_follows_the_os_again(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", seed=False)
    try:
        page.click("#themeToggle")
        assert _theme(page) == "light"
        assert page.evaluate("() => localStorage.getItem('spx-theme')") == "light"
        page.reload()
        page.wait_for_function("() => document.documentElement.hasAttribute('data-theme')")
        assert _theme(page) == "light"
        page.click("#themeToggle", modifiers=["Shift"])
        assert _theme(page) == "dark"                      # the OS is dark
        assert page.evaluate("() => localStorage.getItem('spx-theme')") is None
    finally:
        browser.close()


def test_an_invalid_stored_value_is_ignored(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "light", init_script="localStorage.setItem('spx-theme', 'purple')", seed=False)
    try:
        assert _theme(page) == "light"
    finally:
        browser.close()


def test_blocked_storage_still_themes_and_toggles(browser_env):
    p, url = browser_env
    browser = p.chromium.launch()
    ctx = browser.new_context(color_scheme="dark")
    ctx.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked'); }})")
    page = ctx.new_page()
    try:
        page.goto(f"{url}/#dashboard", wait_until="domcontentloaded")
        assert _theme(page) == "dark"
        page.click("#themeToggle")
        assert _theme(page) == "light"
    finally:
        browser.close()


def test_themechange_fires_once_per_change(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", seed=False)
    try:
        page.evaluate("() => { window.__tc = []; window.addEventListener('themechange', e => window.__tc.push(e.detail.theme)); }")
        page.click("#themeToggle")
        page.click("#themeToggle")
        assert page.evaluate("() => window.__tc") == ["light", "dark"]
    finally:
        browser.close()


@pytest.mark.parametrize("scheme,canvas,panel", [("dark", "rgb(7, 9, 12)", "rgb(13, 17, 23)"),
                                                  ("light", "rgb(243, 245, 248)", "rgb(255, 255, 255)")])
def test_shell_uses_the_tokens(browser_env, scheme, canvas, panel):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, scheme)
    try:
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == canvas
        assert page.evaluate("() => getComputedStyle(document.querySelector('.header')).backgroundColor") == panel
    finally:
        browser.close()


def test_flipping_the_theme_restyles_the_shell_live(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        page.click("#themeToggle")
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(243, 245, 248)"
        page.click("#themeToggle")
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(7, 9, 12)"
    finally:
        browser.close()


def test_chain_rows_and_place_order_use_tokens(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="chain")
    try:
        # Select the first ask on the table so the builder shows the order row.
        page.evaluate("() => { const td = document.querySelector('#chainBody td.cell-ask'); td.click(); }")
        page.wait_for_selector("#orderEntryRow", state="visible")
        btn_bg = page.evaluate("() => getComputedStyle(document.getElementById('stratPlaceBtn')).backgroundColor")
        assert btn_bg == "rgb(240, 180, 41)"                      # --accent in the dark theme
        page.click("#themeToggle")
        # The button has a short background transition: wait for the computed color to settle.
        page.wait_for_function("() => getComputedStyle(document.getElementById('stratPlaceBtn')).backgroundColor === 'rgb(138, 90, 0)'",
                               timeout=3000)                      # --accent in the light theme
        assert page.evaluate("() => getComputedStyle(document.querySelector('#chainBody td.cell-ask')).backgroundColor") != ""
    finally:
        browser.close()


def test_no_css_file_holds_a_color_literal():
    import tests.test_ui_tokens as t
    leftovers = [(f, c) for f, c in t.hardcoded_colors() if f.endswith(".css") or f == "index.html"]
    assert not leftovers, leftovers[:10]


@pytest.mark.parametrize("tab", ["account", "strategies", "sim", "log"])
def test_each_remaining_tab_renders_in_both_themes_without_errors(browser_env, tab):
    p, url = browser_env
    for scheme in ("dark", "light"):
        browser, _ctx, page = _open(p, url, scheme, tab=tab)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.evaluate("t => switchTab(t)", tab)
            page.wait_for_timeout(500)
            color = page.evaluate("() => getComputedStyle(document.body).color")
            assert color == ("rgb(36, 41, 47)" if scheme == "light" else "rgb(201, 209, 217)")
            assert errors == []
        finally:
            browser.close()


def _plotly_bg(page, el_id):
    return page.evaluate("id => document.getElementById(id)._fullLayout.paper_bgcolor", el_id)


def _price_bg(page):
    return page.evaluate("() => priceChart.chart.options().layout.background.color")


def test_charts_recolor_when_the_theme_flips(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.wait_for_function("() => document.getElementById('gexChart')._fullLayout && document.getElementById('smileChart')._fullLayout")
        assert _plotly_bg(page, "gexChart") == "#0d1117"
        assert _plotly_bg(page, "smileChart") == "#0d1117"
        assert _price_bg(page) == "#0d1117"
        page.click("#themeToggle")
        page.evaluate(FRAMES)
        page.evaluate(FRAMES)
        page.evaluate(FRAMES)
        assert _plotly_bg(page, "gexChart") == "#ffffff"
        assert _plotly_bg(page, "smileChart") == "#ffffff"
        assert _price_bg(page) == "#ffffff"
        assert errors == []
    finally:
        browser.close()


def test_a_flip_while_the_dashboard_is_hidden_shows_the_new_colors_on_return(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.evaluate("() => switchTab('chain')")
        page.evaluate(FRAMES)
        page.click("#themeToggle")
        page.evaluate("() => switchTab('dashboard')")
        for _ in range(4):
            page.evaluate(FRAMES)
        assert _plotly_bg(page, "gexChart") == "#ffffff"
        assert _plotly_bg(page, "smileChart") == "#ffffff"
        assert _price_bg(page) == "#ffffff"
        assert errors == []
    finally:
        browser.close()


def test_theme_colors_helper(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", seed=False)
    try:
        assert page.evaluate("() => themeColors().bgPanel") == "#0d1117"
        assert page.evaluate("() => withAlpha('#3fb950', 0.5)") == "rgba(63,185,80,0.5)"
        page.click("#themeToggle")
        assert page.evaluate("() => themeColors().bgPanel") == "#ffffff"      # cache invalidated by themechange
    finally:
        browser.close()


def _rect(page, selector):
    return page.evaluate("s => { const r = document.querySelector(s).getBoundingClientRect(); "
                         "return {l: r.left, t: r.top, w: r.width, h: r.height, b: r.bottom, r: r.right}; }", selector)


@pytest.mark.parametrize("size", [(1920, 1080), (1440, 900)])
def test_dashboard_is_a_quad_that_fits_the_screen(browser_env, size):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", viewport=size)
    try:
        for _ in range(3):
            page.evaluate(FRAMES)
        assert page.evaluate("() => document.documentElement.scrollHeight <= window.innerHeight + 1")
        price, smile = _rect(page, ".panel-price"), _rect(page, ".panel-smile")
        gex, pos = _rect(page, ".panel-gex"), _rect(page, ".panel-pos")
        for r in (price, smile, gex, pos):
            assert r["w"] > 250 and r["h"] > 150
            assert r["b"] <= size[1] + 1 and r["r"] <= size[0] + 1
        assert abs(price["t"] - smile["t"]) < 2 and price["l"] < smile["l"]
        assert gex["t"] > price["t"] + 100 and abs(gex["l"] - price["l"]) < 2
        assert abs(pos["l"] - smile["l"]) < 2 and abs(pos["t"] - gex["t"]) < 2
        assert page.evaluate("() => document.querySelectorAll('#priceChart canvas').length") > 0
        assert page.evaluate("() => !!document.querySelector('#gexChart .main-svg')")
    finally:
        browser.close()


def test_dashboard_collapses_to_one_column_when_narrow(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", viewport=(1000, 800))
    try:
        cols = page.evaluate("() => getComputedStyle(document.getElementById('dashboardTab')).gridTemplateColumns")
        assert len(cols.split(" ")) == 1
    finally:
        browser.close()


def test_positions_panel_shows_the_seeded_account(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        page.evaluate(FRAMES)
        assert page.evaluate("() => document.getElementById('dashNetLiq').textContent") == "$100.0K"
        assert page.evaluate("() => document.getElementById('dashUnPnl').textContent") == "$412.50"
        assert "pos" in page.evaluate("() => document.getElementById('dashUnPnl').className")
        assert "neg" in page.evaluate("() => document.getElementById('dashRePnl').className")
        rows = page.evaluate("() => [...document.querySelectorAll('#dashPositionsBody tr')].map(r => r.textContent)")
        assert len(rows) == 2 and "6100P" in rows[0]
    finally:
        browser.close()


def test_positions_panel_survives_empty_and_malformed_accounts(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        inject = "m => window.__benchInject({type: 'account_update', data: m})"
        page.evaluate(inject, {"summary": {}, "positions": [], "orders": [], "executions": []})
        assert page.evaluate("() => document.getElementById('dashPositionsBody').textContent") == "No open positions"
        assert page.evaluate("() => document.getElementById('dashNetLiq').textContent") == "-"
        page.evaluate(inject, {"summary": {"NetLiquidation": None}, "orders": [], "executions": [],
                               "positions": [None, {"position": 0, "contract": {}},
                                             {"position": 2, "contract": None, "unrealizedPNL": None}]})
        rows = page.evaluate("() => [...document.querySelectorAll('#dashPositionsBody tr')].map(r => r.textContent)")
        assert len(rows) == 1 and rows[0].startswith("?")
        assert errors == []
    finally:
        browser.close()


def _add_ask_leg(page):
    page.evaluate("() => document.querySelector('#chainBody td.cell-ask').click()")
    page.wait_for_selector("#orderEntryRow", state="visible")


def test_dock_is_collapsed_by_default_and_expands(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="chain")
    try:
        _add_ask_leg(page)
        assert page.evaluate("() => document.getElementById('strategyPanel').classList.contains('dock-collapsed')")
        assert not page.is_visible("#strategyContent")
        line = page.evaluate("() => document.getElementById('strategyDockLine').textContent")
        assert line.startswith("Buy ") and "Max loss" in line
        page.click("#strategyDockToggle")
        assert page.is_visible("#strategyContent") and page.is_visible("#strategySummary")
        assert page.get_attribute("#strategyDockToggle", "aria-expanded") == "true"
        page.click("#strategyDockToggle")
        assert not page.is_visible("#strategyContent")
        assert page.get_attribute("#strategyDockToggle", "aria-expanded") == "false"
    finally:
        browser.close()


def test_collapsed_dock_keeps_stop_loss_and_place_order_usable(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="chain")
    try:
        _add_ask_leg(page)
        page.check("#stopLossEnabled")
        assert page.is_visible("#stopLossStop") and page.is_enabled("#stopLossStop")
        page.fill("#stopLossStop", "2.50")
        page.fill("#stopLossLimit", "2.80")
        page.click("#strategyDockToggle")
        page.click("#strategyDockToggle")
        assert page.input_value("#stopLossStop") == "2.50" and page.input_value("#stopLossLimit") == "2.80"
        assert page.is_visible("#stratPlaceBtn") and page.is_visible("#stopLossStop")
        page.uncheck("#stopLossEnabled")                 # a plain order: the stop-loss rules are not under test here
        page.click("#stratPlaceBtn")
        page.wait_for_function("() => !document.getElementById('orderModalBackdrop').classList.contains('hidden')")
    finally:
        browser.close()                                  # the confirm modal was never confirmed: nothing was sent


def test_dock_stays_pinned_while_the_table_scrolls_and_clears(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="chain")
    try:
        _add_ask_leg(page)
        before = _rect(page, "#strategyPanel")
        page.evaluate("() => { document.getElementById('chainTableWrap').scrollTop = 600; }")
        page.evaluate(FRAMES)
        after = _rect(page, "#strategyPanel")
        assert abs(before["t"] - after["t"]) < 1 and after["b"] <= 1001
        page.evaluate("() => clearStrategy()")
        assert page.evaluate("() => document.getElementById('strategyDockLine').textContent") == ""
        assert not page.is_visible("#orderEntryRow")
    finally:
        browser.close()


def _flash_count_after_a_tick(p, url, reduced):
    browser = p.chromium.launch()
    ctx = browser.new_context(reduced_motion="reduce" if reduced else "no-preference", viewport={"width": 1600, "height": 1000})
    page = ctx.new_page()
    try:
        page.goto(f"{url}/#chain")
        page.wait_for_function("() => typeof state !== 'undefined' && state.wsConnected === true", timeout=30000)
        page.wait_for_timeout(1500)
        page.add_script_tag(path=BENCH_JS)
        page.evaluate("() => window.__benchSeed()")
        page.evaluate("""() => window.__benchInject({type: 'chain_tick', data: {ticks: [{strike: 6150, right: 'C', bid: 4.2}], timestamp_iso: 'x'}})""")
        flashes = page.evaluate("() => document.getAnimations().filter(a => a.effect && a.effect.pseudoElement === '::after').length")
        dur = page.evaluate("() => getComputedStyle(document.getElementById('chainTab')).animationDuration")
        return flashes, dur
    finally:
        browser.close()


def test_reduced_motion_disables_flash_and_cross_fade(browser_env):
    p, url = browser_env
    assert _flash_count_after_a_tick(p, url, reduced=False)[0] == 1               # control: the flash exists
    flashes, dur = _flash_count_after_a_tick(p, url, reduced=True)
    assert flashes == 0
    assert dur in ("0.001s", "1e-06s", "0s")


COUNT_REACTS = """() => { window.__reacts = {gexChart: 0, smileChart: 0}; const orig = Plotly.react;
                          Plotly.react = function (id, ...rest) { if (id in window.__reacts) window.__reacts[id]++; return orig.call(this, id, ...rest); }; }"""
REACTS = "() => ({...window.__reacts})"
GEX_X = "() => document.getElementById('gexChart').data[0].x"


def _resend_gex(page, mutate="g => g"):
    page.evaluate(f"""() => {{ const g = JSON.parse(JSON.stringify(state.gex)); ({mutate})(g);
                               handleMessage({{type: 'gex', data: g}}); }}""")
    for _ in range(3):
        page.evaluate(FRAMES)


def test_gex_shows_the_strikes_near_spot_and_all_on_demand(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        for _ in range(3):
            page.evaluate(FRAMES)
        xs = page.evaluate(GEX_X)
        assert 80 <= len(xs) <= 82 and min(xs) >= 5950 and max(xs) <= 6350       # spot 6150, 40 steps of 5
        page.click("#gexRangeToggle [data-range=all]")
        for _ in range(3):
            page.evaluate(FRAMES)
        assert len(page.evaluate(GEX_X)) == 120
        page.click("#gexRangeToggle [data-range=near]")
        for _ in range(3):
            page.evaluate(FRAMES)
        assert len(page.evaluate(GEX_X)) < 120
    finally:
        browser.close()


@pytest.mark.parametrize("spot", [0, 9000])
def test_the_window_falls_back_to_every_strike_without_a_usable_spot(browser_env, spot):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        page.evaluate(f"() => {{ state.currentSpot = 0; }}")
        _resend_gex(page, f"g => {{ g.spot_price = {spot}; }}")
        assert len(page.evaluate(GEX_X)) == 120
    finally:
        browser.close()


def test_an_unchanged_gex_is_not_redrawn_and_a_changed_one_is(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        page.evaluate(FRAMES)
        page.evaluate(COUNT_REACTS)
        _resend_gex(page)                                               # same data
        assert page.evaluate(REACTS)["gexChart"] == 0
        _resend_gex(page, "g => { g.gex_bars[60].call_gex += 5e6; }")  # one bar changed
        assert page.evaluate(REACTS)["gexChart"] == 1
    finally:
        browser.close()


def test_the_smile_redraw_is_throttled_with_a_trailing_draw(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        page.evaluate("() => { state.smileMinIntervalMs = 700; }")
        page.wait_for_timeout(900)                                      # the seed's own draw is outside the window now
        page.evaluate(COUNT_REACTS)
        _resend_gex(page, "g => { g.smile_data[60].call_iv += 1; }")
        assert page.evaluate(REACTS)["smileChart"] == 1                 # first change: drawn at once
        _resend_gex(page, "g => { g.smile_data[60].call_iv += 2; }")
        assert page.evaluate(REACTS)["smileChart"] == 1                 # second change: held inside the window
        page.wait_for_timeout(1200)
        assert page.evaluate(REACTS)["smileChart"] == 2                 # and drawn once, by the trailing timer
    finally:
        browser.close()


def test_a_reconnect_snapshot_keeps_the_zoom_of_the_same_session(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        for _ in range(3):
            page.evaluate(FRAMES)
        page.evaluate("() => priceChart.chart.timeScale().setVisibleLogicalRange({from: 60, to: 120})")
        snap = page.evaluate("() => ({session_date: priceChart.sessionDate, mode: 'live', overnight: [], bars: []})")
        page.evaluate("""s => { s.bars = Array.from(priceChart.bars.values()).map(c => ({
                                  time: new Date(c.time * 1000).toISOString().slice(0, 19) + '-05:00',
                                  time_short: '', open: c.open, high: c.high, low: c.low, close: c.close}));
                                window.__snap = s; }""", snap)
        page.evaluate("() => window.__benchInject({type: 'price_snapshot', data: window.__snap})")
        rng = page.evaluate("() => priceChart.chart.timeScale().getVisibleLogicalRange()")
        assert abs(rng["from"] - 60) < 2 and abs(rng["to"] - 120) < 2
        page.evaluate("() => { window.__snap.session_date = '2099-01-03'; }")
        page.evaluate("() => window.__benchInject({type: 'price_snapshot', data: window.__snap})")
        rng = page.evaluate("() => priceChart.chart.timeScale().getVisibleLogicalRange()")
        assert rng["to"] - rng["from"] > 150                            # a new session fits the content again
    finally:
        browser.close()


def test_help_tooltips_are_not_clipped_by_their_panel(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", viewport=(1440, 900))
    try:
        for i in range(4):
            page.evaluate("i => document.querySelectorAll('#dashboardTab .chart-title .help')[i].focus()", i)
            visible = page.evaluate("""i => { const t = document.querySelectorAll('#dashboardTab .chart-title .help .tooltip')[i];
                const r = t.getBoundingClientRect(); const el = document.elementFromPoint(r.right - 4, r.bottom - 4);
                return {inside: t.contains(el), right: r.right, bottom: r.bottom}; }""", i)
            assert visible["inside"], (i, visible)
            assert visible["right"] <= 1440 and visible["bottom"] <= 900
    finally:
        browser.close()
