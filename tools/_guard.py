"""tools 下脚本共用的护栏：编码、进程检测、状态库备份。

为什么要有这个模块
------------------
2026-11-16 的审查发现三个反复出现的问题，每个都在十几个脚本里各写一遍：

1. **GBK 控制台崩溃**：脚本用 ``print('✅ ...')``，在计划任务、管道、
   未 ``chcp 65001`` 的控制台里直接 ``UnicodeEncodeError``。bat 包装里的
   ``chcp`` 只是掩盖了它。
2. **进程匹配过宽**：``'main.py' in 'domain.py' == True``，且不判断属主，
   于是"机器上有别的项目在跑 main.py"会让 DMR 永远不启动；``--force``
   停进程还可能杀掉无关项目。
3. **活库热拷贝**：直接把正在写的 ``dmr_state.db`` 复制成备份，可能拿到
   半页数据。要用 SQLite 的在线备份 API。

用法（脚本里只需两行）::

    from _guard import ensure_utf8_stdout, dmr_running
    ensure_utf8_stdout()

注意：本模块**不做任何**副作用，import 即安全。
"""
import os
import sys

#: 项目根目录（本文件在 tools/ 下）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_utf8_stdout():
    """把 stdout/stderr 切到 UTF-8 + 替换非法字符，避免 emoji 打印崩溃。

    只在支持 ``reconfigure`` 的流上操作（Python 3.7+）。pythonw 下
    ``sys.stdout`` 可能是 ``None``，此时直接跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                        # noqa: BLE001
            pass


def _norm(path) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:                                            # noqa: BLE001
        return ''


#: 会被误判成 DMR 的脚本名。只认这些，不做子串匹配。
_DMR_SCRIPTS = ('main.py', 'render_only.py', 'dryrun.py')


def dmr_running(include_dryrun: bool = False) -> list:
    """返回属于**本项目**的 DMR 进程 PID 列表。

    判据（必须同时满足）：
      · 命令行里出现 ``main.py`` / ``render_only.py`` / ``dryrun.py``
        **作为独立参数**（不是任何文件名的一部分）；
      · 能确认属主是本项目 —— 命令行含项目路径，或进程 cwd 就是项目根。

    这样别处跑的 ``domain.py``、``mymain.py``、另一个目录的同名脚本
    都不会被算进来。拿不到 psutil 时返回空列表（调用方按"没在跑"处理，
    与项目 fail-open 的惯例一致）。
    """
    try:
        import psutil
    except Exception:                                            # noqa: BLE001
        return []

    root = _norm(PROJECT_ROOT)
    name = os.path.basename(root)
    pids = []
    for proc in psutil.process_iter(['pid', 'cmdline', 'cwd']):
        try:
            info = proc.info or {}
            cmdline = info.get('cmdline') or []
            if not cmdline:
                continue
            if not any(os.path.basename(str(a)).lower() in _DMR_SCRIPTS
                       for a in cmdline[1:]):
                continue
            if not include_dryrun and \
                    any(os.path.basename(str(a)).lower() == 'dryrun.py'
                        for a in cmdline[1:]):
                continue
            joined = _norm(' '.join(str(x) for x in cmdline))
            cwd = info.get('cwd')
            owned = root in joined or (cwd and _norm(cwd) == root)
            if not owned:
                # 退一步：命令行里出现项目目录名，且进程 cwd 未知
                owned = bool(name) and os.sep + name + os.sep in joined
            if owned:
                pids.append(int(info.get('pid') or proc.pid))
        except Exception:                                        # noqa: BLE001
            continue
    return pids


def sqlite_backup(src: str, dst: str) -> bool:
    """用 SQLite 在线备份 API 复制状态库（活库也一致）。

    直接 ``shutil.copy2`` 活库可能拿到"主文件已更新、WAL 还没合并"的
    半页状态；``Connection.backup()`` 会拿一致性快照。
    """
    import sqlite3
    try:
        src_con = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
        dst_con = sqlite3.connect(dst)
        try:
            with dst_con:
                src_con.backup(dst_con)
        finally:
            dst_con.close()
            src_con.close()
        return True
    except Exception as e:                                       # noqa: BLE001
        print(f'  [警告] 状态库备份失败: {e}')
        return False


def require_not_running(what: str, force: bool = False) -> bool:
    """改状态库/删文件的工具在 DMR 运行时应当拒绝执行。

    返回 True 表示可以继续。``force`` 为真时只警告。
    """
    pids = dmr_running()
    if not pids:
        return True
    msg = (f'检测到 DMR 正在运行（PID {", ".join(map(str, pids))}）。'
           f'{what}会在内存状态与磁盘之间产生竞争，建议先 Stop_DMR 再执行。')
    if force:
        print(f'  [警告] {msg}（--force 已指定，继续）')
        return True
    print(f'  [拒绝] {msg}')
    print('         如确认要继续，加 --force。')
    return False


if __name__ == '__main__':
    # 自检：能直接运行，方便排查"为什么工具说 DMR 在跑/没跑"
    ensure_utf8_stdout()
    print(f'项目根: {PROJECT_ROOT}')
    print(f'DMR 进程: {dmr_running() or "未检测到"}')
    print('UTF-8 输出: OK')
