"""登录 / 开机时可靠地把 DMR 拉起来，并保证窗口在"你所在的会话"里。

为什么需要这个脚本（2026-10-19 / 10-20 的两次折腾）
--------------------------------------------------
· 原自启用 `explorer.exe "Start_Render.bat"`，登录瞬间 explorer 还没初始化完，
  调用被静默丢弃 —— 10-19 14:44 / 14:52 / 14:54 三次登录，一次都没起来且无报错。
· DMR 跑在 session 0（开机兜底）时，用户登录进来**看不到窗口**，以为程序退了。
· 2026-10-20 凌晨发现：登录自启即使检测到"DMR 在别的会话"也直接跳过，
  于是窗口永远不在用户眼前；而且之前的 detached 启动（DETACHED_PROCESS）
  根本不产生控制台窗口。

三个使用场景
------------
1. 登录自启（默认模式，无参数）—— 三处都走这里：
     · Startup 目录的 DMR_AutoUp.cmd（用户登录时，未提权）
     · 计划任务 DMR_SessionWatch（登录时 + 解锁时，管理员身份）
     · 计划任务 DMR_Desktop（手动运行，管理员身份）

       .venv\\Scripts\\python.exe tools\\autostart_launch.py

   行为：
     · DMR 没在跑        → 在**当前会话**启动，带可见的控制台窗口；
     · DMR 就在当前会话  → 什么都不做（幂等；解锁触发会频繁调它）；
     · DMR 在**别的**会话 → 停掉旧实例，在当前会话重启 —— 窗口跟着人走，
       RDP / UU远程 / RustDesk 换着用，每次登录/解锁窗口都出现在你眼前。
   启动带重试（3 次 × 25 秒），全程写 logs/autostart.log。
   多个入口可能同时触发（Startup .cmd + 计划任务），用 logs/autostart.lock
   互斥，保证只有一个在动手。

2. 开机任务（守护模式）—— Run_DMR.bat 是 DMR_Boot 开机任务的动作：

       .venv\\Scripts\\python.exe tools\\autostart_launch.py --boot-fallback 180

   常驻循环：
     · 有人登录           → 什么都不做，交给登录自启（这样窗口可见）；
     · 没人登录且 DMR 没跑 → 先等 180 秒（给自动登录留时间），然后在 session 0
       前台拉起 DMR 保证录像不停，之后一直守着：DMR 死了且仍没人登录就再拉；
     · 有人登录但 DMR 死了 → 等 10 分钟（登录自启自己有 3 次重试，足够了），
       仍无动静才在 session 0 兜底（无窗口，但录像优先）。
   本模式**从不杀/迁移进程** —— 迁移只由登录模式做，避免两边打架。

3. 测试专用：--pretend-session N  假装自己在会话 N（验证迁移分支）。
   --dry-run  只判断和记录，不启动、不杀进程、不加锁。
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time
import traceback

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录，自动识别（可放在任意路径）


UP = _PROJECT_ROOT
BAT = os.path.join(UP, 'Start_Render.bat')
STOP = os.path.join(UP, 'tools', 'stop_dmr.py')
PY = os.path.join(UP, '.venv', 'Scripts', 'python.exe')
LOG = os.path.join(UP, 'logs', 'autostart.log')
LOCK = os.path.join(UP, 'logs', 'autostart.lock')

RETRIES = 3                 # 启动重试次数
WAIT_AFTER_LAUNCH = 25      # 每次启动后等多少秒查进程
INITIAL_GRACE = 180         # 守护模式：开机后等这么久才允许 session 0 兜底
LOGGED_IN_DEAD_GRACE = 1800  # 有人登录但 DMR 死了，等这么久才 session 0 兜底
                             # （登录自启自己有 3 次重试；给够了时间，
                             #   用户手动双击启动也不会被"已在运行"挡住）
POLL = 60                   # 守护模式轮询间隔（秒）
#: 互斥锁"陈旧"阈值。必须**大于**最坏持锁时长（migrate 分支 stop_all 最多
#: 3×300s + 启动重试 3×25s ≈ 975s）。2026-11-16 之前是 150s，持锁超过 150s
#: 后另一次运行会把锁判成陈旧删掉并并发进入 → 双 stop / 双 start。
#: 同时持有者每 LOCK_HEARTBEAT 秒刷新 mtime，正常情况下永远不算陈旧。
LOCK_MAX_AGE = 1800
LOCK_HEARTBEAT = 30

_heartbeat_thread = None
_heartbeat_stop = None

DRY_RUN = '--dry-run' in sys.argv


def _int_flag(name: str, malformed_default, absent_default=0):
    """解析 --name N 或 --name=N。

    没给该参数 → absent_default；给了但值缺失/不是数字 → malformed_default。
    """
    for i, a in enumerate(sys.argv):
        if a == name:
            try:
                return int(sys.argv[i + 1])
            except (IndexError, ValueError):
                return malformed_default
        if a.startswith(name + '='):
            try:
                return int(a.split('=', 1)[1])
            except ValueError:
                return malformed_default
    return absent_default


#: --boot-fallback N：给了才进入守护模式（开机任务用）；值坏了按 180 秒算
BOOT_FALLBACK = _int_flag('--boot-fallback', 180, 0)
#: 测试专用：假装自己在某个会话
PRETEND_SESSION = _int_flag('--pretend-session', None, None)

#: 有 explorer 进程就说明存在一个已登录的交互桌面
EXPLORER_Q = ("(Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\" | "
              "Measure-Object).Count")

#: DMR 主进程（venv 启动器和 C:\Python311 真身都会匹配 main.py），带出会话号。
#:
#: 2026-11-16：原写法 `$_.CommandLine -like '*main.py*'` 有两个问题：
#:   1. 子串匹配 —— `domain.py` / `mymain.py` 也会命中，机器上有别的项目在跑
#:      时，本脚本会认定"DMR 已在运行"，于是永不启动 DMR；
#:   2. 不判属主 —— 别的项目的 main.py 同样算数。
#: 改成"main.py 作为独立参数出现" + "命令行含本项目名"。
_PROJ_NAME = os.path.basename(UP)
DMR_Q = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -match '(?i)(^|[\\s\"])main\\.py([\\s\"]|$)'"
         f" -and $_.CommandLine -like '*{_PROJ_NAME}*' }} | "
         "ForEach-Object { \"$($_.ProcessId),$($_.SessionId)\" }")


def log(msg: str) -> None:
    line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    try:
        print(line)
    except Exception:
        pass


def _session_id(pid: int) -> int:
    """Windows 会话号；拿不到返回 -1。"""
    try:
        sess = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.ProcessIdToSessionId(
                ctypes.c_ulong(pid), ctypes.byref(sess)):
            return int(sess.value)
    except Exception:
        pass
    return -1


def _procs_psutil(name: str, needle: str = None) -> list:
    """用 psutil 找进程，返回 [(pid, session), ...]。

    2026-10-30：无人值守/沙箱环境里 PATH 上没有 powershell，
    原来 `subprocess.run(['powershell', ...])` 直接 FileNotFoundError，
    脚本在"准备启动"之前就崩了 —— DMR 起不来也停不下。
    psutil 是 requirements.txt 里的既有依赖，不依赖任何外部可执行文件。
    """
    out = []
    try:
        import psutil
    except ImportError:
        return out
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if (p.info['name'] or '').lower() != name.lower():
                continue
            if needle and not any(needle in (a or '')
                                  for a in (p.info['cmdline'] or [])):
                continue
            out.append((p.info['pid'], _session_id(p.info['pid'])))
        except Exception:
            continue
    return out


def ps(cmd: str) -> str:
    """跑一条进程查询。powershell 不在 PATH 时用 psutil 复刻（行为不变）。"""
    if shutil.which('powershell'):
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=90)
            return ((r.stdout or '') + (r.stderr or '')).strip()
        except Exception:
            pass
    if cmd is DMR_Q:
        return '\n'.join('%d,%d' % (pid, sess)
                         for pid, sess in _procs_psutil('python.exe', 'main.py'))
    if cmd is EXPLORER_Q:
        return str(len(_procs_psutil('explorer.exe')))
    return ''


def _proj_norm() -> str:
    """项目根目录的归一化形式（比较用）。"""
    return os.path.normcase(os.path.abspath(UP))


def _is_our_process(proc, proj: str) -> bool:
    """:func:`_dmr_pids_psutil` 的归属判据（任一命中即认）。"""
    try:
        exe = os.path.normcase(os.path.abspath(proc.info.get('exe') or ''))
        if exe and exe.startswith(proj + os.sep):
            return True
    except Exception:                                            # noqa: BLE001
        pass
    try:
        cwd = os.path.normcase(os.path.abspath(proc.cwd() or ''))
        if cwd and cwd == proj:
            return True
    except Exception:                                            # noqa: BLE001
        pass
    try:
        cmd = ' '.join(proc.info.get('cmdline') or [])
        if proj in os.path.normcase(cmd) or os.path.basename(proj) in cmd:
            return True
    except Exception:                                            # noqa: BLE001
        pass
    try:
        parent = proc.parent()
        for _ in range(4):          # 启动器 → 真身，最多往上找 4 层
            if parent is None:
                break
            try:
                pexe = os.path.normcase(os.path.abspath(parent.exe() or ''))
            except Exception:                                    # noqa: BLE001
                pexe = ''
            pcmd = ''
            try:
                pcmd = os.path.normcase(' '.join(parent.cmdline() or []))
            except Exception:                                    # noqa: BLE001
                pass
            if (pexe and pexe.startswith(proj + os.sep)) or proj in pcmd:
                return True
            parent = parent.parent()
    except Exception:                                            # noqa: BLE001
        pass
    return False


def _dmr_pids_psutil() -> list:
    """用 psutil 找本项目自己的 DMR 主进程，返回 [(pid, session_id)]。

    ⚠️ 2026-11-16 第二轮复审 N-8：原来唯一的判据是"命令行含项目名"，但
    ``.venv/Scripts/python.exe`` 只是**启动器** —— 真正的解释器是
    ``C:\\Python311\\python.exe``，它的命令行里**没有项目名**
    （生产实测：2388 = ``C:\\Python311\\python.exe main.py --quiet``，7940 =
    启动器）。启动器一退出（或换一种启动方式），旧判据就得到"DMR 没在跑"
    → 守护反复拉起 → 新实例撞 Global 互斥体 exit 3 → 10 秒后再拉，
    变成启动循环 + 崩溃计数误报。

    判据改为"任一命中"（任一成立就算本项目）：
      · 可执行文件在项目目录下（.venv 启动器）
      · 工作目录 == 项目根（真身的典型特征）
      · 父进程链上有本项目进程（真身是启动器的子进程）
      · 命令行含项目路径/项目名（旧判据，兜底）
    """
    out = []
    proj = _proj_norm()
    try:
        import psutil
    except ImportError:
        return out
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'exe']):
        try:
            if (proc.info.get('name') or '').lower() != 'python.exe':
                continue
            cmd = [a for a in (proc.info.get('cmdline') or []) if a]
            if not any(os.path.basename(a).lower() == 'main.py' for a in cmd):
                continue
            if _is_our_process(proc, proj):
                out.append((proc.info['pid'], _session_id(proc.info['pid'])))
        except Exception:                                        # noqa: BLE001
            continue
    return out


def dmr_processes() -> list:
    """返回 [(pid, session_id), ...]；没有则空列表。

    优先用 psutil（能拿到 exe / cwd / 父进程，见 :func:`_dmr_pids_psutil`），
    psutil 不可用时才退回 ``DMR_Q`` 的命令行匹配（弱一些，但聊胜于无）。
    """
    try:
        import psutil  # noqa: F401
    except ImportError:
        procs = None
    else:
        procs = _dmr_pids_psutil()
    if procs is not None:
        return procs
    out = ps(DMR_Q).replace('\r', ' ').replace('\n', ' ')
    procs = []
    for tok in out.split():
        pid, _, sess = tok.partition(',')
        if pid.isdigit() and sess.isdigit():
            procs.append((int(pid), int(sess)))
    return procs


def has_desktop_session() -> bool:
    """有没有人登录到桌面（有 explorer 进程就算）。"""
    out = ps(EXPLORER_Q)
    return out.isdigit() and int(out) > 0


def my_session() -> int:
    """本进程坐在哪个会话；失败返回 -1。"""
    if PRETEND_SESSION is not None:
        return PRETEND_SESSION
    try:
        sess = ctypes.c_ulong(0)
        if not ctypes.windll.kernel32.ProcessIdToSessionId(
                os.getpid(), ctypes.byref(sess)):
            return -1
        return int(sess.value)
    except Exception:
        return -1


def logon_action(procs, mine: int) -> str:
    """登录模式下该干什么（纯函数，便于测试）。

    procs 非空时返回：
        noop     已在本会话运行，别动
        migrate  在别的会话，停掉它在本会话重启
        skip     在别的会话，但本进程不在交互会话（session 0 / 检测失败），别动
    procs 为空时返回 'start'。
    """
    if not procs:
        return 'start'
    if all(s == mine for _, s in procs):
        return 'noop'
    if mine <= 0:
        return 'skip'
    return 'migrate'


# ---------------------------------------------------------------------------
# 互斥锁：登录瞬间可能有多个自启入口同时触发
# ---------------------------------------------------------------------------
_LOCK_HELD = False


def _start_heartbeat() -> None:
    """持锁期间定时刷新锁文件 mtime。

    没有心跳时，"持锁多久"就等于"锁文件多久没被写"，而 migrate 分支在
    某些机器上真的会持锁十几分钟 —— 另一次运行据此判"陈旧"删锁并发进入，
    两个 stop/start 撞在一起。心跳把"持锁时长"和"文件年龄"解耦。
    """
    global _heartbeat_thread, _heartbeat_stop
    try:
        import threading
        _heartbeat_stop = threading.Event()

        def _beat():
            while not _heartbeat_stop.wait(LOCK_HEARTBEAT):
                try:
                    os.utime(LOCK, None)
                except OSError:
                    return

        _heartbeat_thread = threading.Thread(target=_beat, daemon=True,
                                             name='autostart-lock-heartbeat')
        _heartbeat_thread.start()
    except Exception:                                            # noqa: BLE001
        pass


def _stop_heartbeat() -> None:
    global _heartbeat_thread, _heartbeat_stop
    try:
        if _heartbeat_stop is not None:
            _heartbeat_stop.set()
    except Exception:                                            # noqa: BLE001
        pass
    _heartbeat_thread = None
    _heartbeat_stop = None


def acquire_lock(max_age: float = LOCK_MAX_AGE) -> bool:
    """拿到返回 True；别人正在跑（锁很新）返回 False。

    锁异常陈旧说明上一个运行者崩了，清理后重试一次。文件系统出问题时
    不互相挡（宁可偶尔双开，也有 main.py 的进程互斥兜底）。
    """
    global _LOCK_HELD
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f'{os.getpid()} {time.time():.0f}'.encode())
            finally:
                os.close(fd)
            _LOCK_HELD = True
            _start_heartbeat()
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(LOCK)
            except OSError:
                return True
            if age <= max_age:
                return False
            log(f'发现陈旧的自启锁（{int(age)} 秒前创建），清理后重试。')
            try:
                os.remove(LOCK)
            except OSError:
                pass
        except OSError:
            return True
    return False


def release_lock() -> None:
    global _LOCK_HELD
    if not _LOCK_HELD:
        return
    _stop_heartbeat()
    try:
        os.remove(LOCK)
    except OSError:
        pass
    _LOCK_HELD = False


# ---------------------------------------------------------------------------
# 启动 / 停止
# ---------------------------------------------------------------------------
#: 给子进程单独开一个控制台窗口（兜底路径用）
CREATE_NEW_CONSOLE = 0x00000010


def _launch_via_explorer() -> None:
    """经 explorer 启动：控制台窗口出现在用户桌面上。

    这正是 Start_DMR.bat 手动双击时用的方式，也就是用户熟悉的那个窗口。
    2026-10-20 实测：任务计划程序直接拉起的 cmd、以及 pythonw 里 Popen
    出来的子进程，控制台窗口都是**隐藏的**（tasklist /v 显示"暂缺"），
    所以不能靠它们拿窗口 —— explorer 是唯一稳定给出可见窗口的方式。
    """
    subprocess.Popen(['explorer.exe', BAT], cwd=UP, close_fds=True)


def _launch_via_cmd() -> None:
    """兜底：直接起 cmd。控制台可能隐藏，但程序一定在跑。"""
    subprocess.Popen(['cmd.exe', '/c', BAT], cwd=UP, close_fds=True,
                     creationflags=CREATE_NEW_CONSOLE)


def launch(attempt: int = 1) -> None:
    """带**可见窗口**启动守护循环。

    前几次用 explorer（窗口可见，就是手动双击时的样子）；
    最后一次退回 cmd —— explorer 在登录瞬间可能还没就绪，
    2026-10-19 14:44 三次登录全被静默丢弃就是这么来的，
    而重试之间隔了 25 秒，足够它起来。
    """
    if attempt >= RETRIES:
        _launch_via_cmd()
    else:
        _launch_via_explorer()


def start_with_retries() -> bool:
    for attempt in range(1, RETRIES + 1):
        if DRY_RUN:
            log(f'[DRY-RUN] 第 {attempt} 次：准备启动 {BAT}')
            return True
        try:
            log(f'没有检测到 DMR，准备启动（第 {attempt}/{RETRIES} 次）…')
            launch(attempt)
        except Exception as e:
            log(f'启动失败（{type(e).__name__}: {e}）')
            time.sleep(5)
            continue
        # 等它把 python 拉起来再复查；进程刚起时查不到是正常的
        for _ in range(WAIT_AFTER_LAUNCH):
            time.sleep(1)
            try:
                if dmr_processes():
                    log('启动成功，DMR 已在运行。')
                    return True
            except Exception:
                continue
        log(f'第 {attempt} 次启动后 {WAIT_AFTER_LAUNCH} 秒仍未见进程。')
    log('三次尝试都没能把 DMR 拉起来 —— 请手动双击 Start_DMR.bat '
        '并检查 logs/DMR-*.log。')
    return False


def stop_all() -> bool:
    """整机停止 DMR（守护 + 主进程 + 录制子进程），确认干净才返回 True。"""
    env = dict(os.environ, PYTHONIOENCODING='utf-8')  # 否则中文按 GBK 输出，日志乱码
    for attempt in range(1, 4):
        try:
            r = subprocess.run([PY, STOP], cwd=UP, capture_output=True,
                               text=True, encoding='utf-8', errors='replace',
                               timeout=300, env=env)
            for stream, tag in ((r.stdout, 'out'), (r.stderr, 'err')):
                for line in (stream or '').splitlines():
                    line = line.strip()
                    if line:
                        log(f'    stop_dmr[{tag}] | ' + line[:200])
            if r.returncode != 0:
                log(f'stop_dmr.py 退出码 {r.returncode}')
        except Exception as e:
            log(f'stop_dmr.py 执行失败（{type(e).__name__}: {e}）')
        if not dmr_processes():
            return True
        log(f'stop_dmr 第 {attempt} 次后仍有 DMR 进程，3 秒后重停 …')
        time.sleep(3)
    return not dmr_processes()


# ---------------------------------------------------------------------------
# 模式一：登录（默认）
# ---------------------------------------------------------------------------
def mode_logon() -> int:
    """保证 DMR 在**当前会话**里跑，而且带窗口。"""
    if not DRY_RUN and not acquire_lock():
        log('另一个登录自启/迁移正在运行，本次退出（避免双开）。')
        return 0
    try:
        mine = my_session()
        procs = dmr_processes()
        action = logon_action(procs, mine)

        if action == 'noop':
            log(f'会话 {mine}：DMR 已在本会话运行（{len(procs)} 个进程），'
                f'无需操作。')
            return 0
        if action == 'skip':
            log(f'本进程在会话 {mine}（非交互会话或检测失败），不迁移；'
                f'DMR 正在会话 {sorted({s for _, s in procs})} 运行，'
                f'录像不受影响。')
            return 0
        if action == 'migrate':
            log(f'DMR 跑在会话 {sorted({s for _, s in procs})}，当前会话是 '
                f'{mine} —— 准备迁移到本会话，让控制台窗口出现在你眼前'
                f'（RDP / UU远程 / RustDesk 换着用也不怕）。')
            if DRY_RUN:
                log('[DRY-RUN] 将先停掉旧实例，再在本会话启动。')
                return 0
            if not stop_all():
                log('旧实例没有停干净，放弃迁移（避免双开踩状态库）。'
                    '若旧实例是开机任务以管理员身份拉起的，而本进程未提权，'
                    '就会这样 —— 提权的 DMR_SessionWatch 任务会'
                    '接着完成迁移。')
                return 1
            time.sleep(2)

        return 0 if start_with_retries() else 1
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# 模式二：守护（开机任务）
# ---------------------------------------------------------------------------
def run_foreground() -> None:
    """前台跑守护循环（阻塞）：任务状态 = DMR 状态，session 0 没有控制台，
    日志是唯一的观察窗口。"""
    if DRY_RUN:
        log('[DRY-RUN] 将前台运行 Start_Render.bat（阻塞）')
        return
    log('前台运行 Start_Render.bat（本任务在此阻塞，任务状态 = DMR 状态）')
    try:
        rc = subprocess.call(['cmd.exe', '/c', BAT], cwd=UP)
        log(f'Start_Render.bat 已退出，returncode={rc}；继续守护。')
    except Exception as e:
        log(f'前台运行失败（{type(e).__name__}: {e}），30 秒后重试。')
        time.sleep(30)


def mode_supervise() -> int:
    """常驻守护：保证任何情况下都有一个 DMR 实例在录。"""
    started = time.time()
    dead_since = None
    last_backstop = 0.0
    log(f'守护模式启动：初始宽限 {INITIAL_GRACE}s；有人登录时交给登录自启；'
        f'无人登录且 DMR 未运行超过宽限期 → session 0 兜底。')

    while True:
        try:
            procs = dmr_processes()
            desktop = has_desktop_session()
        except Exception as e:
            log(f'进程/会话检测失败（{type(e).__name__}: {e}），'
                f'{POLL}s 后重试。')
            time.sleep(POLL)
            continue

        now = time.time()
        if procs:
            dead_since = None
            decision = 'running'
        elif desktop:
            if dead_since is None:
                dead_since = now
                log('检测到已登录的桌面会话，但 DMR 未运行 —— 等登录自启启动'
                    '（Startup / 登录+解锁触发的任务，自带 3 次重试）。')
                decision = 'wait-logon'
            elif (now - dead_since > LOGGED_IN_DEAD_GRACE
                  and now - last_backstop > LOGGED_IN_DEAD_GRACE):
                decision = 'backstop'
            else:
                decision = 'wait-logon'
        else:
            dead_since = None
            decision = 'start-s0' if now - started >= INITIAL_GRACE else 'grace'

        if DRY_RUN:
            log(f'[DRY-RUN] 当前判断：{decision}'
                f'（DMR 进程 {len(procs)} 个，桌面会话 {desktop}）')
            return 0

        if decision == 'running':
            time.sleep(POLL)
        elif decision == 'grace':
            time.sleep(15)
        elif decision == 'wait-logon':
            time.sleep(POLL)
        elif decision == 'backstop':
            log(f'已等待 {int(now - dead_since)}s 登录自启仍未拉起 DMR → '
                f'session 0 兜底启动（无窗口；要窗口请运行 '
                f'DMR_Desktop 任务把它搬过来）。')
            last_backstop = now
            dead_since = None
            run_foreground()
        else:  # start-s0
            log('无人登录且 DMR 未运行 → session 0 前台启动（守护循环接棒）。')
            run_foreground()


def main() -> int:
    try:
        if BOOT_FALLBACK > 0:
            return mode_supervise()
        return mode_logon()
    except Exception:
        log('未处理异常：\n' + traceback.format_exc())
        return 1


if __name__ == '__main__':
    sys.exit(main())
