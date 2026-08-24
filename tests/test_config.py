import json

import pytest

from mi_remote_linux.config import ConfigError, load_config, load_default_config, parse_config


def test_example_config_loads_all_remote_keys():
    config = load_default_config()

    assert len(config.bindings) == 13
    assert config.bindings["home"].tap.value == "mission_control"
    assert config.bindings["power"].hold.type == "mouse_mode"
    assert config.bindings["back"].hold is None
    assert "ghostty" in config.profiles
    assert config.profile_apps["chrome"]
    assert config.profiles["ghostty"]["back"].tap.key == "backspace"
    assert config.profiles["codex"]["back"].tap.key == "backspace"
    assert config.profiles["claude"]["back"].tap.key == "backspace"


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"version": 3}, "unsupported"),
        ({"bindings": {"banana": {}}}, "unknown remote keys"),
        ({"bindings": {"ok": {"tap": {"type": "shell"}}}}, "unknown"),
        (
            {"bindings": {"ok": {"tap": {"type": "command", "argv": "echo hi"}}}},
            "argv",
        ),
        (
            {
                "bindings": {
                    "ok": {"tap": {"type": "macro", "steps": [{"type": "delay", "ms": -1}]}}
                }
            },
            "0 to 60000",
        ),
        ({"bindings": {"ok": {"tap": {"type": "tab_jump"}}}}, "requires"),
        ({"settings": {"delete_all_on_hold": 1}}, "boolean"),
        (
            {
                "version": 2,
                "profiles": {"terminal": {}},
                "profile_apps": {"missing": ["foot"]},
            },
            "unknown profile",
        ),
    ],
)
def test_invalid_config_is_rejected(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_key_code_override_is_parsed_from_json_keys(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({"key_codes": {"200": "voice"}}), encoding="utf-8")

    assert load_config(path).key_codes == {200: "voice"}
