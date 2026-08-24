"""Lifecycle glue for HIDEngine, MappingEngine, and LinuxActionRunner."""

from __future__ import annotations

import asyncio
import signal
import sys

from .action_runner import LinuxActionRunner
from .config import MappingConfig
from .hid_engine import ButtonEvent, HIDEngine
from .mapping_engine import MappingEngine


class RemoteKeyService:
    def __init__(
        self,
        config: MappingConfig,
        runner: LinuxActionRunner,
        *,
        hid_engine: HIDEngine | None = None,
    ):
        self.mapping = MappingEngine(config, runner.run, on_layer=self._on_layer)
        self.hid = hid_engine or HIDEngine(
            self.mapping.handle,
            on_reset=self.mapping.reset,
            key_codes=config.key_codes,
        )

    async def start(self) -> None:
        await self.hid.start()

    async def stop(self) -> None:
        await self.hid.stop()
        await self.mapping.close()

    @staticmethod
    def _on_layer(layer: int) -> None:
        print(f"🎛️  遥控器层: {layer}", file=sys.stderr, flush=True)


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
