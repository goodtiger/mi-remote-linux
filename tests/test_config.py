import json

import pytest

from mi_remote_linux.config import ConfigError, load_config, parse_config


def test_example_config_loads_all_remote_keys():
    config = load_config("examples/remote.example.json")

    assert len(config.bindings) == 13
    assert config.bindings["home"].hold.type == "layer_toggle"
    assert config.bindings["back"].hold is None


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"version": 2}, "unsupported"),
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
    ],
)
def test_invalid_config_is_rejected(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_key_code_override_is_parsed_from_json_keys(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({"key_codes": {"200": "voice"}}), encoding="utf-8")

    assert load_config(path).key_codes == {200: "voice"}
