import asyncio
from unittest.mock import AsyncMock

import pytest

from mi_remote_linux.desktop import DesktopWindow
from mi_remote_linux.hid_engine import ButtonEvent
from mi_remote_linux.interactions import OverlayManager


def event(key, down=True):
    return ButtonEvent(key, down, 0, 1, int(down))


@pytest.mark.asyncio
async def test_window_overlay_is_driven_by_remote_and_focuses_selection():
    desktop = AsyncMock()
    desktop.windows.return_value = [
        DesktopWindow("1", "foot", "Agent 1", True),
        DesktopWindow("2", "firefox", "Docs"),
    ]
    runner = AsyncMock()
    overlay = OverlayManager(desktop, runner)

    await overlay.open("window_picker")
    assert overlay.active
    assert overlay.handle(event("right")) is True
    await asyncio.sleep(0)
    assert overlay.index == 1
    assert overlay.handle(event("ok")) is True
    await asyncio.sleep(0)

    desktop.focus_window.assert_awaited_once_with(DesktopWindow("2", "firefox", "Docs"))
    assert not overlay.active
    await overlay.shutdown()


@pytest.mark.asyncio
async def test_system_menu_runs_typed_action():
    desktop = AsyncMock()
    runner = AsyncMock()
    overlay = OverlayManager(desktop, runner)
    await overlay.open("system_menu")

    # First row, first item mirrors macOS: Mission Control / task view.
    overlay.handle(event("ok"))
    await asyncio.sleep(0)

    action = runner.run.await_args.args[0]
    assert action.type == "system" and action.value == "mission_control"
    await overlay.shutdown()


@pytest.mark.asyncio
async def test_window_picker_menu_cycles_global_current_app_closed():
    desktop = AsyncMock()
    desktop.windows.return_value = [DesktopWindow("1", "foot", "Agent", True)]
    overlay = OverlayManager(desktop, AsyncMock())
    await overlay.open("window_picker")

    overlay.handle(event("menu"))
    await asyncio.sleep(0)
    assert overlay.current_app_only
    desktop.windows.assert_awaited_with(current_app=True)

    overlay.handle(event("menu"))
    await asyncio.sleep(0)
    assert not overlay.active
    await overlay.shutdown()


@pytest.mark.asyncio
async def test_dangerous_system_menu_action_requires_ok_hold():
    desktop = AsyncMock()
    runner = AsyncMock()
    overlay = OverlayManager(desktop, runner)
    overlay.CONFIRM_SECONDS = 0.02
    await overlay.open("system_menu")
    overlay.index = 9  # Quit current app is the first dangerous action.

    overlay.handle(event("ok"))
    overlay.handle(event("ok", down=False))
    await asyncio.sleep(0.03)
    runner.run.assert_not_awaited()

    overlay.handle(event("ok"))
    await asyncio.sleep(0.03)
    action = runner.run.await_args.args[0]
    assert action.type == "key_stroke" and action.key == "q"
    await overlay.shutdown()
