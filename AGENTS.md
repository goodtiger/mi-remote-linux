# MiRemote Linux - 开发指南

## 项目概述

将小米蓝牙遥控器 2 Pro (RC003) 接入 Linux，实现语音输入 + 按键映射，用于 Vibe Coding（Codex / Claude Code 等 AI 编程工具）。

## 参考项目

上游 macOS 项目：<https://github.com/godarrenw/mi_remote_control>

核心协议逆向成果已全部完成，本项目直接复用其协议逻辑。

## 开发路线

### Phase C：最小可行版（MVP）— 语音通道

**目标**：按住遥控器语音键说话 → 本地 Whisper 转写 → 文字输出到 stdout/文件或当前焦点

**核心模块**：
1. `ble_client.py` — bleak 连接遥控器 BLE，订阅 ATVV GATT 服务
2. `adpcm.py` — IMA ADPCM 解码器（从上游 ADPCMDecoder.swift 移植）
3. `atvv.py` — ATVV 常量、命令和值解析（从上游 ATVVBridge.swift 移植）
4. `voice.py` — 音频帧 → ADPCM 解码 → PCM → Paraformer/Whisper 转写
5. `injector.py` — Wayland/X11 后端，把 UTF-8 文本粘贴到当前焦点
6. `main.py` — CLI 入口，按住说话松手发送

**当前状态（2026-08-24）**：Phase C 核心链路、常驻 Paraformer、焦点输入、设备级
语音 HID 键隔离、单实例和 BLE 自动重连已实现并通过自动化；Phase B 的 13 键映射、
tap/hold/double、OK+方向手势、层、宏及 Wayland/X11 动作后端也已实现。

真机结果：BlueZ 已连接设备发现、ATVV v1 CAPS、120 字节裸帧、三次连续录音、
ADPCM→PCM、tiny Whisper 下载/转写、MIC_CLOSE 和 Ctrl+C 后恢复 HID 连接均通过。
本固件未提供 126 字节带头帧，五次连续会话压力测试仍可作为后续稳定性检查。

**硬件信息**：
- 遥控器：小米蓝牙遥控器 2 Pro，型号 RC003-MS
- BLE VID: 0x2717, PID: 0x32B8
- ATVV Service UUID: AB5E0001-5A21-4F05-BC7D-AF01F617B664
- ATVV TX Char: AB5E0002-5A21-4F05-BC7D-AF01F617B664（主机写命令）
- ATVV Audio Char: AB5E0003-5A21-4F05-BC7D-AF01F617B664（音频帧 notify）
- ATVV Control Char: AB5E0004-5A21-4F05-BC7D-AF01F617B664（控制 notify）
- 音频编码：IMA ADPCM，16kHz 单声道，4:1 压缩
- 帧长：120 字节/帧（能力帧协商）

**ATVV 协议握手流程**（严格顺序）：
1. 连接 BLE，发现 ATVV service 和 3 个 characteristic
2. 订阅 audio + control 的 notify
3. 发送 GET_CAPS: `[0x0A, 0x01, 0x00, 0x00, 0x03, 0x03]` → TX
4. 收到 0x0B 能力帧 → 解析版本/codec/帧长
5. 等待用户按下语音键 → 收到 0x08（MIC_REQUEST）
6. 发送 MIC_OPEN: `[0x0C, 0x00]`（v1.0+）→ TX
7. 收到 0x04（AUDIO_START）→ 开始接收音频帧
8. 收到 0x0A（SYNC）→ 重置 ADPCM 解码器
9. 音频帧通过 audio notify 到达，按 120 字节切帧
10. 收到 0x00（AUDIO_STOP）→ v1 发送 MIC_CLOSE: `[0x0D, streamID低字节]`

**Phase C 验收顺序**：
1. 在 Linux/BlueZ 下完成 RC003 配对、信任和连接
2. 验证 CAPS → MIC_OPEN → AUDIO_START/SYNC → AUDIO_STOP/MIC_CLOSE 真机时序
3. 分别验证 120 字节裸帧与 126 字节带头帧（若固件提供）
4. 验证 5 次连续语音会话、常驻模型延迟、ADPCM 前导裁剪和静音不误输入
5. 验证遥控器 F9 被隔离但物理键盘 F9 保持原有行为
6. 验证遥控器休眠/唤醒自动重连、第二实例拒绝启动和 Ctrl+C 有界退出
7. 在隔离测试窗口验证中文注入，再验证常用终端、浏览器输入框和编辑器
8. 记录 `mi-remote -v voice --inject` 日志后再进入 Phase B

**按键处理**：
- Linux 蓝牙配对后，HID 按键自动映射为 `/dev/input/event*`
- 可用 `evdev` 库读取，无需自己实现 BLE HID 解析
- 语音键同时触发 HID 事件（RC003-MS 0x00a4 真机为 KEY_F5，兼容 KEY_F9）和 ATVV
  协议（0x08），用 ATVV 侧控制语音生命周期；按 VID/PID 隔离，不修改物理键盘或桌面配置

### Phase B：完整按键映射

**目标**：13 个按键全部可自定义映射，支持层、手势、宏

**核心模块**：
6. `hid_engine.py` — evdev 读取遥控器按键事件
7. `mapping_engine.py` — 从上游 MappingEngine.swift 移植，按键绑定/层/手势/宏
8. `action_runner.py` — wtype / ydotool / xdotool 虚拟键盘输出
9. `config.py` — JSON 配置文件加载

**当前状态（2026-08-24）**：已完成。真机键码为 power=116、voice=63、up=103、
down=108、left=105、right=106、ok=28、back=158、home=102、menu=127、tv=41、
volUp=115、volDown=114。`mi-remote keys watch` 用于探针，`keys run` 用于纯按键模式，
`voice --config ...` 用于语音和按键合并运行。

macOS v7 对应层已实现：包内默认配置、App 控制模式、前台应用 profile、Hyprland/Sway/X11
窗口与工作区适配、通知式窗口选择器/系统菜单/App 轮盘/教程，以及鼠标模式。完整模式使用
`mi-remote voice --config --inject`；`mi-remote config show` 可导出默认 JSON。

本机 evdev 真机确认是单键 rollover：一个键按住期间第二键不会上报。因此引擎保留
OK+方向手势与 momentary layer 兼容能力，但默认 RC003 配置使用长按/TV toggle 层。

## 技术栈

- Python 3.10+
- `bleak` — BLE GATT 客户端
- `evdev` — Linux 输入设备读取
- `numpy` — ADPCM 解码加速（可选）
- `Sherpa-ONNX Paraformer`（中文优先）、Voxtype CLI 或 `faster-whisper` — 本地语音转写
- `ydotool` — Wayland 兼容按键模拟（Hyprland 用户）

## 文件结构

```
mi-remote-linux/
├── AGENTS.md              # 本文件
├── README.md              # 项目说明
├── pyproject.toml         # Python 项目配置
├── src/
│   └── mi_remote_linux/
│       ├── __init__.py
│       ├── main.py        # CLI 入口
│       ├── ble_client.py  # BLE 连接管理
│       ├── atvv.py        # ATVV 协议状态机
│       ├── adpcm.py       # IMA ADPCM 解码器
│       ├── voice.py       # 语音转写管道
│       ├── injector.py    # Wayland 当前焦点文本注入
│       ├── doctor.py      # 只读安装、权限、桌面与模型诊断
│       ├── hid_guard.py   # RC003 语音 HID 键过滤与其他键 uinput 转发
│       ├── runtime.py     # 语音进程单实例锁
│       ├── text_corrector.py # 可配置的识别术语纠正
│       ├── hid_engine.py  # evdev RC003 热插拔与 13 键读取
│       ├── mapping_engine.py # tap/hold/double、层与手势状态机
│       ├── action_runner.py  # Wayland/X11 动作执行
│       ├── desktop.py     # 前台 App、窗口与工作区适配
│       ├── interactions.py # 浮层和鼠标模式
│       ├── default_mapping.json # macOS v7 对应默认配置
│       ├── remote_keys.py # 按键服务生命周期
│       └── config.py      # 严格 JSON 配置模型
├── tests/
│   ├── test_adpcm.py
│   ├── test_atvv.py
│   ├── test_ble_client.py
│   ├── test_injector.py
│   ├── test_main.py
│   └── test_voice.py
├── systemd/
│   └── mi-remote-voice.service.example
└── scripts/
    ├── download_paraformer.py # Paraformer 模型下载脚本
    └── setup.sh               # 环境安装脚本
```

## 开发规范

- 每个模块先写测试，再写实现
- ATVV 协议逻辑严格参考上游 `Sources/MiRemote/Bluetooth/ATVVBridge.swift`
- ADPCM 解码器严格参考上游 `Sources/MiRemote/Bluetooth/ADPCMDecoder.swift`
- 所有 BLE UUID 和协议常量定义在 `atvv.py` 的顶部，与上游 Contracts.swift 保持一致
- 日志使用 `logging` 模块，级别可调

## 注意事项

- bleak 在 Linux 上需要 BlueZ 5.x + D-Bus
- 首次使用需要 `bluetoothctl` 手动配对遥控器（同时长按 菜单键 + HOME）
- Wayland 焦点输入使用 wl-copy + wtype，X11 使用 xclip + xdotool
- 不修改桌面快捷键；终端检测失败时通过 `--paste-shortcut` 显式选择粘贴组合键
- CLI 默认 `--engine auto`：优先常驻 Sherpa-ONNX Paraformer，再回退 Voxtype CLI 或
  faster-whisper base
