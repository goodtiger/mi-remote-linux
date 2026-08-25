"""VoiceDrainTracker 单元测试 + ATVVClient 排空集成测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mi_remote_linux.voice_drain import (
    OUTPUT_TAIL_SECONDS,
    TIMEOUT_SECONDS,
    VoiceDrainTracker,
)

# ---------------------------------------------------------------------------
# VoiceDrainTracker 纯逻辑测试
# ---------------------------------------------------------------------------


class TestVoiceDrainTracker:
    """排空跟踪器纯数据逻辑。"""

    def test_constants(self):
        assert OUTPUT_TAIL_SECONDS == pytest.approx(0.020)
        assert TIMEOUT_SECONDS == pytest.approx(0.170)

    def test_start_sets_deadline_and_last_activity(self):
        tracker = VoiceDrainTracker()
        tracker.start(1.0)
        assert tracker.deadline == pytest.approx(1.0 + TIMEOUT_SECONDS)
        assert tracker.last_activity == pytest.approx(1.0)

    def test_should_release_false_immediately_after_start(self):
        tracker = VoiceDrainTracker()
        tracker.start(1.0)
        # 刚启动，还没到 20ms 尾部静默
        assert tracker.should_release(1.0) is False
        assert tracker.should_release(1.010) is False

    def test_should_release_true_after_tail_silence(self):
        tracker = VoiceDrainTracker()
        tracker.start(1.0)
        # 20ms 后应释放
        assert tracker.should_release(1.0 + OUTPUT_TAIL_SECONDS) is True

    def test_should_release_true_at_deadline(self):
        tracker = VoiceDrainTracker()
        tracker.start(1.0)
        # 170ms 总超时
        assert tracker.should_release(1.0 + TIMEOUT_SECONDS) is True
        assert tracker.should_release(1.0 + TIMEOUT_SECONDS + 0.05) is True

    def test_on_activity_resets_tail_timer(self):
        tracker = VoiceDrainTracker()
        tracker.start(1.0)
        # 10ms 后收到新数据
        tracker.on_activity(1.010)
        assert tracker.last_activity == pytest.approx(1.010)
        # 原 20ms 时刻不应释放（因为 last_activity 被更新了）
        assert tracker.should_release(1.020) is False
        # 新 last_activity + 20ms 后才释放
        assert tracker.should_release(1.010 + OUTPUT_TAIL_SECONDS) is True

    def test_repeated_activity_keeps_extending(self):
        tracker = VoiceDrainTracker()
        tracker.start(1.0)
        # 每 5ms 收到新数据，持续到 100ms
        for i in range(1, 21):
            tracker.on_activity(1.0 + i * 0.005)
        # 100ms 时刻不应释放（last_activity = 1.100）
        assert tracker.should_release(1.100) is False
        # 120ms 才释放
        assert tracker.should_release(1.120) is True

    def test_deadline_overrides_tail_silence(self):
        tracker = VoiceDrainTracker()
        tracker.start(1.0)
        # 160ms 时收到新数据（last_activity = 1.160）
        tracker.on_activity(1.160)
        # 170ms 总超时到了，即使 last_activity 才 10ms 前，也应强制释放
        assert tracker.should_release(1.170) is True

    def test_no_activity_then_deadline(self):
        tracker = VoiceDrainTracker()
        tracker.start(0.0)
        # 没有任何 on_activity 调用
        # 20ms 后就应该释放（因为 start 时 last_activity=0.0）
        assert tracker.should_release(0.020) is True
        # 但 170ms 总超时也到了
        assert tracker.should_release(0.170) is True


# ---------------------------------------------------------------------------
# ATVVClient 排空集成测试（async）
# ---------------------------------------------------------------------------


def _make_client(
    on_voice_stop=None,
    on_audio_frame=None,
):
    """构造一个不连接真实 BLE 的 ATVVClient 用于测试。"""
    from mi_remote_linux.ble_client import ATVVClient

    client = ATVVClient(
        on_voice_stop=on_voice_stop,
        on_audio_frame=on_audio_frame,
    )
    # 模拟已连接状态
    client._streaming = True
    client._caps_ready = True
    client._frame_format_probed = True
    client._header_mode = False
    client.capabilities.frame_size = 120
    client.capabilities.protocol_version = 0x0100
    client.capabilities.session_id = 1
    client._tx_char = MagicMock()
    client._tx_char.properties = {"write-without-response"}
    client.client = MagicMock()
    client.client.is_connected = True
    client.client.write_gatt_char = AsyncMock()
    return client


class TestATVVClientDrain:
    """ATVVClient 排空行为集成测试。"""

    @pytest.mark.asyncio
    async def test_drain_waits_for_tail_silence(self):
        """排空期间无新数据，应在 ~20ms 后释放。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)

        # 触发 stream stop → 启动排空
        client._handle_stream_stop()

        assert client._draining is True
        assert client._drain_tracker is not None
        assert client._drain_task is not None

        # 等待排空完成
        await asyncio.wait_for(stop_called.wait(), timeout=1.0)

        # 排空完成后状态应重置
        assert client._draining is False
        assert client._streaming is False
        assert client._drain_tracker is None

    @pytest.mark.asyncio
    async def test_drain_activity_resets_timer(self):
        """排空期间收到新数据应重置尾部计时。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)

        client._handle_stream_stop()
        assert client._draining is True

        # 10ms 后发送新数据（残片，不足以拼成完整帧）
        await asyncio.sleep(0.010)
        client._handle_audio_notify(None, bytearray(b"\x01" * 50))

        # 此时不应释放（last_activity 被更新）
        await asyncio.sleep(0.010)
        assert not stop_called.is_set()

        # 再等 20ms 后应释放
        await asyncio.wait_for(stop_called.wait(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_drain_forced_timeout(self):
        """170ms 总超时后强制释放，即使持续有数据。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)

        client._handle_stream_stop()

        # 每 5ms 发送新数据，持续到超过 170ms
        async def send_data():
            for _ in range(40):  # 200ms
                await asyncio.sleep(0.005)
                if client._streaming:
                    client._handle_audio_notify(None, bytearray(b"\x01" * 50))
                else:
                    break

        task = asyncio.create_task(send_data())

        # 应在 170ms 左右释放（允许一些误差）
        await asyncio.wait_for(stop_called.wait(), timeout=0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_new_stream_start_cancels_drain(self):
        """排空中收到新 AUDIO_START，旧排空任务应被取消，不关闭新会话。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)

        # 第一次 stream stop → 启动排空
        client._handle_stream_stop()
        assert client._draining is True
        old_drain_task = client._drain_task

        # 10ms 后收到新 AUDIO_START
        await asyncio.sleep(0.010)
        client._handle_stream_start(bytearray([0x04, 0x00, 0x00, 0x02]))

        # 给事件循环一次机会让 cancel 传播
        await asyncio.sleep(0)

        # 旧排空任务应被取消
        assert old_drain_task.cancelled() or old_drain_task.done()
        # 新会话状态
        assert client._draining is False
        assert client._streaming is True
        assert client._drain_tracker is None
        # voice_stop 不应被调用
        assert not stop_called.is_set()

    @pytest.mark.asyncio
    async def test_drain_does_not_call_voice_stop_twice(self):
        """排空完成只触发一次 voice_stop。"""
        call_count = 0

        def on_stop():
            nonlocal call_count
            call_count += 1

        client = _make_client(on_voice_stop=on_stop)
        client._handle_stream_stop()

        # 等待排空完成
        await asyncio.sleep(0.1)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_drain_sends_mic_close(self):
        """排空完成后应发送 MIC_CLOSE。"""
        client = _make_client()
        client._handle_stream_stop()

        await asyncio.sleep(0.1)

        # 应发送了 MIC_CLOSE
        client.client.write_gatt_char.assert_called()
        calls = client.client.write_gatt_char.call_args_list
        assert len(calls) >= 1
        last_call = calls[-1]
        # MIC_CLOSE for v1: [0x0D, session_id]
        assert last_call[0][1] == bytes([0x0D, 0x01])

    @pytest.mark.asyncio
    async def test_duplicate_stream_stop_ignored(self):
        """排空期间重复的 stream stop 应被忽略。"""
        client = _make_client()
        client._handle_stream_stop()
        assert client._draining is True

        # 再次调用 stream stop
        client._handle_stream_stop()

        # 仍只有一个排空任务
        assert client._drain_task is not None
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_drain_with_partial_frame_data(self):
        """排空期间收到不足一帧的残片数据也应算新活动。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)

        client._handle_stream_stop()

        # 10ms 后发送 30 字节残片（不足 120 字节一帧）
        await asyncio.sleep(0.010)
        client._handle_audio_notify(None, bytearray(b"\xAB" * 30))

        # 此时不应释放
        await asyncio.sleep(0.010)
        assert not stop_called.is_set()

        # 再等 20ms 后应释放
        await asyncio.wait_for(stop_called.wait(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_disconnect_cancels_drain(self):
        """disconnect 应取消排空任务。"""
        client = _make_client()
        client._handle_stream_stop()
        assert client._draining is True

        await client.disconnect(restore_hid=False)

        assert client._draining is False
        assert client._drain_task is None
        assert client._drain_tracker is None

    @pytest.mark.asyncio
    async def test_ble_disconnect_callback_cancels_drain(self):
        """BLE 断连回调 _handle_disconnected 必须取消排空任务。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)
        client._connection_active = True

        client._handle_stream_stop()
        assert client._draining is True
        old_task = client._drain_task

        # 模拟 BLE 断连回调
        client._handle_disconnected(client.client, client._generation)

        # 给事件循环一次机会让 cancel 传播
        await asyncio.sleep(0)

        assert client._draining is False
        assert client._drain_task is None
        assert client._drain_tracker is None
        # voice_stop 不应被调用（排空被取消，未正常完成）
        assert not stop_called.is_set()
        # 旧任务应被取消
        assert old_task.cancelled() or old_task.done()

    @pytest.mark.asyncio
    async def test_drain_exception_is_handled(self):
        """排空任务中 _send_mic_close 等步骤异常时应被记录，不产生未消费异常。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)

        # 让 _send_mic_close 抛出异常
        client._send_mic_close = AsyncMock(side_effect=RuntimeError("BLE write failed"))

        client._handle_stream_stop()
        assert client._draining is True

        # 等待排空完成（应该因异常而结束）
        await asyncio.sleep(0.15)

        # 状态应被清理
        assert client._draining is False
        assert client._drain_task is None
        assert client._drain_tracker is None
        # voice_stop 应被调用（异常发生在 _send_mic_close，在 voice_stop 之后）
        assert stop_called.is_set()

    @pytest.mark.asyncio
    async def test_drain_exception_before_voice_stop(self):
        """排空循环内部异常不应触发 voice_stop。"""
        stop_called = asyncio.Event()
        client = _make_client(on_voice_stop=stop_called.set)

        def raise_during_drain(*args, **kwargs):
            raise RuntimeError("tracker error")

        client._handle_stream_stop()
        # 替换 tracker 的 should_release 方法
        client._drain_tracker.should_release = raise_during_drain

        await asyncio.sleep(0.05)

        assert client._draining is False
        # voice_stop 不应被调用
        assert not stop_called.is_set()

    @pytest.mark.asyncio
    async def test_cancel_drain_does_not_clear_new_task(self):
        """旧任务被取消后不应覆盖新任务的状态。

        真实顺序：
        1. 启动旧 drain
        2. stream start 取消旧 drain
        3. 不 yield 就立刻 stream stop 创建新 drain
        4. 再 yield 让旧取消传播
        5. 断言新 task/tracker/_draining 仍完整
        """
        client = _make_client()

        # 1. 启动旧 drain
        client._handle_stream_stop()
        assert client._draining is True
        old_task = client._drain_task
        old_tracker = client._drain_tracker

        # 2. stream start 取消旧 drain（不 yield）
        client._handle_stream_start(bytearray([0x04, 0x00, 0x00, 0x02]))
        assert client._draining is False
        assert client._drain_task is None
        assert client._streaming is True

        # 3. 不 yield 就立刻 stream stop 创建新 drain
        client._handle_stream_stop()
        assert client._draining is True
        new_task = client._drain_task
        new_tracker = client._drain_tracker

        # 新任务应该与旧任务不同
        assert new_task is not old_task
        assert new_tracker is not old_tracker

        # 4. 让事件循环运行，旧任务的取消传播
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # 5. 新任务的状态应完整，未被旧任务覆盖
        assert client._draining is True
        assert client._drain_task is new_task
        assert client._drain_tracker is new_tracker

        # 清理
        client._cancel_drain()
