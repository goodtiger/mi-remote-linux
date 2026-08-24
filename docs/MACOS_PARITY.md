# macOS MiRemote v7 → Linux 功能对应表

权威参考：macOS 仓库 `AppServices.defaultConfig()`、`Presets.all`、`MappingEngine` 和
`ActionRunner`。Linux 不复制 AppKit/Accessibility API，而保持用户可见语义并选择桌面后端。

| macOS 能力 | Linux 对应实现 | 后端/降级 |
|---|---|---|
| 13 键 HID | `HIDEngine` | evdev，严格匹配 RC003 VID/PID |
| tap/hold/double | `MappingEngine` | asyncio 定时状态机 |
| gesture/layer/macro | `MappingEngine` | 配置 v2；宏命令有界执行 |
| per-app overlay | `ApplicationTracker` + profile merge | Hyprland/Sway/X11 活动窗口；未知桌面回 global |
| Mission Control | `mission_control` overlay | 窗口/任务列表，原生 IPC 激活 |
| Window Picker | `window_picker` overlay | 通知 HUD + 遥控器选择 |
| System Menu | `system_menu` overlay | 12 项目录；危险动作按住 OK 0.6 秒确认 |
| App Wheel | `app_wheel` overlay | 每 App 一个代表窗口 |
| Tutorial | `tutorial` overlay | 分页通知 HUD |
| Control-mode HUD | layer 2 notification | 显示 profile 和键位提示 |
| Window cycle | `window_cycle` | Hyprland/Sway/X11 |
| Workspace actions | `space_left/right` | Hyprland/Sway；其他桌面明确报不支持 |
| Tab jump | `tab_jump` | Linux 通用 Ctrl+PageUp/PageDown/Ctrl+数字 |
| Focus input | `focus_input` | 恢复活动客户端焦点；Linux 无跨 toolkit 通用文本控件 AX API |
| Mouse mode | `MouseMode` | Hyprland 原生 cursor IPC（自动兼容 0.55+ Lua）；Wayland ydotool；X11 xdotool |
| Open app | `open_app` | freedesktop `gtk-launch` desktop ID |
| Voice | 原生 ATVV → 本地 ASR → 焦点粘贴 | 不需要 macOS 虚拟声卡/外部语音 App |

浮层交互也按参考实现对应：窗口选择器使用“全局 → 当前 App → 关闭”三段菜单流程，App
轮盘空闲 3 秒关闭，其他捕获式浮层空闲 20 秒关闭，Home/返回是统一退出键，长按菜单
1.5 秒是任何状态下的逃生键。基础层长按返回受保护；只有显式打开
`settings.delete_all_on_hold` 才执行全选删除。

## 默认预设翻译原则

- macOS Command → Linux Ctrl；Option → Alt。
- Safari → Firefox；IINA → mpv；Keynote/PowerPoint → PowerPoint/LibreOffice Impress。
- Zoom/腾讯会议/飞书使用各自 Linux 常用会议快捷键语义。
- Ghostty profile 同时匹配 Foot、Kitty、Alacritty、Konsole、WezTerm 等 Linux 终端。
- 当前 RC003 Linux HID 是单键 rollover，因此默认与 macOS v7 一样不使用组合按键；引擎仍
  支持能上报多键的其他固件/设备。

## 平台边界

Linux Wayland 没有等价于 macOS Accessibility 的统一、无授权全局 API。窗口和工作区操作
优先使用合成器官方 IPC；无法可靠执行时明确提示，不修改用户桌面快捷键作为替代。通知式
浮层确保不依赖 GTK/Qt/特定 launcher，也不会写入 Hyprland、GNOME 或 KDE 配置。macOS
原生设置页在 Linux 以严格 JSON + `config show/validate` CLI 对应，核心遥控操作无需 GUI。
