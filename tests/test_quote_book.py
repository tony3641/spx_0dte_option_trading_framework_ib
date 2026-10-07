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


def test_sizes_follow_their_own_side_of_the_quote():
    b = _book()
    b.update([opt(7700, bid=1.0, ask=1.2, bid_size=5, ask_size=7)], "poll", now=0.0)
    b.update([opt(7700, bid=1.05, bid_size=3)], "poll", now=1.0)
    o = b.options()[0]
    assert (o.bid_size, o.ask_size) == (3, 7)
    b.update([opt(7700, ask=1.3, ask_size=9)], "poll", now=2.0)
    o = b.options()[0]
    assert (o.bid_size, o.ask_size) == (3, 9)
    b.update([opt(7700, last=1.1)], "poll", now=3.0)          # no bid/ask: sizes untouched
    o = b.options()[0]
    assert (o.bid_size, o.ask_size) == (3, 9)


def test_update_without_quote_data_keeps_age_and_source():
    b = _book()
    b.update([opt(7700, bid=1.0)], "stream", now=10.0)
    b.update([opt(7700)], "poll", now=50.0)
    assert b.ages(now=60.0) == {(7700.0, "P"): 50.0}
    assert b.sources() == {(7700.0, "P"): "stream"}


def test_open_interest_only_update_merges_but_keeps_age_and_source():
    b = _book()
    b.update([opt(7700, bid=1.0)], "stream", now=10.0)
    b.update([opt(7700, open_interest=800)], "poll", now=50.0)
    assert b.options()[0].open_interest == 800
    assert b.ages(now=60.0) == {(7700.0, "P"): 50.0}
    assert b.sources() == {(7700.0, "P"): "stream"}


def test_first_sighting_is_stamped_even_without_quote_data():
    b = _book()
    b.update([opt(7700)], "poll", now=10.0)
    assert b.ages(now=12.0) == {(7700.0, "P"): 2.0}
    assert b.sources() == {(7700.0, "P"): "poll"}


def test_zero_volume_does_not_wipe_positive_volume():
    b = _book()
    b.update([opt(7700, volume=40)], "poll", now=0.0)
    b.update([opt(7700, volume=0)], "poll", now=1.0)
    assert b.options()[0].volume == 40
    b.update([opt(7700, volume=55)], "poll", now=2.0)
    assert b.options()[0].volume == 55


def test_stream_none_bid_and_ask_clear_the_books_quote_but_poll_none_does_not():
    b = _book()
    b.update([opt(7700, bid=1.0, ask=1.2, bid_size=5, ask_size=7)], "poll", now=0.0)
    b.update([opt(7700, last=1.1)], "poll", now=1.0)            # poll: absent = unknown
    o = b.options()[0]
    assert (o.bid, o.ask, o.bid_size, o.ask_size) == (1.0, 1.2, 5, 7)
    b.update([opt(7700, ask=1.3, ask_size=2)], "stream", now=2.0)   # stream: no bid = no bid
    o = b.options()[0]
    assert (o.bid, o.ask, o.bid_size, o.ask_size) == (None, 1.3, 0, 2)


def test_get_returns_a_copy_and_the_age():
    b = _book()
    b.update([opt(7700, bid=1.0, ask=1.2)], "stream", now=10.0)
    got, age = b.get((7700.0, "P"), now=12.5)
    assert (got.bid, got.ask, age) == (1.0, 1.2, 2.5)
    got.bid = 9.9
    assert b.get((7700.0, "P"), now=12.5)[0].bid == 1.0          # the book's row is untouched
    assert b.get((7705.0, "P"), now=12.5) is None
