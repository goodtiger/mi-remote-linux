from types import SimpleNamespace

import pytest

from mi_remote_linux.hid_engine import RC003_KEY_CODES, HIDEngine


class FakeEcodes:
    EV_KEY = 1


class FakeDevice:
    def __init__(self, path="/dev/input/event17"):
        self.path = path
        self.name = "小米蓝牙语音遥控器"
        self.info = SimpleNamespace(vendor=0x2717, product=0x32B8)
        self.fd = 17
        self.grabbed = False
        self.closed = False
        self.events = []

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def close(self):
        self.closed = True

    def read(self):
        events, self.events = self.events, []
        return events


class FakeEvdev:
    ecodes = FakeEcodes

    def __init__(self, device):
        self.device = device

    def list_devices(self):
        return [self.device.path]

    def InputDevice(self, _path):
        return self.device


@pytest.mark.asyncio
async def test_rc003_is_grabbed_and_events_are_logical(monkeypatch):
    device = FakeDevice()
    received = []
    engine = HIDEngine(received.append, evdev_module=FakeEvdev(device))
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)
    device.events = [
        SimpleNamespace(type=1, code=63, value=1),
        SimpleNamespace(type=1, code=63, value=2),
        SimpleNamespace(type=1, code=63, value=0),
    ]

    await engine.start()
    engine._drain(device.path)

    assert device.grabbed is True
    assert [(event.key, event.is_down) for event in received] == [("voice", True), ("voice", False)]
    await engine.stop()
    assert device.closed is True


def test_truth_table_contains_confirmed_thirteen_keys():
    assert set(RC003_KEY_CODES.values()) == {
        "power",
        "voice",
        "up",
        "down",
        "left",
        "right",
        "ok",
        "back",
        "home",
        "menu",
        "tv",
        "vol_up",
        "vol_down",
    }
