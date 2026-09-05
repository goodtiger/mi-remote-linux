"""Wayland/X11 action execution for Phase B mappings."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping

from .config import Action
from .desktop import DesktopActionError, LinuxDesktop
from .injector import LinuxTextInjector, TextInjectionError

logger = logging.getLogger(__name__)


MODIFIERS = {
    "ctrl": "ctrl",
    "left_ctrl": "ctrl",
    "right_ctrl": "ctrl",
    "shift": "shift",
    "left_shift": "shift",
    "right_shift": "shift",
    "alt": "alt",
    "left_alt": "alt",
    "right_alt": "alt",
    "left_option": "alt",
    "right_option": "alt",
    "meta": "logo",
    "super": "logo",
    "left_meta": "logo",
    "right_meta": "logo",
    "left_cmd": "ctrl",
    "right_cmd": "ctrl",
}

WAYLAND_KEYS = {
    "return": "Return",
    "enter": "Return",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "insert": "Insert",
    "tab": "Tab",
    "space": "space",
    "home": "Home",
    "end": "End",
    "page_up": "Page_Up",
    "page_down": "Page_Down",
    "up": "Up",
    "up_arrow": "Up",
    "down": "Down",
    "down_arrow": "Down",
    "left": "Left",
    "left_arrow": "Left",
    "right": "Right",
    "right_arrow": "Right",
    "period": "period",
    "comma": "comma",
    "left_bracket": "bracketleft",
    "right_bracket": "bracketright",
}

# X11 目前与 Wayland 共用同一套 keysym 名称；保留独立表以便后续按后端分化。
X11_KEYS = {**WAYLAND_KEYS}

MEDIA_KEYS = {
    "volume_up": "XF86AudioRaiseVolume",
    "volume_down": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute",
    "play_pause": "XF86AudioPlay",
    "next": "XF86AudioNext",
    "previous": "XF86AudioPrev",
    "prev": "XF86AudioPrev",
}

YDOTOOL_KEYS = {
    "return": 28,
    "enter": 28,
    "escape": 1,
    "backspace": 14,
    "delete": 111,
    "insert": 110,
    "tab": 15,
    "space": 57,
    "home": 102,
    "end": 107,
    "page_up": 104,
    "page_down": 109,
    "up": 103,
    "up_arrow": 103,
    "down": 108,
    "down_arrow": 108,
    "left": 105,
    "left_arrow": 105,
    "right": 106,
    "right_arrow": 106,
    "XF86AudioRaiseVolume": 115,
    "XF86AudioLowerVolume": 114,
    "XF86AudioMute": 113,
    "XF86AudioPlay": 164,
    "XF86AudioNext": 163,
    "XF86AudioPrev": 165,
}

YDOTOOL_MODIFIERS = {"ctrl": 29, "shift": 42, "alt": 56, "logo": 125}


class ActionError(RuntimeError):
    """An action could not be executed."""


class LinuxActionRunner:
    """Execute typed actions without invoking a shell."""

    def __init__(
        self,
        *,
        session: str = "auto",
        environment: Mapping[str, str] | None = None,
        wtype: str | None = None,
        ydotool: str | None = None,
        xdotool: str | None = None,
        wpctl: str | None = None,
        pactl: str | None = None,
        playerctl: str | None = None,
        injector: LinuxTextInjector | None = None,
        desktop: LinuxDesktop | None = None,
        overlay_handler: Callable[[str], Awaitable[None]] | None = None,
        mouse_mode_handler: Callable[[], Awaitable[None]] | None = None,
    ):
        self.environment = dict(os.environ if environment is None else environment)
        if session == "auto":
            session = (
                "wayland"
                if self.environment.get("WAYLAND_DISPLAY")
                else ("x11" if self.environment.get("DISPLAY") else "none")
            )
        if session not in {"wayland", "x11", "none"}:
            raise ValueError(f"unsupported graphical session: {session}")
        self.session = session
        self.wtype = shutil.which("wtype") if wtype is None else wtype
        self.ydotool = shutil.which("ydotool") if ydotool is None else ydotool
        self.xdotool = shutil.which("xdotool") if xdotool is None else xdotool
        self.wpctl = shutil.which("wpctl") if wpctl is None else wpctl
        self.pactl = shutil.which("pactl") if pactl is None else pactl
        self.playerctl = shutil.which("playerctl") if playerctl is None else playerctl
        self.desktop = desktop or LinuxDesktop(
            environment=self.environment,
            xdotool=self.xdotool,
        )
        self.injector = injector or LinuxTextInjector(
            session=session if session != "none" else "auto",
            environment=self.environment,
            wtype=self.wtype,
            ydotool=self.ydotool,
            xdotool=self.xdotool,
            desktop=self.desktop,
        )
        self.overlay_handler = overlay_handler
        self.mouse_mode_handler = mouse_mode_handler

    def missing_dependencies(self) -> list[str]:
        if (
            self.session == "wayland"
            and getattr(self.desktop, "backend", None) != "hyprland"
            and not (self.wtype or self.ydotool)
        ):
            return ["wtype or ydotool"]
        if self.session == "x11" and not self.xdotool:
            return ["xdotool"]
        if self.session == "none":
            return ["WAYLAND_DISPLAY/DISPLAY"]
        return []

    @property
    def supports_mouse(self) -> bool:
        return bool(
            (self.session == "x11" and self.xdotool)
            or self.ydotool
            or self.desktop.backend == "hyprland"
        )

    async def run(self, action: Action) -> None:
        if action.type in {"none", "voice"}:
            return
        if action.type == "key_stroke":
            assert action.key is not None
            await self.key_stroke(action.key, action.mods)
            return
        if action.type == "text":
            try:
                await self.injector.inject(str(action.value or ""))
            except TextInjectionError as exc:
                raise ActionError(str(exc)) from exc
            return
        if action.type == "command":
            await self._run_command(*action.argv)
            return
        if action.type == "system":
            await self.system(str(action.value))
            return
        if action.type == "open_app":
            try:
                await self.desktop.open_app(str(action.value))
            except DesktopActionError as exc:
                raise ActionError(str(exc)) from exc
            return
        if action.type == "window_cycle":
            try:
                await self.desktop.cycle_window(action.scope or "app")
            except DesktopActionError as exc:
                raise ActionError(str(exc)) from exc
            return
        if action.type == "tab_jump":
            if action.index is not None:
                await self.key_stroke(str(action.index), ("ctrl",))
            else:
                await self.key_stroke(
                    "page_down" if (action.direction or 1) > 0 else "page_up",
                    ("ctrl",),
                )
            return
        if action.type == "focus_input":
            try:
                await self.desktop.focus_input()
            except DesktopActionError as exc:
                raise ActionError(str(exc)) from exc
            return
        if action.type == "mouse_mode":
            if not self.mouse_mode_handler:
                raise ActionError("mouse mode is not connected")
            await self.mouse_mode_handler()
            return
        if action.type == "overlay":
            if not self.overlay_handler:
                raise ActionError("overlay UI is not connected")
            await self.overlay_handler(str(action.value))
            return
        if action.type == "macro":
            for step in action.steps:
                if step.type == "delay":
                    await asyncio.sleep(step.delay_ms / 1000)
                else:
                    await self.run(step)
            return
        raise ActionError(f"action type must be handled by mapping engine: {action.type}")

    async def key_stroke(self, key: str, modifiers: tuple[str, ...] = ()) -> None:
        unknown = [modifier for modifier in modifiers if modifier not in MODIFIERS]
        if unknown:
            raise ActionError(f"unknown modifiers: {', '.join(unknown)}")
        if len(set(modifiers)) != len(modifiers):
            raise ActionError("duplicate modifiers are not allowed")
        if self.session == "wayland":
            translated = [MODIFIERS[item] for item in modifiers]
            native_key = WAYLAND_KEYS.get(key, key)
            if getattr(self.desktop, "backend", None) == "hyprland":
                try:
                    await self.desktop.key_stroke(native_key, tuple(translated))
                    return
                except DesktopActionError as exc:
                    logger.debug("Hyprland native key input failed; using fallback: %s", exc)
            if self.wtype:
                command = [self.wtype]
                for modifier in translated:
                    command.extend(("-M", modifier))
                command.extend(("-k", native_key))
                for modifier in reversed(translated):
                    command.extend(("-m", modifier))
                await self._run_command(*command)
            elif self.ydotool:
                code = self._ydotool_code(key)
                modifier_codes = [YDOTOOL_MODIFIERS[item] for item in translated]
                sequence = [*(f"{item}:1" for item in modifier_codes), f"{code}:1", f"{code}:0"]
                sequence.extend(f"{item}:0" for item in reversed(modifier_codes))
                await self._run_command(self.ydotool, "key", *sequence)
            else:
                raise ActionError("wtype or ydotool is required for Wayland key actions")
            return
        if self.session == "x11":
            if not self.xdotool:
                raise ActionError("xdotool is required for X11 key actions")
            translated = [MODIFIERS[item].replace("logo", "super") for item in modifiers]
            chord = "+".join((*translated, X11_KEYS.get(key, key)))
            await self._run_command(self.xdotool, "key", "--clearmodifiers", chord)
            return
        raise ActionError("no graphical session is available")

    @staticmethod
    def _ydotool_code(key: str) -> int:
        if key in YDOTOOL_KEYS:
            return YDOTOOL_KEYS[key]
        translated = WAYLAND_KEYS.get(key, key)
        if translated in YDOTOOL_KEYS:
            return YDOTOOL_KEYS[translated]
        if len(translated) == 1 and translated.isascii():
            if translated.isalpha():
                # Linux KEY_A..KEY_Z are not contiguous; use evdev's canonical table.
                from evdev import ecodes

                return int(ecodes.ecodes[f"KEY_{translated.upper()}"])
            if translated.isdigit():
                from evdev import ecodes

                return int(ecodes.ecodes[f"KEY_{translated}"])
        if translated.lower().startswith("f") and translated[1:].isdigit():
            from evdev import ecodes

            name = f"KEY_F{int(translated[1:])}"
            if name in ecodes.ecodes:
                return int(ecodes.ecodes[name])
        raise ActionError(f"key is not supported by ydotool backend: {key}")

    async def system(self, name: str) -> None:
        if name in {"volume_up", "volume_down", "mute"}:
            if self.wpctl:
                if name == "mute":
                    await self._run_command(
                        self.wpctl, "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"
                    )
                else:
                    step = "5%+" if name == "volume_up" else "5%-"
                    await self._run_command(
                        self.wpctl,
                        "set-volume",
                        "--limit",
                        "1.0",
                        "@DEFAULT_AUDIO_SINK@",
                        step,
                    )
                await self._show_volume_feedback(name)
                return
            if self.pactl:
                if name == "mute":
                    await self._run_command(self.pactl, "set-sink-mute", "@DEFAULT_SINK@", "toggle")
                else:
                    step = "+5%" if name == "volume_up" else "-5%"
                    await self._run_command(self.pactl, "set-sink-volume", "@DEFAULT_SINK@", step)
                await self._show_volume_feedback(name)
                return
        if name in {"play_pause", "next", "prev", "previous"} and self.playerctl:
            operation = {
                "play_pause": "play-pause",
                "next": "next",
                "prev": "previous",
                "previous": "previous",
            }[name]
            await self._run_command(self.playerctl, operation)
            return
        media_key = MEDIA_KEYS.get(name)
        if media_key:
            await self.key_stroke(media_key)
            return
        if name == "lock_screen":
            loginctl = shutil.which("loginctl")
            if not loginctl:
                raise ActionError("loginctl is required for lock_screen")
            await self._run_command(loginctl, "lock-session")
            return
        try:
            if name == "display_sleep":
                await self.desktop.display_sleep()
            elif name == "space_left":
                await self.desktop.workspace(-1)
            elif name == "space_right":
                await self.desktop.workspace(1)
            elif name in {"next_app", "next_global_window"}:
                await self.desktop.cycle_window("global", 1)
            elif name in {"previous_app", "previous_global_window", "app_mru_back"}:
                await self.desktop.cycle_window("global", -1)
            elif name in {"next_app_window", "focus_next_window"}:
                await self.desktop.cycle_window("app", 1)
            elif name in {"previous_app_window", "focus_previous_window"}:
                await self.desktop.cycle_window("app", -1)
            elif name in {"mission_control", "app_expose", "launchpad", "spotlight"}:
                if not self.overlay_handler:
                    raise ActionError(f"{name} requires overlay UI")
                overlay = {
                    "mission_control": "mission_control",
                    "app_expose": "app_expose",
                    "launchpad": "app_launcher",
                    "spotlight": "app_launcher",
                }[name]
                await self.overlay_handler(overlay)
            elif name == "show_desktop":
                await self.desktop.show_desktop()
            elif name == "screenshot":
                await self.key_stroke("s", ("super", "shift"))
            elif name == "focus_input":
                await self.desktop.focus_input()
            elif name == "minimize_window":
                await self.desktop.active_window_action("minimize")
            elif name in {"minimize_app_windows", "hide_app"}:
                await self.desktop.minimize_application_windows()
            elif name == "close_window":
                await self.desktop.active_window_action("close")
            elif name == "toggle_full_screen":
                await self.desktop.active_window_action("fullscreen")
            elif name == "hide_other_apps":
                raise ActionError("hide_other_apps has no portable Linux desktop equivalent")
            elif name in {
                "focus_menu_bar",
                "focus_dock",
                "focus_toolbar",
                "focus_next_floating_window",
                "focus_previous_floating_window",
                "focus_status_menus",
                "notification_center",
                "control_center",
            }:
                raise ActionError(f"{name} has no portable Linux desktop equivalent")
            else:
                raise ActionError(f"unsupported system action: {name}")
        except DesktopActionError as exc:
            raise ActionError(str(exc)) from exc
        return

    async def _show_volume_feedback(self, name: str) -> None:
        labels = {
            "volume_up": "音量已提高",
            "volume_down": "音量已降低",
            "mute": "静音状态已切换",
        }
        body = labels[name]
        value = None
        if self.wpctl:
            try:
                output = await self._capture_command(
                    self.wpctl, "get-volume", "@DEFAULT_AUDIO_SINK@"
                )
                match = re.search(r"Volume:\s+([0-9.]+)", output)
                if match:
                    value = round(float(match.group(1)) * 100)
                    muted = "[MUTED]" in output
                    if name == "mute":
                        body = "已静音" if muted else "已取消静音"
                    else:
                        body = f"音量 {value}%" + ("（静音中）" if muted else "")
            except ActionError:
                logger.debug("cannot read volume for notification", exc_info=True)
        try:
            await self.desktop.notify("MiRemote", body, value=value)
        except DesktopActionError:
            logger.debug("cannot show volume notification", exc_info=True)

    async def mouse_move(self, dx: int, dy: int) -> None:
        if not dx and not dy:
            return
        if self.session == "x11" and self.xdotool:
            await self._run_command(
                self.xdotool,
                "mousemove_relative",
                "--sync",
                "--",
                str(dx),
                str(dy),
            )
            return
        if self.ydotool:
            await self._run_command(self.ydotool, "mousemove", "-x", str(dx), "-y", str(dy))
            return
        if self.desktop.backend == "hyprland":
            try:
                await self.desktop.mouse_move(dx, dy)
            except DesktopActionError as exc:
                raise ActionError(str(exc)) from exc
            return
        raise ActionError("mouse mode requires xdotool (X11) or ydotool (Wayland)")

    async def mouse_click(self, button: str) -> None:
        number = "1" if button == "left" else "3"
        if self.session == "x11" and self.xdotool:
            await self._run_command(self.xdotool, "click", number)
            return
        if self.ydotool:
            # ydotool click 自有编码：0x40 按下、0x80 抬起，低位 0 左键、1 右键。
            code = "0xC0" if button == "left" else "0xC1"
            await self._run_command(self.ydotool, "click", code)
            return
        if self.desktop.backend == "hyprland":
            try:
                await self.desktop.mouse_click(button)
            except DesktopActionError as exc:
                raise ActionError(str(exc)) from exc
            return
        raise ActionError("mouse mode requires xdotool (X11) or ydotool (Wayland)")

    async def _run_command(self, *command: str, timeout: float = 10.0) -> None:
        if not command:
            raise ActionError("empty command")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
            )
        except OSError as exc:
            raise ActionError(f"cannot start {command[0]}: {exc}") from exc
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise ActionError(f"{command[0]} timed out after {timeout:g}s") from exc
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise ActionError(
                f"{command[0]} exited with {process.returncode}: {detail or 'unknown error'}"
            )

    async def _capture_command(self, *command: str, timeout: float = 10.0) -> str:
        if not command:
            raise ActionError("empty command")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
            )
        except OSError as exc:
            raise ActionError(f"cannot start {command[0]}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise ActionError(f"{command[0]} timed out after {timeout:g}s") from exc
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise ActionError(
                f"{command[0]} exited with {process.returncode}: {detail or 'unknown error'}"
            )
        return stdout.decode("utf-8", errors="replace")
