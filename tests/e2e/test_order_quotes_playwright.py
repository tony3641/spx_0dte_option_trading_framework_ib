"""Order dock quotes: the order row shows the order's bid / mid / ask, the limit defaults to the mid on the
SPX tick, and clicking a quote puts it in the limit. Quotes are set on the chain data directly (invented)."""
import asyncio

import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.hermetic_server import hermetic_server            # noqa: E402
from tests.e2e.render_bench import _proactor_policy              # noqa: E402
from tests.e2e.test_theme_playwright import _open                # noqa: E402


@pytest.fixture(scope="module")
def browser_env():
    from playwright.sync_api import sync_playwright
    prev = _proactor_policy()
    with hermetic_server() as url, sync_playwright() as p:
        yield p, url
    if prev is not None:
        asyncio.set_event_loop_policy(prev)


SET_QUOTES = """q => {
    const strikes = Object.keys(state.chainData).map(Number).sort((a, b) => a - b);
    const hi = strikes[Math.floor(strikes.length / 2)], lo = strikes[Math.floor(strikes.length / 2) - 1];
    Object.assign(state.chainData[hi], {put_bid: q.hiBid, put_ask: q.hiAsk});
    Object.assign(state.chainData[lo], {put_bid: q.loBid, put_ask: q.loAsk});
    return [hi, lo];
}"""


def _quotes(page):
    return page.evaluate("""() => ['Bid', 'Mid', 'Ask'].map(s => [
        document.getElementById('orderQuote' + s + 'Val').textContent,
        document.getElementById('orderQuote' + s).offsetParent !== null])""")


def test_a_single_leg_shows_its_quotes_and_defaults_to_the_mid_on_tick(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="chain")
    try:
        hi, _lo = page.evaluate(SET_QUOTES, {"hiBid": 1.10, "hiAsk": 1.25, "loBid": 0.5, "loAsk": 0.6})
        page.evaluate("s => addLeg(s, 'P', 'BUY')", hi)
        page.wait_for_selector("#orderEntryRow", state="visible")
        assert _quotes(page) == [["1.10", True], ["1.20", True], ["1.25", True]]   # collapsed dock; mid on tick
        assert page.input_value("#stratLmtPrice") == "1.20"                          # mid 1.175 on the 0.05 tick
        page.click("#orderQuoteAsk")
        assert page.input_value("#stratLmtPrice") == "1.25"
        page.evaluate("() => { Object.assign(state.chainData[Object.keys(state.chainData).map(Number)"
                      ".sort((a, b) => a - b)[Math.floor(Object.keys(state.chainData).length / 2)]], "
                      "{put_bid: 1.30, put_ask: 1.40}); updateStrategyPrices(); }")
        assert page.input_value("#stratLmtPrice") == "1.25"                          # a picked price stays
        page.click("#orderQuoteBid")
        assert page.input_value("#stratLmtPrice") == "1.30"
        page.click("#orderQuoteMid")
        assert page.input_value("#stratLmtPrice") == "1.35"
    finally:
        browser.close()


def test_a_credit_spread_quotes_signed_nets_and_rounds_a_click_to_the_tick(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="chain")
    try:
        hi, lo = page.evaluate(SET_QUOTES, {"hiBid": 2.40, "hiAsk": 2.60, "loBid": 1.65, "loAsk": 1.80})
        page.evaluate("([h, l]) => { addLeg(h, 'P', 'SELL'); addLeg(l, 'P', 'BUY'); }", [hi, lo])
        page.wait_for_selector("#orderEntryRow", state="visible")
        assert [v for v, _ in _quotes(page)] == ["-0.95", "-0.80", "-0.60"]      # mid -0.775 on the tick
        assert page.input_value("#stratLmtPrice") == "-0.80"
        page.click("#orderQuoteBid")
        assert page.input_value("#stratLmtPrice") == "-0.95"
    finally:
        browser.close()


def test_changing_the_legs_returns_the_limit_to_the_mid(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="chain")
    try:
        hi, lo = page.evaluate(SET_QUOTES, {"hiBid": 2.40, "hiAsk": 2.60, "loBid": 1.65, "loAsk": 1.80})
        page.evaluate("h => addLeg(h, 'P', 'SELL')", hi)
        page.wait_for_selector("#orderEntryRow", state="visible")
        page.click("#orderQuoteAsk")
        assert page.input_value("#stratLmtPrice") == "2.60"
        page.evaluate("l => addLeg(l, 'P', 'BUY')", lo)
        assert page.input_value("#stratLmtPrice") == "-0.80"
    finally:
        browser.close()
