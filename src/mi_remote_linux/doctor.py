"""Read-only installation and runtime diagnostics for MiRemote Linux."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Literal

from .ble_client import find_known_bluez_remote
from .config import ConfigError, load_config
from .hid_engine import HIDEngine
from .model_manager import model_files_present, resolve_model_dir

CheckStatus = Literal["pass", "warn", "fail"]
_AUTO = object()


@dataclass(frozen=True)
class DoctorCheck:
    key: str
    label: str
    status: CheckStatus
    detail: str
    hint: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"pass", "warn", "fail"}:
            raise ValueError(f"invalid doctor status: {self.status}")


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def summary(self) -> dict[str, int]:
        return {
            status: sum(check.status == status for check in self.checks)
            for status in ("pass", "warn", "fail")
        }

    @property
    def exit_code(self) -> int:
        return 1 if self.summary["fail"] else 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "checks": [asdict(check) for check in self.checks],
                "summary": self.summary,
                "ok": self.exit_code == 0,
            },
            ensure_ascii=False,
            indent=2,
        )


def render_report(report: DoctorReport) -> str:
    symbols = {"pass": "✓", "warn": "!", "fail": "✗"}
    lines = ["MiRemote Doctor（只读诊断）", ""]
    for check in report.checks:
        lines.append(f"{symbols[check.status]} {check.label}: {check.detail}")
        if check.hint:
            lines.append(f"  → {check.hint}")
    summary = report.summary
    lines.extend(
        (
            "",
            f"汇总：{summary['pass']} 通过，{summary['warn']} 警告，{summary['fail']} 失败",
        )
    )
    return "\n".join(lines)


class Doctor:
    """Collect diagnostics without connecting, grabbing devices, or loading models."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        find_spec: Callable[[str], Any | None] = find_spec,
        platform_name: str = sys.platform,
        kernel_release: str | None = None,
        bluez_finder: Callable[[str | None], Awaitable[Any | None]] = find_known_bluez_remote,
        evdev_module: Any = _AUTO,
        uinput_access: Callable[[], bool] | None = None,
    ):
        self.environment = dict(os.environ if environment is None else environment)
        self.which = which
        self.find_spec = find_spec
        self.platform_name = platform_name
        self.kernel_release = kernel_release or platform.release()
        self.bluez_finder = bluez_finder
        self.evdev_module = evdev_module
        self.uinput_access = uinput_access or self._can_access_uinput

    async def run(
        self,
        *,
        address: str | None = None,
        model_dir: str | Path | None = None,
    ) -> DoctorReport:
        checks = [self._platform_check()]
        checks.append(await self._remote_check(address))
        checks.append(self._hid_check())
        checks.append(self._uinput_check())

        session, desktop_check = self._desktop_check()
        checks.append(desktop_check)
        checks.append(self._injection_check(session))
        checks.append(self._key_actions_check(session))
        checks.append(self._window_actions_check(session))
        checks.append(self._notification_check())
        checks.append(self._audio_check())
        checks.append(self._media_check(session))
        checks.append(self._speech_check(model_dir))
        checks.append(self._config_check())
        return DoctorReport(tuple(checks))

    def _platform_check(self) -> DoctorCheck:
        if self.platform_name.startswith("linux"):
            return DoctorCheck("platform", "系统", "pass", f"Linux {self.kernel_release}")
        return DoctorCheck(
            "platform",
            "系统",
            "fail",
            f"不支持的平台：{self.platform_name}",
            "MiRemote Linux 需要 Linux 和 BlueZ",
        )

    async def _remote_check(self, address: str | None) -> DoctorCheck:
        try:
            remote = await self.bluez_finder(address)
        except Exception as exc:  # noqa: BLE001 - D-Bus backends expose platform exceptions
            return DoctorCheck(
                "remote",
                "蓝牙遥控器",
                "fail",
                f"无法查询 BlueZ：{exc}",
                "确认 bluetooth.service 正在运行，且当前用户可访问系统 D-Bus",
            )
        if remote is None:
            requested = f"地址 {address}" if address else "RC003"
            return DoctorCheck(
                "remote",
                "蓝牙遥控器",
                "fail",
                f"BlueZ 中未找到{requested}",
                "先用 bluetoothctl 完成配对、信任和连接，再按任意遥控器键唤醒",
            )

        properties = remote.details.get("props", {}) if isinstance(remote.details, dict) else {}
        states = []
        paired = properties.get("Paired")
        trusted = properties.get("Trusted")
        connected = properties.get("Connected")
        if paired is not None:
            states.append("已配对" if paired else "未配对")
        if trusted is not None:
            states.append("已信任" if trusted else "未信任")
        if connected is not None:
            states.append("已连接" if connected else "当前休眠/未连接")
        suffix = f" · {'，'.join(states)}" if states else ""
        detail = f"{remote.name or 'RC003'} ({remote.address}){suffix}"
        if paired is False:
            return DoctorCheck(
                "remote",
                "蓝牙遥控器",
                "fail",
                detail,
                f"在 bluetoothctl 中执行 pair {remote.address}",
            )
        if trusted is False:
            return DoctorCheck(
                "remote",
                "蓝牙遥控器",
                "warn",
                detail,
                f"建议在 bluetoothctl 中执行 trust {remote.address}，便于自动重连",
            )
        return DoctorCheck("remote", "蓝牙遥控器", "pass", detail)

    def _hid_check(self) -> DoctorCheck:
        evdev_module = self.evdev_module
        if evdev_module is _AUTO:
            try:
                import evdev as evdev_module
            except ImportError:
                return DoctorCheck(
                    "hid",
                    "遥控器按键输入",
                    "fail",
                    "未安装 Python evdev",
                    "安装项目运行依赖：python -m pip install -e .",
                )
        try:
            paths = list(evdev_module.list_devices())
        except OSError as exc:
            return DoctorCheck(
                "hid",
                "遥控器按键输入",
                "fail",
                f"无法枚举 /dev/input：{exc}",
                "检查当前用户的 input 组和 udev 权限",
            )

        denied = []
        for path in paths:
            try:
                device = evdev_module.InputDevice(path)
            except OSError:
                denied.append(path)
                continue
            try:
                if HIDEngine._is_rc003(device):
                    return DoctorCheck(
                        "hid",
                        "遥控器按键输入",
                        "pass",
                        f"{device.name or 'RC003'} · {path} 可读取",
                    )
            finally:
                device.close()
        if denied:
            return DoctorCheck(
                "hid",
                "遥控器按键输入",
                "fail",
                f"存在 {len(denied)} 个不可读取的 input 节点，未能确认 RC003",
                "安装仓库 udev 规则，或把当前用户加入 input 组后重新登录",
            )
        return DoctorCheck(
            "hid",
            "遥控器按键输入",
            "fail",
            "未发现 RC003 evdev 节点",
            "按任意遥控器键唤醒；若仍缺失，重新连接 BlueZ HID",
        )

    def _uinput_check(self) -> DoctorCheck:
        if self.uinput_access():
            return DoctorCheck("uinput", "uinput 转发", "pass", "/dev/uinput 可读写")
        return DoctorCheck(
            "uinput",
            "uinput 转发",
            "warn",
            "/dev/uinput 不可读写",
            "仅纯语音 safe 隔离共享按键节点时需要；可安装仓库 udev 规则",
        )

    def _desktop_check(self) -> tuple[str | None, DoctorCheck]:
        if self.environment.get("WAYLAND_DISPLAY"):
            if self.environment.get("HYPRLAND_INSTANCE_SIGNATURE") and self.which("hyprctl"):
                name = "Hyprland"
            elif self.environment.get("SWAYSOCK") and self.which("swaymsg"):
                name = "Sway"
            else:
                name = self.environment.get("XDG_CURRENT_DESKTOP") or "通用 Wayland"
            return "wayland", DoctorCheck("desktop", "图形会话", "pass", f"Wayland · {name}")
        if self.environment.get("DISPLAY"):
            name = self.environment.get("XDG_CURRENT_DESKTOP") or "X11"
            return "x11", DoctorCheck("desktop", "图形会话", "pass", f"X11 · {name}")
        return None, DoctorCheck(
            "desktop",
            "图形会话",
            "fail",
            "未检测到 WAYLAND_DISPLAY 或 DISPLAY",
            "请从桌面会话启动 doctor；无头环境只能使用 stdout/文件输出",
        )

    def _injection_check(self, session: str | None) -> DoctorCheck:
        requirements = {
            "wayland": ("wl-copy", "wtype"),
            "x11": ("xclip", "xdotool"),
        }
        names = requirements.get(session)
        if names is None:
            return DoctorCheck(
                "injection",
                "焦点文字输入",
                "fail",
                "没有可用的图形会话",
                "仍可不加 --inject，把识别结果输出到 stdout 或文件",
            )
        missing = [name for name in names if not self.which(name)]
        if not missing:
            return DoctorCheck(
                "injection", "焦点文字输入", "pass", f"{session}：{' + '.join(names)}"
            )
        return DoctorCheck(
            "injection",
            "焦点文字输入",
            "fail",
            f"缺少：{', '.join(missing)}",
            "Wayland 安装 wl-clipboard 和 wtype；X11 安装 xclip 和 xdotool",
        )

    def _key_actions_check(self, session: str | None) -> DoctorCheck:
        if session == "wayland":
            available = [name for name in ("wtype", "ydotool") if self.which(name)]
            if available:
                return DoctorCheck(
                    "key_actions", "虚拟按键", "pass", "Wayland · " + " / ".join(available)
                )
            return DoctorCheck(
                "key_actions",
                "虚拟按键",
                "fail",
                "缺少 wtype 或 ydotool",
                "优先安装 wtype；受限合成器可配置 ydotool/ydotoold",
            )
        if session == "x11":
            if self.which("xdotool"):
                return DoctorCheck("key_actions", "虚拟按键", "pass", "X11 · xdotool")
            return DoctorCheck("key_actions", "虚拟按键", "fail", "缺少 xdotool", "安装 xdotool")
        return DoctorCheck("key_actions", "虚拟按键", "fail", "没有可用的图形会话")

    def _window_actions_check(self, session: str | None) -> DoctorCheck:
        if session == "wayland":
            if self.environment.get("HYPRLAND_INSTANCE_SIGNATURE") and self.which("hyprctl"):
                return DoctorCheck("window_actions", "窗口控制", "pass", "Hyprland · hyprctl")
            if self.environment.get("SWAYSOCK") and self.which("swaymsg"):
                return DoctorCheck("window_actions", "窗口控制", "pass", "Sway · swaymsg")
            return DoctorCheck(
                "window_actions",
                "窗口控制",
                "warn",
                "当前 Wayland 合成器没有已适配的窗口 IPC",
                "基础按键和语音仍可用；任务视图、工作区和鼠标模式可能受限",
            )
        if session == "x11" and self.which("xdotool"):
            return DoctorCheck("window_actions", "窗口控制", "pass", "X11 · xdotool")
        return DoctorCheck("window_actions", "窗口控制", "warn", "窗口控制后端不可用")

    def _notification_check(self) -> DoctorCheck:
        if self.which("notify-send"):
            return DoctorCheck("notifications", "桌面通知", "pass", "notify-send")
        return DoctorCheck(
            "notifications",
            "桌面通知",
            "warn",
            "缺少 notify-send",
            "安装 libnotify；无通知时按键动作仍可运行，但看不到浮层",
        )

    def _audio_check(self) -> DoctorCheck:
        if self.which("wpctl"):
            return DoctorCheck("audio", "系统音量", "pass", "PipeWire · wpctl")
        if self.which("pactl"):
            return DoctorCheck("audio", "系统音量", "pass", "PulseAudio · pactl")
        return DoctorCheck(
            "audio",
            "系统音量",
            "warn",
            "缺少 wpctl 和 pactl",
            "安装 WirePlumber/wpctl 或 pulseaudio-utils；否则回退桌面媒体键",
        )

    def _media_check(self, session: str | None) -> DoctorCheck:
        if self.which("playerctl"):
            return DoctorCheck("media", "媒体播放控制", "pass", "playerctl")
        if session and (self.which("wtype") or self.which("xdotool")):
            return DoctorCheck(
                "media",
                "媒体播放控制",
                "warn",
                "缺少 playerctl，将回退 XF86 媒体键",
                "若桌面忽略合成媒体键，请安装 playerctl",
            )
        return DoctorCheck(
            "media",
            "媒体播放控制",
            "warn",
            "playerctl 和媒体键后端均不可用",
            "安装 playerctl",
        )

    def _speech_check(self, configured: str | Path | None) -> DoctorCheck:
        model_dir = self._resolve_model_dir(configured)
        model_ready = model_files_present(model_dir)
        sherpa_ready = self.find_spec("sherpa_onnx") is not None
        if model_ready and sherpa_ready:
            return DoctorCheck(
                "speech",
                "语音识别",
                "pass",
                f"Sherpa-ONNX Paraformer · {model_dir}",
            )
        if model_ready and self.which("voxtype"):
            return DoctorCheck("speech", "语音识别", "pass", f"Voxtype Paraformer · {model_dir}")
        if self.find_spec("faster_whisper") is not None:
            missing = "Paraformer 模型" if not model_ready else "sherpa-onnx"
            return DoctorCheck(
                "speech",
                "语音识别",
                "warn",
                f"faster-whisper 可用；缺少 {missing}",
                "运行 mi-remote model download 可获得更好的中文识别",
            )
        missing = []
        if not model_ready:
            missing.append("Paraformer 模型")
        if not sherpa_ready:
            missing.append("sherpa-onnx")
        return DoctorCheck(
            "speech",
            "语音识别",
            "fail",
            "缺少：" + "、".join(missing or ["可用的识别引擎"]),
            '安装语音依赖 python -m pip install -e ".[voice]"，然后下载 Paraformer 模型',
        )

    def _config_check(self) -> DoctorCheck:
        try:
            config = load_config("default")
        except ConfigError as exc:
            return DoctorCheck(
                "config", "默认按键配置", "fail", str(exc), "重新安装 mi-remote-linux 包"
            )
        return DoctorCheck(
            "config",
            "默认按键配置",
            "pass",
            f"v{config.version} · {len(config.bindings)} 键 · {len(config.profiles)} Profiles",
        )

    def _resolve_model_dir(self, configured: str | Path | None) -> Path:
        return resolve_model_dir(configured, environment=self.environment)

    @staticmethod
    def _can_access_uinput() -> bool:
        path = Path("/dev/uinput")
        return path.exists() and os.access(path, os.R_OK | os.W_OK)
