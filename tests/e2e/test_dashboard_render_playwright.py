"""Dashboard charts render only when visible and at a real size; cancel feedback is routed by order id;
client paint spans are recorded only for work that paints.

Invented data only (2099 dates, strikes 6100-6195); nothing depends on the wall clock or market hours.
"""
import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.hermetic_server import hermetic_server            # noqa: E402
from tests.e2e.render_bench import _proactor_policy, open_page    # noqa: E402

# Counts the Plotly calls the page makes (installed after load, so the page's own boot is not counted).
COUNT_PLOTLY = """() => {
    window.__calls = {react: 0, relayout: 0, resize: 0};
    for (const k of ['react', 'relayout']) {
        const f = Plotly[k];
        Plotly[k] = function () { window.__calls[k]++; return f.apply(this, arguments); };
    }
    const rs = Plotly.Plots.resize;
    Plotly.Plots.resize = function () { window.__calls.resize++; return rs.apply(this, arguments); };
}"""
CALLS = "() => window.__calls"
FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


def _gex(spot=6150, n=20, call_wall=6150, put_wall=6110):
    strikes = [6100 + 5 * i for i in range(n)]
    return {"gex_bars": [{"strike": s, "call_gex": 1e6, "put_gex": -1e6, "net_gex": 1e4, "call_oi": 1, "put_oi": 1,
                          "call_vol": 1, "put_vol": 1} for s in strikes],
            "call_wall": call_wall, "put_wall": put_wall, "gamma_flip": 6130, "max_pain": 6140, "spot_price": spot,
            "expiration": "20990105", "timestamp": "", "total_call_gex": 1e7, "total_put_gex": -1e7,
            "total_net_gex": 1e5, "total_call_oi": 10, "total_put_oi": 12, "total_call_vol": 1, "total_put_vol": 1,
            "smile_data": [{"strike": s, "call_iv": 15.0, "put_iv": 16.0, "call_efficiency": 0.1, "put_efficiency": 0.1,
                            "call_delta": 0.5, "put_delta": -0.5, "call_charm": 0.001, "put_charm": -0.001} for s in strikes],
            "es_derived": False}


def _inject(pg, msg_type, data):
    return pg.evaluate("m => window.__benchInject(m)", {"type": msg_type, "data": data})


def _gex_x(pg):
    return pg.evaluate("() => (document.getElementById('gexChart')._fullData || [{x: []}])[0].x.length")


def _gex_shape_xs(pg, chart="gexChart"):
    return pg.evaluate("id => document.getElementById(id).layout.shapes.map(s => s.x0)", chart)


def _toast(pg):
    return pg.evaluate("() => document.getElementById('orderToast').textContent")


@pytest.fixture(scope="module")
def browser_env():
    import asyncio
    from playwright.sync_api import sync_playwright
    prev = _proactor_policy()
    with hermetic_server() as url, sync_playwright() as p:
        yield p, url
    if prev is not None:
        asyncio.set_event_loop_policy(prev)


def _page_fixture(browser_env, tab):
    p, url = browser_env
    problems = []
    browser, pg = open_page(p, url, tab, problems=problems)
    yield pg
    browser.close()
    assert problems == []              # no page error or console error in the whole test


@pytest.fixture()
def page_on_chain(browser_env):
    yield from _page_fixture(browser_env, "chain")


@pytest.fixture()
def page_on_dashboard(browser_env):
    yield from _page_fixture(browser_env, "dashboard")


# --- hidden tabs and sizing ---------------------------------------------------------------------

def test_gex_received_on_chain_tab_renders_sized_on_show(page_on_chain):
    pg = page_on_chain
    _inject(pg, "gex", _gex())
    assert _gex_x(pg) == 0
    pg.evaluate("() => switchTab('dashboard')")
    pg.wait_for_function("() => document.getElementById('gexChart')._fullData[0].x.length === 20", timeout=5000)
    assert pg.evaluate("() => document.getElementById('gexChart')._fullLayout.width") > 200
    assert pg.evaluate("() => document.getElementById('smileChart')._fullLayout.width") > 200


def test_hidden_dashboard_never_redraws_and_the_show_draws_each_chart_once(page_on_chain):
    pg = page_on_chain
    pg.evaluate(COUNT_PLOTLY)
    _inject(pg, "gex", _gex(6150))
    _inject(pg, "gex", _gex(6155))
    _inject(pg, "status", {"connected": False, "spot_price": 6160.5})
    assert pg.evaluate(CALLS) == {"react": 0, "relayout": 0, "resize": 0}
    pg.evaluate("() => switchTab('dashboard')")
    pg.wait_for_function("() => window.__calls.react >= 2", timeout=5000)
    pg.evaluate(FRAMES)
    calls = pg.evaluate(CALLS)
    assert calls["react"] == 2                       # one GEX and one smile draw for all three messages
    assert 6160.5 in _gex_shape_xs(pg)               # drawn with the latest spot, not the first one


def test_two_gex_messages_in_one_frame_draw_once(page_on_dashboard):
    pg = page_on_dashboard
    pg.evaluate(COUNT_PLOTLY)
    pg.evaluate("g => window.__benchInject([{type: 'gex', data: g}, {type: 'gex', data: g}])", _gex())
    assert pg.evaluate(CALLS)["react"] == 2


def test_resize_follows_the_container_and_a_hidden_chart_is_left_alone(page_on_dashboard):
    pg = page_on_dashboard
    pg.set_viewport_size({"width": 1000, "height": 800})
    pg.wait_for_function("""() => {
        const g = document.getElementById('gexChart');
        return Math.abs(g._fullLayout.width - g.clientWidth) <= 1 && g.clientWidth < 1000;
    }""", timeout=5000)
    pg.evaluate("() => switchTab('chain')")
    pg.evaluate(COUNT_PLOTLY)
    pg.set_viewport_size({"width": 1300, "height": 900})
    pg.wait_for_timeout(300)
    assert pg.evaluate(CALLS)["resize"] == 0        # a hidden chart is never resized (it would draw squashed)
    pg.evaluate("() => switchTab('dashboard')")
    pg.wait_for_function("""() => {
        const g = document.getElementById('gexChart');
        return g.clientWidth > 1000 && Math.abs(g._fullLayout.width - g.clientWidth) <= 1;
    }""", timeout=5000)


def test_window_resize_still_switches_the_mobile_margins(page_on_dashboard):
    pg = page_on_dashboard
    left = "[gexChart.layout.margin.l, smileChart.layout.margin.l]"
    assert pg.evaluate(f"() => {left}") == [60, 60]
    pg.set_viewport_size({"width": 500, "height": 800})
    pg.wait_for_function(f"() => {left}[0] === 40", timeout=5000)
    assert pg.evaluate(f"() => {left}") == [40, 40]
    pg.set_viewport_size({"width": 1000, "height": 800})
    pg.wait_for_function(f"() => {left}[0] === 60", timeout=5000)
    assert pg.evaluate(f"() => {left}") == [60, 60]


# --- the spot line moves without a redraw --------------------------------------------------------

def test_spot_only_change_relayouts_shapes(page_on_dashboard):
    pg = page_on_dashboard
    _inject(pg, "gex", _gex())
    pg.evaluate("() => Plotly.relayout('gexChart', {'xaxis.range': [6120, 6160]})")
    pg.evaluate(FRAMES)                                                         # the zoom sync to the smile chart settles
    pg.evaluate(COUNT_PLOTLY)
    _inject(pg, "status", {"connected": False, "spot_price": 6157.25})
    assert 6157.25 in _gex_shape_xs(pg) and pg.evaluate(CALLS)["react"] == 0
    smile_xs = _gex_shape_xs(pg, "smileChart")
    assert smile_xs.count(6157.25) == 2                                      # the spot, on both subplots
    assert pg.evaluate("() => document.getElementById('gexChart').layout.xaxis.range") == [6120, 6160]
    texts = pg.evaluate("() => document.getElementById('gexChart').layout.annotations.map(a => a.text)")
    assert "SPX: 6157.25" in texts and any(t.startswith("Net GEX") for t in texts)
    assert "CW" in texts                                                       # the key levels stay


def test_spot_change_in_the_frame_of_a_gex_message_is_drawn_by_the_gex_render(page_on_dashboard):
    pg = page_on_dashboard
    _inject(pg, "gex", _gex())
    pg.evaluate(COUNT_PLOTLY)
    pg.evaluate("""g => window.__benchInject([{type: 'gex', data: g},
                                              {type: 'status', data: {connected: false, spot_price: 6162}}])""",
                _gex(6155))
    assert pg.evaluate(CALLS)["relayout"] == 0
    assert 6162 in _gex_shape_xs(pg)


def test_spot_change_while_hidden_lands_on_show(page_on_dashboard):
    pg = page_on_dashboard
    _inject(pg, "gex", _gex())
    pg.evaluate("() => switchTab('chain')")
    _inject(pg, "status", {"connected": False, "spot_price": 6171})
    assert 6171 not in _gex_shape_xs(pg)
    pg.evaluate("() => switchTab('dashboard')")
    pg.wait_for_function("() => gexChart.layout.shapes.some(s => s.x0 === 6171)", timeout=5000)


def test_gex_mode_switch_draws_the_monthly_data_and_the_spot_line_follows_it(page_on_dashboard):
    pg = page_on_dashboard
    _inject(pg, "gex", _gex())
    _inject(pg, "monthly_gex", _gex(6150, n=10, call_wall=6190, put_wall=6105))
    assert _gex_x(pg) == 20                                                    # still the 0DTE data
    try:
        pg.evaluate("() => setGexMode('monthly')")
        pg.wait_for_function("() => gexChart._fullData[0].x.length === 10", timeout=5000)
        assert pg.evaluate("() => document.getElementById('smileChart')._fullData[0].x.length") == 10
        _inject(pg, "status", {"connected": False, "spot_price": 6140.5})
        xs = _gex_shape_xs(pg)
        assert 6140.5 in xs and 6190 in xs                                     # monthly walls, new spot
    finally:
        pg.evaluate("() => setGexMode('0dte')")    # the server keeps the mode: do not leak it into later pages
        pg.evaluate(FRAMES)


# --- cancel feedback ---------------------------------------------------------------------------

def test_cancel_reply_is_not_taken_as_place_reply(page_on_chain):
    pg = page_on_chain
    pg.evaluate("() => { window.__placeReply = null; state._pendingOrderCallback = {id: 1, fn: r => { window.__placeReply = r; }}; state.pendingCancels.set(77, Date.now()); }")
    _inject(pg, "order_status", {"status": "PendingCancel", "orderId": 77, "message": "Cancel requested; IB has not confirmed yet"})
    assert pg.evaluate("() => window.__placeReply") is None
    assert "Cancel pending" in _toast(pg)
    assert pg.evaluate("() => state.pendingCancels.has(77)") is True           # still waiting for IB
    _inject(pg, "account_update", {"orders": []})
    assert "cancelled" in _toast(pg).lower()
    assert pg.evaluate("() => state.pendingCancels.has(77)") is False


def test_a_tagged_cancel_reply_is_never_a_place_reply_even_after_the_account_update_resolved_it(page_on_chain):
    pg = page_on_chain
    pg.evaluate("() => { window.__placeReply = null; state._pendingOrderCallback = {id: 1, fn: r => { window.__placeReply = r; }}; }")
    _inject(pg, "order_status", {"status": "Cancelled", "orderId": 78, "action": "cancel", "message": "Cancel request sent for order 78"})
    assert pg.evaluate("() => window.__placeReply") is None
    assert "78" in _toast(pg) and "cancelled" in _toast(pg).lower()
    assert pg.evaluate("() => state._pendingOrderCallback !== null")           # the place is still waiting for its own reply
    _inject(pg, "order_status", {"status": "Error", "action": "cancel", "orderId": 79, "message": "Order 79 not found in open trades"})
    assert pg.evaluate("() => window.__placeReply") is None
    assert "Cancel failed for order 79" in _toast(pg)
    _inject(pg, "order_status", {"status": "Submitted", "orderId": 5, "message": "Order submitted"})
    assert pg.evaluate("() => window.__placeReply.orderId") == 5               # a real place reply still lands


def test_a_background_cancelled_status_resolves_a_pending_cancel(page_on_chain):
    pg = page_on_chain
    pg.evaluate("() => state.pendingCancels.set(80, Date.now())")
    _inject(pg, "order_status", {"status": "PendingCancel", "orderId": 80, "action": "cancel", "message": "x"})
    assert pg.evaluate("() => state.pendingCancels.has(80)") is True
    _inject(pg, "order_status", {"status": "Cancelled", "orderId": 80, "message": "Order Cancelled"})
    assert pg.evaluate("() => state.pendingCancels.has(80)") is False
    assert "Order 80 cancelled" in _toast(pg)


def test_a_late_pending_cancel_reply_after_the_account_update_resolved_it_is_silent(page_on_chain):
    pg = page_on_chain
    pg.evaluate("() => state.pendingCancels.set(85, Date.now())")
    _inject(pg, "account_update", {"orders": []})
    assert "no longer open" in _toast(pg)
    _inject(pg, "order_status", {"status": "PendingCancel", "orderId": 85, "action": "cancel", "message": "x"})
    assert "no longer open" in _toast(pg)                                     # not replaced by "Cancel pending"


def test_a_cancel_error_reply_clears_the_pending_entry_and_says_why(page_on_chain):
    pg = page_on_chain
    pg.evaluate("() => state.pendingCancels.set(81, Date.now())")
    _inject(pg, "order_status", {"status": "Error", "orderId": 81, "action": "cancel", "message": "Not connected to IB"})
    assert "Cancel failed for order 81: Not connected to IB" in _toast(pg)
    assert pg.evaluate("() => state.pendingCancels.has(81)") is False


def test_a_cancel_that_lost_the_race_to_a_fill_says_so(page_on_chain):
    pg = page_on_chain
    pg.evaluate("() => state.pendingCancels.set(82, Date.now())")
    _inject(pg, "account_update", {"orders": [{"orderId": 82, "status": "Submitted"}]})
    assert pg.evaluate("() => state.pendingCancels.has(82)") is True           # still open: keep waiting
    _inject(pg, "account_update", {"orders": [{"orderId": 82, "status": "Filled"}]})
    assert pg.evaluate("() => state.pendingCancels.has(82)") is False
    assert "Filled" in _toast(pg) and "cancelled" not in _toast(pg).lower()


def test_a_cancel_that_never_confirms_is_dropped_with_a_message(page_on_chain):
    pg = page_on_chain
    pg.evaluate("() => state.pendingCancels.set(83, Date.now() - 61000)")
    _inject(pg, "account_update", {"orders": [{"orderId": 83, "status": "PreSubmitted"}]})
    assert pg.evaluate("() => state.pendingCancels.has(83)") is False
    assert "83" in _toast(pg) and "not confirmed" in _toast(pg)


def test_double_cancel_click_sends_one_request_and_one_pending_entry(page_on_chain):
    pg = page_on_chain
    out = pg.evaluate("""() => {
        window.__sent = [];
        ws.send = m => { window.__sent.push(m); };
        cancelOrder(84);
        const first = document.getElementById('orderToast').textContent;
        cancelOrder(84);
        const second = document.getElementById('orderToast').textContent;
        state.pendingCancels.set(84, Date.now() - 61000);      // an old entry never confirmed: a new click re-sends
        cancelOrder(84);
        return {sent: window.__sent, first, second, pending: state.pendingCancels.size};
    }""")
    assert out["sent"] == ["cancel_order:84", "cancel_order:84"]
    assert "sent" in out["first"] and "already" in out["second"] and out["pending"] == 1


def test_cancel_round_trip_through_the_server_reports_the_failure_by_order_id(page_on_chain):
    """The hermetic server has no IB, so its cancel reply is an error: it must carry the order id and the
    cancel tag, or the browser could not tell it from the reply to a place_order in flight."""
    pg = page_on_chain
    pg.evaluate("() => { window.__placeReply = null; state._pendingOrderCallback = {id: 1, fn: r => { window.__placeReply = r; }}; }")
    pg.evaluate("() => cancelOrder(91)")
    pg.wait_for_function("() => document.getElementById('orderToast').textContent.includes('Cancel failed')", timeout=5000)
    assert "order 91" in _toast(pg)
    assert pg.evaluate("() => window.__placeReply") is None
    assert pg.evaluate("() => state.pendingCancels.has(91)") is False


# --- client perf: a paint span only for work that paints ---------------------------------------

RECORD_PERF = """() => {
    window.__spans = [];
    const orig = _perfPush;
    window._perfPush = (name, ms) => { window.__spans.push(name); orig(name, ms); };
}"""
SEND = """async (msgs) => {
    window.__spans.length = 0;
    for (const m of msgs) ws.onmessage({data: JSON.stringify(m)});
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    return window.__spans.filter(n => n.endsWith('.paint'));
}"""


def _send(pg, *msgs):
    return pg.evaluate(SEND, list(msgs))


def test_paint_spans_follow_the_tab_that_paints(page_on_dashboard):
    pg = page_on_dashboard
    pg.evaluate(RECORD_PERF)
    gex = {"type": "gex", "data": _gex()}
    same_spot = {"type": "status", "data": {"connected": False, "spot_price": 6150}}
    moved = {"type": "status", "data": {"connected": False, "spot_price": 6152}}
    acct = {"type": "account_update", "data": {"orders": []}}
    assert _send(pg, gex) == ["gex.paint"]
    assert _send(pg, same_spot) == []                       # nothing is redrawn: no span waiting for a stray flush
    assert pg.evaluate("() => _perfPending.length") == 0
    assert _send(pg, moved) == ["status.paint"]
    assert _send(pg, acct) == []                            # the Account tab is hidden: parked, not painted
    pg.evaluate("() => switchTab('chain')")
    assert _send(pg, gex, {"type": "status", "data": {"connected": False, "spot_price": 6155}}) == []
    pg.evaluate("() => { window.__spans.length = 0; switchTab('account'); }")
    pg.wait_for_function("() => window.__spans.includes('account_update.paint')", timeout=5000)   # the server's own update
    assert _send(pg, acct) == ["account_update.paint"]


def test_a_missing_perf_module_never_drops_a_data_message(page_on_dashboard):
    pg = page_on_dashboard
    pg.evaluate("""() => {
        window.__perfOnMessage = perfOnMessage; window.__perfAfterHandle = perfAfterHandle;
        window.perfOnMessage = undefined; window.perfAfterHandle = undefined;
    }""")
    try:
        pg.evaluate("g => ws.onmessage({data: JSON.stringify({type: 'gex', data: g})})", _gex(6150))
        pg.wait_for_function("() => gexChart._fullData[0].x.length === 20", timeout=5000)
    finally:
        pg.evaluate("() => { window.perfOnMessage = window.__perfOnMessage; window.perfAfterHandle = window.__perfAfterHandle; }")
