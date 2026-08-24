# MiRemote Linux

把小米蓝牙遥控器 2 Pro（RC003）接入 Linux。当前 Phase C 已实现 ATVV 语音采集、
IMA ADPCM 解码、本地语音转写和 Linux 桌面焦点输入；Phase B 按键映射仍在计划中。

## 当前能力

- 按住语音键说话，松手后把文字输出到 stdout
- 在 Wayland 或 X11 下把中文自动粘贴到当前焦点
- 可把转写结果同时追加到文本文件
- 本地运行 Voxtype Paraformer 或 faster-whisper，不上传录音
- 兼容 ATVV v1 与旧 codec 字段布局
- 兼容 120 字节裸音频帧和 126 字节带同步头音频帧

## 系统要求

- Linux + BlueZ 5.x + D-Bus
- Python 3.10+
- 已完成蓝牙配对的小米蓝牙遥控器 2 Pro

安装系统包：

```bash
# Arch Linux / Omarchy
sudo pacman -S bluez bluez-utils python wtype wl-clipboard

# Debian / Ubuntu
sudo apt install bluez python3 python3-venv wtype wl-clipboard
```

X11 桌面使用 `xclip + xdotool`，例如：

```bash
# Arch Linux
sudo pacman -S xclip xdotool

# Debian / Ubuntu
sudo apt install xclip xdotool
```

创建隔离环境并安装 faster-whisper 语音依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[voice]"
```

默认 `--engine auto`：若系统已经安装 Voxtype 及 `paraformer-zh` 模型，就优先使用
中文准确率更好的 Paraformer；否则使用 faster-whisper。首次使用 faster-whisper 时会
下载所选模型，之后可离线运行。

也可以从项目根目录运行 `./scripts/setup.sh` 完成上述 Python 安装。

## 配对

```bash
bluetoothctl
power on
agent on
default-agent
scan on
# 同时长按遥控器“菜单键 + HOME”进入配对模式，看到地址后执行：
pair <MAC>
trust <MAC>
connect <MAC>
quit
```

## 使用

```bash
# 自动扫描已配对/广播中的遥控器
.venv/bin/mi-remote voice

# 指定地址并打开协议日志
.venv/bin/mi-remote -v voice --address AA:BB:CC:DD:EE:FF

# 同时追加保存转写文本
.venv/bin/mi-remote voice --output transcript.txt

# 转写后自动粘贴到当前焦点（推荐）
.venv/bin/mi-remote voice --address AA:BB:CC:DD:EE:FF --inject

# 明确选择识别引擎
.venv/bin/mi-remote voice --engine voxtype-paraformer --inject
.venv/bin/mi-remote voice --engine faster-whisper --model base --inject

# 粘贴后自动按 Enter，仅用于明确需要自动提交的场景
.venv/bin/mi-remote voice --address AA:BB:CC:DD:EE:FF --inject --submit
```

stdout 只输出识别文本，状态和日志写到 stderr，便于交给其他程序消费。

`--inject` 会自动根据 `WAYLAND_DISPLAY`/`DISPLAY` 选择图形后端：Wayland 使用
`wl-copy + wtype`，X11 使用 `xclip + xdotool`。终端窗口使用 `Shift+Insert`，其他应用
使用 `Ctrl+V`；无法自动识别终端时，可加
`--paste-shortcut ctrl-shift-v` 或 `--paste-shortcut shift-insert` 手动指定。这个实现不依赖
桌面环境自己的快捷键，也不会修改 Hyprland、GNOME、KDE 等桌面配置。

文字通过剪贴板传递，因此不会受当前输入法中英文状态影响。每次识别会覆盖文本剪贴板；
粘贴失败时文本仍保留在剪贴板和 stdout。默认不会按 Enter。

兼容范围：蓝牙语音链路适用于使用 BlueZ 的 Linux；X11 注入适用于常见 X11 桌面；
Wayland 注入要求合成器支持 `wtype` 使用的虚拟键盘协议（Hyprland、Sway 等 wlroots
合成器通常支持）。部分 GNOME/KDE Wayland 会限制该协议，此时仍可使用 stdout/文件，
或切换到 X11 会话。Linux 没有一个不经桌面授权、适用于所有 Wayland 合成器的全局
焦点输入接口。

faster-whisper 会在启动后提前加载。Voxtype Paraformer 每次调用外部 CLI，本机实测首次
载入约 0.92 秒、3 秒音频推理约 0.08 秒；它收到的是遥控器解码后的 16 kHz WAV，不会
改用电脑麦克风。

同一段遥控器真机录音对照结果（2026-08-24）：

- faster-whisper base：`這是小米熬空機與銀蔬測試`
- Voxtype Paraformer：`这是小米遥控器语音输入测试`

因此中文输入推荐保留默认 `--engine auto`。Paraformer 是可选增强，不是项目的强制依赖。

## 已知限制

- RC003 的语音键还会通过 HID 产生 F9。如果桌面已把 F9 绑定到另一个语音输入程序，
  两个程序可能同时响应。MiRemote 不会擅自修改或抢占全局 F9；使用前请自行避免冲突。
- 部分 GNOME/KDE Wayland 会限制虚拟键盘协议，`--inject` 在这些环境中可能只能把结果
  保留到剪贴板/stdout。
- `--inject` 会覆盖当前文本剪贴板；`--submit` 会额外发送 Enter，默认不开启。

## 后台自启动

仓库提供了 `systemd/mi-remote-voice.service.example`。复制到用户服务目录后，把其中的
项目路径和遥控器 MAC 地址改成实际值：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/mi-remote-voice.service.example \
  ~/.config/systemd/user/mi-remote-voice.service
systemctl --user daemon-reload
systemctl --user enable --now mi-remote-voice.service

# 查看实时状态/识别文本
journalctl --user -u mi-remote-voice.service -f
```

常驻服务运行时不要再手动启动第二个 `mi-remote voice`；需要前台调试时先执行：

```bash
systemctl --user stop mi-remote-voice.service
```

## 开发与验证

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

当前自动化覆盖 ATVV 字段解析、握手音频状态、裸帧/带头帧、错位重同步、ADPCM
跨批状态、PCM 后处理、语音会话隔离和 Wayland/X11 注入失败回退。实机验收步骤见
[AGENTS.md](AGENTS.md)。

## 开发路线

1. Phase C：在 RC003 真机完成连续语音、焦点注入、固件兼容与断连验收
2. Phase B：通过 evdev 读取 HID，加入 13 键映射、层、手势和宏
3. Wayland/Hyprland 按键动作输出：优先 uinput/ydotool

Phase B 开发时安装 `.[keys]` 可选依赖。

## 参考

- 协议与 macOS 参考实现：
  [godarrenw/mi_remote_control](https://github.com/godarrenw/mi_remote_control)

## 许可证

[MIT](LICENSE)。Linux 移植保留了上游 MIT 许可与署名，详见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
