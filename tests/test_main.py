"""CLI 语音结果输出测试。"""

import contextlib
import json
import sys
from unittest.mock import AsyncMock

import numpy as np
import pytest

from mi_remote_linux import __version__
from mi_remote_linux import main as main_module
from mi_remote_linux.doctor import DoctorCheck, DoctorReport
from mi_remote_linux.main import VoiceApp
from mi_remote_linux.self_test import SelfTestCheck, SelfTestReport
from mi_remote_linux.text_corrector import TextCorrector


def test_cli_reports_package_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mi-remote", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"mi-remote {__version__}"


@pytest.mark.asyncio
async def test_transcription_is_injected_when_enabled(capsys):
    injector = AsyncMock()
    app = VoiceApp(injector=injector)
    app.pipeline.transcribe = lambda _samples: "你好，焦点窗口"
    app._transcription_lock = __import__("asyncio").Lock()

    await app._transcribe_and_emit(np.array([1, 2], dtype=np.int16))

    injector.inject.assert_awaited_once_with("你好，焦点窗口")
    assert capsys.readouterr().out == "你好，焦点窗口\n"


@pytest.mark.asyncio
async def test_stdout_mode_does_not_require_an_injector(capsys):
    app = VoiceApp()
    app.pipeline.transcribe = lambda _samples: "只输出"
    app._transcription_lock = __import__("asyncio").Lock()

    await app._transcribe_and_emit(np.array([1, 2], dtype=np.int16))

    assert capsys.readouterr().out == "只输出\n"


@pytest.mark.asyncio
async def test_corrected_text_is_emitted_and_injected(capsys):
    injector = AsyncMock()
    app = VoiceApp(
        injector=injector,
        text_corrector=TextCorrector({"电脑号本": "GitHub"}),
    )
    app.pipeline.transcribe = lambda _samples: "提交到电脑号本"
    app._transcription_lock = __import__("asyncio").Lock()

    await app._transcribe_and_emit(np.array([1, 2], dtype=np.int16))

    assert capsys.readouterr().out == "提交到GitHub\n"
    injector.inject.assert_awaited_once_with("提交到GitHub")


@pytest.mark.asyncio
async def test_run_retries_failed_connection_until_success():
    app = VoiceApp(reconnect_initial_delay=0, reconnect_max_delay=0)
    app.pipeline.load_model = lambda: True
    attempts = 0

    async def connect(_address):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        assert app._stop_event is not None
        app._stop_event.set()
        return True

    app.client = AsyncMock()
    app.client.connect.side_effect = connect

    await app.run()

    assert attempts == 2
    app.client.disconnect.assert_awaited_once_with(restore_hid=True)


def test_disconnect_requests_reconnect_without_stopping_app():
    app = VoiceApp()
    app._stop_event = __import__("asyncio").Event()
    app._connection_lost_event = __import__("asyncio").Event()

    app._on_disconnected()

    assert app._connection_lost_event.is_set()
    assert not app._stop_event.is_set()


def test_config_show_outputs_packaged_default(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mi-remote", "config", "show"])

    main_module.main()

    raw = json.loads(capsys.readouterr().out)
    assert raw["version"] == 2
    assert len(raw["bindings"]) == 13


def test_config_validate_accepts_packaged_default(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mi-remote", "config", "validate", "default"])

    main_module.main()

    assert "13 个全局按键" in capsys.readouterr().out


def test_doctor_cli_outputs_machine_readable_json(monkeypatch, capsys):
    report = DoctorReport((DoctorCheck("platform", "系统", "pass", "Linux test"),))

    class FakeDoctor:
        async def run(self, **kwargs):
            assert kwargs == {"address": "AA:BB", "model_dir": "/models"}
            return report

    monkeypatch.setattr(main_module, "Doctor", FakeDoctor)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mi-remote", "doctor", "--address", "AA:BB", "--model-dir", "/models", "--json"],
    )

    main_module.main()

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["checks"][0]["key"] == "platform"


def test_doctor_cli_returns_nonzero_when_required_check_fails(monkeypatch, capsys):
    report = DoctorReport((DoctorCheck("remote", "蓝牙遥控器", "fail", "未找到"),))

    class FakeDoctor:
        async def run(self, **_kwargs):
            return report

    monkeypatch.setattr(main_module, "Doctor", FakeDoctor)
    monkeypatch.setattr(sys, "argv", ["mi-remote", "doctor"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert "1 失败" in capsys.readouterr().out


def test_desktop_self_test_cli_outputs_json_without_hardware_lock(monkeypatch, capsys):
    report = SelfTestReport(
        "desktop", (SelfTestCheck("notification", "桌面通知", "pass", "发送成功"),)
    )

    class FakeDesktopTest:
        def __init__(self, *, session):
            assert session == "wayland"

        async def run(self, *, inject):
            assert inject is False
            return report

    monkeypatch.setattr(main_module, "DesktopSelfTest", FakeDesktopTest)
    monkeypatch.setattr(
        sys, "argv", ["mi-remote", "test", "desktop", "--session", "wayland", "--json"]
    )

    main_module.main()

    assert json.loads(capsys.readouterr().out)["suite"] == "desktop"


def test_model_status_cli_outputs_json(monkeypatch, capsys):
    class FakeStatus:
        ready = True

        @staticmethod
        def to_json():
            return '{"ready": true, "target": "/models"}'

    class FakeModelManager:
        def __init__(self, *, target):
            assert target == "/models"

        @staticmethod
        def status():
            return FakeStatus()

    monkeypatch.setattr(main_module, "ModelManager", FakeModelManager)
    monkeypatch.setattr(
        sys, "argv", ["mi-remote", "model", "status", "--target", "/models", "--json"]
    )

    main_module.main()

    assert json.loads(capsys.readouterr().out)["ready"] is True


def test_model_status_cli_returns_nonzero_for_invalid_model(monkeypatch, capsys):
    class FakeStatus:
        ready = False

    class FakeModelManager:
        def __init__(self, *, target):
            assert target is None

        @staticmethod
        def status():
            return FakeStatus()

    monkeypatch.setattr(main_module, "ModelManager", FakeModelManager)
    monkeypatch.setattr(main_module, "render_model_status", lambda _status: "状态：不可用")
    monkeypatch.setattr(sys, "argv", ["mi-remote", "model", "status"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert "状态：不可用" in capsys.readouterr().out


def test_voice_test_count_rejects_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mi-remote", "test", "voice", "--count", "0"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 2
    assert "必须大于等于 1" in capsys.readouterr().err


def test_grab_hid_is_reported_as_ignored_when_config_takes_over(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["mi-remote", "voice", "--config", "default", "--grab-hid", "off"],
    )
    monkeypatch.setattr(
        main_module.LinuxActionRunner,
        "missing_dependencies",
        lambda _self: [],
    )
    started = []
    monkeypatch.setattr(main_module.asyncio, "run", lambda coroutine: coroutine.close())
    monkeypatch.setattr(main_module, "VoiceInstanceLock", contextlib.nullcontext)
    monkeypatch.setattr(
        main_module,
        "RemoteKeyService",
        lambda *args, **kwargs: started.append(args) or object(),
    )

    main_module.main()

    assert "--grab-hid 本次被忽略" in capsys.readouterr().err
