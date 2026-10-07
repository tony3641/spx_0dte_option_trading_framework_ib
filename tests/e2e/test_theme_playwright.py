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
