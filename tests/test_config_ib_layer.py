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


def _config_values(tmp_path, **env):
    """Import config in a clean interpreter (no repo .env) with ``env`` set; return the push/price settings."""
    import json
    import os
    import subprocess
    import sys
    code = ("import json; from spx_trade_desk.core import config as c; "
            "print(json.dumps([c.PUSH_ORDERED_BACKLOG_MAX, c.PUSH_SEND_TIMEOUT_S, c.PRICE_BARS_KEEP_UP_TO_DATE]))")
    full = {k: v for k, v in os.environ.items()
            if k not in ("PUSH_ORDERED_BACKLOG_MAX", "PUSH_SEND_TIMEOUT_S", "PRICE_BARS_KEEP_UP_TO_DATE")}
    full.update(env, DOTENV_PATH=str(tmp_path / ".env"))
    out = subprocess.run([sys.executable, "-c", code], env=full, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_push_settings_are_clamped_to_safe_floors(tmp_path):
    assert _config_values(tmp_path, PUSH_ORDERED_BACKLOG_MAX="0", PUSH_SEND_TIMEOUT_S="0") == [10, 0.5, True]
    assert _config_values(tmp_path, PUSH_ORDERED_BACKLOG_MAX="-5", PUSH_SEND_TIMEOUT_S="0.1")[:2] == [10, 0.5]
    assert _config_values(tmp_path, PUSH_ORDERED_BACKLOG_MAX="250", PUSH_SEND_TIMEOUT_S="2.5")[:2] == [250, 2.5]


def test_empty_values_resolve_to_the_defaults(tmp_path):
    assert _config_values(tmp_path, PRICE_BARS_KEEP_UP_TO_DATE="", PUSH_ORDERED_BACKLOG_MAX="",
                          PUSH_SEND_TIMEOUT_S="  ") == [1000, 5.0, True]
    assert _config_values(tmp_path, PRICE_BARS_KEEP_UP_TO_DATE="false")[2] is False
