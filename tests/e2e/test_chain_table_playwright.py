"""Chain table in a real browser: row identity, partial ticks, scroll anchor, delegated click, hidden
tab, stale marks, in-place order-ticket prices.

Data is invented (2099 dates, strikes 5850+) and nothing depends on the wall clock or on market
hours. Every test page fails on teardown if the page raised an error or logged a console error.
"""
import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.hermetic_server import hermetic_server            # noqa: E402
from tests.e2e.render_bench import _proactor_policy, open_page    # noqa: E402

STRIKES = [5850 + 5 * i for i in range(120)]            # 5850 .. 6445
FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


def _row(s, bid=1.0):
    out = {"strike": s, "sigma_distance_abs": 1.0, "sigma_distance_signed": 1.0}
    for side in ("call", "put"):
        out.update({f"{side}_bid": bid, f"{side}_ask": bid + 0.1, f"{side}_bid_size": 1, f"{side}_ask_size": 1,
                    f"{side}_last": bid, f"{side}_delta": 0.5, f"{side}_gamma": 0.001, f"{side}_oi": 10,
                    f"{side}_volume": 5, f"{side}_iv": 15.0, f"{side}_age_s": 1.0})
    return out


def _full(spot=6150, strikes=STRIKES, max_age=180, cw=6200, pw=6100, gf=6140):
    return {"strikes": [_row(s) for s in strikes], "spot_price": spot, "annual_vol": 0.18,
            "expiration_raw": "20990105", "trading_class": "SPXW", "tte_years": 0.001, "sigma_move": 25,
            "call_wall": cw, "put_wall": pw, "gamma_flip": gf, "max_age_s": max_age,
            "timestamp_iso": "2099-01-05T10:00:00-05:00", "scope": "full"}


@pytest.fixture(scope="module")
def browser_env():
    import asyncio
    from playwright.sync_api import sync_playwright
    prev = _proactor_policy()
    with hermetic_server() as url, sync_playwright() as p:
        yield p, url
    if prev is not None:
        asyncio.set_event_loop_policy(prev)


@pytest.fixture()
def page(browser_env):
    p, url = browser_env
    browser, pg = open_page(p, url, "chain")
    problems = []

    def on_console(m):
        if m.type == "error" and not m.location.get("url", "").endswith("favicon.ico"):
            problems.append(f"console: {m.text}")

    pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    pg.on("console", on_console)
    pg.evaluate("d => window.__benchInject({type: 'chain_quotes', data: d})", _full())
    yield pg
    browser.close()
    assert problems == []


def _inject(page, mtype, data):
    page.evaluate("m => window.__benchInject(m)", {"type": mtype, "data": data})


def _text(page, cell_id):
    return page.evaluate("id => document.getElementById(id).textContent", cell_id)


def _has_class(page, cell_id, cls):
    return page.evaluate("([id, c]) => document.getElementById(id).classList.contains(c)", [cell_id, cls])


def _atm_row_distance_from_centre(page):
    return page.evaluate("""() => {
        const w = document.getElementById('chainTableWrap').getBoundingClientRect();
        const r = document.querySelector('tr.row-atm').getBoundingClientRect();
        return Math.abs((r.top + r.height / 2) - (w.top + w.height / 2));
    }""")


# --- cells are patched in place ----------------------------------------------------------------

def test_tick_patches_the_same_cell_element(page):
    page.evaluate("() => { window.__cell = document.getElementById('chain_6150_call_bid'); }")
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "bid": 2.35}], "timestamp_iso": "x"})
    assert page.evaluate("() => window.__cell.isConnected && window.__cell === document.getElementById('chain_6150_call_bid')")
    assert page.evaluate("() => window.__cell.textContent") == "2.35"


def test_partial_tick_leaves_the_other_fields_alone(page):
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "P", "ask": 4.4}], "timestamp_iso": "x"})
    assert _text(page, "chain_6150_put_ask") == "4.40"
    assert _text(page, "chain_6150_put_bid") == "1.00"
    assert _text(page, "chain_6150_call_ask") == "1.10"


def test_each_field_keeps_its_number_format(page):
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "bid": 2.5, "ask": 2.6, "bid_size": 12345,
                                            "ask_size": 7, "volume": 1234567, "delta": 0.4567, "gamma": 0.00123,
                                            "iv": 15.25}], "timestamp_iso": "x"})
    got = page.evaluate("""() => ['bid', 'ask', 'bid_size', 'ask_size', 'volume', 'delta', 'gamma', 'iv', 'oi']
        .map(f => document.getElementById('chain_6150_call_' + f).textContent)""")
    assert got == ["2.50", "2.60", "12,345", "7", "1,234,567", "0.457", "0.0012", "15.3%", "10"]


def test_a_field_turning_null_shows_the_empty_value(page):
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "bid": None, "delta": None}],
                                 "timestamp_iso": "x"})
    assert _text(page, "chain_6150_call_bid") == "-"
    assert _text(page, "chain_6150_call_delta") == "-"


def test_tick_for_an_unknown_strike_or_side_is_ignored(page):
    _inject(page, "chain_tick", {"ticks": [{"strike": 9999, "right": "C", "bid": 1.0},
                                           {"strike": 6150, "right": "X", "bid": 7.7},
                                           {"strike": 6155, "right": "C", "bid": 3.3}], "timestamp_iso": "x"})
    assert _text(page, "chain_6155_call_bid") == "3.30"
    assert page.evaluate("() => document.getElementById('chain_9999_call_bid')") is None
    assert "7.70" not in page.evaluate("() => document.getElementById('chainBody').textContent")


def test_a_changed_number_flashes_and_an_unchanged_one_does_not(page):
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "bid": 2.0}], "timestamp_iso": "x"})
    assert page.evaluate("() => document.getElementById('chain_6150_call_bid').getAnimations().length") == 1
    _inject(page, "chain_tick", {"ticks": [{"strike": 6155, "right": "C", "bid": 1.0}], "timestamp_iso": "x"})
    assert page.evaluate("() => document.getElementById('chain_6155_call_bid').getAnimations().length") == 0


def test_flash_uses_animations_not_classes(page):
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "bid": 2.0}], "timestamp_iso": "x"})
    assert page.evaluate("() => document.querySelectorAll('.flash-up, .flash-down').length") == 0


# --- full payloads keep row identity -----------------------------------------------------------

def test_full_payload_keeps_row_elements(page):
    page.evaluate("() => { window.__tr = document.querySelector('tr[data-strike=\"6150\"]'); }")
    _inject(page, "chain_quotes", _full())
    assert page.evaluate("() => window.__tr.isConnected")
    assert page.evaluate("() => window.__tr === document.querySelector('tr[data-strike=\"6150\"]')")


def test_hover_and_a_selected_leg_survive_ticks_and_full_payloads(page):
    page.click("#chain_6150_put_bid")
    page.hover("#chain_6150_call_ask")
    for _ in range(3):
        _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "ask": 1.7}], "timestamp_iso": "x"})
        _inject(page, "chain_quotes", _full())
    assert page.evaluate("() => document.getElementById('chain_6150_call_ask').matches(':hover')")
    assert _has_class(page, "chain_6150_put_bid", "cell-selected-bid")


def test_full_payload_overwrites_cells_from_the_new_model(page):
    full = _full()
    full["strikes"] = [_row(s, bid=7.0) if s == 6150 else _row(s) for s in STRIKES]
    _inject(page, "chain_quotes", full)
    assert _text(page, "chain_6150_call_bid") == "7.00"
    assert _text(page, "chain_6155_call_bid") == "1.00"


def _without_native_scroll_anchoring(page):
    """Chromium's own scroll anchoring would hide a missing anchor in our code (Safari has none)."""
    page.evaluate("() => { document.getElementById('chainTableWrap').style.overflowAnchor = 'none'; }")


def test_window_shift_keeps_row_identity_and_scroll_anchor(page):
    _without_native_scroll_anchoring(page)
    page.evaluate("() => { const w = document.getElementById('chainTableWrap'); w.scrollTop = 600; }")
    before = page.evaluate("() => getChainViewportCenterStrike()")
    page.evaluate("() => { window.__tr = document.querySelector(`tr[data-strike=\"${getChainViewportCenterStrike()}\"]`); }")
    _inject(page, "chain_quotes", _full(spot=6250, strikes=STRIKES + [6450 + 5 * i for i in range(1, 21)]))
    assert page.evaluate("() => getChainViewportCenterStrike()") == before
    assert page.evaluate("() => window.__tr.isConnected")


def test_rows_removed_above_the_viewport_do_not_move_what_is_on_screen(page):
    _without_native_scroll_anchoring(page)
    page.evaluate("() => { document.getElementById('chainTableWrap').scrollTop = 600; }")
    probe = """() => {
        const wrap = document.getElementById('chainTableWrap');
        const s = getChainViewportCenterStrike();
        const tr = document.querySelector(`tr[data-strike="${s}"]`);
        return {strike: s, top: tr.getBoundingClientRect().top - wrap.getBoundingClientRect().top,
                rows: document.querySelectorAll('#chainBody tr[data-strike]').length};
    }"""
    before = page.evaluate(probe)
    page.evaluate("() => { window.__tr = document.querySelector(`tr[data-strike=\"${getChainViewportCenterStrike()}\"]`); }")
    # Spot 6300: the 5-sigma window starts near 5883, so 5850..5880 go; 6450..6700 come in below.
    _inject(page, "chain_quotes", _full(spot=6300, strikes=STRIKES + [6450 + 5 * i for i in range(51)]))
    after = page.evaluate(probe)
    assert page.evaluate("() => !!document.querySelector('tr[data-strike=\"5850\"]')") is False
    assert page.evaluate("() => !!document.querySelector('tr[data-strike=\"6700\"]')") is True
    assert page.evaluate("() => window.__tr.isConnected")
    assert after["strike"] == before["strike"]
    assert abs(after["top"] - before["top"]) <= 1
    assert after["rows"] != before["rows"]


def test_rows_stay_in_ascending_strike_order_after_a_shift(page):
    _inject(page, "chain_quotes", _full(spot=6300, strikes=STRIKES + [6450 + 5 * i for i in range(51)]))
    strikes = page.evaluate("() => Array.from(document.querySelectorAll('#chainBody tr[data-strike]')).map(r => Number(r.dataset.strike))")
    assert strikes == sorted(strikes) and len(strikes) == len(set(strikes))


def test_empty_full_payload_is_ignored(page):
    _inject(page, "chain_quotes", _full(strikes=[]))
    assert page.evaluate("() => document.querySelectorAll('#chainBody tr[data-strike]').length") > 0


def test_row_classes_and_tags_follow_atm_walls_and_itm(page):
    probe = """() => {
        const tags = s => Array.from(document.querySelector(`tr[data-strike="${s}"]`).querySelectorAll('.strike-tag')).map(e => e.textContent);
        const has = (id, c) => document.getElementById(id).classList.contains(c);
        return {
            atm: Array.from(document.querySelectorAll('tr.row-atm')).map(r => r.dataset.strike),
            walls: [tags(6200), tags(6100), tags(6140), tags(6350)],
            wallRows: ['row-call-wall', 'row-put-wall', 'row-gamma-flip'].map(c => Array.from(document.querySelectorAll('tr.' + c)).map(r => r.dataset.strike)),
            callItmBelowSpot: has('chain_6100_call_bid', 'itm-call'), callItmAboveSpot: has('chain_6200_call_bid', 'itm-call'),
            putItmAboveSpot: has('chain_6200_put_bid', 'itm-put'), putItmBelowSpot: has('chain_6100_put_bid', 'itm-put'),
            strikeCell: document.querySelector('tr[data-strike="6150"] td.strike-col').className,
        };
    }"""
    first = page.evaluate(probe)
    assert first["atm"] == ["6150"]
    assert first["walls"] == [["CW"], ["PW"], ["GF"], []]
    assert first["wallRows"] == [["6200"], ["6100"], ["6140"]]
    assert first["callItmBelowSpot"] and not first["callItmAboveSpot"]
    assert first["putItmAboveSpot"] and not first["putItmBelowSpot"]
    assert first["strikeCell"] == "strike-col strike-sigma-1"
    _inject(page, "chain_quotes", _full(spot=6300, cw=6350, pw=6100, gf=6140))
    second = page.evaluate(probe)
    assert second["atm"] == ["6300"]                                  # the old ATM row lost its class
    assert second["walls"] == [[], ["PW"], ["GF"], ["CW"]]            # the CW tag moved
    assert second["wallRows"] == [["6350"], ["6100"], ["6140"]]


def test_the_first_build_centres_on_the_atm_row(page):
    assert _atm_row_distance_from_centre(page) < 30


def test_visible_range_text_keeps_the_five_sigma_rule(page):
    assert page.evaluate("() => document.getElementById('chainRangeInfo').textContent") == \
        "Visible range: 5741 to 6559 (5sigma +/- 60)"


# --- clicks ------------------------------------------------------------------------------------

def test_delegated_click_adds_a_leg_and_highlights(page):
    page.click("#chain_6150_call_ask")
    assert page.evaluate("() => state.strategyLegs.map(l => [l.strike, l.right, l.action])") == [[6150, "C", "BUY"]]
    assert page.evaluate("() => document.getElementById('chain_6150_call_ask').classList.contains('cell-selected-ask')")
    _inject(page, "chain_quotes", _full())
    assert page.evaluate("() => document.getElementById('chain_6150_call_ask').classList.contains('cell-selected-ask')")


def test_each_priced_cell_clicks_to_its_own_right_and_action(page):
    for cell in ("chain_6100_call_ask", "chain_6105_call_bid", "chain_6110_put_bid", "chain_6115_put_ask"):
        page.click(f"#{cell}")
    assert page.evaluate("() => state.strategyLegs.map(l => [l.strike, l.right, l.action])") ==         [[6100, "C", "BUY"], [6105, "C", "SELL"], [6110, "P", "SELL"], [6115, "P", "BUY"]]


def test_clicking_a_non_priced_cell_does_nothing(page):
    page.click("#chain_6150_call_oi")
    page.click("tr[data-strike='6150'] td.strike-col")
    assert page.evaluate("() => state.strategyLegs.length") == 0


def test_chain_body_has_no_inline_handlers(page):
    assert page.evaluate("() => document.querySelectorAll('#chainBody [onclick]').length") == 0


# --- hidden tab --------------------------------------------------------------------------------

def test_hidden_tab_does_not_flush_and_shows_current_state(page):
    page.evaluate("() => switchTab('dashboard')")
    for bid in (3.0, 3.1, 3.2):
        _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "P", "bid": bid}], "timestamp_iso": "x"})
    assert page.evaluate("() => document.getElementById('chain_6150_put_bid').textContent") != "3.20"
    assert page.evaluate("() => chainView.dirty.size") <= 1
    page.evaluate("() => switchTab('chain')")
    page.wait_for_timeout(100)
    assert page.evaluate("() => document.getElementById('chain_6150_put_bid').textContent") == "3.20"


def test_full_payload_while_hidden_is_painted_once_on_show_and_recentres_on_the_new_atm(page):
    page.evaluate("() => switchTab('dashboard')")
    _inject(page, "chain_quotes", _full(spot=6300, cw=6350))
    assert page.evaluate("() => Array.from(document.querySelectorAll('tr.row-atm')).map(r => r.dataset.strike)") == ["6150"]
    page.evaluate("() => switchTab('chain')")
    page.wait_for_timeout(150)
    assert page.evaluate("() => Array.from(document.querySelectorAll('tr.row-atm')).map(r => r.dataset.strike)") == ["6300"]
    assert _atm_row_distance_from_centre(page) < 30


# --- stale marks -------------------------------------------------------------------------------

def test_stale_marks_follow_tick_receipt(page):
    _inject(page, "chain_quotes", _full(max_age=1))
    page.evaluate("() => { state.chainSideSeenMs['6150|C'] = Date.now() - 5000; refreshStaleMarks(); }")
    assert page.evaluate("() => document.getElementById('chain_6150_call_bid').classList.contains('quote-stale')")
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "bid": 1.5}], "timestamp_iso": "x"})
    page.evaluate("() => refreshStaleMarks()")
    assert not page.evaluate("() => document.getElementById('chain_6150_call_bid').classList.contains('quote-stale')")


def test_stale_marks_cover_one_side_only_and_toggle_only_on_change(page):
    _inject(page, "chain_quotes", _full(max_age=2))
    page.evaluate("() => { state.chainSideSeenMs['6150|C'] = Date.now() - 5000; refreshStaleMarks(); }")
    assert _has_class(page, "chain_6150_call_iv", "quote-stale")
    assert not _has_class(page, "chain_6150_put_iv", "quote-stale")
    n = page.evaluate("""() => {
        const td = document.getElementById('chain_6150_call_iv');
        let n = 0;
        new MutationObserver(m => { n += m.length; }).observe(td, {attributes: true});
        refreshStaleMarks(); refreshStaleMarks();
        return new Promise(r => setTimeout(() => r(n), 50));
    }""")
    assert n == 0                                     # an unchanged state touches no class


def test_every_side_goes_stale_when_nothing_ticks(page):
    _inject(page, "chain_quotes", _full(max_age=2))
    assert page.evaluate("() => document.querySelectorAll('#chainBody td.quote-stale').length") == 0
    page.evaluate("() => { for (const k of Object.keys(state.chainSideSeenMs)) state.chainSideSeenMs[k] = Date.now() - 5000; refreshStaleMarks(); }")
    counts = page.evaluate("""() => [document.querySelectorAll('#chainBody td.quote-stale').length,
                                    document.querySelectorAll('#chainBody tr[data-strike]').length * 18]""")
    assert counts[0] == counts[1] > 0


def test_full_payload_age_reseeds_the_stale_state(page):
    _inject(page, "chain_quotes", _full(max_age=1))
    page.evaluate("() => { state.chainSideSeenMs['6150|C'] = Date.now() - 5000; refreshStaleMarks(); }")
    full = _full(max_age=100)
    _inject(page, "chain_quotes", full)                # age_s is 1.0 and the new limit is 100 s
    assert not _has_class(page, "chain_6150_call_bid", "quote-stale")


# --- the order ticket follows the quotes in place ----------------------------------------------

def test_leg_prices_update_in_place_and_keep_the_quantity_input_focused(page):
    page.click("#chain_6150_call_ask")
    assert page.evaluate("() => document.querySelector('.leg-delta').className") == "leg-delta summary-credit"
    page.evaluate("() => { window.__qty = document.querySelector('.leg-qty'); window.__qty.focus(); }")
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "bid": 2.0, "ask": 2.2, "delta": -0.1}],
                                 "timestamp_iso": "x"})
    row = page.evaluate("""() => {
        const tr = document.querySelector('tr[data-leg-id]');
        const t = c => tr.querySelector('.' + c).textContent;
        return {bid: t('leg-bid'), ask: t('leg-ask'), mid: t('leg-mid'), delta: t('leg-delta'),
                deltaClass: tr.querySelector('.leg-delta').className,
                sameInput: document.querySelector('.leg-qty') === window.__qty,
                focused: document.activeElement === window.__qty};
    }""")
    assert row == {"bid": "2.00", "ask": "2.20", "mid": "2.10", "delta": "-10.0", "deltaClass": "leg-delta summary-debit",
                   "sameInput": True, "focused": True}
    assert page.evaluate("() => document.getElementById('comboNet').textContent") != "-"


def test_the_order_ticket_is_recomputed_only_when_a_selected_legs_quote_changed(page):
    page.click("#chain_6150_call_ask")
    page.evaluate("""() => {
        window.__n = 0;
        const real = window.updateStrategyPrices;
        window.updateStrategyPrices = () => { window.__n += 1; return real(); };
    }""")
    n = lambda: page.evaluate("() => window.__n")             # noqa: E731
    _inject(page, "chain_tick", {"ticks": [{"strike": 6155, "right": "C", "bid": 2.0}], "timestamp_iso": "x"})
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "P", "bid": 2.0}], "timestamp_iso": "x"})
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "volume": 99}], "timestamp_iso": "x"})
    assert n() == 0
    _inject(page, "chain_tick", {"ticks": [{"strike": 6150, "right": "C", "ask": 1.7}], "timestamp_iso": "x"})
    assert n() == 1


def test_a_full_payload_refreshes_the_order_ticket_prices(page):
    page.click("#chain_6150_call_bid")
    full = _full()
    full["strikes"] = [_row(s, bid=3.0) if s == 6150 else _row(s) for s in STRIKES]
    _inject(page, "chain_quotes", full)
    assert page.evaluate("() => document.querySelector('tr[data-leg-id] .leg-bid').textContent") == "3.00"


# --- viewport-centre reporting and perf spans still work ----------------------------------------

def test_a_full_payload_reports_the_viewport_centre_to_the_server(page):
    page.evaluate("p => { window.__payload = p; }", _full())
    sent = page.evaluate("""async () => {
        const out = [];
        const real = ws.send.bind(ws);
        ws.send = m => { out.push(m); return real(m); };
        document.getElementById('chainTableWrap').scrollTop = 600;
        await new Promise(r => setTimeout(r, 400));          // let the throttled scroll report go out
        out.length = 0;
        await window.__benchInject({type: 'chain_quotes', data: window.__payload});
        ws.send = real;
        return {out, centre: getChainViewportCenterStrike()};
    }""")
    assert sent["out"] == [f"viewport_center:{sent['centre']:.1f}"]


def test_chain_messages_record_a_paint_span_when_the_tab_is_shown(page):
    page.evaluate("p => { window.__payload = p; }", _full())
    spans = page.evaluate("""async () => {
        for (const k of Object.keys(_perfSpans)) delete _perfSpans[k];
        const tick = {type: 'chain_tick', ts: Date.now() - 3, data: {ticks: [{strike: 6150, right: 'C', bid: 2.5}], timestamp_iso: 'x'}};
        const full = {type: 'chain_quotes', ts: Date.now() - 3, data: window.__payload};
        for (const m of [tick, full]) { perfOnMessage(m); handleMessage(m); }
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        return Object.keys(_perfSpans).filter(n => n.startsWith('chain_')).sort();
    }""")
    assert spans == ["chain_quotes.paint", "chain_quotes.recv", "chain_tick.paint", "chain_tick.recv"]
