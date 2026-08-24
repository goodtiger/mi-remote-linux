import asyncio

import pytest

from mi_remote_linux.config import parse_config
from mi_remote_linux.hid_engine import ButtonEvent
from mi_remote_linux.mapping_engine import MappingEngine


def event(key, down):
    return ButtonEvent(key=key, is_down=down, time_ns=0, code=1, value=int(down))


@pytest.mark.asyncio
async def test_tap_runs_immediately_without_double_binding():
    config = parse_config({"bindings": {"up": {"tap": {"type": "system", "value": "mute"}}}})
    actions = []

    async def run(action):
        actions.append(action)

    engine = MappingEngine(config, run)
    engine.handle(event("up", True))
    engine.handle(event("up", False))
    await asyncio.sleep(0)

    assert [action.value for action in actions] == ["mute"]
    await engine.close()


@pytest.mark.asyncio
async def test_double_suppresses_tap():
    config = parse_config(
        {
            "settings": {"double_ms": 100},
            "bindings": {
                "tv": {
                    "tap": {"type": "system", "value": "mute"},
                    "double": {"type": "system", "value": "play_pause"},
                }
            },
        }
    )
    actions = []

    async def run(action):
        actions.append(action)

    engine = MappingEngine(config, run)
    for down in (True, False, True, False):
        engine.handle(event("tv", down))
    await asyncio.sleep(0)

    assert [action.value for action in actions] == ["play_pause"]
    await engine.close()


@pytest.mark.asyncio
async def test_hold_momentary_layer_and_layered_tap():
    config = parse_config(
        {
            "settings": {"hold_ms": 50},
            "bindings": {
                "ok": {"hold": {"type": "layer_momentary", "value": 1}},
                "up": {
                    "tap": {"type": "system", "value": "mute"},
                    "layers": {"1": {"type": "system", "value": "volume_up"}},
                },
            },
        }
    )
    actions = []

    async def run(action):
        actions.append(action)

    engine = MappingEngine(config, run)
    engine.handle(event("ok", True))
    await asyncio.sleep(0.06)
    assert engine.effective_layer == 1
    engine.handle(event("up", True))
    engine.handle(event("up", False))
    engine.handle(event("ok", False))
    await asyncio.sleep(0)

    assert engine.effective_layer == 0
    assert [action.value for action in actions] == ["volume_up"]
    await engine.close()


@pytest.mark.asyncio
async def test_ok_direction_gesture_consumes_both_buttons():
    config = parse_config(
        {
            "bindings": {
                "ok": {
                    "tap": {"type": "system", "value": "play_pause"},
                    "gesture": {"right": {"type": "system", "value": "next"}},
                },
                "right": {"tap": {"type": "system", "value": "volume_up"}},
            }
        }
    )
    actions = []

    async def run(action):
        actions.append(action)

    engine = MappingEngine(config, run)
    engine.handle(event("ok", True))
    engine.handle(event("right", True))
    engine.handle(event("right", False))
    engine.handle(event("ok", False))
    await asyncio.sleep(0)

    assert [action.value for action in actions] == ["next"]
    await engine.close()


@pytest.mark.asyncio
async def test_toggle_layer_expires_after_idle_timeout():
    config = parse_config(
        {
            "settings": {"layer_idle_ms": 50},
            "bindings": {"tv": {"tap": {"type": "layer_toggle", "value": 2}}},
        }
    )

    async def run(_action):
        pass

    engine = MappingEngine(config, run)
    engine.handle(event("tv", True))
    engine.handle(event("tv", False))
    assert engine.effective_layer == 2
    await asyncio.sleep(0.06)
    assert engine.effective_layer == 0
    await engine.close()


@pytest.mark.asyncio
async def test_input_reset_clears_locked_layer():
    config = parse_config({"bindings": {"tv": {"tap": {"type": "layer_toggle", "value": 2}}}})

    async def run(_action):
        pass

    engine = MappingEngine(config, run)
    engine.handle(event("tv", True))
    engine.handle(event("tv", False))
    assert engine.effective_layer == 2

    engine.reset("device disconnected")

    assert engine.effective_layer == 0
    await engine.close()


@pytest.mark.asyncio
async def test_app_profile_overlays_declared_slots_and_inherits_the_rest():
    config = parse_config(
        {
            "version": 2,
            "bindings": {
                "ok": {
                    "tap": {"type": "key_stroke", "key": "return"},
                    "hold": {"type": "system", "value": "mute"},
                }
            },
            "profiles": {"terminal": {"ok": {"tap": {"type": "key_stroke", "key": "space"}}}},
            "profile_apps": {"terminal": ["ghostty"]},
        }
    )
    actions = []

    async def run(action):
        actions.append(action)

    engine = MappingEngine(config, run)
    engine.set_active_application("com.mitchellh.ghostty Ghostty")
    assert engine.active_profile == "terminal"
    engine.handle(event("ok", True))
    engine.handle(event("ok", False))
    await asyncio.sleep(0)
    assert actions[-1].key == "space"

    engine.handle(event("ok", True))
    await asyncio.sleep(0.36)
    assert actions[-1].value == "mute"
    engine.handle(event("ok", False))
    await engine.close()


@pytest.mark.asyncio
async def test_base_back_hold_is_protected_from_profile_override():
    config = parse_config(
        {
            "version": 2,
            "settings": {"hold_ms": 50},
            "bindings": {"back": {"tap": {"type": "key_stroke", "key": "backspace"}}},
            "profiles": {
                "terminal": {"back": {"hold": {"type": "key_stroke", "key": "u", "mods": ["ctrl"]}}}
            },
            "profile_apps": {"terminal": ["foot"]},
        }
    )
    actions = []

    async def run(action):
        actions.append(action)

    engine = MappingEngine(config, run)
    engine.set_active_application("foot terminal")
    engine.handle(event("back", True))
    await asyncio.sleep(0.06)
    engine.handle(event("back", False))

    assert [(action.key, action.mods) for action in actions] == [("backspace", ())]
    await engine.close()


@pytest.mark.asyncio
async def test_overlay_capture_cleans_up_key_that_opened_it_on_hold():
    captured = False
    config = parse_config(
        {
            "settings": {"hold_ms": 50},
            "bindings": {"home": {"hold": {"type": "overlay", "value": "tutorial"}}},
        }
    )

    async def run(_action):
        nonlocal captured
        captured = True

    engine = MappingEngine(config, run, event_filter=lambda _event: captured)
    engine.handle(event("home", True))
    await asyncio.sleep(0.06)
    engine.handle(event("home", False))

    assert engine._states["home"].phase == "idle"
    await engine.close()
