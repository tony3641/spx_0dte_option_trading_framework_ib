"""Key helpers shared by the quote book, the wing poller and the contract registry."""
from spx_trade_desk.ib.contracts import norm_key as contracts_norm_key
from spx_trade_desk.market.qualification import Key, norm_key, unknown_retry_due


def test_norm_key_rounds_strike_and_uppercases_right():
    assert norm_key(7700.04, "p") == (7700.0, "P")


def test_the_qualification_module_re_exports_the_registry_helpers():
    assert norm_key is contracts_norm_key
    assert Key is not None                      # still importable for the quote book and the poller


def test_unknown_retry_is_due_only_after_the_cooldown():
    unknown = {(7705.0, "P"): 0.0, (7710.0, "P"): 100.0}
    assert unknown_retry_due(unknown, now=120.0, cooldown=120.0) == {(7705.0, "P")}
