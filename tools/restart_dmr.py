"""一键重启 DMR：优雅停止 -> 重新拉起守护 -> 验证真的起来了。

为什么需要单独的重启脚本
------------------------
DMR 平时由 `Start_Render.bat` 的守护循环常驻（程序退出后 10 秒自动重拉）。
改完 `DMR/` 下的代码想让它生效时，直接关窗口是没用的 —— 守护会立刻再拉一个
起来；而只杀 python 又会留下孤儿 `[正在录制]` 分段（2026-10-07 就是这么
丢出 3 个孤儿分段、1.8 GB 的）。

正确顺序必须是"先停守护 → 再停 DMR → 最后重新拉起"，本脚本把三步合成一步：

    1. 停守护 `cmd`（Start_Render 的循环）—— 否则它会在你停 DMR 的瞬间重拉
    2. 优雅停 DMR：写 `graceful_stop_<PID>` 标志，main.py 轮询到就走
       `dmr.stop()`——ffmpeg 收到 'q' 写完当前段并改名，正在渲染的会被中断
       但源视频还在，下次启动 reconciler 自动补派渲染
    3. 清理残留标志 —— **关键**：残留标志会让新实例"启动即退出"，
       再被守护拉起、再退出，变成崩溃循环刷日志
    4. 用 explorer.exe 重新拉起 Start_Render.bat（脱离本进程，窗口独立）
    5. 轮询验证 DMR 与守护都在（最多等 90 秒），把结果如实打出来

用法：
    .venv\\Scripts\\python.exe tools\\restart_dmr.py              # 优雅重启（默认）
    .venv\\Scripts\\python.exe tools\\restart_dmr.py --force       # 不等收尾，硬停后重启
    .venv\\Scripts\\python.exe tools\\restart_dmr.py --timeout 60  # 优雅等待秒数
    .venv\\Scripts\\python.exe tools\\restart_dmr.py --stop-only   # 只停不启
    .venv\\Scripts\\python.exe tools\\restart_dmr.py --dry-run     # 只看会做什么

⚠️ 录制中重启会中断当前分段（内容通常完整，只是少一次改名，可用
   tools/rename_orphans.py 补登记），所以默认会先把正在录的任务打出来。
"""
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# stdout/stderr 可能是管道或被 GBK 控制台捕获，下面这些 ✅/⚠️ 不在 GBK 里，
# 不设置 errors='replace' 会直接 UnicodeEncodeError 崩在半路
# （2026-10-20 在 stop_dmr.py 上踩过一模一样的坑）。
try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass


def _flag(name, default=None):
    return name in sys.argv


def _opt_int(name, default):
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default


FORCE = _flag('--force')
DRY_RUN = _flag('--dry-run')
STOP_ONLY = _flag('--stop-only')
TIMEOUT = _opt_int('--timeout', 180)
VERIFY_TIMEOUT = _opt_int('--verify', 90)


def load_stop_dmr():
    """按文件路径加载 tools/stop_dmr.py，复用它的进程查找与优雅停止逻辑。

    为什么不用 `from tools import stop_dmr`：`tools/__init__.py` 会
    `from .check_env import *`，那是个体检脚本，import 它有副作用。
    按文件加载既拿到复用，又不牵连任何别的模块。
    """
    import importlib.util
    path = os.path.join(PROJECT_ROOT, 'tools', 'stop_dmr.py')
    spec = importlib.util.spec_from_file_location('_dmr_stop', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def explorer_exe() -> str:
    """explorer.exe 的绝对路径（它在 %SystemRoot% 下，不在 System32）。"""
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    cand = os.path.join(root, 'explorer.exe')
    return cand if os.path.exists(cand) else 'explorer.exe'


def hr(title=''):
    print()
    print('=' * 78)
    if title:
        print(title)
        print('=' * 78)


def main() -> int:
    st = load_stop_dmr()
    UP = PROJECT_ROOT

    hr('1. 当前状态')
    st.show_state('重启前')
    parts = st.recording_files()
    print(f'  [正在录制] 文件: {len(parts)} 个')
    for p, sz in sorted(parts, key=lambda x: -x[1])[:10]:
        print(f'    {sz/1024/1024:>8.1f} MB  {os.path.basename(p)[:70]}')
    if parts and not FORCE and not DRY_RUN:
        print('  这些分段会被优雅收尾（写完并改名）；如不想等，加 --force。')

    if DRY_RUN:
        print('\n  DRY-RUN 结束：以上就是要重启的东西，什么都没动。')
        return 0

    # ---------------------------------------------------------------- 停
    hr('2. 停止守护（否则它会立刻把 DMR 拉回来）')
    cmds = st.pids_cmd()
    print(f'  守护 cmd: {cmds or "无"}')
    st.kill(cmds, '守护')
    time.sleep(2)

    hr('3. 停止 DMR' + ('（--force 硬停）' if FORCE else '（优雅，等待收尾）'))
    dmr = st.pids_dmr()
    print(f'  DMR: {dmr or "无"}')
    if dmr:
        if FORCE:
            st.kill(list(reversed(dmr)), 'DMR')
            time.sleep(3)
        else:
            if not st.graceful_stop(dmr, TIMEOUT):
                print(f'  ⚠️ 等了 {TIMEOUT} 秒仍未退出，转为硬杀。')
                st.kill(list(reversed(st.pids_dmr())), 'DMR')
                time.sleep(3)

    hr('4. 停止录制子进程')
    st.kill(st.pids_sg(), 'streamgears')
    st.kill(st.pids_ff(), 'ffmpeg')
    time.sleep(2)

    # 残留的 graceful_stop_* 会让新实例启动即退出 —— 必须清干净
    n = st._clear_flags()
    if n:
        print(f'  已清理 {n} 个残留的优雅停止标志')

    left = st.pids_cmd() + st.pids_dmr() + st.pids_sg() + st.pids_ff()
    if left:
        print(f'  ❌ 仍有进程未退出: {left}，重启中止（先手动处理它们）。')
        return 1
    print('  ✅ 已全部停止')

    if STOP_ONLY:
        print('\n  --stop-only：不重新拉起。要启动请双击 Start_DMR.bat。')
        return 0

    # ---------------------------------------------------------------- 起
    hr('5. 重新拉起（Start_Render.bat，守护 + 通知都在里面）')
    bat = os.path.join(UP, 'Start_Render.bat')
    if not os.path.exists(bat):
        print(f'  ❌ 找不到 {bat}，无法拉起。')
        return 1
    try:
        # explorer.exe 拉起是"发射后不管"：新窗口与本进程完全脱钩，
        # 本脚本退出后守护窗口照常常驻（与 Start_DMR.bat 的做法一致）。
        subprocess.Popen([explorer_exe(), bat],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('  已发起，等待启动...')
    except Exception as e:
        print(f'  ❌ 拉起失败: {type(e).__name__}: {e}')
        return 1

    ok = False
    for i in range(max(1, VERIFY_TIMEOUT // 5)):
        time.sleep(5)
        n_dmr = len(st.pids_dmr())
        n_cmd = len(st.pids_cmd())
        if n_dmr and n_cmd:
            print(f'    +{(i + 1) * 5}s  DMR={n_dmr} 守护={n_cmd}  ✅')
            ok = True
            break
        if i % 2 == 0:
            print(f'    +{(i + 1) * 5}s  DMR={n_dmr} 守护={n_cmd}')

    hr('6. 最终状态')
    st.show_state('重启后')
    if not ok:
        print('\n  ❌ 没等到 DMR 起来。看日志：双击 DMR-看实时日志，或看 logs/。')
        return 1

    leftover = st.recording_files()
    if leftover:
        print(f'\n  ⚠️ 仍有 {len(leftover)} 个 [正在录制] 没能改名：')
        for p, sz in sorted(leftover, key=lambda x: -x[1])[:10]:
            print(f'     {sz/1024/1024:>8.1f} MB  {os.path.basename(p)[:70]}')
        print('     内容通常完整，只是没走到 rename；需要时用 tools/rename_orphans.py 登记。')
    else:
        print('\n  ✅ 重启完成，没有孤儿分段。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
