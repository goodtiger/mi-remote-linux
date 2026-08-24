import json
from unittest.mock import AsyncMock

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


def add_process(proc_root, pid, name, children=()):
    process = proc_root / str(pid)
    task = process / "task" / str(pid)
    task.mkdir(parents=True)
    (process / "comm").write_text(name)
    (process / "cmdline").write_bytes(name.encode() + b"\0")
    (task / "children").write_text(" ".join(str(child) for child in children))


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["pi", "codex", "claude"])
async def test_terminal_foreground_cli_selects_its_profile(tmp_path, profile):
    add_process(tmp_path, 100, "foot", [101])
    add_process(tmp_path, 101, "bash", [102])
    add_process(tmp_path, 102, profile)
    desktop = FakeDesktop(
        {
            ("hyprctl", "-j", "activewindow"): json.dumps(
                {"class": "foot", "title": "project", "pid": 100}
            )
        }
    )
    desktop.proc_root = tmp_path

    assert await desktop.active_application() == profile


@pytest.mark.asyncio
async def test_terminal_shell_without_supported_cli_keeps_terminal_profile(tmp_path):
    add_process(tmp_path, 100, "foot", [101])
    add_process(tmp_path, 101, "bash")
    desktop = FakeDesktop(
        {
            ("hyprctl", "-j", "activewindow"): json.dumps(
                {"class": "foot", "title": "shell", "pid": 100}
            )
        }
    )
    desktop.proc_root = tmp_path

    assert await desktop.active_application() == "foot"


@pytest.mark.asyncio
async def test_stale_proc_entry_does_not_break_active_app_detection(tmp_path):
    desktop = FakeDesktop(
        {
            ("hyprctl", "-j", "activewindow"): json.dumps(
                {"class": "foot", "title": "shell", "pid": 999}
            )
        }
    )
    desktop.proc_root = tmp_path

    assert await desktop.active_application() == "foot"


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
async def test_notification_can_include_a_progress_value():
    desktop = FakeDesktop({})
    desktop.notify_send = "notify-send"

    await desktop.notify("MiRemote", "音量 50%", value=50)

    assert desktop.commands[-1] == (
        "notify-send",
        "-u",
        "normal",
        "-h",
        "string:x-canonical-private-synchronous:mi-remote-linux",
        "-h",
        "int:value:50",
        "MiRemote",
        "音量 50%",
    )


@pytest.mark.asyncio
async def test_hyprland_mouse_click_supplies_required_lua_mods_field():
    desktop = FakeDesktop({})
    desktop._hypr_lua = True

    await desktop.mouse_click("right")

    assert desktop.commands[-2:] == [
        (
            "hyprctl",
            "dispatch",
            (
                'hl.dsp.send_key_state({ mods = "", key = "mouse:273", state = "down", '
                'window = "activewindow" })'
            ),
        ),
        (
            "hyprctl",
            "dispatch",
            (
                'hl.dsp.send_key_state({ mods = "", key = "mouse:273", state = "up", '
                'window = "activewindow" })'
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_hyprland_key_stroke_holds_then_releases(monkeypatch):
    desktop = FakeDesktop({})
    desktop._hypr_lua = True
    sleep = AsyncMock()
    monkeypatch.setattr("mi_remote_linux.desktop.asyncio.sleep", sleep)

    await desktop.key_stroke("Left", ("ctrl", "shift"))

    assert desktop.commands[-2:] == [
        (
            "hyprctl",
            "dispatch",
            (
                'hl.dsp.send_key_state({ mods = "CTRL SHIFT", key = "Left", '
                'state = "down", window = "activewindow" })'
            ),
        ),
        (
            "hyprctl",
            "dispatch",
            (
                'hl.dsp.send_key_state({ mods = "CTRL SHIFT", key = "Left", '
                'state = "up", window = "activewindow" })'
            ),
        ),
    ]
    sleep.assert_awaited_once_with(0.05)


@pytest.mark.asyncio
async def test_legacy_hyprland_key_stroke_uses_sendshortcut():
    desktop = FakeDesktop({})
    desktop._hypr_lua = False

    await desktop.key_stroke("Insert", ("shift",))

    assert desktop.commands[-1] == (
        "hyprctl",
        "dispatch",
        "sendshortcut",
        "SHIFT, Insert, activewindow",
    )


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
