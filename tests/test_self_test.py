import asyncio
import json
from types import SimpleNamespace

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
