"""RC003 设备级 F9 隔离测试。"""

from types import SimpleNamespace

import pytest

from mi_remote_linux.hid_guard import RemoteHIDGuard


class FakeEcodes:
    EV_KEY = 1
    KEY_F5 = 63
    KEY_F9 = 67


class FakeDevice:
    def __init__(self, path, keys, *, vendor=0x2717, product=0x32B8):
        self.path = path
        self.name = "小米蓝牙语音遥控器"
        self.info = SimpleNamespace(vendor=vendor, product=product)
        self.fd = 100 + int(path.rsplit("event", 1)[1])
        self.keys = keys
        self.grabbed = False
        self.closed = False

    def capabilities(self):
        return {FakeEcodes.EV_KEY: self.keys}

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def close(self):
        self.closed = True

    def read(self):
        return []


class FakeRelay:
    def __init__(self, *_args, **_kwargs):
        self.events = []
        self.closed = False

    def write_event(self, event):
        self.events.append(event)

    def close(self):
        self.closed = True


class FakeEvdev:
    ecodes = FakeEcodes

    def __init__(self, devices):
        self.devices = {device.path: device for device in devices}

    def list_devices(self):
        return list(self.devices)

    def InputDevice(self, path):
        return self.devices[path]


@pytest.mark.asyncio
async def test_safe_mode_grabs_f9_only_node(monkeypatch):
    device = FakeDevice("/dev/input/event1", [FakeEcodes.KEY_F9])
    guard = RemoteHIDGuard(evdev_module=FakeEvdev([device]))
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)

    await guard.start()

    assert guard.grabbed_paths == ("/dev/input/event1",)
    assert device.grabbed is True
    await guard.stop()
    assert device.closed is True


@pytest.mark.asyncio
async def test_confirmed_f5_voice_key_is_filtered(monkeypatch):
    device = FakeDevice("/dev/input/event7", [FakeEcodes.KEY_F5])
    guard = RemoteHIDGuard(evdev_module=FakeEvdev([device]))
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)

    await guard.start()

    assert guard.grabbed_paths == ("/dev/input/event7",)
    await guard.stop()


@pytest.mark.asyncio
async def test_safe_mode_refuses_shared_keyboard_node(monkeypatch):
    device = FakeDevice("/dev/input/event2", [FakeEcodes.KEY_F9, 28, 103])
    guard = RemoteHIDGuard(evdev_module=FakeEvdev([device]))
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)

    await guard.start()

    assert guard.grabbed_paths == ()
    assert device.grabbed is False
    assert device.closed is True
    await guard.stop()


@pytest.mark.asyncio
async def test_safe_mode_relays_shared_remote_keys_except_f9(monkeypatch):
    device = FakeDevice("/dev/input/event2", [FakeEcodes.KEY_F9, 28, 103])
    relay = FakeRelay()
    guard = RemoteHIDGuard(
        evdev_module=FakeEvdev([device]),
        uinput_factory=lambda *_args, **_kwargs: relay,
    )
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)
    other_event = SimpleNamespace(type=FakeEcodes.EV_KEY, code=28, value=1)
    f9_event = SimpleNamespace(type=FakeEcodes.EV_KEY, code=FakeEcodes.KEY_F9, value=1)
    device.read = lambda: [other_event, f9_event]

    await guard.start()
    guard._drain(device.path)

    assert device.grabbed is True
    assert relay.events == [other_event]
    await guard.stop()
    assert relay.closed is True


@pytest.mark.asyncio
async def test_relay_failure_releases_grab_for_retry(monkeypatch):
    device = FakeDevice("/dev/input/event6", [FakeEcodes.KEY_F9, 28])
    relay = FakeRelay()
    relay.write_event = lambda _event: (_ for _ in ()).throw(RuntimeError("uinput lost"))
    guard = RemoteHIDGuard(
        evdev_module=FakeEvdev([device]),
        uinput_factory=lambda *_args, **_kwargs: relay,
    )
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)
    device.read = lambda: [SimpleNamespace(type=FakeEcodes.EV_KEY, code=28, value=1)]

    await guard.start()
    guard._drain(device.path)

    assert guard.grabbed_paths == ()
    assert device.grabbed is False
    assert relay.closed is True
    await guard.stop()


@pytest.mark.asyncio
async def test_force_mode_only_grabs_remote_node_containing_f9(monkeypatch):
    remote = FakeDevice("/dev/input/event3", [FakeEcodes.KEY_F9, 28])
    keyboard = FakeDevice(
        "/dev/input/event4",
        [FakeEcodes.KEY_F9, 28],
        vendor=1,
        product=1,
    )
    keyboard.name = "AT keyboard"
    guard = RemoteHIDGuard(mode="force", evdev_module=FakeEvdev([remote, keyboard]))
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)

    await guard.start()

    assert guard.grabbed_paths == ("/dev/input/event3",)
    assert keyboard.grabbed is False
    await guard.stop()


@pytest.mark.asyncio
async def test_force_mode_tolerates_evdev_uinput_error(monkeypatch):
    device = FakeDevice("/dev/input/event5", [FakeEcodes.KEY_F9, 28])

    class FakeUInputError(Exception):
        pass

    guard = RemoteHIDGuard(
        mode="force",
        evdev_module=FakeEvdev([device]),
        uinput_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(FakeUInputError()),
    )
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)

    await guard.start()

    assert guard.grabbed_paths == ("/dev/input/event5",)
    await guard.stop()


@pytest.mark.asyncio
async def test_grab_failure_is_retried_on_the_next_scan(monkeypatch):
    device = FakeDevice("/dev/input/event8", [FakeEcodes.KEY_F5])
    attempts = []

    def grab():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError(16, "Device or resource busy")
        device.grabbed = True

    device.grab = grab
    guard = RemoteHIDGuard(evdev_module=FakeEvdev([device]))
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)

    await guard.start()
    assert guard.grabbed_paths == ()

    # 权限或占用是暂时的：下一轮扫描必须重新尝试这个节点。
    await guard._scan_once()

    assert guard.grabbed_paths == ("/dev/input/event8",)
    assert device.grabbed is True
    await guard.stop()


@pytest.mark.asyncio
async def test_open_failure_is_retried_on_the_next_scan(monkeypatch):
    device = FakeDevice("/dev/input/event9", [FakeEcodes.KEY_F5])
    evdev = FakeEvdev([device])
    opens = []

    def open_device(path):
        opens.append(path)
        if len(opens) == 1:
            raise OSError(13, "Permission denied")
        return device

    evdev.InputDevice = open_device
    guard = RemoteHIDGuard(evdev_module=evdev)
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_reader", lambda *_args: None)
    monkeypatch.setattr(loop, "remove_reader", lambda *_args: None)

    await guard.start()
    assert guard.grabbed_paths == ()

    await guard._scan_once()

    assert guard.grabbed_paths == ("/dev/input/event9",)
    await guard.stop()
