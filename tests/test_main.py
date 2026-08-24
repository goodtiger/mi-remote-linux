"""CLI 语音结果输出测试。"""

from unittest.mock import AsyncMock

import numpy as np
import pytest

from mi_remote_linux.main import VoiceApp


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
