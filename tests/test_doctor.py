import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mi_remote_linux.doctor import Doctor, DoctorCheck, DoctorReport, render_report


class FakeDevice:
    path = "/dev/input/event17"
    name = "小米蓝牙语音遥控器"
    info = SimpleNamespace(vendor=0x2717, product=0x32B8)

    def close(self):
        pass


class FakeEvdev:
    def list_devices(self):
        return [FakeDevice.path]

    def InputDevice(self, _path):
        return FakeDevice()


def test_report_renders_summary_hints_and_json():
    report = DoctorReport(
        (
            DoctorCheck("platform", "系统", "pass", "Linux 6.12"),
            DoctorCheck("media", "媒体控制", "warn", "缺少 playerctl", "安装 playerctl"),
            DoctorCheck("remote", "遥控器", "fail", "未找到 RC003", "先完成蓝牙配对"),
        )
    )

    rendered = render_report(report)

    assert "✓ 系统: Linux 6.12" in rendered
    assert "! 媒体控制: 缺少 playerctl" in rendered
    assert "→ 安装 playerctl" in rendered
    assert "✗ 遥控器: 未找到 RC003" in rendered
    assert "1 通过，1 警告，1 失败" in rendered
    assert report.exit_code == 1
    assert json.loads(report.to_json())["summary"] == {"pass": 1, "warn": 1, "fail": 1}


@pytest.mark.asyncio
async def test_doctor_reports_a_ready_wayland_install(tmp_path: Path):
    (tmp_path / "model.int8.onnx").write_bytes(b"model")
    (tmp_path / "tokens.txt").write_text("tokens")
    tools = {
        name: f"/usr/bin/{name}"
        for name in (
            "hyprctl",
            "notify-send",
            "playerctl",
            "wl-copy",
            "wpctl",
            "wtype",
        )
    }
    remote = SimpleNamespace(
        name="小米蓝牙语音遥控器",
        address="AA:BB:CC:DD:EE:FF",
        details={"props": {"Paired": True, "Connected": True, "Trusted": True}},
    )
    doctor = Doctor(
        environment={
            "WAYLAND_DISPLAY": "wayland-1",
            "HYPRLAND_INSTANCE_SIGNATURE": "test",
        },
        which=lambda name: tools.get(name),
        find_spec=lambda name: object() if name == "sherpa_onnx" else None,
        platform_name="linux",
        kernel_release="6.12-test",
        bluez_finder=AsyncMock(return_value=remote),
        evdev_module=FakeEvdev(),
        uinput_access=lambda: True,
    )

    report = await doctor.run(model_dir=tmp_path)
    checks = {check.key: check for check in report.checks}

    assert report.exit_code == 0
    assert checks["remote"].status == "pass"
    assert "已配对" in checks["remote"].detail
    assert checks["hid"].status == "pass"
    assert checks["desktop"].detail == "Wayland · Hyprland"
    assert checks["injection"].status == "pass"
    assert checks["audio"].status == "pass"
    assert checks["speech"].status == "pass"
    assert "Sherpa-ONNX Paraformer" in checks["speech"].detail


@pytest.mark.asyncio
async def test_doctor_reports_actionable_failures_without_mutating_system(tmp_path: Path):
    doctor = Doctor(
        environment={},
        which=lambda _name: None,
        find_spec=lambda _name: None,
        platform_name="linux",
        kernel_release="6.12-test",
        bluez_finder=AsyncMock(return_value=None),
        evdev_module=FakeEvdev(),
        uinput_access=lambda: False,
    )

    report = await doctor.run(model_dir=tmp_path)
    checks = {check.key: check for check in report.checks}

    assert report.exit_code == 1
    assert checks["remote"].status == "fail"
    assert "配对" in (checks["remote"].hint or "")
    assert checks["desktop"].status == "fail"
    assert checks["injection"].status == "fail"
    assert checks["uinput"].status == "warn"
    assert checks["speech"].status == "fail"
