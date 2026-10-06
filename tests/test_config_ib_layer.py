"""Settings added for the IB layer."""
import pytest

from spx_trade_desk.core import config


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("YES", True), ("on", True),
                                          ("false", False), ("0", False), ("off", False), ("", False)])
def test_as_bool_parses_common_spellings(raw, expected):
    assert config._as_bool(raw) is expected


def test_as_bool_rejects_garbage():
    with pytest.raises(ValueError):
        config._as_bool("maybe")


def test_a_bool_setting_falls_back_to_its_default_on_garbage(monkeypatch):
    monkeypatch.setenv("IB_LAYER_TEST_FLAG", "maybe")
    assert config._get_setting("IB_LAYER_TEST_FLAG", True, config._as_bool) is True
    monkeypatch.setenv("IB_LAYER_TEST_FLAG", "off")
    assert config._get_setting("IB_LAYER_TEST_FLAG", True, config._as_bool) is False
