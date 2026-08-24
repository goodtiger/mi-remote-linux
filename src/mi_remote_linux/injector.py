"""把 UTF-8 转写文本粘贴到当前 Linux 桌面焦点。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Mapping

PASTE_SHORTCUTS = ("auto", "ctrl-v", "ctrl-shift-v", "shift-insert")
TERMINAL_CLASSES = {
    "alacritty",
    "com.mitchellh.ghostty",
    "foot",
    "gnome-terminal-server",
    "kitty",
    "konsole",
    "org.gnome.console",
    "org.wezfurlong.wezterm",
    "terminator",
    "tilix",
    "wezterm",
    "xterm",
}


class TextInjectionError(RuntimeError):
    """焦点文本注入失败。"""

    def __init__(self, message: str, *, clipboard_ready: bool = False):
        super().__init__(message)
        self.clipboard_ready = clipboard_ready


class LinuxTextInjector:
    """通过系统剪贴板粘贴文本，支持 Wayland 和 X11。"""

    def __init__(
        self,
        *,
        session: str = "auto",
        paste_shortcut: str = "auto",
        submit: bool = False,
        environment: Mapping[str, str] | None = None,
        wl_copy: str | None = None,
        wtype: str | None = None,
        xclip: str | None = None,
        xdotool: str | None = None,
        hyprctl: str | None = None,
        paste_delay: float = 0.08,
    ):
        if session not in {"auto", "wayland", "x11"}:
            raise ValueError(f"不支持的图形会话: {session}")
        if paste_shortcut not in PASTE_SHORTCUTS:
            raise ValueError(f"不支持的粘贴快捷键: {paste_shortcut}")

        self.environment = dict(os.environ if environment is None else environment)
        self.session = self._detect_session(session)
        self.paste_shortcut = paste_shortcut
        self.submit = submit
        self.wl_copy = shutil.which("wl-copy") if wl_copy is None else wl_copy
        self.wtype = shutil.which("wtype") if wtype is None else wtype
        self.xclip = shutil.which("xclip") if xclip is None else xclip
        self.xdotool = shutil.which("xdotool") if xdotool is None else xdotool
        self.hyprctl = shutil.which("hyprctl") if hyprctl is None else hyprctl
        self.paste_delay = paste_delay

    def _detect_session(self, requested: str) -> str | None:
        if requested != "auto":
            return requested
        if self.environment.get("WAYLAND_DISPLAY"):
            return "wayland"
        if self.environment.get("DISPLAY"):
            return "x11"
        return None

    def missing_dependencies(self) -> list[str]:
        """返回当前图形会话缺少的命令或环境。"""
        if self.session == "wayland":
            return [
                name
                for name, path in (("wl-copy", self.wl_copy), ("wtype", self.wtype))
                if not path
            ]
        if self.session == "x11":
            return [
                name
                for name, path in (("xclip", self.xclip), ("xdotool", self.xdotool))
                if not path
            ]
        return ["WAYLAND_DISPLAY/DISPLAY"]

    async def inject(self, text: str) -> None:
        """复制 UTF-8 文本并粘贴到当前焦点，可选再按 Enter。"""
        missing = self.missing_dependencies()
        if missing:
            raise TextInjectionError(f"缺少焦点输入环境或工具: {', '.join(missing)}")
        if not text:
            return

        if self.session == "wayland":
            assert self.wl_copy is not None
            await self._run(
                self.wl_copy,
                "--type",
                "text/plain;charset=utf-8",
                input_data=text.encode("utf-8"),
                clipboard_owner=True,
            )
        else:
            assert self.xclip is not None
            await self._run(
                self.xclip,
                "-selection",
                "clipboard",
                "-in",
                input_data=text.encode("utf-8"),
                clipboard_owner=True,
            )

        if self.paste_delay:
            await asyncio.sleep(self.paste_delay)

        try:
            shortcut = await self._resolved_paste_shortcut()
            await self._send_shortcut(shortcut)
            if self.submit:
                if self.paste_delay:
                    await asyncio.sleep(self.paste_delay)
                await self._send_return()
        except TextInjectionError as exc:
            raise TextInjectionError(str(exc), clipboard_ready=True) from exc

    async def _resolved_paste_shortcut(self) -> str:
        if self.paste_shortcut != "auto":
            return self.paste_shortcut
        if await self._active_window_is_terminal():
            return "shift-insert"
        return "ctrl-v"

    async def _active_window_is_terminal(self) -> bool:
        """尽力识别终端；无法识别时安全回落为普通 Ctrl+V。"""
        if self.session == "wayland":
            if not self.hyprctl or not self.environment.get("HYPRLAND_INSTANCE_SIGNATURE"):
                return False
            try:
                raw = await self._capture(self.hyprctl, "activewindow", "-j")
                window = json.loads(raw)
            except (TextInjectionError, json.JSONDecodeError):
                return False
            tags = {str(tag).rstrip("*") for tag in window.get("tags", [])}
            window_class = str(window.get("class", "")).lower()
            return "terminal" in tags or window_class in TERMINAL_CLASSES

        if self.session == "x11" and self.xdotool:
            try:
                window_id = (await self._capture(self.xdotool, "getactivewindow")).strip()
                window_class = (
                    await self._capture(self.xdotool, "getwindowclassname", window_id)
                ).strip()
            except TextInjectionError:
                return False
            return window_class.lower() in TERMINAL_CLASSES
        return False

    async def _send_shortcut(self, shortcut: str) -> None:
        if self.session == "wayland":
            assert self.wtype is not None
            commands = {
                "ctrl-v": (self.wtype, "-M", "ctrl", "-k", "v", "-m", "ctrl"),
                "ctrl-shift-v": (
                    self.wtype,
                    "-M",
                    "ctrl",
                    "-M",
                    "shift",
                    "-k",
                    "v",
                    "-m",
                    "shift",
                    "-m",
                    "ctrl",
                ),
                "shift-insert": (
                    self.wtype,
                    "-M",
                    "shift",
                    "-k",
                    "Insert",
                    "-m",
                    "shift",
                ),
            }
            await self._run(*commands[shortcut])
            return

        assert self.xdotool is not None
        keys = {
            "ctrl-v": "ctrl+v",
            "ctrl-shift-v": "ctrl+shift+v",
            "shift-insert": "shift+Insert",
        }
        await self._run(self.xdotool, "key", "--clearmodifiers", keys[shortcut])

    async def _send_return(self) -> None:
        if self.session == "wayland":
            assert self.wtype is not None
            await self._run(self.wtype, "-k", "Return")
        else:
            assert self.xdotool is not None
            await self._run(self.xdotool, "key", "--clearmodifiers", "Return")

    async def _capture(self, *command: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
            )
        except OSError as exc:
            raise TextInjectionError(f"无法启动 {command[0]}: {exc}") from exc
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise TextInjectionError(
                f"{command[0]} 退出码 {process.returncode}: {detail or '未知错误'}"
            )
        return stdout.decode("utf-8", errors="replace")

    async def _run(
        self,
        *command: str,
        input_data: bytes | None = None,
        clipboard_owner: bool = False,
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE
                if input_data is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                # wl-copy/xclip fork a clipboard owner which inherits open FDs.
                # A PIPE here would never reach EOF while it owns the selection.
                stderr=(asyncio.subprocess.DEVNULL if clipboard_owner else asyncio.subprocess.PIPE),
                env=self.environment,
            )
        except OSError as exc:
            raise TextInjectionError(f"无法启动 {command[0]}: {exc}") from exc

        _stdout, stderr = await process.communicate(input_data)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            raise TextInjectionError(
                f"{command[0]} 退出码 {process.returncode}: {detail or '未知错误'}"
            )
