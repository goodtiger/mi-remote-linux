#!/usr/bin/env bash
# MiRemote Linux 本地虚拟环境安装脚本。

set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_dir"

echo "=== MiRemote Linux 安装 ==="

if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未找到 Python 3.10+"
    exit 1
fi

python3 -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10+"'
echo "✅ $(python3 --version)"

if ! command -v bluetoothctl >/dev/null 2>&1; then
    echo "❌ 未找到 bluetoothctl，请先安装 BlueZ："
    echo "   Arch/Omarchy: sudo pacman -S bluez bluez-utils"
    echo "   Debian/Ubuntu: sudo apt install bluez"
    exit 1
fi
echo "✅ BlueZ 已安装"

python3 -m venv .venv
extras=voice
if [[ ${MI_REMOTE_INSTALL_DEV:-0} == 1 ]]; then
    extras=voice,dev
fi
.venv/bin/python -m pip install -e ".[${extras}]"

missing_injection=()
command -v wl-copy >/dev/null 2>&1 || missing_injection+=(wl-clipboard)
command -v wtype >/dev/null 2>&1 || missing_injection+=(wtype)
if (( ${#missing_injection[@]} > 0 )); then
    echo "ℹ️  自动输入依赖未完整安装：${missing_injection[*]}"
    echo "   Arch/Omarchy: sudo pacman -S wtype wl-clipboard"
    echo "   Debian/Ubuntu: sudo apt install wtype wl-clipboard"
else
    echo "✅ Wayland 自动输入依赖已安装"
fi

if [[ ${XDG_SESSION_TYPE:-} == x11 ]] && \
    { ! command -v xclip >/dev/null 2>&1 || ! command -v xdotool >/dev/null 2>&1; }; then
    echo "ℹ️  X11 自动输入需要 xclip + xdotool。"
fi

echo
echo "=== 安装完成 ==="
echo "配对后运行: $project_dir/.venv/bin/mi-remote voice --inject"
echo "详细配对步骤见 README.md"
