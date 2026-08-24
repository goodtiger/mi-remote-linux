"""语音管道：ATVV ADPCM 帧 → PCM → 本地 Whisper。"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import wave
from importlib.util import find_spec
from pathlib import Path

import numpy as np

from .adpcm import ADPCMDecoder, PCMPostprocessor
from .atvv import SyncFrame

logger = logging.getLogger(__name__)


class VoicePipeline:
    """保留跨帧解码状态，并在会话结束后转写冻结的 PCM。"""

    def __init__(
        self,
        sample_rate: int = 16000,
        gain_db: float = 6.0,
        model_size: str = "base",
        language: str = "zh",
        engine: str = "faster-whisper",
        voxtype_path: str | None = None,
        voxtype_model_dir: str | Path | None = None,
        paraformer_model_dir: str | Path | None = None,
        paraformer_threads: int = 2,
        minimum_rms: float = 25.0,
    ):
        if engine not in {
            "auto",
            "faster-whisper",
            "sherpa-paraformer",
            "voxtype-paraformer",
        }:
            raise ValueError(f"不支持的转写引擎: {engine}")
        if paraformer_threads < 1:
            raise ValueError("paraformer_threads 必须大于 0")
        if minimum_rms < 0:
            raise ValueError("minimum_rms 不能小于 0")
        self.sample_rate = sample_rate
        self.model_size = model_size
        self.language = language
        self.engine = engine
        self.paraformer_threads = paraformer_threads
        self.minimum_rms = minimum_rms
        self._voxtype_path = shutil.which("voxtype") if voxtype_path is None else voxtype_path
        if voxtype_model_dir is not None and paraformer_model_dir is not None:
            raise ValueError("不能同时指定 voxtype_model_dir 和 paraformer_model_dir")
        configured_model_dir = paraformer_model_dir or voxtype_model_dir
        self._paraformer_model_dir = self._resolve_paraformer_model_dir(configured_model_dir)
        self.active_engine = self._select_engine()
        self.decoder = ADPCMDecoder()
        self.postprocessor = PCMPostprocessor(gain_db=gain_db)
        self.samples: list[np.ndarray] = []
        self._last_samples: np.ndarray | None = None
        self._whisper_model = None
        self._sherpa_recognizer = None

    def _select_engine(self) -> str:
        if self.engine == "auto":
            if self._paraformer_files_ready() and find_spec("sherpa_onnx") is not None:
                return "sherpa-paraformer"
            if self._voxtype_ready():
                return "voxtype-paraformer"
            return "faster-whisper"
        return self.engine

    def _paraformer_files_ready(self) -> bool:
        return (self._paraformer_model_dir / "model.int8.onnx").is_file() and (
            self._paraformer_model_dir / "tokens.txt"
        ).is_file()

    @staticmethod
    def _resolve_paraformer_model_dir(configured: str | Path | None) -> Path:
        if configured is not None:
            return Path(configured).expanduser()
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        env_dir = os.environ.get("MI_REMOTE_PARAFORMER_MODEL_DIR")
        candidates = [
            data_home / "mi-remote-linux/models/paraformer-zh",
            data_home / "voxtype/models/paraformer-zh",
        ]
        if env_dir:
            candidates.insert(0, Path(env_dir).expanduser())
        for candidate in candidates:
            if (candidate / "model.int8.onnx").is_file() and (candidate / "tokens.txt").is_file():
                return candidate
        return candidates[0]

    def _voxtype_ready(self) -> bool:
        return bool(self._voxtype_path and self._paraformer_files_ready())

    def _fallback_after_sherpa_failure(self) -> bool:
        self.active_engine = "voxtype-paraformer" if self._voxtype_ready() else "faster-whisper"
        logger.warning("改用转写引擎: %s", self.active_engine)
        return self.load_model()

    def load_model(self) -> bool:
        """检查/加载所选模型；可在线程中预热 Whisper。"""
        if self.active_engine == "sherpa-paraformer":
            if self._sherpa_recognizer is not None:
                return True
            if not self._paraformer_files_ready():
                logger.error("未找到 Paraformer 模型: %s", self._paraformer_model_dir)
                return self._fallback_after_sherpa_failure() if self.engine == "auto" else False
            try:
                import sherpa_onnx

                logger.info("加载常驻 Sherpa-ONNX Paraformer 中文模型")
                self._sherpa_recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    paraformer=str(self._paraformer_model_dir / "model.int8.onnx"),
                    tokens=str(self._paraformer_model_dir / "tokens.txt"),
                    num_threads=self.paraformer_threads,
                    sample_rate=self.sample_rate,
                    feature_dim=80,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - ONNX 后端异常类型随平台变化
                logger.error("Sherpa-ONNX Paraformer 加载失败: %s", exc)
                return self._fallback_after_sherpa_failure() if self.engine == "auto" else False

        if self.active_engine == "voxtype-paraformer":
            if not self._voxtype_path:
                logger.error("未找到 voxtype 命令")
                return False
            if not (self._paraformer_model_dir / "model.int8.onnx").is_file():
                logger.error("未找到 Voxtype Paraformer 模型: %s", self._paraformer_model_dir)
                return False
            logger.info("使用 Voxtype Paraformer 中文模型")
            return True

        if self._whisper_model is not None:
            return True
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.error('未安装 faster-whisper，请运行: pip install -e ".[voice]"')
            return False

        logger.info("加载 Whisper 模型: %s", self.model_size)
        self._whisper_model = WhisperModel(self.model_size)
        return True

    def on_voice_start(self) -> None:
        """开始新会话，不能沿用上一段的解码和平滑状态。"""
        self.samples.clear()
        self.decoder.reset(0, 0)
        self.postprocessor.reset()
        logger.debug("语音管道：开始录音")

    def on_audio_frame(self, frame: bytes, sync: SyncFrame | None) -> None:
        """解码一帧；SYNC 同时重置解码器和平滑历史。"""
        if sync:
            self.decoder.reset(sync.predictor, sync.step_index)
            self.postprocessor.reset()

        pcm = self.decoder.decode(frame)
        self.samples.append(self.postprocessor.process(pcm))

    def finish_capture(self, minimum_duration: float = 0.5) -> np.ndarray | None:
        """冻结当前录音，使下一会话可立即开始而不污染本次转写。"""
        chunks = self.samples
        self.samples = []
        if not chunks:
            logger.warning("语音管道：无音频数据")
            return None

        all_samples = np.concatenate(chunks)
        self._last_samples = all_samples
        duration = len(all_samples) / self.sample_rate
        logger.info("语音管道：录音结束，时长 %.2fs，样本数 %d", duration, len(all_samples))
        if duration < minimum_duration:
            logger.warning("语音管道：录音时间太短（< %.1fs），忽略", minimum_duration)
            return None
        return all_samples

    def on_voice_stop(self) -> str | None:
        """同步兼容入口；CLI 使用后台线程调用 :meth:`transcribe`。"""
        samples = self.finish_capture()
        return self.transcribe(samples) if samples is not None else None

    def transcribe(self, samples: np.ndarray) -> str | None:
        """阻塞式本地转写；调用方应放在线程中，避免阻塞 BLE 事件循环。"""
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0
        if rms < self.minimum_rms:
            logger.info("录音能量过低（RMS %.1f < %.1f），忽略", rms, self.minimum_rms)
            return None

        if not self.load_model():
            return None

        if self.active_engine == "sherpa-paraformer":
            try:
                return self._transcribe_sherpa(samples)
            except Exception as exc:  # noqa: BLE001 - ONNX 后端异常类型随平台变化
                if self.engine != "auto":
                    logger.error("Sherpa-ONNX Paraformer 转写失败: %s", exc)
                    return None
                logger.warning("Sherpa-ONNX Paraformer 失败: %s", exc)
                if not self._fallback_after_sherpa_failure():
                    return None

        if self.active_engine == "voxtype-paraformer":
            try:
                return self._transcribe_voxtype(samples)
            except (OSError, subprocess.SubprocessError) as exc:
                if self.engine != "auto":
                    logger.error("Voxtype Paraformer 转写失败: %s", exc)
                    return None
                logger.warning("Voxtype Paraformer 失败，回退 faster-whisper: %s", exc)
                self.active_engine = "faster-whisper"
                if not self.load_model():
                    return None

        return self._transcribe_whisper(samples)

    def _transcribe_sherpa(self, samples: np.ndarray) -> str | None:
        assert self._sherpa_recognizer is not None
        audio = np.ascontiguousarray(samples.astype(np.float32) / 32768.0)
        stream = self._sherpa_recognizer.create_stream()
        stream.accept_waveform(self.sample_rate, audio)
        logger.info("开始 Sherpa-ONNX Paraformer 转写...")
        self._sherpa_recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        logger.info("转写结果: %s", text)
        return text or None

    def _transcribe_whisper(self, samples: np.ndarray) -> str | None:
        assert self._whisper_model is not None

        audio = samples.astype(np.float32) / 32768.0
        logger.info("开始 faster-whisper 转写...")
        segments, _info = self._whisper_model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
            vad_filter=True,
        )
        text = "".join(segment.text for segment in segments).strip()
        logger.info("转写结果: %s", text)
        return text or None

    def _transcribe_voxtype(self, samples: np.ndarray) -> str | None:
        assert self._voxtype_path is not None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
        try:
            self.save_wav(path, samples)
            logger.info("开始 Voxtype Paraformer 转写...")
            result = subprocess.run(
                [self._voxtype_path, "transcribe", "--engine", "paraformer", str(path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
                raise subprocess.SubprocessError(detail)
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            text = lines[-1] if lines else ""
            logger.info("转写结果: %s", text)
            return text or None
        finally:
            path.unlink(missing_ok=True)

    def save_wav(self, path: str | Path, samples: np.ndarray | None = None) -> None:
        """把指定录音（默认最近一次）保存成 16 kHz mono PCM WAV。"""
        audio = self._last_samples if samples is None else samples
        if audio is None or len(audio) == 0:
            return

        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(audio.astype("<i2", copy=False).tobytes())
        logger.info("已保存 WAV: %s", path)
