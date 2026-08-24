# MiRemote Linux

把小米蓝牙遥控器 2 Pro（RC003）接入 Linux。当前已实现 ATVV 语音采集、IMA ADPCM
解码、本地语音转写、Linux 桌面焦点输入，以及按设备隔离语音键 F9。

## 当前能力

- 按住语音键说话，松手后把文字输出到 stdout
- 在 Wayland 或 X11 下把中文自动粘贴到当前焦点
- 可把转写结果同时追加到文本文件
- 进程内常驻 Sherpa-ONNX Paraformer，也可回退 Voxtype CLI 或 faster-whisper
- 自动发现已配对的 RC003，断线后持续重连
- 只拦截 RC003 自己的 F9，不修改桌面快捷键或物理键盘行为
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

创建隔离环境并安装语音依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[voice]"
```

为获得更好的中文识别率，下载官方 Sherpa-ONNX Paraformer 模型（约 233 MiB）：

```bash
.venv/bin/python scripts/download_paraformer.py
```

默认 `--engine auto` 会优先把 Paraformer 模型加载到当前进程并持续复用；没有项目模型时
也会复用 `$XDG_DATA_HOME/voxtype/models/paraformer-zh`，再回退 Voxtype CLI 或
faster-whisper。首次使用 faster-whisper 时会下载所选模型，之后均可离线运行。也可通过
`--paraformer-model-dir` 或 `MI_REMOTE_PARAFORMER_MODEL_DIR` 指定其他模型目录。

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
.venv/bin/mi-remote voice --engine sherpa-paraformer --inject
.venv/bin/mi-remote voice --engine voxtype-paraformer --inject
.venv/bin/mi-remote voice --engine faster-whisper --model base --inject

# 粘贴后自动按 Enter，仅用于明确需要自动提交的场景
.venv/bin/mi-remote voice --address AA:BB:CC:DD:EE:FF --inject --submit
```

stdout 只输出识别文本，状态和日志写到 stderr，便于交给其他程序消费。

`--address` 是遥控器的蓝牙 MAC 地址，不是 IP 地址；通常可以省略。程序会从 BlueZ 已
配对设备和广播中自动识别 RC003。遥控器休眠或临时断开后，进程会指数退避重连；同一用户
只能启动一个 `mi-remote voice` 实例，避免两个进程争抢 BLE 语音通道。

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

Sherpa-ONNX Paraformer 会在启动时加载一次并常驻复用。本机实测加载约 0.99 秒，之后
1 秒静音推理约 0.04 秒；近静音录音会在识别前直接丢弃，避免误输入。Voxtype CLI 仍作为
兼容回退。所有引擎收到的都是遥控器解码后的 16 kHz PCM，不会改用电脑麦克风。

同一段遥控器真机录音对照结果（2026-08-24）：

- faster-whisper base：`這是小米熬空機與銀蔬測試`
- Voxtype Paraformer：`这是小米遥控器语音输入测试`

因此中文输入推荐保留默认 `--engine auto`。模型文件是可选下载项，缺失时仍可使用
faster-whisper。

## F9 隔离与权限

RC003 的语音键会同时产生 ATVV 语音事件和 HID F9。默认 `--grab-hid safe` 只匹配
RC003（VID `2717`、PID `32b8`）：独占其输入节点、丢弃 F9，并通过 uinput 原样转发其余
按键。普通键盘的 F9 不会被打开或修改。如果无法创建 uinput，safe 模式不会独占共享
节点；显式使用 `--grab-hid force` 可在这种情况下继续隔离 F9，但会暂时屏蔽该节点上的
其他遥控器键。完全关闭隔离可使用 `--grab-hid off`。

读取输入节点和创建过滤后的虚拟设备需要权限。推荐安装仓库提供的 udev 规则；其中 event
节点规则只匹配 RC003，uinput 规则则授予 `input` 组使用虚拟输入设备的权限：

```bash
sudo install -m 0644 udev/99-mi-remote-linux.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input
sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput
```

重新连接遥控器后规则生效。uinput 能模拟输入，属于敏感权限；也可以自行采用发行版的
uinput 授权方案。项目不会自动安装 udev 规则，
也不会修改 Hyprland、GNOME、KDE 或 Voxtype 的快捷键配置。

## 已知限制

- 某些 RC003 固件把 F9 与其他遥控器按键放在同一 event 节点；没有 uinput 权限时默认
  safe 模式不会独占它，此时可安装规则或权衡后使用 `--grab-hid force`。
- 部分 GNOME/KDE Wayland 会限制虚拟键盘协议，`--inject` 在这些环境中可能只能把结果
  保留到剪贴板/stdout。
- `--inject` 会覆盖当前文本剪贴板；`--submit` 会额外发送 Enter，默认不开启。

## 后台自启动

仓库提供了 `systemd/mi-remote-voice.service.example`。复制到用户服务目录后，把其中的
项目路径改成实际值；MAC 地址可以省略并自动发现：

```bash
mkdir -p ~/.config/systemd/user
cp systemd/mi-remote-voice.service.example \
  ~/.config/systemd/user/mi-remote-voice.service
systemctl --user daemon-reload
systemctl --user enable --now mi-remote-voice.service

# 查看实时状态/识别文本
journalctl --user -u mi-remote-voice.service -f
```

程序本身会阻止第二个语音实例。需要前台调试时先执行：

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
跨批状态、PCM 后处理、常驻 Paraformer、静音保护、断线重连、单实例、RC003 设备级
F9 隔离和 Wayland/X11 注入失败回退。实机验收步骤见 [AGENTS.md](AGENTS.md)。

## 开发路线

1. Phase C：在 RC003 真机完成常驻 Paraformer、F9 隔离、自动重连压力验收
2. Phase B：在现有 evdev 设备识别上加入 13 键映射、层、手势和宏
3. Wayland/Hyprland 按键动作输出：优先 uinput/ydotool

## 参考

- 协议与 macOS 参考实现：
  [godarrenw/mi_remote_control](https://github.com/godarrenw/mi_remote_control)

## 许可证

[MIT](LICENSE)。Linux 移植保留了上游 MIT 许可与署名，详见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
