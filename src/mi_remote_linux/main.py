"""CLI 入口：语音输入模式。"""

import argparse
import asyncio
import logging
import signal
import sys

import numpy as np

from .action_runner import LinuxActionRunner
from .atvv import SyncFrame
from .ble_client import ATVVClient
from .config import ConfigError, default_config_text, load_config
from .doctor import Doctor, render_report
from .hid_guard import HID_GRAB_MODES, RemoteHIDGuard
from .injector import PASTE_SHORTCUTS, LinuxTextInjector, TextInjectionError
from .remote_keys import KeyRunApp, KeyWatchApp, RemoteKeyService
from .runtime import AlreadyRunningError, VoiceInstanceLock
from .self_test import (
    DesktopSelfTest,
    KeySelfTest,
    VoiceSelfTest,
    combine_self_test_reports,
    render_self_test_report,
)
from .text_corrector import TermsFileError, TextCorrector
from .voice import VoicePipeline

logger = logging.getLogger(__name__)


class VoiceApp:
    """语音输入应用。

    按住遥控器语音键说话，松手后转写文字输出到 stdout。
    """

    def __init__(
        self,
        address: str | None = None,
        model_size: str = "base",
        language: str = "zh",
        engine: str = "auto",
        gain_db: float = 6.0,
        paraformer_model_dir: str | None = None,
        paraformer_threads: int = 2,
        save_audio_dir: str | None = None,
        output_file: str | None = None,
        injector: LinuxTextInjector | None = None,
        text_corrector: TextCorrector | None = None,
        hid_guard: RemoteHIDGuard | None = None,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 15.0,
    ):
        self.address = address
        self.output_file = output_file
        self.injector = injector
        self.text_corrector = text_corrector
        self.hid_guard = hid_guard
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay

        # 语音管道
        self.pipeline = VoicePipeline(
            sample_rate=16000,
            gain_db=gain_db,
            model_size=model_size,
            language=language,
            engine=engine,
            paraformer_model_dir=paraformer_model_dir,
            paraformer_threads=paraformer_threads,
            save_audio_dir=save_audio_dir,
        )

        # BLE 客户端
        self.client = ATVVClient(
            on_audio_frame=self._on_audio_frame,
            on_voice_start=self._on_voice_start,
            on_voice_stop=self._on_voice_stop,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )

        self._stop_event: asyncio.Event | None = None
        self._connection_lost_event: asyncio.Event | None = None
        self._transcription_lock: asyncio.Lock | None = None
        self._transcription_tasks: set[asyncio.Task[None]] = set()

    def _on_audio_frame(self, frame: bytes, sync: SyncFrame | None) -> None:
        """音频帧回调。"""
        self.pipeline.on_audio_frame(frame, sync)

    def _on_voice_start(self) -> None:
        """语音开始回调。"""
        self.pipeline.on_voice_start()
        print("🎤 录音中...（松开语音键结束）", file=sys.stderr, flush=True)

    def _on_voice_stop(self) -> None:
        """冻结录音并在线程中转写，保持 BLE 事件循环可响应。"""
        samples = self.pipeline.finish_capture()
        if samples is None:
            print("（未识别到语音）", file=sys.stderr, flush=True)
            return

        task = asyncio.create_task(self._transcribe_and_emit(samples))
        self._transcription_tasks.add(task)
        task.add_done_callback(self._transcription_done)

    async def _transcribe_and_emit(self, samples: np.ndarray) -> None:
        """串行使用 Whisper 模型，但把 CPU/GPU 阻塞工作移出事件循环。"""
        if self._transcription_lock is None:
            self._transcription_lock = asyncio.Lock()
        async with self._transcription_lock:
            text = await asyncio.to_thread(self.pipeline.transcribe, samples)
            if text:
                corrected = self.text_corrector.apply(text) if self.text_corrector else text
                if corrected != text:
                    logger.info("术语纠正结果: %s", corrected)
                text = corrected
                print(text, flush=True)
                if self.output_file:
                    try:
                        await asyncio.to_thread(self._append_output, text)
                    except OSError as exc:
                        logger.error("无法写入输出文件 %s: %s", self.output_file, exc)
                if self.injector:
                    try:
                        await self.injector.inject(text)
                        print("⌨️  已输入当前焦点", file=sys.stderr, flush=True)
                    except TextInjectionError as exc:
                        fallback = (
                            "文本已保留在剪贴板中"
                            if exc.clipboard_ready
                            else "请从 stdout 获取文本"
                        )
                        logger.error("自动输入失败（%s）: %s", fallback, exc)
            else:
                print("（未识别到语音）", file=sys.stderr, flush=True)

    def _append_output(self, text: str) -> None:
        if self.output_file:
            with open(self.output_file, "a", encoding="utf-8") as output:
                output.write(text + "\n")

    def _transcription_done(self, task: asyncio.Task[None]) -> None:
        self._transcription_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("语音转写失败")

    def _on_connected(self) -> None:
        """BLE 连接成功回调。"""
        print("✅ 已连接遥控器，按住语音键说话", file=sys.stderr, flush=True)

    def _on_disconnected(self) -> None:
        """BLE 断连回调。"""
        print("❌ 遥控器断开连接", file=sys.stderr, flush=True)
        if self._connection_lost_event:
            self._connection_lost_event.set()

    async def _warmup_model(self) -> None:
        """低优先级预热模型；录音仍可在下载/加载期间开始。"""
        if self._transcription_lock is None:
            self._transcription_lock = asyncio.Lock()
        async with self._transcription_lock:
            loaded = await asyncio.to_thread(self.pipeline.load_model)
        if loaded:
            print(
                f"✅ 语音识别引擎已就绪: {self.pipeline.active_engine}",
                file=sys.stderr,
                flush=True,
            )

    async def run(self) -> None:
        """运行应用。"""
        self._stop_event = asyncio.Event()
        self._connection_lost_event = asyncio.Event()
        self._transcription_lock = asyncio.Lock()

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, self._stop_event.set)
        except NotImplementedError:
            pass

        warmup_task = asyncio.create_task(self._warmup_model())
        self._transcription_tasks.add(warmup_task)
        warmup_task.add_done_callback(self._transcription_done)

        if self.hid_guard:
            await self.hid_guard.start()

        try:
            retry_delay = self.reconnect_initial_delay
            first_attempt = True
            while not self._stop_event.is_set():
                self._connection_lost_event.clear()
                message = "正在连接遥控器..." if first_attempt else "正在重新连接遥控器..."
                print(message, file=sys.stderr, flush=True)
                first_attempt = False

                if not await self.client.connect(self.address):
                    print(
                        f"将在 {retry_delay:.0f} 秒后重试连接",
                        file=sys.stderr,
                        flush=True,
                    )
                    if await self._wait_for_stop(retry_delay):
                        break
                    retry_delay = min(retry_delay * 2, self.reconnect_max_delay)
                    continue

                retry_delay = self.reconnect_initial_delay
                disconnected = await self._wait_for_stop_or_disconnect()
                if not disconnected:
                    break
                await self.client.disconnect(restore_hid=False)
                print("等待遥控器唤醒后自动重连", file=sys.stderr, flush=True)
        except asyncio.CancelledError:
            print("\n正在退出...", file=sys.stderr, flush=True)
            raise
        finally:
            try:
                loop.remove_signal_handler(signal.SIGTERM)
            except NotImplementedError:
                pass
            if self.hid_guard:
                await self.hid_guard.stop()
            await self.client.disconnect(restore_hid=True)
            if self._transcription_tasks:
                await asyncio.gather(*self._transcription_tasks, return_exceptions=True)

    async def _wait_for_stop(self, timeout: float) -> bool:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _wait_for_stop_or_disconnect(self) -> bool:
        """返回 True 表示断连，False 表示用户要求停止。"""
        assert self._stop_event is not None
        assert self._connection_lost_event is not None
        stop_task = asyncio.create_task(self._stop_event.wait())
        lost_task = asyncio.create_task(self._connection_lost_event.wait())
        done, pending = await asyncio.wait(
            {stop_task, lost_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return lost_task in done and lost_task.result()


def cmd_voice(args: argparse.Namespace) -> None:
    """voice 子命令。"""
    try:
        text_corrector = TextCorrector.from_file(args.terms) if args.terms else None
    except TermsFileError as exc:
        print(f"术语表无效: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    injector = (
        LinuxTextInjector(
            session=args.session,
            paste_shortcut=args.paste_shortcut,
            submit=args.submit,
        )
        if args.inject
        else None
    )
    if injector:
        missing = injector.missing_dependencies()
        if missing:
            print(
                f"自动输入不可用，缺少: {', '.join(missing)}",
                file=sys.stderr,
            )
            print("Wayland: 安装 wl-clipboard 和 wtype", file=sys.stderr)
            print("X11: 安装 xclip 和 xdotool", file=sys.stderr)
            return

    key_service = None
    config_path = getattr(args, "config", None)
    if config_path:
        try:
            mapping_config = load_config(config_path)
        except ConfigError as exc:
            print(f"按键配置无效: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        action_runner = LinuxActionRunner(session=args.session)
        missing = action_runner.missing_dependencies()
        if missing:
            print(f"按键动作不可用，缺少: {', '.join(missing)}", file=sys.stderr)
            return
        key_service = RemoteKeyService(mapping_config, action_runner)

    app = VoiceApp(
        address=args.address,
        model_size=args.model,
        language=args.language,
        engine=args.engine,
        gain_db=args.gain,
        paraformer_model_dir=args.paraformer_model_dir,
        paraformer_threads=args.paraformer_threads,
        save_audio_dir=args.save_audio_dir,
        output_file=args.output,
        injector=injector,
        text_corrector=text_corrector,
        hid_guard=key_service or RemoteHIDGuard(mode=args.grab_hid),
    )
    try:
        with VoiceInstanceLock():
            asyncio.run(app.run())
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


def cmd_keys_watch(args: argparse.Namespace) -> None:
    try:
        asyncio.run(KeyWatchApp(grab=not args.no_grab).run())
    except KeyboardInterrupt:
        pass


def cmd_keys_run(args: argparse.Namespace) -> None:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"按键配置无效: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    runner = LinuxActionRunner(session=args.session)
    missing = runner.missing_dependencies()
    if missing:
        print(f"按键动作不可用，缺少: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    try:
        with VoiceInstanceLock():
            asyncio.run(KeyRunApp(RemoteKeyService(config, runner)).run())
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


def cmd_config_show(_args: argparse.Namespace) -> None:
    print(default_config_text(), end="")


def cmd_config_validate(args: argparse.Namespace) -> None:
    try:
        config = load_config(args.path)
    except ConfigError as exc:
        print(f"按键配置无效: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(
        f"配置有效: v{config.version}, {len(config.bindings)} 个全局按键, "
        f"{len(config.profiles)} 个 App profile"
    )


def cmd_doctor(args: argparse.Namespace) -> None:
    report = asyncio.run(
        Doctor().run(
            address=args.address,
            model_dir=args.model_dir,
        )
    )
    print(report.to_json() if args.json else render_report(report))
    if report.exit_code:
        raise SystemExit(report.exit_code)


async def _run_self_test(args: argparse.Namespace):
    reports = []
    if args.suite in {"all", "keys"}:
        reports.append(await KeySelfTest().run(timeout=args.timeout))
        if reports[-1].exit_code:
            return combine_self_test_reports(*reports) if args.suite == "all" else reports[-1]
    if args.suite in {"all", "voice"}:
        pipeline = VoicePipeline(
            model_size=args.model,
            language=args.language,
            engine=args.engine,
            gain_db=args.gain,
            paraformer_model_dir=args.model_dir,
        )
        reports.append(
            await VoiceSelfTest(pipeline).run(
                address=args.address,
                phrase=args.phrase,
                timeout=args.timeout,
            )
        )
    if args.suite in {"all", "desktop"}:
        reports.append(await DesktopSelfTest(session=args.session).run(inject=args.inject))
    return combine_self_test_reports(*reports) if args.suite == "all" else reports[0]


def cmd_self_test(args: argparse.Namespace) -> None:
    try:
        if args.suite == "desktop":
            report = asyncio.run(_run_self_test(args))
        else:
            with VoiceInstanceLock():
                report = asyncio.run(_run_self_test(args))
    except AlreadyRunningError as exc:
        print(f"无法开始真机验收：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(report.to_json() if args.json else render_self_test_report(report))
    if report.exit_code:
        raise SystemExit(report.exit_code)


def main() -> None:
    """主入口。"""
    parser = argparse.ArgumentParser(
        prog="mi-remote",
        description="小米蓝牙遥控器 2 Pro Linux 驱动",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="启用详细日志",
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # voice 子命令
    voice_parser = subparsers.add_parser(
        "voice",
        help="语音输入模式（按住说话，松手转写）",
    )
    voice_parser.add_argument(
        "-a",
        "--address",
        help="遥控器 BLE MAC 地址（不指定则自动扫描）",
    )
    voice_parser.add_argument(
        "-m",
        "--model",
        default="base",
        choices=["tiny", "base", "small", "medium", "large-v3"],
        help="faster-whisper 模型大小（默认: base）",
    )
    voice_parser.add_argument(
        "--engine",
        choices=["auto", "faster-whisper", "sherpa-paraformer", "voxtype-paraformer"],
        default="auto",
        help="转写引擎；auto 优先常驻 Sherpa-ONNX Paraformer（默认: auto）",
    )
    voice_parser.add_argument(
        "--paraformer-model-dir",
        help="Paraformer 模型目录（默认查找 MiRemote 或 Voxtype 模型目录）",
    )
    voice_parser.add_argument(
        "--paraformer-threads",
        type=int,
        default=2,
        help="Sherpa-ONNX Paraformer CPU 线程数（默认: 2）",
    )
    voice_parser.add_argument(
        "-l",
        "--language",
        default="zh",
        help="语音语言（默认: zh 中文）",
    )
    voice_parser.add_argument(
        "-g",
        "--gain",
        type=float,
        default=6.0,
        help="音频增益 dB（默认: 6.0）",
    )
    voice_parser.add_argument(
        "-o",
        "--output",
        help="将转写结果追加到文件",
    )
    voice_parser.add_argument(
        "--save-audio-dir",
        help="把每次解码后的 WAV 保存到指定目录（仅用于调试）",
    )
    voice_parser.add_argument(
        "--terms",
        help="JSON 术语纠正表；纠正后的文本用于 stdout、文件和焦点输入",
    )
    voice_parser.add_argument(
        "--grab-hid",
        choices=HID_GRAB_MODES,
        default="safe",
        help="隔离遥控器语音 HID 键：safe 保留其他键，force 在无 uinput 时仍可独占",
    )
    voice_parser.add_argument(
        "--config",
        nargs="?",
        const="default",
        help="启用完整按键映射；不带路径时使用内置 macOS 对应默认配置",
    )
    voice_parser.add_argument(
        "--inject",
        action="store_true",
        help="转写后自动粘贴到当前桌面焦点（Wayland/X11）",
    )
    voice_parser.add_argument(
        "--session",
        choices=["auto", "wayland", "x11"],
        default="auto",
        help="图形会话类型（默认: auto）",
    )
    voice_parser.add_argument(
        "--paste-shortcut",
        choices=PASTE_SHORTCUTS,
        default="auto",
        help="粘贴快捷键；自动检测失败时可手动指定（默认: auto）",
    )
    voice_parser.add_argument(
        "--submit",
        action="store_true",
        help="自动粘贴后再按 Enter（必须与 --inject 同用）",
    )
    voice_parser.set_defaults(func=cmd_voice)

    keys_parser = subparsers.add_parser("keys", help="13 键探针和映射模式")
    keys_subparsers = keys_parser.add_subparsers(dest="keys_command")
    watch_parser = keys_subparsers.add_parser("watch", help="显示 RC003 原始逻辑按键")
    watch_parser.add_argument(
        "--no-grab",
        action="store_true",
        help="仅监听，不独占遥控器输入节点（按键仍会传给桌面）",
    )
    watch_parser.set_defaults(func=cmd_keys_watch)
    run_parser = keys_subparsers.add_parser("run", help="仅运行按键映射，不连接语音通道")
    run_parser.add_argument(
        "-c",
        "--config",
        default="default",
        help="映射 JSON（默认使用内置 macOS 对应配置）",
    )
    run_parser.add_argument(
        "--session",
        choices=["auto", "wayland", "x11"],
        default="auto",
        help="图形会话类型（默认: auto）",
    )
    run_parser.set_defaults(func=cmd_keys_run)

    config_parser = subparsers.add_parser("config", help="查看或校验按键配置")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    show_parser = config_subparsers.add_parser("show", help="输出内置默认配置 JSON")
    show_parser.set_defaults(func=cmd_config_show)
    validate_parser = config_subparsers.add_parser("validate", help="严格校验配置 JSON")
    validate_parser.add_argument("path", help="配置文件路径，或 default")
    validate_parser.set_defaults(func=cmd_config_validate)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="只读检查蓝牙、输入权限、桌面后端和语音模型",
    )
    doctor_parser.add_argument(
        "-a",
        "--address",
        help="指定要检查的遥控器 BLE MAC 地址（默认自动发现）",
    )
    doctor_parser.add_argument(
        "--model-dir",
        help="指定要检查的 Paraformer 模型目录",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="输出适合脚本处理的 JSON",
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    test_parser = subparsers.add_parser(
        "test",
        help="交互验证按键、语音和安全桌面功能",
    )
    test_parser.add_argument(
        "suite",
        nargs="?",
        choices=["all", "keys", "voice", "desktop"],
        default="all",
        help="测试套件（默认: all）",
    )
    test_parser.add_argument("-a", "--address", help="遥控器 BLE MAC 地址（默认自动发现）")
    test_parser.add_argument("--timeout", type=float, default=30.0, help="每一步超时秒数")
    test_parser.add_argument(
        "--phrase",
        default="这是小米遥控器语音输入测试",
        help="语音测试时显示的目标语句",
    )
    test_parser.add_argument(
        "--engine",
        choices=["auto", "faster-whisper", "sherpa-paraformer", "voxtype-paraformer"],
        default="auto",
        help="语音识别引擎（默认: auto）",
    )
    test_parser.add_argument("--model", default="base", help="faster-whisper 模型")
    test_parser.add_argument("--model-dir", help="Paraformer 模型目录")
    test_parser.add_argument("--language", default="zh", help="语音语言（默认: zh）")
    test_parser.add_argument("--gain", type=float, default=6.0, help="音频增益 dB")
    test_parser.add_argument("--session", choices=["auto", "wayland", "x11"], default="auto")
    test_parser.add_argument(
        "--inject",
        action="store_true",
        help="桌面测试中经确认后向焦点输入测试文字",
    )
    test_parser.add_argument("--json", action="store_true", help="最终报告输出为 JSON")
    test_parser.set_defaults(func=cmd_self_test)

    args = parser.parse_args()
    if getattr(args, "submit", False) and not getattr(args, "inject", False):
        parser.error("--submit 必须与 --inject 同时使用")

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("mi_remote_linux").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    # Bleak DEBUG 会逐帧打印原始音频；协议层已有足够的摘要日志。
    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("dbus_fast").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not hasattr(args, "func"):
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
