from unittest.mock import AsyncMock

import pytest

from mi_remote_linux.action_runner import ActionError, LinuxActionRunner
from mi_remote_linux.config import Action


@pytest.mark.asyncio
async def test_wayland_shortcut_builds_balanced_modifier_command():
    runner = LinuxActionRunner(
        session="wayland",
        environment={"WAYLAND_DISPLAY": "wayland-1"},
        wtype="/usr/bin/wtype",
    )
    runner._run_command = AsyncMock()

    await runner.run(Action(type="key_stroke", key="tab", mods=("ctrl", "shift")))

    runner._run_command.assert_awaited_once_with(
        "/usr/bin/wtype",
        "-M",
        "ctrl",
        "-M",
        "shift",
        "-k",
        "Tab",
        "-m",
        "shift",
        "-m",
        "ctrl",
    )


@pytest.mark.asyncio
async def test_x11_shortcut_uses_clearmodifiers():
    runner = LinuxActionRunner(session="x11", environment={"DISPLAY": ":0"}, xdotool="xdotool")
    runner._run_command = AsyncMock()

    await runner.key_stroke("return", ("ctrl",))

    runner._run_command.assert_awaited_once_with(
        "xdotool", "key", "--clearmodifiers", "ctrl+Return"
    )


@pytest.mark.asyncio
async def test_macro_runs_steps_in_order(monkeypatch):
    runner = LinuxActionRunner(
        session="wayland", wtype="wtype", environment={"WAYLAND_DISPLAY": "x"}
    )
    runner._run_command = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr("mi_remote_linux.action_runner.asyncio.sleep", sleep)
    macro = Action(
        type="macro",
        steps=(
            Action(type="key_stroke", key="a"),
            Action(type="delay", delay_ms=25),
            Action(type="key_stroke", key="b"),
        ),
    )

    await runner.run(macro)

    assert runner._run_command.await_count == 2
    sleep.assert_awaited_once_with(0.025)


@pytest.mark.asyncio
async def test_unknown_modifier_is_rejected():
    runner = LinuxActionRunner(
        session="wayland", wtype="wtype", environment={"WAYLAND_DISPLAY": "x"}
    )
    with pytest.raises(ActionError, match="unknown modifiers"):
        await runner.key_stroke("a", ("hyper",))


@pytest.mark.asyncio
async def test_wayland_falls_back_to_ydotool_key_codes():
    runner = LinuxActionRunner(
        session="wayland",
        environment={"WAYLAND_DISPLAY": "wayland-1"},
        wtype="",
        ydotool="/usr/bin/ydotool",
    )
    runner._run_command = AsyncMock()

    await runner.key_stroke("return", ("ctrl",))

    runner._run_command.assert_awaited_once_with(
        "/usr/bin/ydotool", "key", "29:1", "28:1", "28:0", "29:0"
    )


@pytest.mark.asyncio
async def test_macos_modifier_names_translate_to_linux_semantics():
    runner = LinuxActionRunner(
        session="wayland", environment={"WAYLAND_DISPLAY": "x"}, wtype="wtype"
    )
    runner._run_command = AsyncMock()

    await runner.key_stroke("k", ("left_cmd", "left_option"))

    runner._run_command.assert_awaited_once_with(
        "wtype", "-M", "ctrl", "-M", "alt", "-k", "k", "-m", "alt", "-m", "ctrl"
    )


@pytest.mark.asyncio
async def test_advanced_actions_delegate_to_desktop_and_overlay():
    desktop = AsyncMock()
    overlay = AsyncMock()
    runner = LinuxActionRunner(
        session="wayland",
        environment={"WAYLAND_DISPLAY": "x"},
        wtype="wtype",
        desktop=desktop,
        overlay_handler=overlay,
    )

    await runner.run(Action(type="window_cycle", scope="app"))
    await runner.run(Action(type="open_app", value="org.mozilla.firefox"))
    await runner.run(Action(type="overlay", value="window_picker"))
    await runner.run(Action(type="system", value="mission_control"))

    desktop.cycle_window.assert_awaited_once_with("app")
    desktop.open_app.assert_awaited_once_with("org.mozilla.firefox")
    assert overlay.await_args_list[0].args == ("window_picker",)
    assert overlay.await_args_list[1].args == ("mission_control",)
