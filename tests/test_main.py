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
