"""语音会话生命周期测试（不加载真实 Whisper 模型）。"""

import subprocess
import sys
from types import SimpleNamespace

import numpy as np

import mi_remote_linux.voice as voice_module
from mi_remote_linux.atvv import SyncFrame
from mi_remote_linux.voice import VoicePipeline


def test_new_voice_session_resets_decoder_and_postprocessor():
    pipeline = VoicePipeline()
    pipeline.decoder.reset(1234, 40)
    pipeline.postprocessor.process(np.array([100, 200], dtype=np.int16))

    pipeline.on_voice_start()

    assert pipeline.decoder.predictor == 0
    assert pipeline.decoder.step_index == 0
    assert pipeline.postprocessor.prev1 is None
    assert pipeline.postprocessor.prev2 is None


def test_sync_makes_output_independent_of_old_state():
    frame = bytes([0x12, 0x34, 0x56])
    sync = SyncFrame(predictor=1000, step_index=10)

    clean = VoicePipeline(gain_db=0)
    clean.on_voice_start()
    clean.on_audio_frame(frame, sync)

    polluted = VoicePipeline(gain_db=0)
    polluted.decoder.reset(-5000, 50)
    polluted.postprocessor.process(np.array([-3000, 3000], dtype=np.int16))
    polluted.on_audio_frame(frame, sync)

    np.testing.assert_array_equal(polluted.samples[0], clean.samples[0])


def test_finish_capture_freezes_and_clears_current_session():
    pipeline = VoicePipeline(sample_rate=4)
    pipeline.samples = [
        np.array([1, 2], dtype=np.int16),
        np.array([3, 4], dtype=np.int16),
    ]

    frozen = pipeline.finish_capture(minimum_duration=0)

    assert frozen is not None
    assert frozen.tolist() == [1, 2, 3, 4]
    assert pipeline.samples == []


def test_transcribe_uses_injected_model_and_joins_segments():
    class FakeModel:
        def transcribe(self, audio, **kwargs):
            assert audio.dtype == np.float32
            assert kwargs["language"] == "zh"
            return [SimpleNamespace(text=" 你好"), SimpleNamespace(text="，Linux ")], object()

    pipeline = VoicePipeline()
    pipeline._whisper_model = FakeModel()

    assert pipeline.transcribe(np.array([0, 32767], dtype=np.int16)) == "你好，Linux"


def test_auto_prefers_persistent_sherpa_paraformer(tmp_path, monkeypatch):
    (tmp_path / "model.int8.onnx").touch()
    (tmp_path / "tokens.txt").touch()
    monkeypatch.setattr(voice_module, "find_spec", lambda _name: object())

    pipeline = VoicePipeline(
        engine="auto",
        voxtype_path="/usr/bin/voxtype",
        voxtype_model_dir=tmp_path,
    )

    assert pipeline.active_engine == "sherpa-paraformer"


def test_auto_uses_voxtype_when_sherpa_is_not_installed(tmp_path, monkeypatch):
    (tmp_path / "model.int8.onnx").touch()
    (tmp_path / "tokens.txt").touch()
    monkeypatch.setattr(voice_module, "find_spec", lambda _name: None)

    pipeline = VoicePipeline(
        engine="auto",
        voxtype_path="/usr/bin/voxtype",
        voxtype_model_dir=tmp_path,
    )

    assert pipeline.active_engine == "voxtype-paraformer"


def test_auto_falls_back_to_whisper_without_paraformer(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_module, "find_spec", lambda _name: object())
    pipeline = VoicePipeline(
        engine="auto",
        voxtype_path="/usr/bin/voxtype",
        voxtype_model_dir=tmp_path,
    )

    assert pipeline.active_engine == "faster-whisper"


def test_sherpa_model_is_loaded_once_and_reused(tmp_path, monkeypatch):
    (tmp_path / "model.int8.onnx").touch()
    (tmp_path / "tokens.txt").touch()
    created = []

    class FakeStream:
        def __init__(self):
            self.result = SimpleNamespace(text=" 常驻识别 ")

        def accept_waveform(self, sample_rate, audio):
            assert sample_rate == 16000
            assert audio.dtype == np.float32

    class FakeRecognizer:
        def create_stream(self):
            return FakeStream()

        def decode_stream(self, _stream):
            pass

    class FakeOfflineRecognizer:
        @staticmethod
        def from_paraformer(**kwargs):
            assert kwargs["num_threads"] == 3
            created.append(kwargs)
            return FakeRecognizer()

    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(OfflineRecognizer=FakeOfflineRecognizer),
    )
    pipeline = VoicePipeline(
        engine="sherpa-paraformer",
        voxtype_model_dir=tmp_path,
        paraformer_threads=3,
    )
    samples = np.array([100, -100], dtype=np.int16)

    assert pipeline.transcribe(samples) == "常驻识别"
    assert pipeline.transcribe(samples) == "常驻识别"
    assert len(created) == 1


def test_near_silence_is_ignored_before_loading_model():
    pipeline = VoicePipeline(minimum_rms=25)

    assert pipeline.transcribe(np.zeros(16000, dtype=np.int16)) is None
    assert pipeline._whisper_model is None


def test_project_model_directory_is_discovered(tmp_path, monkeypatch):
    model_dir = tmp_path / "mi-remote-linux/models/paraformer-zh"
    model_dir.mkdir(parents=True)
    (model_dir / "model.int8.onnx").touch()
    (model_dir / "tokens.txt").touch()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(voice_module, "find_spec", lambda _name: object())

    pipeline = VoicePipeline(engine="auto")

    assert pipeline.active_engine == "sherpa-paraformer"
    assert pipeline._paraformer_model_dir == model_dir


def test_voxtype_transcription_uses_last_nonempty_output_line(tmp_path, monkeypatch):
    (tmp_path / "model.int8.onnx").touch()
    (tmp_path / "tokens.txt").touch()
    pipeline = VoicePipeline(
        engine="voxtype-paraformer",
        voxtype_path="voxtype",
        voxtype_model_dir=tmp_path,
    )

    def fake_run(command, **kwargs):
        assert command[:4] == ["voxtype", "transcribe", "--engine", "paraformer"]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Loading model...\n\n这是小米遥控器语音输入测试\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    text = pipeline.transcribe(np.array([0, 100, -100], dtype=np.int16))

    assert text == "这是小米遥控器语音输入测试"
