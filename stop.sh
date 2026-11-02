#!/usr/bin/env sh
# 优雅停止 DMR（Linux / macOS 版）。
#
# 默认是**优雅停止**：给 ffmpeg 发 'q'，让它把当前分段写完并改名，
# 下次启动由对账器自动补渲染。加 --force 才硬杀。
#
# 用法：
#     ./stop.sh
#     ./stop.sh --force

set -e
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "[X] 找不到 python3"
    exit 1
fi

exec "$PY" tools/stop_dmr.py "$@"
