"""Remote-controlled Linux overlays and mouse mode."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path

from .action_runner import ActionError, LinuxActionRunner
from .config import Action
from .desktop import DesktopActionError, DesktopWindow, LinuxDesktop
from .hid_engine import ButtonEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OverlayEntry:
    label: str
    action: Action | None = None
    window: DesktopWindow | None = None
    dangerous: bool = False


class OverlayManager:
    """Dependency-free modal UI: notifications show state; remote keys drive it."""

    IDLE_SECONDS = 20
    WHEEL_IDLE_SECONDS = 3
    CONFIRM_SECONDS = 0.6

    def __init__(self, desktop: LinuxDesktop, runner: LinuxActionRunner):
        self.desktop = desktop
        self.runner = runner
        self.name: str | None = None
        self.entries: list[OverlayEntry] = []
        self.index = 0
        self.current_app_only = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._idle: asyncio.TimerHandle | None = None
        self._confirm: asyncio.TimerHandle | None = None
        self._confirming_index: int | None = None

    @property
    def active(self) -> bool:
        return self.name is not None

    async def open(self, name: str) -> None:
        try:
            entries = await self._entries(name)
        except DesktopActionError as exc:
            await self.desktop.notify("MiRemote", str(exc), urgency="critical")
            return
        self.name = name
        self.entries = entries or [OverlayEntry("没有可用项目")]
        self.index = 0
        self.current_app_only = name == "app_expose"
        self._restart_idle()
        await self._show()

    async def close(self, reason: str = "") -> None:
        if not self.active:
            return
        self.name = None
        self.entries = []
        self.index = 0
        self.current_app_only = False
        self._cancel_timers()
        if reason:
            await self.desktop.notify("MiRemote", reason)

    async def shutdown(self) -> None:
        await self.close()
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def handle(self, event: ButtonEvent) -> bool:
        if not self.active:
            return False
        self._restart_idle()
        if event.key == "ok" and not event.is_down and self._confirming_index is not None:
            self._cancel_confirmation(show=True)
            return True
        if not event.is_down:
            return True
        if self.name == "tutorial":
            if event.key in {"home", "back", "ok"}:
                self._spawn(self.close("已关闭"))
            return True
        if self.name == "system_menu":
            self._handle_system_menu(event.key)
            return True
        if self.name in {"window_picker", "mission_control", "app_expose"}:
            self._handle_window_picker(event.key)
            return True
        if self.name == "app_wheel":
            self._handle_app_wheel(event.key)
            return True
        if event.key in {"back", "home"}:
            self._spawn(self.close("已关闭"))
        elif event.key in {"up", "left", "vol_down"}:
            self._step(-1)
        elif event.key in {"down", "right", "vol_up"}:
            self._step(1)
        elif event.key in {"ok", "tv"}:
            self._spawn(self._activate())
        return True

    async def show_layer(self, layer: int, profile: str | None) -> None:
        if layer == 2:
            await self.desktop.notify(
                "MiRemote · App 控制模式",
                f"Profile: {profile or 'global'}\n↑↓选择  ←→切标签  OK批准  返回拒绝\n"
                "菜单 Shift+Tab  Home Ctrl+C  TV退出",
            )
        elif layer:
            await self.desktop.notify("MiRemote", f"已进入控制层 {layer}")
        else:
            await self.desktop.notify("MiRemote", "已回到基础层")

    async def _entries(self, name: str) -> list[OverlayEntry]:
        if name in {"window_picker", "mission_control", "app_wheel", "app_expose"}:
            current = name == "app_expose"
            windows = await self.desktop.windows(current_app=current)
            if name == "app_wheel":
                seen: set[str] = set()
                windows = [
                    item
                    for item in windows
                    if not (item.app_id.casefold() in seen or seen.add(item.app_id.casefold()))
                ]
            return [OverlayEntry(f"{item.app_id} — {item.title}", window=item) for item in windows]
        if name == "system_menu":
            return [
                OverlayEntry("任务视图", Action(type="system", value="mission_control")),
                OverlayEntry("当前 App 窗口", Action(type="system", value="app_expose")),
                OverlayEntry("显示桌面", Action(type="system", value="show_desktop")),
                OverlayEntry("左侧工作区", Action(type="system", value="space_left")),
                OverlayEntry("右侧工作区", Action(type="system", value="space_right")),
                OverlayEntry("聚焦编辑区", Action(type="focus_input")),
                OverlayEntry("播放 / 暂停", Action(type="system", value="play_pause")),
                OverlayEntry("静音", Action(type="system", value="mute")),
                OverlayEntry("使用与配置说明", Action(type="overlay", value="open_settings")),
                OverlayEntry(
                    "退出当前 App",
                    Action(type="key_stroke", key="q", mods=("ctrl",)),
                    dangerous=True,
                ),
                OverlayEntry(
                    "锁定屏幕", Action(type="system", value="lock_screen"), dangerous=True
                ),
                OverlayEntry(
                    "关闭显示器",
                    Action(type="system", value="display_sleep"),
                    dangerous=True,
                ),
            ]
        if name == "tutorial":
            return [
                OverlayEntry("方向键：内容导航"),
                OverlayEntry("OK：确认 / Enter；返回：删除"),
                OverlayEntry("Home：任务视图；菜单：窗口选择器"),
                OverlayEntry("TV：进入 App 控制模式"),
                OverlayEntry("电源长按：鼠标模式"),
                OverlayEntry("长按菜单 1.5 秒：紧急退出"),
            ]
        if name == "open_settings":
            return [
                OverlayEntry("导出默认配置：mi-remote config show > my-remote.json"),
                OverlayEntry("校验配置：mi-remote config validate my-remote.json"),
                OverlayEntry("使用配置：mi-remote voice --config my-remote.json --inject"),
            ]
        if name == "app_launcher":
            return self._desktop_apps()
        raise DesktopActionError(f"unknown overlay: {name}")

    def _desktop_apps(self) -> list[OverlayEntry]:
        entries: dict[str, OverlayEntry] = {}
        directories = [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.glob("*.desktop"):
                name = path.stem
                try:
                    for line in path.read_text(errors="replace").splitlines():
                        if line.startswith("Name="):
                            name = line.partition("=")[2].strip() or name
                            break
                except OSError:
                    continue
                entries[path.name] = OverlayEntry(
                    name,
                    Action(type="open_app", value=path.name.removesuffix(".desktop")),
                )
        return sorted(entries.values(), key=lambda item: item.label.casefold())

    async def _activate(self) -> None:
        if not self.entries:
            await self.close()
            return
        entry = self.entries[self.index]
        await self.close()
        if entry.window:
            await self.desktop.focus_window(entry.window)
        elif entry.action:
            await self.runner.run(entry.action)

    def _step(self, delta: int) -> None:
        if not self.entries:
            return
        self.index = (self.index + delta) % len(self.entries)
        self._spawn(self._show())

    def _handle_window_picker(self, key: str) -> None:
        if key == "left":
            self._step(-1)
        elif key == "right":
            self._step(1)
        elif key in {"up", "down"}:
            self._spawn(self._set_window_scope(not self.current_app_only))
        elif key == "menu":
            if self.current_app_only:
                self._spawn(self.close("已关闭"))
            else:
                self._spawn(self._set_window_scope(True))
        elif key == "ok":
            self._spawn(self._activate())
        elif key in {"back", "home"}:
            self._spawn(self.close("已关闭"))

    async def _set_window_scope(self, current_app_only: bool) -> None:
        self.current_app_only = current_app_only
        try:
            windows = await self.desktop.windows(current_app=current_app_only)
        except DesktopActionError as exc:
            await self.desktop.notify("MiRemote", str(exc), urgency="critical")
            return
        self.entries = [
            OverlayEntry(f"{item.app_id} — {item.title}", window=item) for item in windows
        ] or [OverlayEntry("没有可用项目")]
        self.index = 0
        await self._show()

    def _handle_app_wheel(self, key: str) -> None:
        if key in {"left", "up"}:
            self._step(-1)
        elif key in {"right", "down"}:
            self._step(1)
        elif key in {"ok", "tv"}:
            self._spawn(self._activate())
        elif key in {"back", "home", "menu"}:
            self._spawn(self.close("已关闭"))

    def _handle_system_menu(self, key: str) -> None:
        if key in {"up", "down", "left", "right"}:
            self._cancel_confirmation(show=False)
            self.index = self._grid_move(self.index, key, len(self.entries))
            self._spawn(self._show())
        elif key == "ok":
            entry = self.entries[self.index]
            if entry.dangerous:
                self._begin_confirmation()
            else:
                self._spawn(self._activate())
        elif key in {"back", "home", "menu"}:
            self._spawn(self.close("已关闭"))

    @staticmethod
    def _grid_move(index: int, key: str, count: int) -> int:
        if count <= 0:
            return 0
        if key == "left":
            return (index - 1) % count
        if key == "right":
            return (index + 1) % count
        row, column = divmod(index, 3)
        target_row = max(0, row - 1) if key == "up" else min((count - 1) // 3, row + 1)
        return min(target_row * 3 + column, count - 1)

    def _begin_confirmation(self) -> None:
        if self._confirming_index is not None:
            return
        self._confirming_index = self.index
        self._spawn(self._show())
        loop = asyncio.get_running_loop()
        self._confirm = loop.call_later(
            self.CONFIRM_SECONDS,
            lambda: self._spawn(self._confirm_action()),
        )

    async def _confirm_action(self) -> None:
        index = self._confirming_index
        self._confirming_index = None
        self._confirm = None
        if index is None or not self.active or index != self.index:
            return
        await self._activate()

    def _cancel_confirmation(self, *, show: bool) -> None:
        if self._confirm:
            self._confirm.cancel()
            self._confirm = None
        changed = self._confirming_index is not None
        self._confirming_index = None
        if changed and show and self.active:
            self._spawn(self._show())

    def _restart_idle(self) -> None:
        if self._idle:
            self._idle.cancel()
        seconds = self.WHEEL_IDLE_SECONDS if self.name == "app_wheel" else self.IDLE_SECONDS
        self._idle = asyncio.get_running_loop().call_later(
            seconds,
            lambda: self._spawn(self.close("浮层空闲超时")),
        )

    def _cancel_timers(self) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        self._cancel_confirmation(show=False)

    async def _show(self) -> None:
        if not self.active or not self.entries:
            return
        title = {
            "window_picker": "窗口选择器",
            "mission_control": "任务视图",
            "app_expose": "当前 App 窗口",
            "app_wheel": "App 轮盘",
            "system_menu": "系统功能菜单",
            "tutorial": "MiRemote 教程",
            "app_launcher": "应用启动器",
        }.get(self.name or "", "MiRemote")
        entry = self.entries[self.index]
        suffix = (
            f"\n按住 OK {self.CONFIRM_SECONDS:g} 秒确认"
            if entry.dangerous and self._confirming_index is None
            else ("\n继续按住 OK…" if self._confirming_index is not None else "")
        )
        await self.desktop.notify(
            f"{title}  {self.index + 1}/{len(self.entries)}",
            f"› {entry.label}\n方向/音量选择 · OK确认 · 返回关闭{suffix}",
        )

    def _spawn(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error("overlay action failed: %s", error)


class MouseMode:
    BASE_SPEED = 4
    MAX_SPEED = 40
    RAMP_SECONDS = 1.5
    IDLE_SECONDS = 20

    def __init__(self, runner: LinuxActionRunner, desktop: LinuxDesktop):
        self.runner = runner
        self.desktop = desktop
        self.active = False
        self._held: set[str] = set()
        self._hold_started = 0.0
        self._move_task: asyncio.Task[None] | None = None
        self._idle: asyncio.TimerHandle | None = None

    async def toggle(self) -> None:
        if self.active:
            await self.deactivate("已退出鼠标模式")
            return
        if not self.runner.supports_mouse:
            raise ActionError("Wayland 鼠标模式需要 ydotool；X11 需要 xdotool")
        self.active = True
        self._restart_idle()
        await self.desktop.notify(
            "MiRemote · 鼠标模式",
            "方向键移动 · OK左键 · 菜单右键 · 返回退出",
        )

    async def deactivate(self, message: str = "") -> None:
        if not self.active:
            return
        self.active = False
        self._held.clear()
        if self._idle:
            self._idle.cancel()
            self._idle = None
        task = self._move_task
        self._move_task = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if message:
            await self.desktop.notify("MiRemote", message)

    def handle(self, event: ButtonEvent) -> bool:
        if not self.active:
            return False
        if event.key in {"up", "down", "left", "right"}:
            if event.is_down:
                if not self._held:
                    self._hold_started = time.monotonic()
                self._held.add(event.key)
                if self._move_task is None:
                    self._move_task = asyncio.create_task(self._move_loop())
            else:
                self._held.discard(event.key)
            self._restart_idle()
            return True
        if event.key == "ok":
            if event.is_down:
                asyncio.create_task(self.runner.mouse_click("left"))
            self._restart_idle()
            return True
        if event.key == "menu":
            if event.is_down:
                asyncio.create_task(self.runner.mouse_click("right"))
            self._restart_idle()
            return True
        if event.key == "back":
            if event.is_down:
                asyncio.create_task(self.deactivate("已退出鼠标模式"))
            return True
        return False

    async def _move_loop(self) -> None:
        try:
            while self.active:
                if self._held:
                    held_for = time.monotonic() - self._hold_started
                    ratio = min(1.0, held_for / self.RAMP_SECONDS)
                    speed = round(self.BASE_SPEED + (self.MAX_SPEED - self.BASE_SPEED) * ratio)
                    dx = speed * (("right" in self._held) - ("left" in self._held))
                    dy = speed * (("down" in self._held) - ("up" in self._held))
                    await self.runner.mouse_move(dx, dy)
                await asyncio.sleep(0.05)
        except ActionError as exc:
            logger.error("mouse movement failed: %s", exc)
            self.active = False
            self._held.clear()
            if self._idle:
                self._idle.cancel()
                self._idle = None
            await self.desktop.notify("MiRemote", f"鼠标模式已退出：{exc}", urgency="critical")
        finally:
            self._move_task = None

    def _restart_idle(self) -> None:
        if self._idle:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(
            self.IDLE_SECONDS,
            self._idle_expired,
        )

    def _idle_expired(self) -> None:
        self._idle = None
        if not self.active:
            return
        if self._held:
            self._restart_idle()
            return
        asyncio.create_task(self.deactivate("鼠标模式空闲超时"))
