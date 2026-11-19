"""The vertical GEX profile beside the SPX chart: its bars sit on the chart's own price axis, and the price
scale zooms and pans vertically from the profile (wheel, drag, buttons, double-click).

Invented data only (2099 dates, strikes 6100-6195); nothing depends on the wall clock or market hours.
"""
import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.hermetic_server import hermetic_server            # noqa: E402
from tests.e2e.render_bench import _proactor_policy, open_page    # noqa: E402

S = "2099-01-05"
FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
IDLE = "() => _renderJobs.size === 0 && _heavyJobs.size === 0"


def _gex(spot=6150, call_wall=6160, put_wall=6130, dealer=None):
    strikes = [6100 + 5 * i for i in range(20)]
    bars = []
    for s in strikes:
        bars.append({"strike": s, "call_gex": 1e6 * (1 + (s == call_wall) * 4), "put_gex": -1e6 * (1 + (s == put_wall) * 3),
                     "net_gex": 1e4, "call_oi": 10, "put_oi": 20, "call_vol": 1, "put_vol": 2})
    return {"gex_bars": bars, "call_wall": call_wall, "put_wall": put_wall, "gamma_flip": None, "max_pain": 6140,
            "spot_price": spot, "expiration": "20990105", "timestamp": "", "total_call_gex": 3e7, "total_put_gex": -2e7,
            "total_net_gex": 1e7, "total_call_oi": 200, "total_put_oi": 400, "total_call_vol": 20, "total_put_vol": 40,
            "smile_data": [], "es_derived": False, **({"dealer": dealer} if dealer is not None else {})}


# The dealer block as the server sends it (invented values): profile 6100 + 2.5 x i with zero at 6150,
# a pin node R1 at 6160, an accel node S1 at 6130, a void 6170-6180, and the open band 6138-6162.
DEALER = {
    "model": "oi_assumed", "regime": "long_gamma", "gamma_at_spot": 200000000, "zero_gamma": 6150.0,
    "zero_gamma_all": [6150.0], "zero_gamma_distance": 0.0,
    "profile": {"from": 6100.0, "step": 2.5, "values": [(i - 20) * 2000000 for i in range(41)],
                "hedge": [(i - 20) * 500000 for i in range(41)]},
    "nodes": [
        {"label": "R1", "strike": 6160.0, "kind": "pin", "gex": 5000000, "side": "above", "hedge_usd": 2000000, "rank": 1},
        {"label": "S1", "strike": 6130.0, "kind": "accel", "gex": -4000000, "side": "below", "hedge_usd": -1500000, "rank": 1},
    ],
    "voids": [{"lo": 6170.0, "hi": 6180.0}],
    "em": {"live": 8.0, "open": 12.0, "open_spot": 6150.0, "high": 6162.0, "low": 6138.0, "used_pct": 40.0,
           "anchor_time": "09:31"},
    "charm": {"flow_15m_usd": 1000000, "to_close_usd": 5000000, "path": [], "pivot": 6150.0},
    "vanna": {"usd_per_vol_down": -3000000, "atm_iv": 14.2, "iv_change_15m": 0.0, "iv_change_30m": 0.0,
              "flow_est_usd": 0},
}


def _inject_gex(pg, **kw):
    pg.evaluate("m => window.__benchInject(m)", {"type": "gex", "data": _gex(**kw)})


def _snapshot(lo=6095, hi=6200):
    return {"session_date": S, "mode": "live", "overnight": [],
            "bars": [{"time": f"{S}T09:30:00-05:00", "time_short": "09:30", "open": 6150, "high": hi, "low": lo,
                      "close": 6150}]}


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
    problems = []
    browser, pg = open_page(p, url, "dashboard", problems=problems)
    pg.evaluate("m => window.__benchInject(m)", {"type": "price_snapshot", "data": _snapshot()})
    pg.evaluate("m => window.__benchInject(m)", {"type": "gex", "data": _gex()})
    pg.wait_for_function(IDLE, timeout=5000)
    pg.evaluate(FRAMES)
    pg.wait_for_function("() => gexProfile.rows.length === 20", timeout=5000)
    yield pg
    browser.close()
    assert problems == []              # no page error or console error in the whole test


def _range(pg):
    return pg.evaluate("() => priceChart.chart.priceScale('right').getVisibleRange()")


def _auto(pg):
    return pg.evaluate("() => priceChart.chart.priceScale('right').options().autoScale")


def _settle(pg):
    pg.wait_for_function(IDLE, timeout=5000)
    pg.evaluate(FRAMES)
    pg.evaluate(FRAMES)


# --- pure helpers ---------------------------------------------------------------------------------

def test_zoom_range_keeps_the_anchor_fixed_and_respects_the_limits(page):
    out = page.evaluate("""() => ({
        in_: gexZoomRange({from: 100, to: 200}, 0.5, 150, 4, 1000),
        anchored: gexZoomRange({from: 100, to: 200}, 0.5, 120, 4, 1000),
        out: gexZoomRange({from: 100, to: 200}, 2, 150, 4, 1000),
        tooClose: gexZoomRange({from: 100, to: 200}, 0.001, 150, 4, 1000),
        tooFar: gexZoomRange({from: 100, to: 200}, 100, 150, 4, 300),
        offscreen: gexZoomRange({from: 100, to: 200}, 0.5, 900, 4, 1000),
    })""")
    assert out["in_"] == {"from": 125, "to": 175}
    assert out["anchored"] == {"from": 110, "to": 160}                     # 120 stays at 20% of the range
    assert out["out"] == {"from": 50, "to": 250}
    assert out["offscreen"] == {"from": 125, "to": 175}                    # an anchor outside the range: the middle
    assert out["tooClose"]["to"] - out["tooClose"]["from"] == pytest.approx(4)
    assert out["tooFar"]["to"] - out["tooFar"]["from"] == pytest.approx(300)


def test_zoom_range_never_moves_the_wrong_way_when_already_past_a_limit(page):
    out = page.evaluate("""() => ({
        wideOut: gexZoomRange({from: 0, to: 400}, 1.5, 200, 4, 300),
        wideIn: gexZoomRange({from: 0, to: 400}, 0.5, 200, 4, 300),
        narrowIn: gexZoomRange({from: 100, to: 102}, 0.5, 101, 4, 300),
        narrowOut: gexZoomRange({from: 100, to: 102}, 2, 101, 4, 300),
    })""")
    assert out["wideOut"] == {"from": 0, "to": 400}                        # zoom out on a too-wide range: unchanged
    assert out["wideIn"]["to"] - out["wideIn"]["from"] == pytest.approx(200)
    assert out["narrowIn"] == {"from": 100, "to": 102}                     # zoom in on a too-narrow range: unchanged
    assert out["narrowOut"]["to"] - out["narrowOut"]["from"] == pytest.approx(4)   # zoom out reaches the minimum


def test_pan_range_follows_the_pointer_and_levels_range_covers_what_it_is_given(page):
    out = page.evaluate("""() => ({
        down: gexPanRange({from: 100, to: 200}, 50, 500),
        up: gexPanRange({from: 100, to: 200}, -100, 500),
        noHeight: gexPanRange({from: 100, to: 200}, 50, 0),
        levels: gexLevelsRange({call_wall: 6200, put_wall: 6100, gamma_flip: null, max_pain: 6150}, 6140, [6090, 6210]),
        none: gexLevelsRange(null, 0, []),
    })""")
    assert out["down"] == {"from": 110, "to": 210}                         # dragging down shows higher prices
    assert out["up"] == {"from": 80, "to": 180}
    assert out["noHeight"] == {"from": 100, "to": 200}
    lv = out["levels"]
    assert lv["from"] < 6090 and lv["to"] > 6210 and lv["to"] - lv["from"] < 160
    assert out["none"] is None


def test_levels_range_ignores_the_levels_that_stay_off_the_profile(page):
    out = page.evaluate("""() => ({
        base: gexLevelsRange({call_wall: 6200, put_wall: 6100, gamma_flip: null, max_pain: 6150}, 6140, []),
        withOff: gexLevelsRange({call_wall: 6200, put_wall: 6100, gamma_flip: null, max_pain: 6150,
                                 em_high: 9999, em_low: 1, node_r1: 9999, node_s1: 1}, 6140, []),
    })""")
    assert out["withOff"] == out["base"]


def test_curve_helpers_map_values_and_interpolate(page):
    out = page.evaluate("() => ({xy: gexCurveXY([-2, 0, 4], 100, 2.5, 4, 50, 40), mid: gexInterp(100, 2.5, [0, 10, 20], 101.25), off: gexInterp(100, 2.5, [0, 10], 90)})")
    assert out["xy"] == [{"p": 100, "x": 30}, {"p": 102.5, "x": 50}, {"p": 105, "x": 90}]
    assert out["mid"] == 5 and out["off"] is None


def test_bands_run_halfway_to_each_neighbour(page):
    bands = page.evaluate("() => gexBands([100, 105, 115])")
    assert bands == [{"strike": 100, "lo": 97.5, "hi": 102.5}, {"strike": 105, "lo": 102.5, "hi": 110},
                     {"strike": 115, "lo": 110, "hi": 120}]
    assert page.evaluate("() => gexBands([100])") == [{"strike": 100, "lo": 100, "hi": 100}]


# --- alignment with the price axis ----------------------------------------------------------------

def test_every_row_sits_at_the_height_of_its_price_on_the_candles(page):
    bad = page.evaluate("""() => gexProfile.rows.filter(r =>
        Math.abs(r.y - priceChart.candles.priceToCoordinate(r.strike)) > 0.5).length""")
    assert bad == 0
    assert page.evaluate("() => gexProfile.drawn") > 0
    # the NOW line is where the candles put the live quote
    page.evaluate("m => window.__benchInject(m)", {"type": "status", "data": {"connected": False, "spot_price": 6163.5}})
    _settle(page)
    assert page.evaluate("""() => Math.abs(gexProfile.spotY - priceChart.candles.priceToCoordinate(6163.5)) < 1""")


def test_the_canvas_starts_at_the_chart_top_and_is_as_tall_as_the_price_pane(page):
    out = page.evaluate("""() => {
        const cv = gexProfile.canvas.getBoundingClientRect(), ch = document.getElementById('priceChart').getBoundingClientRect();
        return {top: Math.abs(cv.top - ch.top), h: Math.abs(cv.height - priceChart.chart.panes()[0].getHeight()),
                backing: gexProfile.canvas.width === Math.round(gexProfile.w * devicePixelRatio)};
    }""")
    assert out["top"] < 1 and out["h"] <= 1 and out["backing"]


def test_the_profile_follows_a_change_of_the_price_scale_made_elsewhere(page):
    before = page.evaluate("() => gexProfile.rows.length")
    page.evaluate("() => priceChart.chart.priceScale('right').setVisibleRange({from: 6140, to: 6160})")
    page.wait_for_function("() => gexProfile.rows.length < 20", timeout=5000)        # zoomed in: fewer strikes on screen
    assert page.evaluate("() => gexProfile.rows.length") < before
    page.evaluate("() => priceChart.chart.priceScale('right').setAutoScale(true)")
    page.wait_for_function("() => gexProfile.rows.length === 20", timeout=5000)


# --- vertical zoom and pan ------------------------------------------------------------------------

def test_zoom_buttons_scale_the_price_scale_around_the_live_quote_and_fit_restores_it(page):
    r0 = _range(page)
    assert _auto(page) is True
    y0 = page.evaluate("() => priceChart.candles.priceToCoordinate(state.currentSpot)")
    page.click("#gexZoomIn")
    _settle(page)
    r1 = _range(page)
    assert (r1["to"] - r1["from"]) < 0.7 * (r0["to"] - r0["from"]) and _auto(page) is False
    assert page.evaluate("y => Math.abs(priceChart.candles.priceToCoordinate(state.currentSpot) - y) < 1.5", y0)
    page.click("#gexZoomOut")
    page.click("#gexZoomOut")
    _settle(page)
    r2 = _range(page)
    assert (r2["to"] - r2["from"]) > (r0["to"] - r0["from"])
    assert page.evaluate("() => !document.getElementById('gexZoomFit').classList.contains('active')")
    page.click("#gexZoomFit")
    _settle(page)
    assert _auto(page) is True and _range(page) == pytest.approx(r0, rel=1e-6)
    assert page.evaluate("() => document.getElementById('gexZoomFit').classList.contains('active')")


def test_levels_button_shows_the_quote_the_levels_and_the_session(page):
    page.evaluate("() => priceChart.chart.priceScale('right').setVisibleRange({from: 6145, to: 6155})")
    _settle(page)
    page.click("#gexZoomLevels")
    _settle(page)
    r = _range(page)
    assert r["from"] <= 6095 and r["to"] >= 6200                          # the session's low and high


def test_the_wheel_over_the_profile_zooms_vertically_not_the_page(page):
    r0 = _range(page)
    out = page.evaluate("""() => {
        const cv = gexProfile.canvas, rect = cv.getBoundingClientRect();
        const y = priceChart.candles.priceToCoordinate(state.currentSpot);
        const ev = new WheelEvent('wheel', {deltaY: -240, clientX: rect.left + 100, clientY: rect.top + y, bubbles: true, cancelable: true});
        cv.dispatchEvent(ev);
        return {prevented: ev.defaultPrevented, y};
    }""")
    assert out["prevented"] is True
    _settle(page)
    r1 = _range(page)
    assert (r1["to"] - r1["from"]) < 0.8 * (r0["to"] - r0["from"]) and _auto(page) is False
    # the price under the pointer stayed under it
    assert page.evaluate("y => Math.abs(priceChart.candles.priceToCoordinate(state.currentSpot) - y) < 1.5", out["y"])
    page.evaluate("""() => { const cv = gexProfile.canvas, rect = cv.getBoundingClientRect();
        cv.dispatchEvent(new WheelEvent('wheel', {deltaY: 240, clientX: rect.left + 100, clientY: rect.top + 200, bubbles: true, cancelable: true})); }""")
    _settle(page)
    r2 = _range(page)
    assert (r2["to"] - r2["from"]) > (r1["to"] - r1["from"])


def test_the_zoom_never_goes_below_the_minimum_span(page):
    for _ in range(40):
        page.click("#gexZoomIn")
    _settle(page)
    r = _range(page)
    assert r["to"] - r["from"] >= 3.9


def test_dragging_the_profile_pans_and_a_double_click_fits(page):
    page.click("#gexZoomIn")
    _settle(page)
    r1 = _range(page)
    h = page.evaluate("() => gexProfile.h")
    page.evaluate("""() => {
        const cv = gexProfile.canvas, rect = cv.getBoundingClientRect(), x = rect.left + 100;
        const fire = (type, y) => cv.dispatchEvent(new PointerEvent(type, {button: 0, clientX: x, clientY: rect.top + y, pointerId: 7, bubbles: true}));
        fire('pointerdown', 150); fire('pointermove', 210); fire('pointerup', 210);
    }""")
    _settle(page)
    r2 = _range(page)
    assert r2["to"] - r2["from"] == pytest.approx(r1["to"] - r1["from"])           # panning does not rescale
    assert r2["from"] - r1["from"] == pytest.approx(60 / h * (r1["to"] - r1["from"]), rel=0.05)
    page.evaluate("() => gexProfile.canvas.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
    _settle(page)
    assert _auto(page) is True


# --- reading the bars -----------------------------------------------------------------------------

def test_hovering_a_row_shows_its_strike_and_values(page):
    page.evaluate("""() => {
        const cv = gexProfile.canvas, rect = cv.getBoundingClientRect();
        const row = gexProfile.rows.find(r => r.strike === 6160);
        cv.dispatchEvent(new PointerEvent('pointermove', {clientX: rect.left + 100, clientY: rect.top + row.y, pointerId: 3, bubbles: true}));
    }""")
    tip = page.evaluate("""() => { const t = document.getElementById('gexProfileTip');
        return {hidden: t.classList.contains('hidden'), text: t.textContent}; }""")
    assert tip["hidden"] is False
    assert "Strike 6,160" in tip["text"] and "Call GEX" in tip["text"] and "Put OI" in tip["text"]
    assert page.evaluate("() => gexProfile.hoverStrike") == 6160
    page.evaluate("() => gexProfile.canvas.dispatchEvent(new PointerEvent('pointerleave', {pointerId: 3, bubbles: true}))")
    assert page.evaluate("() => document.getElementById('gexProfileTip').classList.contains('hidden')")
    assert page.evaluate("() => gexProfile.hoverStrike") is None


def test_the_split_and_net_views_switch_and_are_remembered(page):
    assert page.evaluate("() => gexProfile.view") == "split"
    drawn_split = page.evaluate("() => gexProfile.drawn")
    page.click("#gexViewToggle [data-view=net]")
    assert page.evaluate("() => gexProfile.view") == "net"
    assert page.evaluate("() => localStorage.getItem('spx-gex-profile-view')") == "net"
    assert page.evaluate("() => gexProfile.drawn") < drawn_split           # one bar per strike instead of two
    page.reload()
    page.wait_for_function("() => typeof state !== 'undefined' && state.gexProfileReady === true", timeout=15000)
    assert page.evaluate("() => gexProfile.view") == "net"
    assert page.evaluate("() => document.querySelector('#gexViewToggle [data-view=net]').classList.contains('active')")


def test_the_summary_reports_net_gex_open_interest_and_the_expiration(page):
    page.wait_for_function("() => document.getElementById('gexSumNet').textContent !== '-'", timeout=5000)
    text = page.evaluate("""() => ({net: document.getElementById('gexSumNet').textContent,
        regime: document.getElementById('gexSumRegime').textContent,
        oi: document.getElementById('gexSumOi').textContent, pc: document.getElementById('gexSumPc').textContent,
        skew: document.getElementById('gexSumSkew').textContent, exp: document.getElementById('gexChartLabel').textContent})""")
    assert text["net"] == "+$10.0M" and text["regime"] == "CONVERGING"
    assert text["oi"] == "200 / 400" and text["pc"] == "2.00" and text["skew"] == "60%"
    assert text["exp"] == "Exp 2099-01-05"
    assert page.evaluate("() => document.getElementById('gexSumNet').classList.contains('up')")


def test_strikes_outside_the_visible_range_are_not_drawn(page):
    # Zoom to a range that excludes the lower strikes (and the max pain strike, 6140): their rows are skipped.
    page.evaluate("() => priceChart.chart.priceScale('right').setVisibleRange({from: 6150, to: 6190})")
    _settle(page)
    rows = page.evaluate("() => gexProfile.rows.map(r => r.strike)")
    assert rows and min(rows) >= 6145 and max(rows) <= 6195 and len(rows) < 20


# --- dealer overlays: curve, nodes, voids, expected-move brackets, summary rows -------------------

def test_dealer_overlays_are_painted_and_the_curve_toggle_is_remembered(page):
    _inject_gex(page, dealer=DEALER)
    _settle(page)
    c = page.evaluate("() => [gexProfile.curveDrawn > 0, gexProfile.nodesDrawn, gexProfile.voidsDrawn, gexProfile.emDrawn]")
    assert c == [True, 2, 1, 2]
    page.click("#gexCurveToggle")
    _settle(page)
    assert page.evaluate("() => [gexProfile.curveDrawn, localStorage.getItem('spx-gex-curve')]") == [0, "0"]
    assert page.evaluate("() => document.getElementById('gexCurveToggle').classList.contains('active')") is False
    page.click("#gexCurveToggle")
    _settle(page)
    assert page.evaluate("() => gexProfile.curveDrawn > 0") and page.evaluate("() => localStorage.getItem('spx-gex-curve')") == "1"


def test_the_summary_shows_regime_zero_gamma_distance_em_used_charm_and_vanna(page):
    dealer = dict(DEALER, gamma_at_spot=200000000, zero_gamma_distance=-4.5,
                  em=dict(DEALER["em"], used_pct=62), charm=dict(DEALER["charm"], to_close_usd=12000000),
                  vanna=dict(DEALER["vanna"], usd_per_vol_down=-3000000))
    _inject_gex(page, dealer=dealer)
    _settle(page)
    assert page.inner_text("#gexSumGamma").startswith("Long γ")
    assert page.inner_text("#gexSumZgDist") == "-4.5"
    assert page.inner_text("#gexSumEmUsed") == "62%" and page.inner_text("#gexSumCharm") == "+$12.0M"
    assert page.inner_text("#gexSumVanna") == "-$3.0M"


def test_hovering_a_node_strike_lists_the_node_and_the_hedge_to_there(page):
    _inject_gex(page, dealer=DEALER)
    _settle(page)
    page.evaluate("""() => {
        const cv = gexProfile.canvas, rect = cv.getBoundingClientRect();
        const row = gexProfile.rows.find(r => r.strike === 6160);
        cv.dispatchEvent(new PointerEvent('pointermove', {clientX: rect.left + 100, clientY: rect.top + row.y, pointerId: 4, bubbles: true}));
    }""")
    text = page.evaluate("() => document.getElementById('gexProfileTip').textContent")
    assert "Node" in text and "R1 pin" in text and "Hedge $ to here" in text


def test_monthly_mode_draws_no_dealer_overlays(page):
    _inject_gex(page, dealer=DEALER)
    page.evaluate("m => window.__benchInject(m)", {"type": "monthly_gex", "data": _gex()})
    try:
        page.evaluate("() => setGexMode('monthly')")
        _settle(page)
        assert page.evaluate("() => [gexProfile.curveDrawn, gexProfile.nodesDrawn, gexProfile.voidsDrawn, gexProfile.emDrawn]") == [0, 0, 0, 0]
    finally:
        _restore_0dte(page)


# --- customer flow: the Flow view, flow GEX ticks, hot strikes ----------------------------------------

# The streamed strikes (inside the coverage 6130..6170) and the flow block the server sends with them.
STREAMED = [6130, 6135, 6140, 6145, 6150, 6155, 6160, 6165, 6170]
FLOW_DEALER = dict(DEALER, flow={
    "coverage_start": "09:31", "coverage": {"lo": 6130, "hi": 6170},
    "hedge_pressure_15m_usd": 1000000, "hedge_pressure_30m_usd": 1500000, "hedge_pressure_day_usd": 2000000,
    "call_delta_day_usd": 1200000, "put_delta_day_usd": 800000, "classified_pct": 80.0,
    "minute": {"time": f"{S}T10:00", "call_delta_usd": 900000, "put_delta_usd": 600000,
               "hedge_pressure_usd": 1500000}})


def _flow_gex(dealer=FLOW_DEALER):
    """The 0DTE payload with flow keys on the streamed strikes, flow GEX on four of them and one hot strike."""
    g = _gex(dealer=dealer)
    for b in g["gex_bars"]:
        s = b["strike"]
        if s in STREAMED:
            left = (s // 5) % 2 == 0                     # customers net bought calls on half of them, net sold on the rest
            b.update(call_buy=30 if left else 10, call_sell=10 if left else 30, put_buy=5, put_sell=25,
                     call_net_delta_usd=2000000 if left else -1000000, put_net_delta_usd=-1000000)
        if s == 6100:                                    # outside the coverage: keys but no bar
            b.update(call_buy=5, call_sell=0, call_net_delta_usd=900000, put_buy=0, put_sell=0, put_net_delta_usd=0)
        if s in (6130, 6140, 6150, 6160):
            b["flow_gex"] = 4e6 if s in (6130, 6150) else -3e6
        if s == 6170:
            b["flow_gex"] = 0.0                          # a streamed strike with no flow gamma: no tick
        if s == 6140:
            b.update(hot=True, vol_oi=2.5, vol_15m=0)    # hot and stale: outlined
        if s == 6160:
            b.update(hot=True, vol_oi=0.5, vol_15m=50)   # hot and fresh: a filled dot
    return g


def _inject_data(pg, msg_type, data):
    pg.evaluate("m => window.__benchInject(m)", {"type": msg_type, "data": data})


def _restore_0dte(pg):
    pg.evaluate("() => setGexMode('0dte')")
    _settle(pg)


def test_the_flow_view_draws_customer_delta_bars_inside_coverage_and_is_remembered(page):
    _inject_data(page, "gex", _flow_gex())
    _settle(page)
    page.click("#gexViewToggle [data-view=flow]")
    _settle(page)
    assert page.evaluate("() => gexProfile.view") == "flow"
    assert page.evaluate("() => localStorage.getItem('spx-gex-profile-view')") == "flow"
    assert page.evaluate("() => gexProfile.caption") == "PUT Δ$ | CALL Δ$"
    # a call and a put bar for each of the nine streamed strikes inside coverage; the strike outside has none
    assert page.evaluate("() => gexProfile.flowBarsDrawn") == 2 * len(STREAMED)
    page.reload()
    page.wait_for_function("() => typeof state !== 'undefined' && state.gexProfileReady === true", timeout=15000)
    assert page.evaluate("() => gexProfile.view") == "flow"
    assert page.evaluate("() => document.querySelector('#gexViewToggle [data-view=flow]').classList.contains('active')")


def test_split_and_net_views_draw_a_flow_gex_tick_per_streamed_strike(page):
    _inject_data(page, "gex", _flow_gex())
    _settle(page)
    assert page.evaluate("() => gexProfile.flowTicksDrawn") == 4          # four strikes carry flow GEX
    page.click("#gexViewToggle [data-view=net]")
    _settle(page)
    assert page.evaluate("() => gexProfile.flowTicksDrawn") == 4
    page.click("#gexViewToggle [data-view=flow]")                         # the Flow view has no ticks
    _settle(page)
    assert page.evaluate("() => gexProfile.flowTicksDrawn") == 0
    page.click("#gexViewToggle [data-view=split]")
    _settle(page)
    page.click("#gexTickToggle")                                          # the Ticks switch hides them and is remembered
    _settle(page)
    assert page.evaluate("() => [gexProfile.flowTicksDrawn, localStorage.getItem('spx-gex-flow-ticks')]") == [0, "0"]
    page.click("#gexTickToggle")
    _settle(page)
    assert page.evaluate("() => gexProfile.flowTicksDrawn") == 4


def test_hot_strikes_get_a_gutter_dot(page):
    _inject_data(page, "gex", _flow_gex())
    _settle(page)
    assert page.evaluate("() => gexProfile.hotDrawn") == 2                # the two hot strikes on screen
    page.click("#gexHotToggle")
    _settle(page)
    assert page.evaluate("() => [gexProfile.hotDrawn, localStorage.getItem('spx-gex-hot')]") == [0, "0"]
    page.click("#gexHotToggle")
    _settle(page)
    assert page.evaluate("() => gexProfile.hotDrawn") == 2


def test_monthly_mode_hides_flow_elements(page):
    # Monthly data with flow keys on it still shows no flow: the mode, not the data, decides.
    _inject_data(page, "gex", _flow_gex())
    _inject_data(page, "monthly_gex", _flow_gex(dealer=None))
    try:
        page.evaluate("() => setGexMode('monthly')")
        page.click("#gexViewToggle [data-view=flow]")
        _settle(page)
        assert page.evaluate("() => [gexProfile.flowBarsDrawn, gexProfile.flowTicksDrawn, gexProfile.hotDrawn]") == [0, 0, 0]
        flow_as_split = page.evaluate("() => gexProfile.drawn")
        page.click("#gexViewToggle [data-view=split]")
        _settle(page)
        assert page.evaluate("() => gexProfile.drawn") == flow_as_split   # the Flow view draws Split in monthly mode
    finally:
        _restore_0dte(page)
