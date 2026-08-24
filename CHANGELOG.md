# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

### Fixed

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

[Unreleased]: https://github.com/goodtiger/mi-remote-linux/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/goodtiger/mi-remote-linux/releases/tag/v0.4.0
