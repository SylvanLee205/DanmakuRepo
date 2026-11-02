#!/usr/bin/env sh
# DanmakuRepo 启动脚本（Linux / macOS）
#
# 对应 Windows 的 Start_DMR.bat，但**没有崩溃重启循环** ——
# 那部分交给 systemd / launchd 做更合适（它们能处理开机自启、日志、崩溃重拉）。
# 见 docs/跨平台.md。
#
# 用法：
#     ./start.sh               前台运行（Ctrl+C 停）
#     ./start.sh --quiet       同上，控制台只留关键进度
#
# ⚠️ 本脚本**未在真实 Linux 环境验证过**（开发机是 Windows）。
#    核心逻辑（找解释器 → 跑 main.py）很简单，但踩坑请反馈。

set -e
cd "$(dirname "$0")"

PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "[X] 找不到 python3。"
    echo "    先运行 ./setup.sh，或安装 Python 3.9+："
    echo "      Debian/Ubuntu: sudo apt install python3 python3-venv"
    echo "      macOS:         brew install python@3.12"
    exit 1
fi

# 提醒：libass 渲染中文必须有 CJK 字体，否则整场渲染会失败
if command -v fc-list >/dev/null 2>&1; then
    if ! fc-list 2>/dev/null | grep -qi "CJK\|Source Han\|Noto Sans SC\|WenQuanYi"; then
        echo "[!] 警告：系统里没找到中文字体，弹幕渲染会失败。"
        echo "    Debian/Ubuntu: sudo apt install fonts-noto-cjk"
        echo "    macOS 一般自带，可忽略。"
        echo
    fi
fi

echo "[DMR] 解释器: $PY"
exec "$PY" main.py "$@"
