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
