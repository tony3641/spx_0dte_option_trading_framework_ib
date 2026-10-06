"""
Ported chain_fetcher tests — native bridge surface (ib_client.py) + MockIBClient.

Covers ``_stream_to_option_data`` conversion (OI fallback, greek priority, IV
normalization), ``fetch_option_chain`` strike filtering / qualification-cache /
unknown-blacklist retry, ``get_chain_params`` / ``get_monthly_chain_params``,
and the BSM-gamma fallback in ``compute_gex``.
"""

import pytest

from spx_trade_desk.ib.contracts import ContractRegistry
from spx_trade_desk.market.chain_fetcher import (
    _stream_to_option_data,
    fetch_option_chain,
    get_chain_params,
    get_monthly_chain_params,
)
from spx_trade_desk.market.gex import compute_gex, OptionData, _bsm_gamma
from spx_trade_desk.ib.client import Greeks, TickStream
from tests.conftest import MockContract, MockIBClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _greeks(implied_vol=None, delta=None, gamma=None, vega=None, theta=None):
    g = Greeks()
    g.implied_vol = implied_vol
    g.delta = delta
    g.gamma = gamma
    g.vega = vega
    g.theta = theta
    return g


def _make_stream(contract, *, bid=1.0, ask=1.2, last=1.1,
                 bid_size=0, ask_size=0, volume=0,
                 call_oi=0, put_oi=0,
                 model_greeks=None, last_greeks=None,
                 bid_greeks=None, ask_greeks=None):
    s = TickStream(0, contract)
    s.bid = bid
    s.ask = ask
    s.last = last
    s.bid_size = bid_size
    s.ask_size = ask_size
    s.volume = volume
    s.call_oi = call_oi
    s.put_oi = put_oi
    if model_greeks is not None:
        s.model_greeks = model_greeks
    if last_greeks is not None:
        s.last_greeks = last_greeks
    if bid_greeks is not None:
        s.bid_greeks = bid_greeks
    if ask_greeks is not None:
        s.ask_greeks = ask_greeks
    return s


def _spx_option(strike, right):
    return MockContract(
        symbol='SPX',
        secType='OPT',
        lastTradeDateOrContractMonth='20260410',
        strike=strike,
        right=right,
        exchange='SMART',
        tradingClass='SPXW',
    )


# ---------------------------------------------------------------------------
# _stream_to_option_data conversion
# ---------------------------------------------------------------------------

def test_stream_to_option_data_put_open_interest_falls_back_to_call_field():
    stream = _make_stream(
        _spx_option(6800.0, 'P'),
        put_oi=0,
        call_oi=1234,
        model_greeks=_greeks(gamma=0.05, delta=-0.3, implied_vol=0.18),
    )

    opt_data = _stream_to_option_data(stream)

    assert opt_data is not None
    assert opt_data.open_interest == 1234


def test_stream_to_option_data_call_open_interest_falls_back_to_put_field():
    stream = _make_stream(
        _spx_option(6800.0, 'C'),
        call_oi=0,
        put_oi=567,
        model_greeks=_greeks(gamma=0.05, delta=0.7, implied_vol=0.18),
    )

    opt_data = _stream_to_option_data(stream)

    assert opt_data is not None
    assert opt_data.open_interest == 567


def test_stream_to_option_data_uses_bid_greeks_when_model_and_last_missing():
    stream = _make_stream(
        _spx_option(6825.0, 'C'),
        call_oi=100,
        model_greeks=None,
        last_greeks=None,
        bid_greeks=_greeks(gamma=0.01234, delta=0.55, implied_vol=0.199),
    )

    opt_data = _stream_to_option_data(stream)

    assert opt_data is not None
    assert abs(opt_data.gamma - 0.01234) < 1e-9
    assert abs(opt_data.delta - 0.55) < 1e-9
    assert abs(opt_data.implied_vol - 0.199) < 1e-9


def test_stream_to_option_data_normalizes_percent_like_iv_values():
    stream = _make_stream(
        _spx_option(6850.0, 'P'),
        put_oi=100,
        model_greeks=_greeks(gamma=0.02, delta=-0.4, implied_vol=18.3),
    )

    opt_data = _stream_to_option_data(stream)

    assert opt_data is not None
    assert abs(opt_data.implied_vol - 0.183) < 1e-9


def test_stream_to_option_data_returns_none_for_stream_without_strike():
    from types import SimpleNamespace
    bare = _make_stream(SimpleNamespace(symbol='SPX', secType='IND'))
    assert _stream_to_option_data(bare) is None


# ---------------------------------------------------------------------------
# fetch_option_chain — strike filtering, cache, unknown-blacklist
# ---------------------------------------------------------------------------

def _underlying():
    return MockContract(symbol='SPX', secType='IND', conId=12345)


@pytest.mark.asyncio
async def test_fetch_option_chain_filters_strikes_to_multiple_of_five_within_range():
    mock = MockIBClient()
    strikes = [4995.0, 5000.0, 5200.0, 6800.0, 6801.0, 6900.0, 9999.0]

    data = await fetch_option_chain(mock, _underlying(), '20260410', strikes,
                                    spot_price=5200.0, std_dev_range=1.0,
                                    annual_vol=0.20)

    # ±1σ daily range around 5200 ≈ [5134.5, 5265.5] → only 5200 survives
    # (5000 is a multiple of 5 but out of range; 4995/6801/9999 aren't multiples).
    assert {o.strike for o in data} == {5200.0}
    assert {o.right for o in data} == {'C', 'P'}


@pytest.mark.asyncio
async def test_fetch_option_chain_lists_the_expiry_once_and_never_requalifies_on_a_spot_move():
    mock = MockIBClient()
    registry = ContractRegistry()
    strikes = [5000.0, 5200.0, 5400.0]
    expiration = '20260410'

    data1 = await fetch_option_chain(mock, _underlying(), expiration, strikes,
                                     spot_price=5200.0, std_dev_range=8.0, registry=registry)
    assert len(data1) == 6                                    # 3 strikes x (C, P)
    assert mock.count_calls("req_chain_contract_details") == 1
    assert mock.count_calls("req_contract_details") == 0      # one bulk message, no per-contract lookups

    data2 = await fetch_option_chain(mock, _underlying(), expiration, strikes,
                                     spot_price=5200.0, std_dev_range=8.0, registry=registry)
    data3 = await fetch_option_chain(mock, _underlying(), expiration, strikes,
                                     spot_price=5250.0, std_dev_range=8.0, registry=registry)
    assert len(data2) == len(data3) == 6                      # a spot move does not invalidate the listing
    assert mock.count_calls("req_chain_contract_details") == 1


@pytest.mark.asyncio
async def test_fetch_option_chain_unknown_strike_stays_out_until_a_manual_retry():
    mock = MockIBClient()
    mock.unlisted_strikes.add(5500.0)
    registry = ContractRegistry(cooldown=120.0)
    strikes = [5200.0, 5500.0]
    expiration = '20260410'

    data1 = await fetch_option_chain(mock, _underlying(), expiration, strikes,
                                     spot_price=5200.0, std_dev_range=8.0, registry=registry)
    assert {o.strike for o in data1} == {5200.0}
    data2 = await fetch_option_chain(mock, _underlying(), expiration, strikes,
                                     spot_price=5200.0, std_dev_range=8.0, registry=registry)
    assert {o.strike for o in data2} == {5200.0}               # inside the cooldown: still absent
    assert mock.count_calls("req_chain_contract_details") == 1

    mock.unlisted_strikes.clear()
    data3 = await fetch_option_chain(mock, _underlying(), expiration, strikes,
                                     spot_price=5200.0, std_dev_range=8.0, registry=registry,
                                     force_requalify=True, allow_unknown_retry=True)
    assert {o.strike for o in data3} == {5200.0, 5500.0}
    assert mock.count_calls("req_chain_contract_details") == 2


# ---------------------------------------------------------------------------
# Chain params
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_chain_params_returns_sorted_spxw_expirations_and_strikes():
    mock = MockIBClient()

    exps, strikes = await get_chain_params(mock, _underlying())

    assert exps == ['20260410', '20260417', '20260619']
    assert strikes == [5000.0, 5100.0, 5200.0, 5300.0, 5400.0, 5500.0]
    assert mock.call_log[-1]["method"] == "req_sec_def_opt_params"


@pytest.mark.asyncio
async def test_get_monthly_chain_params_returns_spx_chain():
    mock = MockIBClient()

    exps, strikes = await get_monthly_chain_params(mock, _underlying())

    assert exps == ['20260619']
    assert strikes == [5000.0, 5200.0, 5400.0]


# ---------------------------------------------------------------------------
# BSM gamma fallback in compute_gex
# ---------------------------------------------------------------------------

def _make_option(strike, right, oi, gamma=None, iv=None):
    return OptionData(
        strike=strike, right=right, open_interest=oi,
        gamma=gamma, implied_vol=iv,
    )


def test_compute_gex_uses_bsm_gamma_when_ib_gamma_missing():
    """When IB gamma is None but IV is present, BSM gamma should be used and GEX != 0."""
    spot = 6800.0
    tte = 30.0 / (390.0 * 252.0)  # ~30 min of 0DTE
    call = _make_option(6800, 'C', oi=1000, gamma=None, iv=0.20)
    put  = _make_option(6800, 'P', oi=1000, gamma=None, iv=0.20)

    result = compute_gex([call, put], spot, time_to_expiry_years=tte)

    assert result.total_call_gex != 0.0, "Call GEX must be non-zero when BSM fallback is used"
    assert result.total_put_gex != 0.0, "Put GEX must be non-zero when BSM fallback is used"
    assert len(result.gex_by_strike) > 0

    # Verify the BSM gamma formula is plausible
    bsm_g = _bsm_gamma(spot, 6800, tte, 0.053, 0.20)
    assert bsm_g is not None and bsm_g > 0
    expected_call_gex = bsm_g * 1000 * 100 * spot * 0.01
    assert abs(result.total_call_gex - expected_call_gex) < 0.01


def test_compute_gex_ib_gamma_takes_precedence_over_bsm():
    """When IB provides gamma it should be used, not BSM."""
    spot = 6800.0
    tte = 30.0 / (390.0 * 252.0)
    ib_gamma = 0.99  # deliberately unrealistic to distinguish from BSM result
    call = _make_option(6800, 'C', oi=100, gamma=ib_gamma, iv=0.20)

    result = compute_gex([call], spot, time_to_expiry_years=tte)

    expected = ib_gamma * 100 * 100 * spot * 0.01
    assert abs(result.total_call_gex - expected) < 0.01


def test_compute_gex_no_gex_when_gamma_and_iv_both_missing():
    """With no gamma and no IV, GEX should be 0 (cannot compute)."""
    spot = 6800.0
    tte = 30.0 / (390.0 * 252.0)
    call = _make_option(6800, 'C', oi=500, gamma=None, iv=None)

    result = compute_gex([call], spot, time_to_expiry_years=tte)

    assert result.total_call_gex == 0.0


@pytest.mark.asyncio
async def test_fetch_option_chain_batches_fit_the_poll_share():
    ib = MockIBClient(line_shares={"fixed": 4, "order": 4, "poll": 7, "stream": 0})
    strikes = [5000.0 + 5 * i for i in range(40)]
    opts = await fetch_option_chain(ib, MockContract(conId=1, symbol="SPX"), "20260410",
                                    strikes, 5100.0, std_dev_range=50.0, annual_vol=0.20)
    sizes = [c["count"] for c in ib.call_log if c["method"] == "fetch_snapshot"]
    assert sizes and max(sizes) <= 7
    assert sum(sizes) == len(opts) == 80
    assert ib.line_budget.used("poll") == 0
