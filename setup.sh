#!/usr/bin/env sh
# DanmakuRepo 首次安装 / 环境修复（Linux / macOS 版）
#
# 对应 Windows 的 Setup.bat。它会调 tools/bootstrap.py，
# 后者在非 Windows 上会跳过桌面快捷方式和计划任务，只装 Python 环境。
#
# 用法：
#     chmod +x setup.sh && ./setup.sh

set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "=============================================================="
    echo " 没有找到 Python 3"
    echo "=============================================================="
    echo
    echo " 先装一个，再重新运行本脚本："
    echo "   Debian/Ubuntu : sudo apt install python3 python3-venv"
    echo "   Fedora        : sudo dnf install python3"
    echo "   Arch          : sudo pacman -S python"
    echo "   macOS         : brew install python@3.12"
    echo
    exit 1
fi

exec "$PY" tools/bootstrap.py "$@"
