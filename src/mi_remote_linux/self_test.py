"""Interactive, non-destructive hardware and desktop acceptance tests."""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np

from .ble_client import ATVVClient
from .desktop import DesktopActionError, LinuxDesktop
from .hid_engine import HIDEngine
from .injector import LinuxTextInjector, TextInjectionError
from .voice import VoicePipeline

TestStatus = Literal["pass", "warn", "fail"]

SELF_TEST_KEYS = (
    ("power", "电源键"),
    ("voice", "语音键"),
    ("up", "上键"),
    ("down", "下键"),
    ("left", "左键"),
    ("right", "右键"),
    ("ok", "OK 键"),
    ("back", "返回键"),
    ("home", "主页键"),
    ("menu", "菜单键"),
    ("tv", "TV 键"),
    ("vol_up", "音量＋"),
    ("vol_down", "音量－"),
)


@dataclass(frozen=True)
class SelfTestCheck:
    key: str
    label: str
    status: TestStatus
    detail: str
    metrics: dict[str, Any] | None = None


@dataclass(frozen=True)
class SelfTestReport:
    suite: str
    checks: tuple[SelfTestCheck, ...]

    @property
    def summary(self) -> dict[str, int]:
        return {
            status: sum(check.status == status for check in self.checks)
            for status in ("pass", "warn", "fail")
        }

    @property
    def exit_code(self) -> int:
        return 1 if self.summary["fail"] else 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "suite": self.suite,
                "checks": [asdict(check) for check in self.checks],
                "summary": self.summary,
                "ok": self.exit_code == 0,
            },
            ensure_ascii=False,
            indent=2,
        )


def render_self_test_report(report: SelfTestReport) -> str:
    symbols = {"pass": "✓", "warn": "!", "fail": "✗"}
    lines = [f"MiRemote Self-Test · {report.suite}", ""]
    for check in report.checks:
        lines.append(f"{symbols[check.status]} {check.label}: {check.detail}")
    summary = report.summary
    lines.extend(
        (
            "",
            f"汇总：{summary['pass']} 通过，{summary['warn']} 警告，{summary['fail']} 失败",
        )
    )
    return "\n".join(lines)


class KeySelfTest:
    """Read balanced down/up events while HID is exclusively captured."""

    def __init__(
        self,
        engine_factory: Callable[[Callable[[Any], None]], Any] | None = None,
        prompt: Callable[[str], None] | None = None,
    ):
        self.engine_factory = engine_factory or (lambda callback: HIDEngine(callback, grab=True))
        self.prompt = prompt or (lambda text: print(text, file=sys.stderr, flush=True))
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    async def run(self, *, timeout: float = 15.0) -> SelfTestReport:
        engine = self.engine_factory(self._queue.put_nowait)
        checks: list[SelfTestCheck] = []
        try:
            await engine.start()
            if not engine.paths:
                checks.append(
                    SelfTestCheck(
                        "device",
                        "遥控器输入",
                        "fail",
                        "没有可独占的 RC003 输入节点；请停止其他 mi-remote 实例",
                    )
                )
                return SelfTestReport("keys", tuple(checks))
            for expected, label in SELF_TEST_KEYS:
                self.prompt(f"请按：{label}")
                check = await self._wait_for_key(expected, label, timeout)
                checks.append(check)
                if check.status == "fail":
                    break
        finally:
            await engine.stop()
        return SelfTestReport("keys", tuple(checks))

    async def _wait_for_key(
        self,
        expected: str,
        label: str,
        timeout: float,
    ) -> SelfTestCheck:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        pressed = None
        code = None
        unexpected: list[str] = []
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                suffix = f"；期间收到：{', '.join(unexpected)}" if unexpected else ""
                return SelfTestCheck(expected, label, "fail", f"等待按键超时{suffix}")
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                suffix = f"；期间收到：{', '.join(unexpected)}" if unexpected else ""
                return SelfTestCheck(expected, label, "fail", f"等待按键超时{suffix}")
            if event.key != expected:
                unexpected.append(event.key)
                continue
            if event.is_down:
                pressed = event.time_ns
                code = event.code
                continue
            if pressed is None:
                continue
            held_ms = max(0.0, (event.time_ns - pressed) / 1_000_000)
            return SelfTestCheck(
                expected,
                label,
                "pass",
                f"code={code}，按下/松开完整，{held_ms:.0f} ms",
                {"code": code, "held_ms": round(held_ms, 1)},
            )


class VoiceSelfTest:
    """Capture and transcribe one or more ATVV utterances on one connection."""

    def __init__(
        self,
        pipeline: VoicePipeline,
        *,
        client_factory: Callable[..., Any] = ATVVClient,
        hid_factory: Callable[[Callable[[Any], None]], Any] | None = None,
        prompt: Callable[[str], None] | None = None,
    ):
        self.pipeline = pipeline
        self.client_factory = client_factory
        self.hid_factory = hid_factory or (lambda callback: HIDEngine(callback, grab=True))
        self.prompt = prompt or (lambda text: print(text, file=sys.stderr, flush=True))
        self._started: asyncio.Event | None = None
        self._stopped: asyncio.Event | None = None
        self._samples: np.ndarray | None = None

    async def run(
        self,
        *,
        address: str | None = None,
        phrase: str = "这是小米遥控器语音输入测试",
        timeout: float = 30.0,
        count: int = 1,
    ) -> SelfTestReport:
        if count < 1:
            raise ValueError("voice self-test count must be at least 1")
        if not await asyncio.to_thread(self.pipeline.load_model):
            return SelfTestReport(
                "voice",
                (SelfTestCheck("voice", "语音识别", "fail", "识别模型加载失败"),),
            )
        self._started = asyncio.Event()
        self._stopped = asyncio.Event()
        self._samples = None
        hid = self.hid_factory(lambda _event: None)
        client = self.client_factory(
            on_audio_frame=self.pipeline.on_audio_frame,
            on_voice_start=self._on_voice_start,
            on_voice_stop=self._on_voice_stop,
        )
        try:
            await hid.start()
            if not hid.paths:
                return SelfTestReport(
                    "voice",
                    (
                        SelfTestCheck(
                            "hid_guard",
                            "语音键隔离",
                            "fail",
                            "无法独占 RC003 输入节点；请停止其他 mi-remote 实例",
                        ),
                    ),
                )
            connected = await client.connect(address)
            if not connected:
                self.prompt("首次 BLE 连接未完成，等待 BlueZ 稳定后重试一次")
                await asyncio.sleep(2)
                connected = await client.connect(address)
            if not connected:
                return SelfTestReport(
                    "voice",
                    (SelfTestCheck("ble", "ATVV 连接", "fail", "两次尝试均无法连接遥控器"),),
                )
            checks: list[SelfTestCheck] = []
            for index in range(count):
                if index:
                    self._started = asyncio.Event()
                    self._stopped = asyncio.Event()
                    self._samples = None
                suffix = f"（{index + 1}/{count}）" if count > 1 else ""
                self.prompt(f"{suffix}按住语音键并说：{phrase}；说完后松开")
                try:
                    await asyncio.wait_for(self._started.wait(), timeout=timeout)
                    await asyncio.wait_for(self._stopped.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    checks.append(
                        SelfTestCheck(
                            f"capture_{index + 1}",
                            f"语音采集 {index + 1}/{count}",
                            "fail",
                            "等待语音开始或结束超时",
                        )
                    )
                    break
                check = await self._transcribe_capture(phrase, index=index, count=count)
                checks.append(check)
                if check.status == "fail":
                    break
            return SelfTestReport("voice", tuple(checks))
        finally:
            await client.disconnect()
            await hid.stop()

    async def _transcribe_capture(self, phrase: str, *, index: int, count: int) -> SelfTestCheck:
        label = "语音识别" if count == 1 else f"语音识别 {index + 1}/{count}"
        key = "voice" if count == 1 else f"voice_{index + 1}"
        if self._samples is None:
            return SelfTestCheck(key, label, "fail", "录音过短或没有音频帧")
        started = time.perf_counter()
        text = await asyncio.to_thread(self.pipeline.transcribe, self._samples)
        latency_ms = (time.perf_counter() - started) * 1000
        duration = len(self._samples) / self.pipeline.sample_rate
        rms = float(np.sqrt(np.mean(self._samples.astype(np.float64) ** 2)))
        peak = int(np.max(np.abs(self._samples.astype(np.int32))))
        metrics = {
            "expected": phrase,
            "text": text,
            "latency_ms": round(latency_ms, 1),
            "duration_s": round(duration, 2),
            "rms": round(rms, 1),
            "peak": peak,
            "engine": self.pipeline.active_engine,
        }
        if not text:
            return SelfTestCheck(key, label, "fail", "未识别到文本", metrics)
        similarity = self._similarity(phrase, text)
        metrics["similarity"] = round(similarity, 3)
        status: TestStatus = "pass" if similarity >= 0.6 else "warn"
        return SelfTestCheck(
            key,
            label,
            status,
            f"{text} · 相似度 {similarity:.0%} · {latency_ms:.0f} ms",
            metrics,
        )

    def _on_voice_start(self) -> None:
        self.pipeline.on_voice_start()
        assert self._started is not None
        self._started.set()

    def _on_voice_stop(self) -> None:
        self._samples = self.pipeline.finish_capture()
        assert self._stopped is not None
        self._stopped.set()

    @staticmethod
    def _similarity(expected: str, actual: str) -> float:
        normalize = lambda value: re.sub(r"\W+", "", value, flags=re.UNICODE).casefold()
        return difflib.SequenceMatcher(None, normalize(expected), normalize(actual)).ratio()


class DesktopSelfTest:
    """Exercise safe desktop APIs; text insertion requires explicit opt-in."""

    def __init__(
        self,
        *,
        session: str = "auto",
        environment: dict[str, str] | None = None,
        desktop: LinuxDesktop | None = None,
        injector: LinuxTextInjector | None = None,
        ask: Callable[[str], str] = input,
    ):
        self.environment = dict(os.environ if environment is None else environment)
        self.desktop = desktop or LinuxDesktop(environment=self.environment)
        self.injector = injector or LinuxTextInjector(session=session, environment=self.environment)
        self.ask = ask

    async def run(self, *, inject: bool = False) -> SelfTestReport:
        checks: list[SelfTestCheck] = []
        try:
            await self.desktop.notify("MiRemote Self-Test", "桌面通知测试")
            checks.append(SelfTestCheck("notification", "桌面通知", "pass", "发送成功"))
        except DesktopActionError as exc:
            checks.append(SelfTestCheck("notification", "桌面通知", "fail", str(exc)))

        try:
            app = await self.desktop.active_application()
            windows = await self.desktop.windows()
            status: TestStatus = "pass" if app else "warn"
            checks.append(
                SelfTestCheck(
                    "windows",
                    "窗口查询",
                    status,
                    f"活动应用 {app or '未知'} · {len(windows)} 个窗口",
                )
            )
        except DesktopActionError as exc:
            checks.append(SelfTestCheck("windows", "窗口查询", "warn", str(exc)))

        audio = await self._audio_status()
        checks.append(audio)

        missing = self.injector.missing_dependencies()
        if missing:
            checks.append(
                SelfTestCheck("injection", "焦点文字输入", "fail", "缺少：" + ", ".join(missing))
            )
        elif not inject:
            checks.append(
                SelfTestCheck(
                    "injection",
                    "焦点文字输入",
                    "warn",
                    "依赖就绪；未写入焦点（加 --inject 执行可见测试）",
                )
            )
        else:
            marker = "MiRemote 焦点输入测试"
            await asyncio.to_thread(self.ask, "请先把焦点放到安全的空白输入框，然后按 Enter 继续：")
            try:
                await self.injector.inject(marker)
                answer = await asyncio.to_thread(self.ask, "是否看到了测试文字？[y/N] ")
                status = "pass" if answer.strip().casefold() in {"y", "yes", "是"} else "fail"
                checks.append(SelfTestCheck("injection", "焦点文字输入", status, marker))
            except TextInjectionError as exc:
                checks.append(SelfTestCheck("injection", "焦点文字输入", "fail", str(exc)))

        checks.append(
            SelfTestCheck(
                "dangerous",
                "危险动作保护",
                "pass",
                "关屏、锁屏和退出应用未执行",
            )
        )
        return SelfTestReport("desktop", tuple(checks))

    async def _audio_status(self) -> SelfTestCheck:
        if shutil.which("wpctl"):
            command = ("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
        elif shutil.which("pactl"):
            command = ("pactl", "get-sink-volume", "@DEFAULT_SINK@")
        else:
            return SelfTestCheck("audio", "音频状态", "warn", "缺少 wpctl 和 pactl")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
            )
            stdout, stderr = await process.communicate()
        except OSError as exc:
            return SelfTestCheck("audio", "音频状态", "warn", str(exc))
        if process.returncode:
            return SelfTestCheck(
                "audio", "音频状态", "warn", stderr.decode(errors="replace").strip()
            )
        return SelfTestCheck("audio", "音频状态", "pass", stdout.decode(errors="replace").strip())


def combine_self_test_reports(*reports: SelfTestReport) -> SelfTestReport:
    return SelfTestReport("all", tuple(check for report in reports for check in report.checks))
