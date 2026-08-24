"""按输入设备隔离 RC003 语音键，避免影响物理键盘 F9。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

RC003_VENDOR_ID = 0x2717
RC003_PRODUCT_ID = 0x32B8
HID_GRAB_MODES = ("off", "safe", "force")


class RemoteHIDGuard:
    """独占带 F9 的 RC003 event 节点，并持续处理热插拔。"""

    def __init__(
        self,
        mode: str = "safe",
        *,
        poll_interval: float = 1.0,
        evdev_module: Any | None = None,
        uinput_factory: Any | None = None,
    ):
        if mode not in HID_GRAB_MODES:
            raise ValueError(f"不支持的 HID 隔离模式: {mode}")
        self.mode = mode
        self.poll_interval = poll_interval
        self._evdev = evdev_module
        self._uinput_factory = uinput_factory
        self._devices: dict[str, Any] = {}
        self._relays: dict[str, Any] = {}
        self._observed_paths: set[str] = set()
        self._warned: set[str] = set()
        self._watch_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def grabbed_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._devices))

    async def start(self) -> None:
        if self.mode == "off" or self._watch_task is not None:
            return
        if self._evdev is None:
            try:
                import evdev
            except ImportError:
                logger.warning("未安装 evdev，无法隔离遥控器 F9")
                return
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
            self._release(path)
        self._observed_paths.clear()
        self._loop = None

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            await self._scan_once()

    async def _scan_once(self) -> None:
        if self._evdev is None:
            return
        available = set(self._evdev.list_devices())
        self._observed_paths.intersection_update(available)
        for path in set(self._devices) - available:
            self._release(path)
        for path in sorted(available - self._observed_paths):
            self._observed_paths.add(path)
            self._consider(path)

    def _consider(self, path: str) -> None:
        assert self._evdev is not None
        try:
            device = self._evdev.InputDevice(path)
        except OSError as exc:
            self._warn_once(path, "无法打开输入节点 %s: %s", path, exc)
            return

        if not self._is_rc003(device):
            device.close()
            return

        try:
            keys = set(device.capabilities().get(self._evdev.ecodes.EV_KEY, []))
        except OSError as exc:
            self._warn_once(path, "无法读取输入节点能力 %s: %s", path, exc)
            device.close()
            return
        f9 = self._evdev.ecodes.KEY_F9
        if f9 not in keys:
            device.close()
            return
        other_keys = keys - {f9}
        relay = self._create_relay(device) if other_keys else None
        if self.mode == "safe" and other_keys and relay is None:
            device.close()
            return

        try:
            device.grab()
        except OSError as exc:
            self._warn_once(
                path,
                "无法独占 RC003 输入节点 %s: %s（请检查 input 组权限）",
                path,
                exc,
            )
            if relay:
                relay.close()
            device.close()
            return

        self._devices[path] = device
        if relay:
            self._relays[path] = relay
        assert self._loop is not None
        self._loop.add_reader(device.fd, self._drain, path)
        if relay:
            logger.info("已隔离 RC003 F9，并通过 uinput 转发该节点上的其他按键: %s", path)
        elif other_keys:
            logger.warning("已独占 RC003 节点 %s；其他遥控器按键暂时被屏蔽", path)
        else:
            logger.info("已隔离 RC003 语音键 F9: %s", path)

    def _create_relay(self, device: Any) -> Any | None:
        assert self._evdev is not None
        factory = self._uinput_factory
        if factory is None:
            try:
                factory = self._evdev.UInput.from_device
            except AttributeError:
                factory = None
        if factory is None:
            self._warn_once(
                device.path,
                "RC003 F9 与其他键共用 %s，但当前无法创建 uinput 转发设备",
                device.path,
            )
            return None
        try:
            return factory(
                device,
                name="MiRemote RC003 filtered",
                vendor=1,
                product=1,
                version=1,
            )
        except Exception as exc:  # noqa: BLE001 - evdev 使用不继承 OSError 的 UInputError
            self._warn_once(
                device.path,
                "RC003 F9 与其他键共用 %s，无法创建 uinput 转发设备: %s",
                device.path,
                exc,
            )
            return None

    def _is_rc003(self, device: Any) -> bool:
        info = device.info
        if info.vendor == RC003_VENDOR_ID and info.product == RC003_PRODUCT_ID:
            return True
        normalized_name = str(device.name or "").casefold()
        return "小米蓝牙语音遥控器" in normalized_name

    def _drain(self, path: str) -> None:
        device = self._devices.get(path)
        if device is None:
            return
        try:
            for event in device.read():
                if (
                    self._evdev
                    and event.type == self._evdev.ecodes.EV_KEY
                    and event.code == self._evdev.ecodes.KEY_F9
                ):
                    logger.debug("已拦截 RC003 F9: value=%d", event.value)
                    continue
                relay = self._relays.get(path)
                if relay:
                    relay.write_event(event)
        except BlockingIOError:
            return
        except Exception as exc:  # noqa: BLE001 - evdev/uinput 后端异常类型不同
            logger.warning("RC003 输入转发失败，释放设备等待重试: %s", exc)
            self._observed_paths.discard(path)
            self._release(path)

    def _release(self, path: str) -> None:
        device = self._devices.pop(path, None)
        relay = self._relays.pop(path, None)
        if device is None:
            if relay:
                relay.close()
            return
        if self._loop:
            self._loop.remove_reader(device.fd)
        try:
            device.ungrab()
        except OSError:
            pass
        device.close()
        if relay:
            relay.close()
        logger.info("已释放 RC003 输入节点: %s", path)

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message, *args)
