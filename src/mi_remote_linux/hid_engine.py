"""RC003 evdev discovery, exclusive capture, and logical button events."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .hid_guard import RC003_PRODUCT_ID, RC003_VENDOR_ID

logger = logging.getLogger(__name__)


# Confirmed on an RC003-MS running firmware 0x00a4 on 2026-08-24.
RC003_KEY_CODES = {
    116: "power",  # KEY_POWER
    63: "voice",  # KEY_F5
    67: "voice",  # KEY_F9 on some reported firmware/remapping stacks
    103: "up",  # KEY_UP
    108: "down",  # KEY_DOWN
    105: "left",  # KEY_LEFT
    106: "right",  # KEY_RIGHT
    28: "ok",  # KEY_ENTER
    158: "back",  # KEY_BACK
    102: "home",  # KEY_HOME
    127: "menu",  # KEY_COMPOSE / HID Application
    41: "tv",  # KEY_GRAVE
    115: "vol_up",  # KEY_VOLUMEUP
    114: "vol_down",  # KEY_VOLUMEDOWN
}


@dataclass(frozen=True)
class ButtonEvent:
    key: str
    is_down: bool
    time_ns: int
    code: int
    value: int


class HIDEngine:
    """Grab only the RC003 input node and survive reconnects/hotplug."""

    def __init__(
        self,
        on_button: Callable[[ButtonEvent], None],
        *,
        on_reset: Callable[[str], None] | None = None,
        key_codes: dict[int, str] | None = None,
        grab: bool = True,
        poll_interval: float = 1.0,
        evdev_module: Any | None = None,
    ):
        self.on_button = on_button
        self.on_reset = on_reset
        self.key_codes = dict(RC003_KEY_CODES)
        if key_codes:
            self.key_codes.update(key_codes)
        self.grab = grab
        self.poll_interval = poll_interval
        self._evdev = evdev_module
        self._devices: dict[str, Any] = {}
        self._observed_paths: set[str] = set()
        self._watch_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._devices))

    async def start(self) -> None:
        if self._watch_task is not None:
            return
        if self._evdev is None:
            try:
                import evdev
            except ImportError as exc:
                raise RuntimeError("Phase B requires the Python evdev package") from exc
            self._evdev = evdev
        self._loop = asyncio.get_running_loop()
        await self._scan_once()
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        task = self._watch_task
        self._watch_task = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for path in list(self._devices):
            self._release(path, "stopped")
        self._observed_paths.clear()
        self._loop = None

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            await self._scan_once()

    async def _scan_once(self) -> None:
        assert self._evdev is not None
        try:
            available = set(self._evdev.list_devices())
        except OSError as exc:
            logger.warning("cannot enumerate input devices: %s", exc)
            return
        self._observed_paths.intersection_update(available)
        for path in set(self._devices) - available:
            self._release(path, "device disconnected")
        for path in sorted(available - self._observed_paths):
            self._observed_paths.add(path)
            self._consider(path)

    def _consider(self, path: str) -> None:
        assert self._evdev is not None
        try:
            device = self._evdev.InputDevice(path)
        except OSError as exc:
            logger.debug("cannot open %s: %s", path, exc)
            self._observed_paths.discard(path)
            return
        if not self._is_rc003(device):
            device.close()
            return
        try:
            if self.grab:
                device.grab()
        except OSError as exc:
            device.close()
            self._observed_paths.discard(path)
            logger.warning("cannot exclusively capture RC003 %s: %s", path, exc)
            return
        self._devices[path] = device
        assert self._loop is not None
        self._loop.add_reader(device.fd, self._drain, path)
        logger.info("RC003 key input ready: %s%s", path, " (exclusive)" if self.grab else "")

    @staticmethod
    def _is_rc003(device: Any) -> bool:
        info = device.info
        return (
            info.vendor == RC003_VENDOR_ID and info.product == RC003_PRODUCT_ID
        ) or "小米蓝牙语音遥控器" in str(device.name or "").casefold()

    def _drain(self, path: str) -> None:
        device = self._devices.get(path)
        if device is None:
            return
        try:
            for raw in device.read():
                if raw.type != self._evdev.ecodes.EV_KEY:
                    continue
                key = self.key_codes.get(raw.code)
                if key is None:
                    logger.debug("unmapped RC003 key code=%d value=%d", raw.code, raw.value)
                    continue
                if raw.value == 2:  # mapping gestures use discrete presses, not kernel repeat
                    continue
                event = ButtonEvent(
                    key=key,
                    is_down=raw.value == 1,
                    time_ns=time.monotonic_ns(),
                    code=raw.code,
                    value=raw.value,
                )
                self.on_button(event)
        except BlockingIOError:
            return
        except OSError as exc:
            logger.warning("RC003 input failed; waiting for rediscovery: %s", exc)
            self._observed_paths.discard(path)
            self._release(path, "read failure")

    def _release(self, path: str, reason: str) -> None:
        device = self._devices.pop(path, None)
        if device is None:
            return
        if self._loop:
            self._loop.remove_reader(device.fd)
        if self.grab:
            try:
                device.ungrab()
            except OSError:
                pass
        device.close()
        if self.on_reset:
            self.on_reset(reason)
        logger.info("RC003 key input released: %s (%s)", path, reason)
