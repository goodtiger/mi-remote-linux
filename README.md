# MiRemote Linux

把小米蓝牙遥控器 2 Pro（RC003）接入 Linux。当前已实现 ATVV 语音采集、IMA ADPCM
解码、本地语音转写、Linux 桌面焦点输入，以及完整的 13 键映射。

## 当前能力

- 按住语音键说话，松手后把文字输出到 stdout
- 在 Wayland 或 X11 下把中文自动粘贴到当前焦点
- 可把转写结果同时追加到文本文件
- 进程内常驻 Sherpa-ONNX Paraformer，也可回退 Voxtype CLI 或 faster-whisper
- 自动发现已配对的 RC003，断线后持续重连
- 13 键全部可配置，支持短按、长按、双击、OK+方向手势、层和宏
- Wayland 使用 `wtype`，也可回退 `ydotool`；X11 使用 `xdotool`
- 只打开 RC003 自己的输入节点，不修改桌面快捷键或物理键盘行为
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

# 调试时保存每段解码后的 WAV（默认不会保存录音）
.venv/bin/mi-remote voice --save-audio-dir debug-audio

# 使用自定义术语纠正表
.venv/bin/mi-remote voice --terms examples/terms.example.json --inject

# 语音、焦点输入和完整 13 键映射在同一进程运行（推荐完整模式）
.venv/bin/mi-remote voice --config examples/remote.example.json --inject

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

### 13 键、层、手势和宏

先确认系统看到的逻辑键；该命令只独占 VID `2717` / PID `32b8` 的 RC003，不会读取
物理键盘：

```bash
.venv/bin/mi-remote keys watch
```

只运行按键功能、不连接 BLE 语音通道：

```bash
.venv/bin/mi-remote keys run --config examples/remote.example.json
```

通常应使用前面的 `voice --config ... --inject`，让一个进程同时持有 evdev 与 BLE，完成
语音输入和全部按键动作。不要同时启动 `voice` 与 `keys run`，单实例锁也会阻止这种争抢。

示例配置包含本机真机确认的 13 键默认表：语音键由 ATVV 处理，方向/确认/返回作为常规
导航键，音量键控制系统音量；长按主页键切换层 1，TV 切换层 2。活动层空闲 20 秒自动
退出，任何状态长按菜单 1.5 秒也会回到基础层。

配置动作类型如下：

- `key_stroke`：`key` 加可选 `mods`，例如 `ctrl`、`shift`、`alt`、`super`
- `text`：把 UTF-8 `value` 粘贴到当前焦点
- `system`：音量、静音、播放控制或锁屏
- `command`：仅接受 `argv` 字符串数组，直接执行程序，不经过 shell
- `layer_momentary` / `layer_toggle`：临时层或锁定层
- `macro`：`steps` 中可混合上述动作和 `{"type":"delay","ms":80}`
- `voice` / `none`：保留给语音链路或显式不执行动作

`bindings.<按键>` 可配置 `tap`、`hold`、`double`、`gesture` 和 `layers`。如果不同固件产生
不同 evdev code，可在根级 `key_codes` 中覆盖，例如 `{"200":"voice"}`；先用
`keys watch` 获取真实 code。配置严格校验，拼错动作、按键或字段时启动会直接报错。

映射引擎完整支持 `OK+方向` 手势和 `layer_momentary`，但本机 RC003-MS 在 Linux evdev
真机测试中是单键 rollover：按住任意键时不会上报第二个键，所以默认配置不用同时按键。
其他固件/遥控器若能上报组合键，可直接在 JSON 启用这两项能力。

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
按键动作可安装并配置 `ydotool/ydotoold` 作为 uinput 回退，焦点文字仍可切换到 X11 会话。
Linux 没有一个不经桌面授权、适用于所有 Wayland 合成器的全局
焦点输入接口。

Sherpa-ONNX Paraformer 会在启动时加载一次并常驻复用。本机实测加载约 0.99 秒，之后
1 秒音频推理约 0.04 秒。RC003 裸 ADPCM 流开头约 0.20 秒是解码状态收敛前导，程序会
丢弃前 0.25 秒，并用 20ms 分帧语音活动检测拦截近静音录音。Voxtype CLI 仍作为兼容
回退。所有引擎收到的都是遥控器解码后的 16 kHz PCM，不会改用电脑麦克风。

`--save-audio-dir` 只用于诊断识别问题，启用后会保留未经前导裁剪的 WAV，并避免覆盖目录
中已有的录音。录音可能包含敏感语音，分析后请自行删除；默认不会把音频写入磁盘。

### 自定义术语纠正

Paraformer 对中文表现较好，但 `Codex`、`GitHub` 等专有名词可能被识别成近音文本。可复制
仓库的 `examples/terms.example.json` 并按实际识别日志维护替换规则：

```json
{
  "replacements": {
    "colalax": "Codex",
    "code x": "Codex",
    "电脑号本": "GitHub",
    "game up": "GitHub",
    "python": "Python"
  }
}
```

```bash
.venv/bin/mi-remote voice --terms ~/.config/mi-remote-linux/terms.json --inject
```

替换按最长词优先，英文大小写不敏感，并且每段文本只替换一次，不会发生连锁误改。日志
保留引擎的原始识别结果；stdout、输出文件和焦点输入使用纠正后的文本。项目默认不启用
任何个人术语规则。

同一段遥控器真机录音对照结果（2026-08-24）：

- faster-whisper base：`這是小米熬空機與銀蔬測試`
- Voxtype Paraformer：`这是小米遥控器语音输入测试`

因此中文输入推荐保留默认 `--engine auto`。模型文件是可选下载项，缺失时仍可使用
faster-whisper。

## 语音 HID 键隔离与权限

RC003 的语音键会同时产生 ATVV 语音事件和 HID 键。本机 RC003-MS 固件 `0x00a4` 真机确认
为 `KEY_F5`；程序也兼容此前环境中观察到的 `KEY_F9`。默认 `--grab-hid safe` 只匹配
RC003（VID `2717`、PID `32b8`）：独占其输入节点、丢弃 F5/F9，并通过 uinput 原样转发其余
按键。普通键盘的 F9 不会被打开或修改。如果无法创建 uinput，safe 模式不会独占共享
节点；显式使用 `--grab-hid force` 可在这种情况下继续隔离语音键，但会暂时屏蔽该节点上的
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

- 某些 RC003 固件把语音键与其他遥控器按键放在同一 event 节点；没有 uinput 权限时默认
  safe 模式不会独占它，此时可安装规则或权衡后使用 `--grab-hid force`。
- 部分 GNOME/KDE Wayland 会限制虚拟键盘协议，`--inject` 在这些环境中可能只能把结果
  保留到剪贴板/stdout。
- `--inject` 会覆盖当前文本剪贴板；`--submit` 会额外发送 Enter，默认不开启。

## 后台自启动

仓库提供纯语音的 `systemd/mi-remote-voice.service.example` 和语音+13 键的
`systemd/mi-remote.service.example`。复制所需版本到用户服务目录后，把其中的项目路径
改成实际值；MAC 地址可以省略并自动发现。以下仍以纯语音版本为例：

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
跨批状态、PCM 后处理、常驻 Paraformer、静音保护、术语纠正、断线重连、单实例、RC003
设备级语音键隔离、13 键 HID 映射、tap/hold/double、手势、层、宏，以及 Wayland/X11
动作生成与注入失败回退。实机验收步骤见 [AGENTS.md](AGENTS.md)。

## 开发路线

1. Phase C：语音采集、常驻 Paraformer、焦点输入和自动重连（已完成）
2. Phase B：13 键映射、层、手势、宏与 Wayland/X11 动作后端（已完成）
3. 后续：per-app profile、配置 GUI、更多合成器真机兼容矩阵

## 参考

- 协议与 macOS 参考实现：
  [godarrenw/mi_remote_control](https://github.com/godarrenw/mi_remote_control)

## 许可证

[MIT](LICENSE)。Linux 移植保留了上游 MIT 许可与署名，详见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
