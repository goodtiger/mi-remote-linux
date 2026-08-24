"""CLI 入口：语音输入模式。"""

import argparse
import asyncio
import logging
import signal
import sys

import numpy as np

from .atvv import SyncFrame
from .ble_client import ATVVClient
from .injector import PASTE_SHORTCUTS, LinuxTextInjector, TextInjectionError
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
        output_file: str | None = None,
        injector: LinuxTextInjector | None = None,
    ):
        self.address = address
        self.output_file = output_file
        self.injector = injector

        # 语音管道
        self.pipeline = VoicePipeline(
            sample_rate=16000,
            gain_db=gain_db,
            model_size=model_size,
            language=language,
            engine=engine,
        )

        # BLE 客户端
        self.client = ATVVClient(
            on_audio_frame=self._on_audio_frame,
            on_voice_start=self._on_voice_start,
            on_voice_stop=self._on_voice_stop,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )

        self._running = False
        self._stop_event: asyncio.Event | None = None
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
        self._running = False
        if self._stop_event:
            self._stop_event.set()

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
        self._running = True
        self._stop_event = asyncio.Event()
        self._transcription_lock = asyncio.Lock()

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, self._stop_event.set)
        except NotImplementedError:
            pass

        print("正在连接遥控器...", file=sys.stderr, flush=True)

        if not await self.client.connect(self.address):
            print("连接失败，请确保：", file=sys.stderr)
            print("  1. 蓝牙已开启", file=sys.stderr)
            print("  2. 遥控器已配对（bluetoothctl pair <MAC>）", file=sys.stderr)
            print("  3. 遥控器在范围内", file=sys.stderr)
            return

        warmup_task = asyncio.create_task(self._warmup_model())
        self._transcription_tasks.add(warmup_task)
        warmup_task.add_done_callback(self._transcription_done)

        try:
            while self._running and not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.1)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            print("\n正在退出...", file=sys.stderr, flush=True)
            raise
        finally:
            try:
                loop.remove_signal_handler(signal.SIGTERM)
            except NotImplementedError:
                pass
            await self.client.disconnect()
            if self._transcription_tasks:
                await asyncio.gather(*self._transcription_tasks, return_exceptions=True)


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
        output_file=args.output,
        injector=injector,
    )
    try:
        asyncio.run(app.run())
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
        choices=["auto", "faster-whisper", "voxtype-paraformer"],
        default="auto",
        help="转写引擎；auto 优先本机 Paraformer（默认: auto）",
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
