"""Cross-desktop discovery and window/workspace actions.

Hyprland and Sway use their native IPC. X11 uses xdotool when available. Other
Wayland desktops retain key injection and receive explicit unsupported errors
instead of silently running compositor-specific commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TERMINAL_APPLICATIONS = {
    "alacritty",
    "com.mitchellh.ghostty",
    "foot",
    "ghostty",
    "kitty",
    "konsole",
    "org.wezfurlong.wezterm",
    "wezterm",
}
TERMINAL_PROFILES = {"claude", "codex", "pi"}


class DesktopActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DesktopWindow:
    id: str
    app_id: str
    title: str
    focused: bool = False


class LinuxDesktop:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        hyprctl: str | None = None,
        swaymsg: str | None = None,
        xdotool: str | None = None,
        gtk_launch: str | None = None,
        notify_send: str | None = None,
        hypr_lua: bool | None = None,
        proc_root: Path | str = Path("/proc"),
    ):
        self.environment = dict(os.environ if environment is None else environment)
        self.hyprctl = shutil.which("hyprctl") if hyprctl is None else hyprctl
        self.swaymsg = shutil.which("swaymsg") if swaymsg is None else swaymsg
        self.xdotool = shutil.which("xdotool") if xdotool is None else xdotool
        self.gtk_launch = shutil.which("gtk-launch") if gtk_launch is None else gtk_launch
        self.notify_send = shutil.which("notify-send") if notify_send is None else notify_send
        self._hypr_lua = hypr_lua
        self.proc_root = Path(proc_root)
        self._desktop_workspace: str | None = None
        if self.environment.get("HYPRLAND_INSTANCE_SIGNATURE") and self.hyprctl:
            self.backend = "hyprland"
        elif self.environment.get("SWAYSOCK") and self.swaymsg:
            self.backend = "sway"
        elif self.environment.get("DISPLAY") and self.xdotool:
            self.backend = "x11"
        else:
            self.backend = "generic-wayland"

    async def active_application(self) -> str | None:
        if self.backend == "hyprland":
            data = await self._json(self.hyprctl, "-j", "activewindow")
            return self._foreground_identity(data)
        if self.backend == "sway":
            tree = await self._json(self.swaymsg, "-t", "get_tree", "-r")
            node = self._focused_sway_node(tree)
            return self._foreground_identity(node or {})
        if self.backend == "x11":
            window_id = (await self._capture(self.xdotool, "getactivewindow")).strip()
            title = (await self._capture(self.xdotool, "getwindowname", window_id)).strip()
            app = (await self._capture(self.xdotool, "getwindowclassname", window_id)).strip()
            if app.casefold() in TERMINAL_APPLICATIONS:
                try:
                    pid_text = (
                        await self._capture(self.xdotool, "getwindowpid", window_id)
                    ).strip()
                    foreground = self._terminal_profile(int(pid_text))
                    if foreground:
                        return foreground
                except (DesktopActionError, TypeError, ValueError):
                    pass
            return app or title or None
        return None

    async def windows(self, *, current_app: bool = False) -> list[DesktopWindow]:
        active = await self.active_application() if current_app else None
        active_app = (active or "").split(" ", 1)[0].casefold()
        if self.backend == "hyprland":
            rows = await self._json(self.hyprctl, "-j", "clients")
            windows = [
                DesktopWindow(
                    id=str(row.get("address", "")),
                    app_id=str(row.get("class") or row.get("initialClass") or "unknown"),
                    title=str(row.get("title") or "Untitled"),
                    focused=int(row.get("focusHistoryID", -1)) == 0,
                )
                for row in rows
                if row.get("mapped", True) and not row.get("hidden", False) and row.get("address")
            ]
            history = {
                str(row.get("address")): int(row.get("focusHistoryID", 2**31 - 1)) for row in rows
            }
            windows.sort(key=lambda item: history.get(item.id, 2**31 - 1))
        elif self.backend == "sway":
            tree = await self._json(self.swaymsg, "-t", "get_tree", "-r")
            windows = []
            self._collect_sway_windows(tree, windows)
        elif self.backend == "x11":
            ids = await self._capture(self.xdotool, "search", "--onlyvisible", "--name", ".")
            active_id = (await self._capture(self.xdotool, "getactivewindow")).strip()
            windows = []
            for window_id in ids.splitlines():
                try:
                    title = (await self._capture(self.xdotool, "getwindowname", window_id)).strip()
                    app = (
                        await self._capture(self.xdotool, "getwindowclassname", window_id)
                    ).strip()
                except DesktopActionError:
                    continue
                windows.append(
                    DesktopWindow(
                        window_id,
                        app or "unknown",
                        title or "Untitled",
                        focused=window_id == active_id,
                    )
                )
        else:
            return []
        if current_app and active_app:
            windows = [item for item in windows if item.app_id.casefold() == active_app]
        return windows

    async def focus_window(self, window: DesktopWindow) -> None:
        if self.backend == "hyprland":
            selector = f"address:{window.id}"
            await self._hypr_dispatch(
                "focuswindow",
                selector,
                f"hl.dsp.focus({{ window = {json.dumps(selector)} }})",
            )
        elif self.backend == "sway":
            await self._run(self.swaymsg, f"[con_id={window.id}]", "focus")
        elif self.backend == "x11":
            await self._run(self.xdotool, "windowactivate", "--sync", window.id)
        else:
            raise DesktopActionError("window focus is unavailable on this Wayland compositor")

    async def cycle_window(self, scope: str, direction: int = 1) -> None:
        windows = await self.windows(current_app=scope == "app")
        if len(windows) < 2:
            return
        focused = next((index for index, item in enumerate(windows) if item.focused), 0)
        await self.focus_window(windows[(focused + (1 if direction >= 0 else -1)) % len(windows)])

    async def active_window_action(self, action: str) -> None:
        if self.backend == "hyprland":
            commands = {
                "close": ("killactive", "", "hl.dsp.window.close({})"),
                "fullscreen": (
                    "fullscreen",
                    "0",
                    'hl.dsp.window.fullscreen({ mode = "fullscreen", action = "toggle" })',
                ),
                "minimize": (
                    "movetoworkspacesilent",
                    "special:mi-remote-minimized",
                    'hl.dsp.window.move({ workspace = "special:mi-remote-minimized", follow = false })',
                ),
            }
            if action not in commands:
                raise DesktopActionError(f"unknown active window action: {action}")
            await self._hypr_dispatch(*commands[action])
        elif self.backend == "sway":
            commands = {
                "close": ("kill",),
                "fullscreen": ("fullscreen", "toggle"),
                "minimize": ("move", "scratchpad"),
            }
            if action not in commands:
                raise DesktopActionError(f"unknown active window action: {action}")
            await self._run(self.swaymsg, *commands[action])
        elif self.backend == "x11":
            window_id = (await self._capture(self.xdotool, "getactivewindow")).strip()
            if action == "close":
                await self._run(self.xdotool, "windowclose", window_id)
            elif action == "minimize":
                await self._run(self.xdotool, "windowminimize", window_id)
            elif action == "fullscreen":
                await self._run(self.xdotool, "key", "F11")
            else:
                raise DesktopActionError(f"unknown active window action: {action}")
        else:
            raise DesktopActionError("window control is unavailable on this Wayland compositor")

    async def minimize_application_windows(self) -> None:
        windows = await self.windows(current_app=True)
        if not windows:
            return
        if self.backend == "hyprland":
            for window in windows:
                selector = f"address:{window.id}"
                await self._hypr_dispatch(
                    "movetoworkspacesilent",
                    f"special:mi-remote-minimized,address:{window.id}",
                    'hl.dsp.window.move({ workspace = "special:mi-remote-minimized", '
                    f"follow = false, window = {json.dumps(selector)} }})",
                )
        elif self.backend == "sway":
            for window in windows:
                await self._run(self.swaymsg, f"[con_id={window.id}]", "move", "scratchpad")
        elif self.backend == "x11":
            for window in windows:
                await self._run(self.xdotool, "windowminimize", window.id)
        else:
            raise DesktopActionError("window control is unavailable on this Wayland compositor")

    async def workspace(self, direction: int) -> None:
        if self.backend == "hyprland":
            workspace = "r+1" if direction > 0 else "r-1"
            await self._hypr_dispatch(
                "workspace",
                workspace,
                f"hl.dsp.focus({{ workspace = {json.dumps(workspace)} }})",
            )
        elif self.backend == "sway":
            await self._run(self.swaymsg, "workspace", "next" if direction > 0 else "prev")
        else:
            raise DesktopActionError("workspace switching requires Hyprland or Sway IPC")

    async def display_sleep(self) -> None:
        if self.backend == "hyprland":
            await self._hypr_dispatch(
                "dpms",
                "off",
                'hl.dsp.dpms({ action = "disable" })',
            )
        elif self.backend == "sway":
            await self._run(self.swaymsg, "output", "*", "power", "off")
        elif self.backend == "x11":
            xset = shutil.which("xset")
            if not xset:
                raise DesktopActionError("xset is required for X11 display sleep")
            await self._run(xset, "dpms", "force", "off")
        else:
            raise DesktopActionError("display sleep is unavailable on this Wayland compositor")

    async def show_desktop(self) -> None:
        if self.backend == "hyprland":
            if self._desktop_workspace is None:
                active = await self._json(self.hyprctl, "-j", "activeworkspace")
                original = str(active.get("id") or active.get("name") or "")
                if not original:
                    raise DesktopActionError("cannot determine the active Hyprland workspace")
                await self._hypr_dispatch(
                    "workspace",
                    "empty",
                    'hl.dsp.focus({ workspace = "empty" })',
                )
                self._desktop_workspace = original
            else:
                target = self._desktop_workspace
                await self._hypr_dispatch(
                    "workspace",
                    target,
                    f"hl.dsp.focus({{ workspace = {json.dumps(target)} }})",
                )
                self._desktop_workspace = None
        elif self.backend == "sway":
            if self._desktop_workspace is None:
                workspaces = await self._json(self.swaymsg, "-t", "get_workspaces", "-r")
                active = next((row for row in workspaces if row.get("focused")), None)
                if not active or not active.get("name"):
                    raise DesktopActionError("cannot determine the active Sway workspace")
                await self._run(self.swaymsg, "workspace", "__mi_remote_desktop")
                self._desktop_workspace = str(active["name"])
            else:
                target = self._desktop_workspace
                await self._run(self.swaymsg, "workspace", target)
                self._desktop_workspace = None
        elif self.backend == "x11":
            await self._run(self.xdotool, "key", "--clearmodifiers", "super+d")
        else:
            raise DesktopActionError("show desktop is unavailable on this Wayland compositor")

    async def mouse_move(self, dx: int, dy: int) -> None:
        if self.backend != "hyprland":
            raise DesktopActionError("native mouse movement is only available on Hyprland")
        position = await self._json(self.hyprctl, "-j", "cursorpos")
        x = int(position["x"]) + dx
        y = int(position["y"]) + dy
        await self._hypr_dispatch(
            "movecursor",
            f"{x} {y}",
            f"hl.dsp.cursor.move({{ x = {x}, y = {y} }})",
        )

    async def key_stroke(
        self,
        key: str,
        modifiers: tuple[str, ...] = (),
        *,
        hold_delay: float = 0.05,
    ) -> None:
        """Send a focused key stroke through Hyprland's native dispatcher."""
        if self.backend != "hyprland":
            raise DesktopActionError("native key input is only available on Hyprland")

        native_modifiers = " ".join(
            "SUPER" if modifier.lower() == "logo" else modifier.upper() for modifier in modifiers
        )
        if not await self._uses_hypr_lua():
            await self._run(
                self.hyprctl,
                "dispatch",
                "sendshortcut",
                f"{native_modifiers}, {key}, activewindow",
            )
            return

        def expression(state: str) -> str:
            return (
                "hl.dsp.send_key_state({"
                f" mods = {json.dumps(native_modifiers)}, key = {json.dumps(key)},"
                f' state = "{state}", window = "activewindow"'
                " })"
            )

        await self._run(self.hyprctl, "dispatch", expression("down"))
        try:
            if hold_delay:
                await asyncio.sleep(hold_delay)
        finally:
            await self._run(self.hyprctl, "dispatch", expression("up"))

    async def mouse_click(self, button: str) -> None:
        if self.backend != "hyprland":
            raise DesktopActionError("native mouse clicks are only available on Hyprland")
        code = "mouse:272" if button == "left" else "mouse:273"
        if await self._uses_hypr_lua():
            for state in ("down", "up"):
                await self._run(
                    self.hyprctl,
                    "dispatch",
                    f'hl.dsp.send_key_state({{ mods = "", key = "{code}", state = "{state}", '
                    'window = "activewindow" })',
                )
        else:
            await self._run(self.hyprctl, "dispatch", "sendshortcut", f", {code}, activewindow")

    async def open_app(self, desktop_id: str) -> None:
        if not self.gtk_launch:
            raise DesktopActionError("gtk-launch is required for open_app")
        await self._run(self.gtk_launch, desktop_id)

    async def focus_input(self) -> None:
        """Restore focus to the active client; text-widget focus is toolkit-specific on Linux."""
        windows = await self.windows()
        active = next((window for window in windows if window.focused), None)
        if active:
            await self.focus_window(active)

    async def notify(
        self,
        title: str,
        body: str,
        *,
        urgency: str = "normal",
        value: int | None = None,
    ) -> None:
        if not self.notify_send:
            logger.info("%s: %s", title, body)
            return
        command = [
            self.notify_send,
            "-u",
            urgency,
            "-h",
            "string:x-canonical-private-synchronous:mi-remote-linux",
        ]
        if value is not None:
            command.extend(("-h", f"int:value:{max(0, min(100, value))}"))
        command.extend((title, body))
        await self._run(*command)

    async def _json(self, *command: str | None):
        raw = await self._capture(*(item for item in command if item is not None))
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DesktopActionError(f"invalid JSON from {command[0]}: {exc}") from exc

    async def _capture(self, *command: str | None) -> str:
        resolved = tuple(item for item in command if item is not None)
        if not resolved:
            raise DesktopActionError("desktop command is unavailable")
        try:
            process = await asyncio.create_subprocess_exec(
                *resolved,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
            )
        except OSError as exc:
            raise DesktopActionError(f"cannot start {resolved[0]}: {exc}") from exc
        stdout, stderr = await process.communicate()
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()
            raise DesktopActionError(
                f"{resolved[0]} exited with {process.returncode}: {detail or 'unknown error'}"
            )
        return stdout.decode(errors="replace")

    async def _run(self, *command: str | None) -> None:
        await self._capture(*command)

    async def _uses_hypr_lua(self) -> bool:
        if self._hypr_lua is not None:
            return self._hypr_lua
        try:
            version = await self._capture(self.hyprctl, "version")
        except DesktopActionError:
            self._hypr_lua = False
            return False
        match = re.search(r"Hyprland\s+(\d+)\.(\d+)", version)
        self._hypr_lua = bool(match and (int(match.group(1)), int(match.group(2))) >= (0, 55))
        return self._hypr_lua

    async def _hypr_dispatch(
        self,
        legacy_name: str,
        legacy_argument: str | None,
        lua_expression: str,
    ) -> None:
        if await self._uses_hypr_lua():
            await self._run(self.hyprctl, "dispatch", lua_expression)
            return
        await self._run(self.hyprctl, "dispatch", legacy_name, legacy_argument)

    @staticmethod
    def _app_identity(data: dict) -> str | None:
        values = [
            data.get("app_id"),
            data.get("class"),
            data.get("initialClass"),
        ]
        primary = list(dict.fromkeys(str(value) for value in values if value))
        identity = " ".join(primary)
        if not identity:
            identity = str(data.get("name") or data.get("title") or "")
        return identity or None

    def _foreground_identity(self, data: dict) -> str | None:
        identity = self._app_identity(data)
        app_names = {
            str(value).casefold()
            for value in (data.get("app_id"), data.get("class"), data.get("initialClass"))
            if value
        }
        if app_names & TERMINAL_APPLICATIONS:
            try:
                foreground = self._terminal_profile(int(data.get("pid")))
            except (TypeError, ValueError):
                foreground = None
            if foreground:
                return foreground
        return identity

    def _terminal_profile(self, terminal_pid: int) -> str | None:
        """Find a supported interactive CLI below a terminal process in /proc."""
        pending = [terminal_pid]
        visited: set[int] = set()
        while pending and len(visited) < 256:
            pid = pending.pop(0)
            if pid in visited:
                continue
            visited.add(pid)
            process_dir = self.proc_root / str(pid)
            if pid != terminal_pid:
                name = self._process_name(process_dir)
                if name in TERMINAL_PROFILES:
                    return name
            try:
                children = (process_dir / "task" / str(pid) / "children").read_text().split()
            except (OSError, UnicodeError):
                continue
            pending.extend(int(child) for child in children if child.isdecimal())
        return None

    @staticmethod
    def _process_name(process_dir: Path) -> str:
        try:
            comm = (process_dir / "comm").read_text().strip().casefold()
        except (OSError, UnicodeError):
            comm = ""
        if comm in TERMINAL_PROFILES:
            return comm
        try:
            argv0 = (process_dir / "cmdline").read_bytes().split(b"\0", 1)[0]
            return Path(os.fsdecode(argv0)).name.casefold()
        except (OSError, UnicodeError):
            return comm

    @classmethod
    def _focused_sway_node(cls, node: dict) -> dict | None:
        if node.get("focused"):
            return node
        for child in (*node.get("nodes", []), *node.get("floating_nodes", [])):
            found = cls._focused_sway_node(child)
            if found:
                return found
        return None

    @classmethod
    def _collect_sway_windows(cls, node: dict, output: list[DesktopWindow]) -> None:
        if node.get("type") in {"con", "floating_con"} and (
            node.get("app_id") or node.get("window_properties")
        ):
            properties = node.get("window_properties") or {}
            output.append(
                DesktopWindow(
                    id=str(node.get("id")),
                    app_id=str(node.get("app_id") or properties.get("class") or "unknown"),
                    title=str(node.get("name") or "Untitled"),
                    focused=bool(node.get("focused")),
                )
            )
        for child in (*node.get("nodes", []), *node.get("floating_nodes", [])):
            cls._collect_sway_windows(child, output)


class ApplicationTracker:
    def __init__(
        self,
        desktop: LinuxDesktop,
        on_change: Callable[[str | None], None],
        *,
        interval: float = 0.5,
    ):
        self.desktop = desktop
        self.on_change = on_change
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._last: str | None = None

    async def start(self) -> None:
        if self._task is None:
            await self._poll()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self._poll()

    async def _poll(self) -> None:
        try:
            current = await self.desktop.active_application()
        except DesktopActionError as exc:
            logger.debug("active app detection failed: %s", exc)
            current = None
        except Exception:  # 合成器返回的 JSON 结构异常不应终止轮询
            logger.warning("active app detection failed unexpectedly", exc_info=True)
            current = None
        if current != self._last:
            self._last = current
            self.on_change(current)
