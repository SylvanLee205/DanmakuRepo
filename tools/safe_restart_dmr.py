"""旧名字的入口，等价于 tools/restart_dmr.py（已合并，保留兼容）。

2026-11-02：原来这份脚本是 2026-10-07 那次事故后手写的，用
`subprocess.run(['powershell', ...])` 列进程（PATH 被裁剪的环境里直接
FileNotFoundError，一个进程都停不掉），而且路径写死了 `./直播回放`。
现在逻辑统一到 `tools/restart_dmr.py`（psutil + paths + 优雅停止 +
自动拉起 + 启动验证），这里只做转发，老文档里的命令继续能用。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from restart_dmr import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
