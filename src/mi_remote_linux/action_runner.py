"""Wayland/X11 action execution for Phase B mappings."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Mapping

from .config import Action
from .injector import LinuxTextInjector, TextInjectionError

logger = logging.getLogger(__name__)


MODIFIERS = {
    "ctrl": "ctrl",
    "left_ctrl": "ctrl",
    "shift": "shift",
    "left_shift": "shift",
    "alt": "alt",
    "left_alt": "alt",
    "meta": "logo",
    "super": "logo",
    "left_meta": "logo",
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
}

X11_KEYS = {
    **WAYLAND_KEYS,
    "page_up": "Page_Up",
    "page_down": "Page_Down",
}

MEDIA_KEYS = {
    "volume_up": "XF86AudioRaiseVolume",
    "volume_down": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute",
    "play_pause": "XF86AudioPlay",
    "next": "XF86AudioNext",
    "previous": "XF86AudioPrev",
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
        injector: LinuxTextInjector | None = None,
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
        self.injector = injector or LinuxTextInjector(
            session=session if session != "none" else "auto",
            environment=self.environment,
            wtype=self.wtype,
            xdotool=self.xdotool,
        )

    def missing_dependencies(self) -> list[str]:
        if self.session == "wayland" and not (self.wtype or self.ydotool):
            return ["wtype or ydotool"]
        if self.session == "x11" and not self.xdotool:
            return ["xdotool"]
        if self.session == "none":
            return ["WAYLAND_DISPLAY/DISPLAY"]
        return []

    async def run(self, action: Action) -> None:
        if action.type in {"none", "voice"}:
            return
        if action.type == "key_stroke":
            assert action.key is not None
            await self.key_stroke(action.key, action.mods)
            return
        if action.type == "text":
            if self.session == "wayland" and not self.wtype and self.ydotool:
                if not self.injector.wl_copy:
                    raise ActionError("wl-copy is required for Wayland text actions")
                await self.injector._run(
                    self.injector.wl_copy,
                    "--type",
                    "text/plain;charset=utf-8",
                    input_data=str(action.value or "").encode("utf-8"),
                    clipboard_owner=True,
                )
                if self.injector.paste_delay:
                    await asyncio.sleep(self.injector.paste_delay)
                await self.key_stroke("insert", ("shift",))
                return
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
            if self.wtype:
                command = [self.wtype]
                for modifier in translated:
                    command.extend(("-M", modifier))
                command.extend(("-k", WAYLAND_KEYS.get(key, key)))
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
        raise ActionError(f"unsupported system action: {name}")

    async def _run_command(self, *command: str) -> None:
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
        _stdout, stderr = await process.communicate()
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise ActionError(
                f"{command[0]} exited with {process.returncode}: {detail or 'unknown error'}"
            )
