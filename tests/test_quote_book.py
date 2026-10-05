"""Merged quote book: stream and poller write, publisher and recorder read."""
from spx_trade_desk.market.gex import OptionData
from spx_trade_desk.market.quote_book import QuoteBook


def opt(strike, right="P", **kw):
    return OptionData(strike=strike, right=right, **kw)


def _book():
    b = QuoteBook()
    b.reset("20261005")
    return b


def test_newer_update_wins_and_records_source_and_age():
    b = _book()
    b.update([opt(7700, bid=1.0, ask=1.2)], "poll", now=10.0)
    b.update([opt(7700, bid=1.1, ask=1.3)], "stream", now=15.0)
    (o,) = b.options()
    assert (o.bid, o.ask) == (1.1, 1.3)
    assert b.sources() == {(7700.0, "P"): "stream"}
    assert b.ages(now=20.0) == {(7700.0, "P"): 5.0}


def test_missing_fields_never_overwrite_present_ones():
    b = _book()
    b.update([opt(7700, bid=1.0, delta=-0.2, implied_vol=0.15, open_interest=500, volume=40)],
             "poll", now=0.0)
    b.update([opt(7700, bid=1.05)], "stream", now=1.0)      # no greeks, OI or volume yet
    o = b.options()[0]
    assert o.bid == 1.05 and o.delta == -0.2 and o.implied_vol == 0.15
    assert o.open_interest == 500 and o.volume == 40


def test_zero_open_interest_does_not_wipe_but_a_positive_value_updates():
    b = _book()
    b.update([opt(7700, open_interest=500)], "poll", now=0.0)
    b.update([opt(7700, open_interest=0)], "poll", now=1.0)
    assert b.options()[0].open_interest == 500
    b.update([opt(7700, open_interest=650)], "poll", now=2.0)
    assert b.options()[0].open_interest == 650


def test_options_are_copies():
    b = _book()
    b.update([opt(7700, bid=1.0)], "poll", now=0.0)
    b.options()[0].bid = 99.0
    assert b.options()[0].bid == 1.0


def test_reset_on_new_expiry_drops_rows():
    b = _book()
    b.update([opt(7700)], "poll", now=0.0)
    b.reset("20261006")
    assert len(b) == 0 and b.expiry == "20261006"


def test_keys_are_normalized_and_options_sorted():
    b = _book()
    b.update([opt(7705, "c"), opt(7700.04, "p"), opt(7700, "C")], "poll", now=0.0)
    assert [(o.strike, o.right) for o in b.options()] == [(7700.0, "C"), (7700.0, "P"), (7705.0, "C")]


def test_age_is_never_negative():
    b = _book()
    b.update([opt(7700)], "poll", now=10.0)
    assert b.ages(now=9.0) == {(7700.0, "P"): 0.0}
