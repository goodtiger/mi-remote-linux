# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

### Added

- 语音键松开后按 macOS 参考实现排空尾部音频：最后一次收到数据后再等 20ms 确认，
  总超时 170ms 强制释放，避免末尾约 20ms 语音被截断。

### Fixed

- RC003 输入节点打开、读能力或独占失败后会在下一轮扫描重试；此前一次暂时性失败
  （udev 规则未生效、节点被其他进程短暂占用）会让语音键隔离整场失效。
- 鼠标模式和按键服务的后台任务不再是无引用的 `create_task`：任务被强引用，
  失败的鼠标点击和浮层通知会记入日志，退出时统一排空。
- `mi-remote test --count 0` 现在返回 argparse 参数错误，不再抛出堆栈。
- Whisper 模型加载失败时记录日志并返回失败，与 Paraformer 后端的处理保持一致。
- 前台应用轮询遇到合成器返回的异常 JSON 不再静默终止，App profile 切换不会失效。
- `--config` 接管输入节点时会明确提示 `--grab-hid` 被忽略。

## [0.4.1] - 2026-08-24

### Added

- 自动读取终端进程树，在 foot/Ghostty 等窗口内区分 Pi、Codex 和 Claude CLI；Pi 默认
  profile 提供四方向、确认和逐字删除。
- `mi-remote test voice --count N` 可在同一 BLE 连接上执行连续语音压力测试并逐轮报告
  相似度、延迟、时长、RMS 和峰值。
- 通用 Wayland 文字注入支持 `ydotool/ydotoold`，与 Hyprland 原生输入和 `wtype`
  共同构成跨合成器回退链。

### Fixed

- 为每次 BLE 连接分配独立代次，过期的断开、控制、音频通知和 MIC 命令不再污染重连
  后的新状态；同代重复 CAPS 被安全忽略。
- Hyprland 优先使用原生焦点按键事件，修复 foot 等终端中 `wtype` 成功返回但方向键、
  返回键和语音粘贴未生效的问题，并保留 `wtype`/`ydotool` 回退。
- 终端自动粘贴与 Omarchy 策略保持一致，改用 `Shift+Insert`。
- Ghostty/foot、Codex 和 Claude profile 的返回键短按改为 `Backspace`，便于在 AI TUI
  输入行中逐字删除；App 控制层仍保留 `Escape`。

## [0.4.0] - 2026-08-24

首个功能完整的 Linux 预览版本。

### Added

- 小米 RC003 ATVV 语音采集、ADPCM 解码与自动重连。
- 常驻 Sherpa-ONNX Paraformer、Voxtype 和 faster-whisper 转写后端。
- Wayland/X11 当前焦点输入、术语纠正与可选自动提交。
- 对应 macOS v7 的 13 键、手势、层、宏、App profile、浮层与鼠标模式。
- `mi-remote doctor` 只读诊断和 `mi-remote test` 交互式真机验收。
- 安全的用户级 systemd 服务管理以及 udev 规则预览。
- Paraformer 模型发现、固定版本下载、SHA-256 校验和原子安装。
- 版本化用户安装器、发行校验与 GitHub Release 自动化。

### Safety

- 不修改 Linux 桌面快捷键或物理键盘配置。
- 服务安装/卸载默认只预览，必须显式使用 `--apply`。
- 模型和发行包在写入或安装前执行完整性校验。

[Unreleased]: https://github.com/goodtiger/mi-remote-linux/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/goodtiger/mi-remote-linux/releases/tag/v0.4.1
[0.4.0]: https://github.com/goodtiger/mi-remote-linux/releases/tag/v0.4.0
