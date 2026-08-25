"""BlueZ/bleak ATVV 客户端。

负责发现遥控器、完成 ATVV 握手，并把对齐后的 ADPCM 帧交给语音管道。
协议字段与音频帧探测逻辑保持和 macOS 参考实现一致。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Iterable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType, MessageType
from dbus_fast.message import Message

from .adpcm import FrameAccumulator
from .atvv import (
    ATVV_AUDIO_UUID,
    ATVV_CONTROL_UUID,
    ATVV_SERVICE_UUID,
    ATVV_TX_UUID,
    GET_CAPS_COMMAND,
    OP_AUDIO_START,
    OP_AUDIO_STOP,
    OP_CAPS,
    OP_MIC_REQUEST,
    OP_SYNC,
    ATVVCapabilities,
    SyncFrame,
    make_mic_close_command,
    make_mic_open_command,
    parse_be16,
    parse_capabilities,
    parse_stream_session_id,
    parse_sync_frame,
)
from .voice_drain import VoiceDrainTracker

logger = logging.getLogger(__name__)

BLUEZ_DEVICE_INTERFACE = "org.bluez.Device1"


def _unwrap_bluez_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """把 ObjectManager 返回的 Variant 字典转成 Bleak 使用的普通值。"""
    return {
        key: value.value if hasattr(value, "value") else value for key, value in properties.items()
    }


def _select_known_bluez_device(
    objects: dict[str, dict[str, dict[str, Any]]],
    address: str | None,
) -> BLEDevice | None:
    """从 BlueZ 已知对象中选出指定地址或最可信的 ATVV 遥控器。"""
    requested_address = address.casefold() if address else None
    candidates: list[tuple[int, BLEDevice]] = []

    for path, interfaces in objects.items():
        variant_properties = interfaces.get(BLUEZ_DEVICE_INTERFACE)
        if variant_properties is None:
            continue
        properties = _unwrap_bluez_properties(variant_properties)
        device_address = str(properties.get("Address", ""))
        if not device_address:
            continue

        name = str(properties.get("Alias") or properties.get("Name") or "")
        if requested_address:
            if device_address.casefold() != requested_address:
                continue
            score = 100
        else:
            uuids = {str(uuid).casefold() for uuid in properties.get("UUIDs", [])}
            normalized_name = name.casefold()
            service_match = ATVV_SERVICE_UUID.casefold() in uuids
            name_match = any(
                marker in normalized_name for marker in ("小米蓝牙遥控器", "mi rc", "xiaomi remote")
            )
            if not service_match and not name_match:
                continue
            score = 20 if service_match else 10

        if properties.get("Connected"):
            score += 2
        if properties.get("Paired"):
            score += 1
        candidates.append(
            (
                score,
                BLEDevice(
                    device_address,
                    name or None,
                    {"path": path, "props": properties},
                ),
            )
        )

    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


async def find_known_bluez_remote(address: str | None = None) -> BLEDevice | None:
    """Read BlueZ ObjectManager state without scanning or connecting."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
        if reply.message_type == MessageType.ERROR or not reply.body:
            logger.debug("BlueZ 已知设备查询失败: %s", reply.error_name)
            return None
        return _select_known_bluez_device(reply.body[0], address)
    finally:
        bus.disconnect()


class ATVVClient:
    """连接小米遥控器并运行 ATVV 语音状态机。"""

    def __init__(
        self,
        on_audio_frame: Callable[[bytes, SyncFrame | None], None] | None = None,
        on_voice_start: Callable[[], None] | None = None,
        on_voice_stop: Callable[[], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
    ):
        self.client: BleakClient | None = None
        self.capabilities = ATVVCapabilities()

        self._tx_char: Any | None = None
        self._streaming = False
        self._caps_ready = False
        self._mic_open_pending = False
        self._caps_event = asyncio.Event()
        self._connection_active = False
        self._connected_notified = False
        self._disconnected_notified = False
        self._suppress_disconnect_notification = False
        self._restore_device_path: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._generation = 0

        self._accumulator = FrameAccumulator()
        self._pending_sync: SyncFrame | None = None
        self._header_mode = False
        self._frame_format_probed = False
        self._probe_buffer = bytearray()
        self._waiting_for_resync = False
        self._draining = False
        self._drain_task: asyncio.Task[Any] | None = None
        self._drain_tracker: VoiceDrainTracker | None = None

        self._on_audio_frame = on_audio_frame
        self._on_voice_start = on_voice_start
        self._on_voice_stop = on_voice_stop
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

    async def connect(
        self,
        address: str | None = None,
        timeout: float = 10.0,
        handshake_timeout: float = 5.0,
    ) -> bool:
        """连接并等待 ATVV 能力协商完成。"""
        if self.client is not None:
            await self.disconnect(restore_hid=False)
        generation = self._reset_connection_state()

        try:
            device = await asyncio.wait_for(self._find_known_remote(address), timeout=timeout)
            if device:
                logger.info("使用 BlueZ 已知设备: %s (%s)", device.name or "?", device.address)
            elif address:
                device = await BleakScanner.find_device_by_address(address, timeout=timeout)
            else:
                device = await self._scan_for_remote(timeout)
        except Exception as exc:  # noqa: BLE001 - bleak 后端会抛出多种平台特定异常
            logger.error("扫描遥控器失败: %s", exc)
            return False

        if not device:
            logger.error("未找到遥控器")
            return False

        if isinstance(device.details, dict):
            known_device_path = device.details.get("path")
            if known_device_path:
                self._restore_device_path = known_device_path

        logger.info("连接遥控器: %s (%s)", device.name or "?", device.address)
        client = BleakClient(
            device,
            disconnected_callback=lambda disconnected_client: self._handle_disconnected(
                disconnected_client,
                generation,
            ),
        )
        self.client = client

        try:
            try:
                await asyncio.wait_for(client.connect(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"BLE 连接超时（{timeout:.1f}s）") from exc
            self._connection_active = True
            logger.info("BLE 连接成功")

            atvv_service = next(
                (
                    service
                    for service in client.services
                    if service.uuid.lower() == ATVV_SERVICE_UUID.lower()
                ),
                None,
            )
            if atvv_service is None:
                raise RuntimeError("未找到 ATVV 服务")

            characteristics = {char.uuid.lower(): char for char in atvv_service.characteristics}
            self._tx_char = characteristics.get(ATVV_TX_UUID.lower())
            audio_char = characteristics.get(ATVV_AUDIO_UUID.lower())
            control_char = characteristics.get(ATVV_CONTROL_UUID.lower())
            if self._tx_char is None or audio_char is None or control_char is None:
                raise RuntimeError("ATVV 特征不完整（需要 TX、audio、control）")

            await client.start_notify(
                audio_char,
                lambda sender, data: self._handle_audio_notify(sender, data, generation),
            )
            await client.start_notify(
                control_char,
                lambda sender, data: self._handle_control_notify(sender, data, generation),
            )
            logger.debug("已订阅 ATVV audio/control notify")
            await self._write_command(GET_CAPS_COMMAND, "GET_CAPS")

            try:
                await asyncio.wait_for(self._caps_event.wait(), timeout=handshake_timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("ATVV 能力协商超时") from exc
            if not self._caps_ready:
                raise RuntimeError("ATVV 能力无效或不支持 16 kHz codec")
            return True
        except Exception as exc:  # noqa: BLE001 - 统一收敛 bleak/DBus 连接错误
            logger.error("连接失败: %s", exc)
            await self.disconnect(restore_hid=False)
            return False

    async def disconnect(self, *, restore_hid: bool = True, timeout: float = 5.0) -> None:
        """在有界时间内主动断开；最终退出时恢复 BlueZ/HID 系统连接。"""
        client = self.client
        self._suppress_disconnect_notification = True
        # 先让本代回调失效，避免 disconnect() 或迟到 notify 污染下一次连接。
        self._generation += 1
        self._cancel_drain()
        self._cancel_tasks()
        if client and client.is_connected:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=timeout)
                logger.info("已断开 ATVV 客户端")
            except asyncio.TimeoutError:
                logger.warning("ATVV 客户端断开超时（%.1fs），继续清理", timeout)
            except Exception as exc:  # noqa: BLE001 - bleak 后端异常类型随平台变化
                logger.warning("ATVV 客户端断开失败，继续清理: %s", exc)
        if restore_hid and self._restore_device_path:
            try:
                await asyncio.wait_for(
                    self._restore_bluez_connection(self._restore_device_path),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("恢复遥控器 BlueZ/HID 连接总超时")
            except Exception as exc:  # noqa: BLE001 - D-Bus 异常类型随平台变化
                logger.warning("恢复遥控器 BlueZ/HID 连接失败: %s", exc)
        self.client = None
        self._connection_active = False
        self._streaming = False

    async def _scan_for_remote(self, timeout: float) -> BLEDevice | None:
        """优先按 ATVV service UUID 扫描，并兼容按广播名称识别。"""
        logger.info("扫描遥控器（超时 %.1fs）...", timeout)

        def matches(device: BLEDevice, advertisement: AdvertisementData) -> bool:
            service_uuids = {uuid.lower() for uuid in advertisement.service_uuids or []}
            if ATVV_SERVICE_UUID.lower() in service_uuids:
                return True
            name = advertisement.local_name or device.name or ""
            normalized = name.casefold()
            return any(marker in normalized for marker in ("小米蓝牙遥控器", "mi rc", "xiaomi"))

        device = await BleakScanner.find_device_by_filter(matches, timeout=timeout)
        if device:
            logger.info("发现遥控器: %s (%s)", device.name or "?", device.address)
        return device

    async def _find_known_remote(self, address: str | None) -> BLEDevice | None:
        """查询 BlueZ ObjectManager，覆盖已连接 HID 设备不再广播的情况。"""
        return await find_known_bluez_remote(address)

    async def _restore_bluez_connection(self, device_path: str) -> None:
        """Bleak 退出会断开共享 BLE 链路；重新连接以恢复 HID 输入。"""
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            try:
                reply = await asyncio.wait_for(
                    bus.call(
                        Message(
                            destination="org.bluez",
                            path=device_path,
                            interface=BLUEZ_DEVICE_INTERFACE,
                            member="Connect",
                        )
                    ),
                    timeout=8.0,
                )
            except asyncio.TimeoutError:
                logger.warning("恢复遥控器系统连接超时；按任意键可唤醒并由 BlueZ 重连")
                return
            if reply.message_type == MessageType.ERROR and reply.error_name not in {
                "org.bluez.Error.AlreadyConnected",
                "org.bluez.Error.InProgress",
            }:
                logger.warning("恢复遥控器系统连接失败: %s", reply.error_name)
                return
            logger.info("已恢复遥控器的 BlueZ/HID 连接")
        finally:
            bus.disconnect()

    async def _write_command(
        self,
        payload: bytes,
        label: str,
        *,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._generation:
            logger.debug("忽略过期 ATVV 命令 %s（generation=%d）", label, generation)
            return

        client = self.client
        tx_char = self._tx_char
        if not client or tx_char is None:
            raise RuntimeError("ATVV TX 特征尚未就绪")

        properties = {str(value).lower() for value in tx_char.properties}
        if "write-without-response" in properties:
            response = False
        elif "write" in properties:
            response = True
        else:
            raise RuntimeError("ATVV TX 特征不可写")

        await client.write_gatt_char(tx_char, payload, response=response)
        logger.debug("已发送 %s: %s（response=%s）", label, payload.hex(), response)

    async def _send_mic_open(self, generation: int | None = None) -> None:
        command = make_mic_open_command(self.capabilities.protocol_version)
        await self._write_command(command, "MIC_OPEN", generation=generation)

    async def _send_mic_close(self, generation: int | None = None) -> None:
        command = make_mic_close_command(
            self.capabilities.protocol_version,
            self.capabilities.session_id,
        )
        await self._write_command(command, "MIC_CLOSE", generation=generation)

    def _handle_disconnected(
        self,
        disconnected_client: BleakClient,
        generation: int | None = None,
    ) -> None:
        if generation is not None and (
            generation != self._generation or disconnected_client is not self.client
        ):
            logger.debug("忽略过期 BLE 断开回调（generation=%d）", generation)
            return
        log = logger.debug if self._suppress_disconnect_notification else logger.warning
        log("BLE 连接断开")
        should_notify = self._connection_active or self._connected_notified
        self._connection_active = False
        self._streaming = False
        self._cancel_drain()
        self._caps_event.set()
        if should_notify and not self._suppress_disconnect_notification:
            self._notify_disconnected()

    def _notify_disconnected(self) -> None:
        if self._disconnected_notified:
            return
        self._disconnected_notified = True
        if self._on_disconnected:
            self._on_disconnected()

    def _handle_control_notify(
        self,
        _sender: Any,
        data: bytearray,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._generation:
            logger.debug("忽略过期 CONTROL notify（generation=%d）", generation)
            return
        if not data:
            return

        op = data[0]
        logger.debug("CONTROL notify: op=0x%02X, data=%s", op, data.hex())
        raw = bytes(data)
        if op == OP_CAPS:
            self._handle_caps(raw, generation)
        elif op == OP_MIC_REQUEST:
            self._handle_mic_request(generation)
        elif op == OP_AUDIO_START:
            self._handle_stream_start(raw)
        elif op == OP_SYNC:
            self._handle_sync_frame(raw)
        elif op == OP_AUDIO_STOP:
            self._handle_stream_stop(generation)
        else:
            logger.debug("忽略未知 CONTROL opcode 0x%02X", op)

    def _handle_caps(self, data: bytes, generation: int | None = None) -> None:
        if self._caps_ready:
            logger.debug("忽略本次连接内重复的 CAPS 帧")
            return
        capabilities = parse_capabilities(data)
        if capabilities is None:
            logger.error("能力帧无效或不支持 16 kHz codec: %s", data.hex())
            self._caps_event.set()
            return

        session_id = self.capabilities.session_id
        self.capabilities = capabilities
        self.capabilities.session_id = session_id
        self._caps_ready = True
        self._caps_event.set()
        logger.info(
            "ATVV 能力: version=0x%04X, codecs=0x%04X, frame_size=%d",
            capabilities.protocol_version,
            capabilities.codec_mask,
            capabilities.frame_size,
        )

        if not self._connected_notified:
            self._connected_notified = True
            if self._on_connected:
                self._on_connected()

        if self._mic_open_pending:
            self._mic_open_pending = False
            active_generation = self._generation if generation is None else generation
            self._schedule(
                self._send_mic_open(active_generation),
                generation=active_generation,
            )

    def _handle_mic_request(self, generation: int | None = None) -> None:
        logger.debug("收到 MIC_REQUEST")
        if self._caps_ready:
            active_generation = self._generation if generation is None else generation
            self._schedule(
                self._send_mic_open(active_generation),
                generation=active_generation,
            )
        else:
            self._mic_open_pending = True
            logger.debug("MIC_REQUEST 挂起（caps 未就绪）")

    def _handle_stream_start(self, data: bytes) -> None:
        self.capabilities.session_id = parse_stream_session_id(data)
        self._streaming = True
        self._cancel_drain()
        self._pending_sync = None
        self._header_mode = False
        self._frame_format_probed = False
        self._probe_buffer.clear()
        self._waiting_for_resync = False
        self._accumulator = FrameAccumulator(self.capabilities.frame_size)

        logger.info("音频流开始: session_id=0x%02X", self.capabilities.session_id)
        if self._on_voice_start:
            self._on_voice_start()

    def _handle_sync_frame(self, data: bytes) -> None:
        sync = parse_sync_frame(data)
        if sync is None:
            logger.warning("忽略格式错误的 SYNC 帧: %s", data.hex())
            return

        self._accumulator.reset()
        self._probe_buffer.clear()
        if self._waiting_for_resync:
            self._waiting_for_resync = False
            self._frame_format_probed = False
            logger.info("收到 SYNC，恢复音频帧探测")
        self._pending_sync = sync
        logger.debug("同步帧: predictor=%d, step_index=%d", sync.predictor, sync.step_index)

    def _handle_stream_stop(self, generation: int | None = None) -> None:
        # MIC_CLOSE 会引来遥控器再回一个 0x00，门控避免来回应答。
        if not self._streaming or self._draining:
            return
        # 不立即结束会话；启动排空任务，等待尾部静默。
        self._draining = True
        active_generation = self._generation if generation is None else generation

        tracker = VoiceDrainTracker()
        tracker.start(time.monotonic())
        self._drain_tracker = tracker

        async def _run_drain() -> None:
            if active_generation != self._generation:
                return
            try:
                await self._drain_and_stop(active_generation, tracker)
            except asyncio.CancelledError:
                # 只有当自己仍是当前排空时才清理，避免覆盖新任务
                if self._drain_tracker is tracker:
                    self._draining = False
                raise
            except Exception:
                logger.exception("语音排空任务异常")
                if self._drain_tracker is tracker:
                    self._draining = False

        def _on_drain_done(task: asyncio.Task[Any]) -> None:
            # 只清理自己的引用，避免覆盖新任务
            if self._drain_task is task:
                self._drain_task = None
                self._drain_tracker = None
            # 消费异常，避免 "Task exception was never retrieved"
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    logger.debug("排空任务已完成，异常已消费: %s", exc)

        try:
            task = asyncio.create_task(_run_drain())
            task.add_done_callback(_on_drain_done)
            self._drain_task = task
        except RuntimeError:
            self._draining = False
            self._drain_tracker = None
            logger.exception("无法启动语音排空任务")

    async def _drain_and_stop(self, generation: int, tracker: VoiceDrainTracker) -> None:
        """等待尾部静默后安全关闭语音通道并触发转写。

        语音键松开后不立刻结束会话：
        - 等待最后一次收到新数据后 20ms 确认无新数据
        - 总超时 170ms 后强制释放
        - 每约 10ms 检查一次
        """
        while True:
            now = time.monotonic()
            if tracker.should_release(now):
                break
            await asyncio.sleep(0.01)  # 10ms 轮询间隔

        # 只有当自己仍是当前排空时才清理和触发回调
        if self._drain_tracker is not tracker:
            return

        self._draining = False
        self._drain_tracker = None
        self._streaming = False
        logger.info("音频流结束（排空完成）")
        if self._on_voice_stop:
            self._on_voice_stop()
        await self._send_mic_close(generation=generation)

    def _handle_audio_notify(
        self,
        _sender: Any,
        data: bytearray,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._generation:
            logger.debug("忽略过期 AUDIO notify（generation=%d）", generation)
            return
        if not self._streaming or self._waiting_for_resync or not data:
            return

        # 排空期间，任何通过检查的非空数据都算新活动，重置尾部计时
        if self._drain_tracker is not None:
            self._drain_tracker.on_activity(time.monotonic())

        if not self._frame_format_probed:
            self._probe_buffer.extend(data)
            plain_size = self.capabilities.frame_size
            if len(self._probe_buffer) < plain_size:
                return

            headered_size = plain_size + 6
            buffered_size = len(self._probe_buffer)
            if buffered_size == headered_size or (
                buffered_size % headered_size == 0 and buffered_size % plain_size != 0
            ):
                self._header_mode = True
                self._accumulator = FrameAccumulator(headered_size)
                logger.debug(
                    "检测到带头音频帧: wire=%d, payload=%d",
                    headered_size,
                    plain_size,
                )
            elif buffered_size % plain_size == 0:
                self._header_mode = False
                self._accumulator = FrameAccumulator(plain_size)
                logger.debug("检测到裸音频帧: %d", plain_size)
            else:
                logger.warning(
                    "音频帧长不匹配: got=%d, caps=%d；等待下一 SYNC",
                    buffered_size,
                    plain_size,
                )
                self._probe_buffer.clear()
                self._waiting_for_resync = True
                return

            self._frame_format_probed = True
            seed = bytes(self._probe_buffer)
            self._probe_buffer.clear()
            self._emit_frames(self._accumulator.append(seed))
            return

        self._emit_frames(self._accumulator.append(bytes(data)))

    def _emit_frames(self, frames: Iterable[bytes]) -> None:
        for raw in frames:
            if self._header_mode and len(raw) == self.capabilities.frame_size + 6:
                predictor = parse_be16(raw, 3)
                if predictor >= 0x8000:
                    predictor -= 0x10000
                sync = SyncFrame(predictor=predictor, step_index=raw[5])
                frame = raw[6:]
                self._take_pending_sync()
            else:
                frame = raw
                sync = self._take_pending_sync()

            if self._on_audio_frame:
                self._on_audio_frame(frame, sync)

    def _take_pending_sync(self) -> SyncFrame | None:
        sync = self._pending_sync
        self._pending_sync = None
        return sync

    def _schedule(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        generation: int | None = None,
    ) -> None:
        async def run_if_current() -> None:
            if generation is not None and generation != self._generation:
                coroutine.close()
                return
            await coroutine

        try:
            task = asyncio.create_task(run_if_current())
        except RuntimeError:
            coroutine.close()
            logger.exception("无法调度 ATVV 命令")
            return
        self._tasks.add(task)

        def finish(completed: asyncio.Task[Any]) -> None:
            # task 可能在第一次运行前就被取消，此时包装协程尚未来得及关闭原协程。
            coroutine.close()
            self._task_done(completed)

        task.add_done_callback(finish)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("ATVV 异步命令失败")

    def _cancel_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    def _cancel_drain(self) -> None:
        """取消正在进行的排空任务。"""
        task = self._drain_task
        if task is not None and not task.done():
            task.cancel()
        # 只清理自己的引用，避免覆盖新任务
        if self._drain_task is task:
            self._drain_task = None
            self._drain_tracker = None
        self._draining = False

    def _reset_connection_state(self) -> int:
        self._generation += 1
        self._cancel_drain()
        self._cancel_tasks()
        self.capabilities = ATVVCapabilities()
        self._tx_char = None
        self._streaming = False
        self._caps_ready = False
        self._mic_open_pending = False
        self._caps_event = asyncio.Event()
        self._connection_active = False
        self._connected_notified = False
        self._disconnected_notified = False
        self._suppress_disconnect_notification = False
        self._pending_sync = None
        self._header_mode = False
        self._frame_format_probed = False
        self._probe_buffer.clear()
        self._waiting_for_resync = False
        self._draining = False
        self._accumulator = FrameAccumulator()
        return self._generation
