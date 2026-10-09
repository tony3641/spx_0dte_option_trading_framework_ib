"""Spreads in the positions tables (Dashboard panel and Account tab): one row per spread, legs on expand.
All data is invented (2099 expiry, ACME-style quantities)."""
import asyncio

import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.hermetic_server import hermetic_server            # noqa: E402
from tests.e2e.render_bench import _proactor_policy              # noqa: E402
from tests.e2e.test_theme_playwright import FRAMES, _open        # noqa: E402


@pytest.fixture(scope="module")
def browser_env():
    from playwright.sync_api import sync_playwright
    prev = _proactor_policy()
    with hermetic_server() as url, sync_playwright() as p:
        yield p, url
    if prev is not None:
        asyncio.set_event_loop_policy(prev)


def _opt(con_id, strike, right, qty, mark, unpnl):
    return {"contract": {"conId": con_id, "symbol": "SPX", "secType": "OPT", "expiry": "20990105",
                         "strike": strike, "right": right, "multiplier": "100"},
            "position": qty, "marketPrice": mark, "marketValue": qty * mark * 100,
            "averageCost": 300.0 if qty < 0 else 200.0, "unrealizedPNL": unpnl, "realizedPNL": 0.0}


POSITIONS = [_opt(101, 1000.0, "P", -1, 2.5, 50.0), _opt(102, 990.0, "P", 1, 1.7, -30.0),
             {"contract": {"conId": 9, "symbol": "ACME", "secType": "STK"}, "position": -100,
              "marketPrice": 10.0, "marketValue": -1000.0, "averageCost": 11.0, "unrealizedPNL": 100.0,
              "realizedPNL": 0.0}]
LEGS = [dict(POSITIONS[0], index=0), dict(POSITIONS[1], index=1)]
ROWS = [{"kind": "spread", "id": "spread:101-102", "label": "SPX 2099-01-05 1000/990 P", "position": -1,
         "marketPrice": 0.8, "averageCost": 100.0, "marketValue": -80.0, "unrealizedPNL": 20.0,
         "realizedPNL": 0.0, "legs": LEGS},
        dict(POSITIONS[2], kind="single", index=2)]
UPDATE = {"summary": {"NetLiquidation": 1000.0}, "positions": POSITIONS, "positionRows": ROWS,
          "orders": [], "executions": []}


def _texts(page, selector):
    return page.evaluate("s => [...document.querySelectorAll(s)].filter(r => r.offsetParent !== null)"
                         r".map(r => r.textContent.replace(/\s+/g, ' ').trim())", selector)


def _inject(page):
    page.evaluate("m => window.__benchInject({type: 'account_update', data: m})", UPDATE)
    page.evaluate(FRAMES)


def test_the_dashboard_shows_a_spread_as_one_row_that_expands(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        _inject(page)
        rows = _texts(page, "#dashPositionsBody tr")
        assert len(rows) == 2
        assert "1000/990 P" in rows[0] and "-1" in rows[0] and "0.80" in rows[0]
        assert rows[1].startswith("ACME")
        page.click("#dashPositionsBody .spread-toggle")
        page.evaluate(FRAMES)
        rows = _texts(page, "#dashPositionsBody tr")
        assert len(rows) == 4 and "1000P" in rows[1] and "990P" in rows[2]
        _inject(page)                                    # a later update keeps the spread open
        assert len(_texts(page, "#dashPositionsBody tr")) == 4
        page.click("#dashPositionsBody .spread-toggle")
        page.evaluate(FRAMES)
        assert len(_texts(page, "#dashPositionsBody tr")) == 2
    finally:
        browser.close()


def test_the_account_tab_groups_the_spread_and_can_close_it_or_its_legs(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="account")
    try:
        _inject(page)
        rows = _texts(page, "#positionsBody tr")
        assert len(rows) == 2 and "1000/990 P" in rows[0]
        assert page.evaluate("() => document.querySelector('#positionsBody tr.spread-row .btn-liquidate').disabled") is False
        page.click("#positionsBody .spread-toggle")
        page.evaluate(FRAMES)
        rows = _texts(page, "#positionsBody tr")
        assert len(rows) == 4
        legs = page.evaluate("() => [...document.querySelectorAll('#positionsBody tr.spread-leg .btn-liquidate')]"
                             ".map(b => b.disabled)")
        assert legs == [False, False]
    finally:
        browser.close()


def test_an_update_without_position_rows_lists_every_position(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark")
    try:
        page.evaluate("m => window.__benchInject({type: 'account_update', data: m})",
                      {"summary": {}, "positions": POSITIONS, "orders": [], "executions": []})
        page.evaluate(FRAMES)
        assert len(_texts(page, "#dashPositionsBody tr")) == 3
    finally:
        browser.close()


def test_a_combo_fill_reads_bag_in_the_executions_table(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="account")
    try:
        bag = {"execId": "X1", "time": "10:00:00 EDT", "symbol": "SPX", "secType": "BAG", "expiry": "",
               "strike": None, "right": "P", "side": "SLD", "shares": 1, "price": 1.0, "commission": None}
        page.evaluate("m => window.__benchInject({type: 'account_update', data: m})",
                      dict(UPDATE, executions=[bag]))
        page.evaluate(FRAMES)
        cells = page.evaluate("() => [...document.querySelectorAll('#executionsBody tr td')].map(td => td.textContent)")
        assert cells[4] == "BAG"
    finally:
        browser.close()


TWO = [_opt(101, 1000.0, "P", -2, 2.5, 100.0), _opt(102, 990.0, "P", 2, 1.7, -60.0)]
TWO_ROWS = [{"kind": "spread", "id": "spread:101-102", "label": "SPX 2099-01-05 1000/990 P", "position": -2,
             "marketPrice": 0.8, "averageCost": 100.0, "marketValue": -160.0, "unrealizedPNL": 40.0,
             "realizedPNL": 0.0, "legs": [dict(TWO[0], index=0), dict(TWO[1], index=1)]}]


def test_liquidating_a_spread_closes_every_spread_in_one_combo_order(browser_env):
    p, url = browser_env
    browser, _ctx, page = _open(p, url, "dark", tab="account")
    try:
        page.evaluate("m => window.__benchInject({type: 'account_update', data: m})",
                      {"summary": {}, "positions": TWO, "positionRows": TWO_ROWS, "orders": [], "executions": []})
        page.evaluate(FRAMES)
        page.evaluate("() => { window.__sent = []; ws.send = m => { window.__sent.push(m); }; }")
        page.click("#positionsBody tr.spread-row .btn-liquidate")
        page.click("#positionsBody tr.spread-row .btn-liquidate", force=True)     # a second click sends nothing
        sent = page.evaluate("() => window.__sent")
        assert len(sent) == 1 and sent[0].startswith("place_order:")
        import json
        order = json.loads(sent[0][len("place_order:"):])
        assert order["comboQuantity"] == 2 and order["comboAction"] == "BUY"
        assert order["dynamicFill"] is True and order["orderType"] == "LMT"
        assert [(l["action"], l["strike"], l["right"], l["qty"]) for l in order["legs"]] == [
            ("BUY", 1000.0, "P", 1), ("SELL", 990.0, "P", 1)]
        assert all(l.get("lmtPrice") is None for l in order["legs"])
    finally:
        browser.close()
