"""不连接真实硬件的 ATVV BLE 状态机测试。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from mi_remote_linux.atvv import ATVVCapabilities, SyncFrame
from mi_remote_linux.ble_client import ATVVClient, _select_known_bluez_device


class Variant:
    def __init__(self, value):
        self.value = value


class FakeCharacteristic:
    def __init__(self, properties):
        self.properties = properties


class FakeBleakClient:
    def __init__(self):
        self.writes = []
        self.is_connected = True
        self.disconnect_calls = 0

    async def write_gatt_char(self, characteristic, payload, *, response):
        self.writes.append((characteristic, payload, response))

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False


def make_client(frame_size: int = 4):
    frames = []
    starts = []
    client = ATVVClient(
        on_audio_frame=lambda frame, sync: frames.append((frame, sync)),
        on_voice_start=lambda: starts.append(True),
    )
    client.capabilities = ATVVCapabilities(frame_size=frame_size)
    return client, frames, starts


def test_stream_start_and_plain_audio_frames():
    client, frames, starts = make_client()

    client._handle_stream_start(bytes([0x04, 0x00, 0x02, 0xA5]))
    client._handle_sync_frame(bytes([0x0A, 0x02, 0, 1, 0xFF, 0x9C, 10]))
    client._handle_audio_notify(None, bytearray([1, 2]))
    client._handle_audio_notify(None, bytearray([3, 4]))

    assert starts == [True]
    assert client.capabilities.session_id == 0xA5
    assert frames == [(bytes([1, 2, 3, 4]), SyncFrame(-100, 10))]


def test_headered_audio_frame_is_stripped_and_carries_its_sync():
    client, frames, _ = make_client()
    client._handle_stream_start(bytes([0x04, 0x00, 0x02, 1]))

    # 6 字节头：[seq 2B, pad 1B, predictor 2B, step 1B]
    client._handle_audio_notify(
        None,
        bytearray([0x00, 0x01, 0x00, 0x01, 0xF4, 20, 9, 8, 7, 6]),
    )

    assert frames == [(bytes([9, 8, 7, 6]), SyncFrame(500, 20))]


def test_bad_frame_length_waits_for_sync_before_recovery():
    client, frames, _ = make_client()
    client._handle_stream_start(bytes([0x04, 0x00, 0x02, 1]))

    client._handle_audio_notify(None, bytearray([1, 2, 3, 4, 5]))
    client._handle_audio_notify(None, bytearray([6, 7, 8, 9]))
    assert frames == []

    client._handle_sync_frame(bytes([0x0A, 0x02, 0, 1, 0, 0, 0]))
    client._handle_audio_notify(None, bytearray([6, 7, 8, 9]))
    assert frames == [(bytes([6, 7, 8, 9]), SyncFrame(0, 0))]


def test_selects_connected_known_bluez_remote_without_scanning():
    objects = {
        "/org/bluez/hci0/dev_AA": {
            "org.bluez.Device1": {
                "Address": Variant("AA:BB:CC:DD:EE:FF"),
                "Alias": Variant("MI RC"),
                "UUIDs": Variant([]),
                "Paired": Variant(True),
                "Connected": Variant(True),
                "Adapter": Variant("/org/bluez/hci0"),
            }
        }
    }

    device = _select_known_bluez_device(objects, address=None)

    assert device is not None
    assert device.address == "AA:BB:CC:DD:EE:FF"
    assert device.details["path"] == "/org/bluez/hci0/dev_AA"


def test_known_bluez_address_takes_precedence_over_name_filter():
    objects = {
        "/org/bluez/hci0/dev_AA": {
            "org.bluez.Device1": {
                "Address": Variant("AA:BB:CC:DD:EE:FF"),
                "Alias": Variant("RC003 custom name"),
                "UUIDs": Variant([]),
                "Adapter": Variant("/org/bluez/hci0"),
            }
        }
    }

    device = _select_known_bluez_device(objects, address="aa:bb:cc:dd:ee:ff")

    assert device is not None
    assert device.name == "RC003 custom name"


@pytest.mark.asyncio
async def test_command_prefers_write_without_response():
    client = ATVVClient()
    fake_client = FakeBleakClient()
    characteristic = FakeCharacteristic(["write", "write-without-response"])
    client.client = fake_client
    client._tx_char = characteristic

    await client._write_command(b"\x0a", "TEST")

    assert fake_client.writes == [(characteristic, b"\x0a", False)]


@pytest.mark.asyncio
async def test_command_falls_back_to_write_with_response():
    client = ATVVClient()
    fake_client = FakeBleakClient()
    characteristic = FakeCharacteristic(["write"])
    client.client = fake_client
    client._tx_char = characteristic

    await client._write_command(b"\x0a", "TEST")

    assert fake_client.writes == [(characteristic, b"\x0a", True)]


@pytest.mark.asyncio
async def test_disconnect_restores_preexisting_hid_connection():
    client = ATVVClient()
    fake_client = FakeBleakClient()
    client.client = fake_client
    client._restore_device_path = "/org/bluez/hci0/dev_AA"
    client._restore_bluez_connection = AsyncMock()

    await client.disconnect()

    assert fake_client.disconnect_calls == 1
    client._restore_bluez_connection.assert_awaited_once_with("/org/bluez/hci0/dev_AA")


@pytest.mark.asyncio
async def test_disconnect_timeout_does_not_block_cleanup():
    client = ATVVClient()
    fake_client = FakeBleakClient()

    async def never_disconnect():
        await asyncio.Event().wait()

    fake_client.disconnect = never_disconnect
    client.client = fake_client

    await client.disconnect(timeout=0.001)

    assert client.client is None
