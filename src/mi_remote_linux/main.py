"""CLI 入口：语音输入模式。"""

import argparse
import asyncio
import logging
import signal
import sys

import numpy as np

from .atvv import SyncFrame
from .ble_client import ATVVClient
from .hid_guard import HID_GRAB_MODES, RemoteHIDGuard
from .injector import PASTE_SHORTCUTS, LinuxTextInjector, TextInjectionError
from .runtime import AlreadyRunningError, VoiceInstanceLock
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
        output_file: str | None = None,
        injector: LinuxTextInjector | None = None,
        hid_guard: RemoteHIDGuard | None = None,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 15.0,
    ):
        self.address = address
        self.output_file = output_file
        self.injector = injector
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
        except TimeoutError:
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

    app = VoiceApp(
        address=args.address,
        model_size=args.model,
        language=args.language,
        engine=args.engine,
        gain_db=args.gain,
        paraformer_model_dir=args.paraformer_model_dir,
        paraformer_threads=args.paraformer_threads,
        output_file=args.output,
        injector=injector,
        hid_guard=RemoteHIDGuard(mode=args.grab_hid),
    )
    try:
        with VoiceInstanceLock():
            asyncio.run(app.run())
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


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
        "--grab-hid",
        choices=HID_GRAB_MODES,
        default="safe",
        help="隔离遥控器 F9：safe 保留其他键，force 在无 uinput 时仍可独占",
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
