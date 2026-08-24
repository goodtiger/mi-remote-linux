import json

import pytest

from mi_remote_linux.desktop import ApplicationTracker, DesktopWindow, LinuxDesktop


class FakeDesktop(LinuxDesktop):
    def __init__(self, responses):
        super().__init__(environment={"HYPRLAND_INSTANCE_SIGNATURE": "test"}, hyprctl="hyprctl")
        self.responses = responses
        self.commands = []

    async def _capture(self, *command):
        self.commands.append(command)
        return self.responses.get(tuple(command), "")


@pytest.mark.asyncio
async def test_hyprland_active_app_and_windows_are_parsed():
    desktop = FakeDesktop(
        {
            ("hyprctl", "-j", "activewindow"): json.dumps(
                {"class": "foot", "initialClass": "foot", "title": "Codex"}
            ),
            ("hyprctl", "-j", "clients"): json.dumps(
                [
                    {
                        "address": "0x1",
                        "class": "foot",
                        "title": "Codex",
                        "focusHistoryID": 0,
                        "mapped": True,
                        "hidden": False,
                    },
                    {
                        "address": "0x2",
                        "class": "firefox",
                        "title": "Docs",
                        "focusHistoryID": 1,
                        "mapped": True,
                        "hidden": False,
                    },
                ]
            ),
        }
    )

    assert await desktop.active_application() == "foot"
    windows = await desktop.windows()
    assert windows == [
        DesktopWindow("0x1", "foot", "Codex", True),
        DesktopWindow("0x2", "firefox", "Docs", False),
    ]


@pytest.mark.asyncio
async def test_application_tracker_only_emits_changes():
    class ActiveDesktop:
        values = iter(["foot", "foot", "firefox"])

        async def active_application(self):
            return next(self.values)

    changes = []
    tracker = ApplicationTracker(ActiveDesktop(), changes.append)
    await tracker._poll()
    await tracker._poll()
    await tracker._poll()

    assert changes == ["foot", "firefox"]


@pytest.mark.asyncio
async def test_hyprland_mouse_move_passes_one_dispatcher_argument():
    desktop = FakeDesktop({("hyprctl", "-j", "cursorpos"): '{"x": 100, "y": 200}'})

    await desktop.mouse_move(5, -7)

    assert desktop.commands[-1] == ("hyprctl", "dispatch", "movecursor", "105 193")


@pytest.mark.asyncio
async def test_hyprland_lua_dispatch_is_used_on_055_and_newer():
    desktop = FakeDesktop({("hyprctl", "-j", "cursorpos"): '{"x": 100, "y": 200}'})
    desktop._hypr_lua = True

    await desktop.mouse_move(5, -7)

    assert desktop.commands[-1] == (
        "hyprctl",
        "dispatch",
        "hl.dsp.cursor.move({ x = 105, y = 193 })",
    )


@pytest.mark.asyncio
async def test_hyprland_show_desktop_toggles_empty_and_original_workspace():
    desktop = FakeDesktop({("hyprctl", "-j", "activeworkspace"): '{"id": 4, "name": "code"}'})
    desktop._hypr_lua = True

    await desktop.show_desktop()
    assert desktop._desktop_workspace == "4"
    assert desktop.commands[-1] == (
        "hyprctl",
        "dispatch",
        'hl.dsp.focus({ workspace = "empty" })',
    )

    await desktop.show_desktop()
    assert desktop._desktop_workspace is None
    assert desktop.commands[-1] == (
        "hyprctl",
        "dispatch",
        'hl.dsp.focus({ workspace = "4" })',
    )
