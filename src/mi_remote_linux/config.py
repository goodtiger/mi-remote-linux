"""Phase B JSON configuration model and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REMOTE_KEYS = (
    "power",
    "voice",
    "up",
    "down",
    "left",
    "right",
    "ok",
    "back",
    "home",
    "menu",
    "tv",
    "vol_up",
    "vol_down",
)

ACTION_TYPES = {
    "key_stroke",
    "text",
    "command",
    "system",
    "voice",
    "layer_momentary",
    "layer_toggle",
    "macro",
    "none",
}


class ConfigError(ValueError):
    """The mapping configuration is invalid."""


@dataclass(frozen=True)
class Action:
    type: str
    key: str | None = None
    mods: tuple[str, ...] = ()
    value: str | int | None = None
    argv: tuple[str, ...] = ()
    steps: tuple[Action, ...] = ()
    delay_ms: int = 0


@dataclass(frozen=True)
class KeyBinding:
    tap: Action | None = None
    hold: Action | None = None
    double: Action | None = None
    gesture: dict[str, Action] = field(default_factory=dict)
    layers: dict[int, Action] = field(default_factory=dict)


@dataclass(frozen=True)
class MappingSettings:
    hold_ms: int = 350
    double_ms: int = 250
    layer_idle_ms: int = 20_000
    escape_hold_ms: int = 1_500


@dataclass(frozen=True)
class MappingConfig:
    version: int = 1
    settings: MappingSettings = MappingSettings()
    bindings: dict[str, KeyBinding] = field(default_factory=dict)
    key_codes: dict[int, str] = field(default_factory=dict)


def _int_setting(raw: dict[str, Any], name: str, default: int, *, minimum: int) -> int:
    value = raw.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"settings.{name} must be an integer >= {minimum}")
    return value


def _action(raw: Any, path: str, *, macro_step: bool = False) -> Action:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be an object")
    action_type = raw.get("type")
    if action_type == "delay" and macro_step:
        delay_ms = raw.get("ms")
        if (
            not isinstance(delay_ms, int)
            or isinstance(delay_ms, bool)
            or not 0 <= delay_ms <= 60_000
        ):
            raise ConfigError(f"{path}.ms must be an integer from 0 to 60000")
        return Action(type="delay", delay_ms=delay_ms)
    if action_type not in ACTION_TYPES:
        raise ConfigError(f"{path}.type is unknown: {action_type!r}")

    if action_type == "key_stroke":
        key = raw.get("key")
        mods = raw.get("mods", [])
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{path}.key must be a non-empty string")
        if not isinstance(mods, list) or not all(isinstance(item, str) for item in mods):
            raise ConfigError(f"{path}.mods must be a string array")
        return Action(type=action_type, key=key, mods=tuple(mods))

    if action_type in {"text", "system"}:
        value = raw.get("value")
        if not isinstance(value, str) or (action_type == "system" and not value):
            raise ConfigError(f"{path}.value must be a string")
        return Action(type=action_type, value=value)

    if action_type == "command":
        argv = raw.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise ConfigError(f"{path}.argv must be a non-empty string array")
        return Action(type=action_type, argv=tuple(argv))

    if action_type in {"layer_momentary", "layer_toggle"}:
        value = raw.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 99:
            raise ConfigError(f"{path}.value must be a layer number from 1 to 99")
        return Action(type=action_type, value=value)

    if action_type == "macro":
        steps = raw.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 100:
            raise ConfigError(f"{path}.steps must be an array with at most 100 entries")
        return Action(
            type=action_type,
            steps=tuple(
                _action(step, f"{path}.steps[{index}]", macro_step=True)
                for index, step in enumerate(steps)
            ),
        )

    return Action(type=action_type)


def _binding(raw: Any, path: str) -> KeyBinding:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be an object")
    allowed = {"tap", "hold", "double", "gesture", "layers"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{path} contains unknown fields: {', '.join(sorted(unknown))}")

    gesture_raw = raw.get("gesture", {})
    if not isinstance(gesture_raw, dict) or set(gesture_raw) - {"up", "down", "left", "right"}:
        raise ConfigError(f"{path}.gesture may only contain up/down/left/right")
    layer_raw = raw.get("layers", {})
    if not isinstance(layer_raw, dict):
        raise ConfigError(f"{path}.layers must be an object")
    layers: dict[int, Action] = {}
    for layer, action in layer_raw.items():
        try:
            number = int(layer)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}.layers key must be an integer") from exc
        if str(number) != str(layer) or not 1 <= number <= 99:
            raise ConfigError(f"{path}.layers key must be from 1 to 99")
        layers[number] = _action(action, f"{path}.layers.{layer}")

    return KeyBinding(
        tap=_action(raw["tap"], f"{path}.tap") if "tap" in raw else None,
        hold=_action(raw["hold"], f"{path}.hold") if "hold" in raw else None,
        double=_action(raw["double"], f"{path}.double") if "double" in raw else None,
        gesture={
            name: _action(action, f"{path}.gesture.{name}") for name, action in gesture_raw.items()
        },
        layers=layers,
    )


def parse_config(raw: Any) -> MappingConfig:
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be an object")
    unknown_root = set(raw) - {"version", "settings", "bindings", "key_codes"}
    if unknown_root:
        raise ConfigError(
            f"configuration contains unknown fields: {', '.join(sorted(unknown_root))}"
        )
    version = raw.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported configuration version: {version!r}")
    settings_raw = raw.get("settings", {})
    if not isinstance(settings_raw, dict):
        raise ConfigError("settings must be an object")
    unknown_settings = set(settings_raw) - {
        "hold_ms",
        "double_ms",
        "layer_idle_ms",
        "escape_hold_ms",
    }
    if unknown_settings:
        raise ConfigError(
            f"settings contains unknown fields: {', '.join(sorted(unknown_settings))}"
        )
    settings = MappingSettings(
        hold_ms=_int_setting(settings_raw, "hold_ms", 350, minimum=50),
        double_ms=_int_setting(settings_raw, "double_ms", 250, minimum=0),
        layer_idle_ms=_int_setting(settings_raw, "layer_idle_ms", 20_000, minimum=0),
        escape_hold_ms=_int_setting(settings_raw, "escape_hold_ms", 1_500, minimum=500),
    )

    bindings_raw = raw.get("bindings", {})
    if not isinstance(bindings_raw, dict):
        raise ConfigError("bindings must be an object")
    unknown_keys = set(bindings_raw) - set(REMOTE_KEYS)
    if unknown_keys:
        raise ConfigError(f"unknown remote keys: {', '.join(sorted(unknown_keys))}")
    bindings = {name: _binding(value, f"bindings.{name}") for name, value in bindings_raw.items()}

    key_codes_raw = raw.get("key_codes", {})
    if not isinstance(key_codes_raw, dict):
        raise ConfigError("key_codes must be an object")
    key_codes: dict[int, str] = {}
    for code, name in key_codes_raw.items():
        try:
            number = int(code)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid key code: {code!r}") from exc
        if not 1 <= number <= 767 or name not in REMOTE_KEYS:
            raise ConfigError(f"invalid key_codes entry: {code!r}: {name!r}")
        key_codes[number] = name
    return MappingConfig(version=version, settings=settings, bindings=bindings, key_codes=key_codes)


def load_config(path: str | Path) -> MappingConfig:
    try:
        with Path(path).open(encoding="utf-8") as source:
            return parse_config(json.load(source))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc
