from spx_trade_desk.core.app_state import create_app_state


def test_strategy_state_defaults():
    s = create_app_state()
    assert s.strategies == {}
    assert s.strategy_candidates == {}
    assert s.vix is None
    assert s.auto_trade_kill_switch is False
    assert s.strategy_log == []
    assert s.strategy_open_positions == {}


def test_subsequent_runtime_state_defaults():
    s = create_app_state()
    assert s.runtime == {}
    assert s.day_key == ""


def test_app_state_owns_the_chain_service_objects():
    from spx_trade_desk.core.app_state import create_app_state
    from spx_trade_desk.market.qualification import QualificationCache
    from spx_trade_desk.market.quote_book import QuoteBook
    st = create_app_state()
    assert isinstance(st.quote_book, QuoteBook)
    assert isinstance(st.qual_cache, QualificationCache)
    assert st.vix1d_stream is None
    for gone in ("chain_fetch_active", "manual_refresh_requested", "chain_stream_unknown_keys"):
        assert not hasattr(st, gone)
