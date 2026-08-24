import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from mi_remote_linux.self_test import (
    SELF_TEST_KEYS,
    KeySelfTest,
    SelfTestCheck,
    SelfTestReport,
    VoiceSelfTest,
    render_self_test_report,
)


def test_self_test_report_supports_human_and_json_output():
    report = SelfTestReport(
        "keys",
        (
            SelfTestCheck("up", "上键", "pass", "code=103"),
            SelfTestCheck("voice", "语音键", "warn", "只检测到按下"),
        ),
    )

    assert report.exit_code == 0
    assert "1 通过，1 警告，0 失败" in render_self_test_report(report)
    assert json.loads(report.to_json())["suite"] == "keys"


class FakeHID:
    def __init__(self, callback):
        self.callback = callback
        self.paths = ("/dev/input/event17",)
        self.stopped = False

    async def start(self):
        loop = asyncio.get_running_loop()
        time_ns = 1
        for key, _label in SELF_TEST_KEYS:
            loop.call_soon(
                self.callback,
                SimpleNamespace(key=key, is_down=True, code=100 + time_ns, time_ns=time_ns),
            )
            loop.call_soon(
                self.callback,
                SimpleNamespace(
                    key=key,
                    is_down=False,
                    code=100 + time_ns,
                    time_ns=time_ns + 50_000_000,
                ),
            )
            time_ns += 1

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_key_self_test_collects_balanced_events_without_running_actions():
    prompts = []
    holder = {}

    def factory(callback):
        holder["engine"] = FakeHID(callback)
        return holder["engine"]

    report = await KeySelfTest(factory, prompts.append).run(timeout=0.2)

    assert report.exit_code == 0
    assert len(report.checks) == 13
    assert all(check.status == "pass" for check in report.checks)
    assert "请按：电源键" in prompts[0]
    assert holder["engine"].stopped is True


@pytest.mark.asyncio
async def test_key_self_test_times_out_with_an_actionable_failure():
    class SilentHID(FakeHID):
        async def start(self):
            pass

    report = await KeySelfTest(lambda callback: SilentHID(callback), lambda _text: None).run(
        timeout=0.001
    )

    assert report.exit_code == 1
    assert report.checks[0].status == "fail"
    assert "超时" in report.checks[0].detail


@pytest.mark.asyncio
async def test_voice_self_test_captures_one_utterance_and_reports_metrics():
    class FakePipeline:
        sample_rate = 16000
        active_engine = "fake-paraformer"

        def load_model(self):
            return True

        def on_voice_start(self):
            pass

        def on_audio_frame(self, _frame, _sync):
            pass

        def finish_capture(self):
            return np.full(16000, 500, dtype=np.int16)

        def transcribe(self, _samples):
            return "这是小米遥控器语音输入测试"

    class FakeClient:
        def __init__(self, **callbacks):
            self.callbacks = callbacks
            self.disconnected = False

        async def connect(self, _address):
            self.callbacks["on_voice_start"]()
            self.callbacks["on_voice_stop"]()
            return True

        async def disconnect(self):
            self.disconnected = True

    clients = []

    def client_factory(**callbacks):
        client = FakeClient(**callbacks)
        clients.append(client)
        return client

    report = await VoiceSelfTest(
        FakePipeline(),
        client_factory=client_factory,
        hid_factory=lambda callback: FakeHID(callback),
        prompt=lambda _text: None,
    ).run(timeout=0.2)

    assert report.exit_code == 0
    assert report.checks[0].status == "pass"
    assert report.checks[0].metrics["similarity"] == 1.0
    assert report.checks[0].metrics["duration_s"] == 1.0
    assert clients[0].disconnected is True


@pytest.mark.asyncio
async def test_voice_self_test_reuses_connection_for_multiple_utterances():
    class FakePipeline:
        sample_rate = 16000
        active_engine = "fake-paraformer"

        def load_model(self):
            return True

        def on_voice_start(self):
            pass

        def on_audio_frame(self, _frame, _sync):
            pass

        def finish_capture(self):
            return np.full(8000, 500, dtype=np.int16)

        def transcribe(self, _samples):
            return "连续语音测试"

    class FakeClient:
        def __init__(self, **callbacks):
            self.callbacks = callbacks
            self.connect_calls = 0
            self.disconnect_calls = 0

        async def connect(self, _address):
            self.connect_calls += 1
            return True

        async def disconnect(self):
            self.disconnect_calls += 1

    client = None

    def client_factory(**callbacks):
        nonlocal client
        client = FakeClient(**callbacks)
        return client

    def prompt(_text):
        client.callbacks["on_voice_start"]()
        client.callbacks["on_voice_stop"]()

    report = await VoiceSelfTest(
        FakePipeline(),
        client_factory=client_factory,
        hid_factory=lambda callback: FakeHID(callback),
        prompt=prompt,
    ).run(phrase="连续语音测试", count=5, timeout=0.2)

    assert report.exit_code == 0
    assert len(report.checks) == 5
    assert all(check.status == "pass" for check in report.checks)
    assert client.connect_calls == 1
    assert client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_voice_self_test_retries_transient_bluez_connection_race(monkeypatch):
    class FakePipeline:
        sample_rate = 16000
        active_engine = "fake"

        def load_model(self):
            return True

        def on_voice_start(self):
            pass

        def on_audio_frame(self, _frame, _sync):
            pass

        def finish_capture(self):
            return np.ones(1600, dtype=np.int16)

        def transcribe(self, _samples):
            return "测试"

    class FlakyClient:
        def __init__(self, **callbacks):
            self.callbacks = callbacks
            self.connect_calls = 0

        async def connect(self, _address):
            self.connect_calls += 1
            return self.connect_calls == 2

        async def disconnect(self):
            pass

    client = None

    def client_factory(**callbacks):
        nonlocal client
        client = FlakyClient(**callbacks)
        return client

    def prompt(text):
        if "按住语音键" in text:
            client.callbacks["on_voice_start"]()
            client.callbacks["on_voice_stop"]()

    sleep = AsyncMock()
    monkeypatch.setattr("mi_remote_linux.self_test.asyncio.sleep", sleep)
    report = await VoiceSelfTest(
        FakePipeline(),
        client_factory=client_factory,
        hid_factory=lambda callback: FakeHID(callback),
        prompt=prompt,
    ).run(phrase="测试", timeout=0.2)

    assert report.exit_code == 0
    assert client.connect_calls == 2
    sleep.assert_awaited_once_with(2)
