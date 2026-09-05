"""Lifecycle glue for HIDEngine, MappingEngine, and LinuxActionRunner."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable

from .action_runner import LinuxActionRunner
from .config import MappingConfig
from .desktop import ApplicationTracker
from .hid_engine import ButtonEvent, HIDEngine
from .interactions import MouseMode, OverlayManager
from .mapping_engine import MappingEngine

logger = logging.getLogger(__name__)


class RemoteKeyService:
    def __init__(
        self,
        config: MappingConfig,
        runner: LinuxActionRunner,
        *,
        hid_engine: HIDEngine | None = None,
    ):
        self.runner = runner
        self.overlay = OverlayManager(runner.desktop, runner)
        self.mouse = MouseMode(runner, runner.desktop)
        runner.overlay_handler = self.overlay.open
        runner.mouse_mode_handler = self.mouse.toggle
        self.mapping = MappingEngine(
            config,
            runner.run,
            on_layer=self._on_layer,
            on_escape=self._on_escape,
            event_filter=self._filter_event,
        )
        self.tracker = ApplicationTracker(runner.desktop, self.mapping.set_active_application)
        self.hid = hid_engine or HIDEngine(
            self.mapping.handle,
            on_reset=self.mapping.reset,
            key_codes=config.key_codes,
        )
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await self.tracker.start()
        try:
            await self.hid.start()
        except BaseException:
            await self.tracker.stop()
            raise

    async def stop(self) -> None:
        await self.hid.stop()
        await self.tracker.stop()
        await self.mouse.shutdown()
        await self.overlay.shutdown()
        await self.mapping.close()
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _on_layer(self, layer: int) -> None:
        print(f"🎛️  遥控器层: {layer}", file=sys.stderr, flush=True)
        self._spawn(self.overlay.show_layer(layer, self.mapping.active_profile))

    def _filter_event(self, event: ButtonEvent) -> bool:
        if self.overlay.handle(event):
            return True
        return self.mouse.handle(event)

    def _on_escape(self) -> None:
        self._spawn(self.overlay.close("已紧急退出"))
        self._spawn(self.mouse.deactivate())

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
            logger.error("remote key service action failed: %s", error)


class KeyWatchApp:
    def __init__(self, *, grab: bool = True):
        self._stop = asyncio.Event()
        self.hid = HIDEngine(self._show, on_reset=self._reset, grab=grab)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        handlers: list[signal.Signals] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self._stop.set)
                handlers.append(signum)
            except NotImplementedError:
                pass
        print("按遥控器按键；Ctrl+C 退出", file=sys.stderr, flush=True)
        await self.hid.start()
        try:
            await self._stop.wait()
        finally:
            await self.hid.stop()
            for signum in handlers:
                loop.remove_signal_handler(signum)

    @staticmethod
    def _show(event: ButtonEvent) -> None:
        state = "down" if event.is_down else "up"
        print(f"{event.key:8s} {state:4s} code={event.code}", flush=True)

    @staticmethod
    def _reset(reason: str) -> None:
        print(f"input reset: {reason}", file=sys.stderr, flush=True)


class KeyRunApp:
    def __init__(self, service: RemoteKeyService):
        self.service = service
        self._stop = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        handlers: list[signal.Signals] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self._stop.set)
                handlers.append(signum)
            except NotImplementedError:
                pass
        await self.service.start()
        try:
            await self._stop.wait()
        finally:
            await self.service.stop()
            for signum in handlers:
                loop.remove_signal_handler(signum)
