"""项目路径常量 —— 全项目**唯一**的路径来源。

为什么需要这个模块
------------------
这个项目必须能放在**任意目录**跑：`F:\\DanmakuRepo`、`D:\\直播工具\\DMR`、
甚至 `C:\\Users\\someone\\DanmakuRepo`。所以：

1. **不许写死盘符**。任何 `F:\\DanmakuRepo\\xxx` 都是 bug。
2. **不许依赖"当前工作目录"**。计划任务（session 0）启动时 cwd 常常是
   `C:\\Windows\\System32`，用 `./tools/ffmpeg.exe` 会直接找不到。
3. **不许依赖 Python 的安装位置**。venv / 系统 Python / 便携版都可能。

所有路径一律从 ``ROOT`` 派生，而 ``ROOT`` 是这个文件所在包的上一级 ——
不管谁在什么目录下 import，结果都指向项目根。

外部目录（配置快照、备份盘）可以用环境变量覆盖，见各函数。
"""
import os
import sys

__all__ = (
    'ROOT', 'TOOLS', 'CONFIGS', 'LOGS', 'TEMP', 'BACKUPS', 'DOCS',
    'RECORDINGS', 'SYSTEM32', 'is_frozen',
    'SOURCE_SUBDIR', 'DANMAKU_SUBDIR', 'DM_MARK',
    'p', 'ensure', 'enter_root', 'system32', 'powershell_exe',
    'recordings_root', 'source_root', 'danmaku_root',
    'streamer_of', 'danmaku_dir_for', 'is_danmaku_path',
    'snapshot_dir', 'backup_dir', 'release_dir', 'data_root',
)


#: Windows 系统目录（System32）。非 Windows 上没有意义。
SYSTEM32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')


def system32(name: str) -> str:
    """返回 System32 下某个系统工具的**绝对路径**；找不到就退回裸名字。

    ⚠️ 为什么不能直接写 'schtasks' / 'curl.exe'：
        计划任务（session 0）和某些精简过的环境 PATH 里**没有** System32，
        2026-10-19 就因此踩过 FileNotFoundError，表现为"脚本一跑就崩，
        但用户自己开命令行跑又正常"。用绝对路径才稳。

    ⚠️ 非 Windows 上直接返回裸名字 —— POSIX 的系统工具就在 PATH 里
        （/usr/bin/curl），拼 System32 没有意义。这样调用方不用到处判断平台。
    """
    if sys.platform != 'win32':
        return name
    path = os.path.join(SYSTEM32, name)
    return path if os.path.exists(path) else name


def powershell_exe() -> str:
    """powershell.exe 的绝对路径。

    ⚠️ 实测：在项目自己的 venv 里 `subprocess.run(['powershell', ...])`
        会 FileNotFoundError —— 进程 PATH 不含 PowerShell 目录。
        所以统一走这里。

    ⚠️ 非 Windows 返回 'pwsh'（PowerShell Core）。注意：**本项目的
        Windows 专属功能（计划任务、快捷方式、注册表）在 POSIX 上本来就用不了**，
        这个函数只是保证"误调用时给出一个合理的名字"，不是真的支持。
    """
    if sys.platform != 'win32':
        return 'pwsh'
    cand = os.path.join(SYSTEM32, 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    if os.path.exists(cand):
        return cand
    return 'powershell'


def is_frozen() -> bool:
    """是否被打包成单文件 exe（PyInstaller 等）。

    打包后 ``__file__`` 指向临时解包目录，项目根要取 exe 所在目录。
    目前没有打包，但预留给将来。
    """
    return bool(getattr(sys, 'frozen', False))


#: 项目根目录。本文件位于 ``<ROOT>/DMR/paths.py``，上跳两层即为根。
if is_frozen():
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 外部可执行文件（ffmpeg / ffprobe / rclone / biliup）
TOOLS = os.path.join(ROOT, 'tools')
#: 每个主播一个 yml
CONFIGS = os.path.join(ROOT, 'configs')
#: 运行日志
LOGS = os.path.join(ROOT, 'logs')
#: 中间状态（状态库、字形缓存、失败渲染队列…）
TEMP = os.path.join(ROOT, '.temp')
#: 配置快照 / 启动链备份
BACKUPS = os.path.join(ROOT, 'backups')
#: 文档
DOCS = os.path.join(ROOT, 'docs')
#: 默认录制输出根目录（每个任务的实际目录由配置里的 output_dir 决定）
RECORDINGS = os.path.join(ROOT, '直播回放')

#: 录制根下的两个分类子目录（2026-11-02 新布局）
#:     直播回放/原视频/<主播>/   源视频 + 弹幕 .ass
#:     直播回放/弹幕版/<主播>/   渲染后的成品
#: 为什么要分两级：原来源目录和 `<主播>（弹幕版）` 平级混在 直播回放/ 里，
#: 主播一多就分不清哪个是源哪个是成品。现在根目录下永远只有两个分类目录。
SOURCE_SUBDIR = '原视频'
DANMAKU_SUBDIR = '弹幕版'

#: 成品**文件名**后缀。⚠️ 这个不能改：Cleaner 里"弹幕版永不删除"的硬保护
#: 判据就是路径里含这个标记（见 DMR/Cleaner/__init__.py 的
#: ``is_dm_video_path``）。目录怎么搬都行，文件名必须留着它。
DM_MARK = '（弹幕版）'


def recordings_root(base=None) -> str:
    """录制根目录 ``<data_root>/直播回放``。"""
    base = base or data_root()
    return os.path.join(base, '直播回放')


def source_root(base=None) -> str:
    """源视频根 ``<data_root>/直播回放/原视频``。

    老机器上这个子目录可能还不存在（未迁移）或已归档，此时退回录制根 ——
    调用方按"找不到就当没数据"处理，不会误判成别的目录。
    """
    p = os.path.join(recordings_root(base), SOURCE_SUBDIR)
    return p if os.path.isdir(p) else recordings_root(base)


def danmaku_root(base=None, create=False) -> str:
    """成品根 ``<data_root>/直播回放/弹幕版``（不存在时按需创建）。"""
    p = os.path.join(recordings_root(base), DANMAKU_SUBDIR)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def streamer_of(path_or_dir: str) -> str:
    """从源/成品路径里取出主播目录名（去掉 ``（弹幕版）`` 后缀）。

    两种布局都能正确解析：
        直播回放/原视频/小鱼大王/xxx.ts              -> 小鱼大王
        直播回放/小鱼大王（弹幕版）/xxx（弹幕版）.mp4 -> 小鱼大王
    """
    s = str(path_or_dir or '').strip()
    if not s:
        return ''
    name = os.path.basename(os.path.normpath(s))
    return name.replace(DM_MARK, '').strip()


def danmaku_dir_for(src_path_or_dir: str, base=None) -> str:
    """源视频（或其所在目录）→ 成品应放的目录。

    新布局：``直播回放/原视频/<主播>/`` → ``直播回放/弹幕版/<主播>/``
    老布局：``直播回放/<主播>/``        → ``直播回放/弹幕版/<主播>/``

    两种都归到同一个地方，所以**某个任务配置没跟着改也不会放错位置**。
    """
    d = str(src_path_or_dir or '')
    # 传进来的是文件路径时先取其所在目录
    if os.path.splitext(d)[1]:
        d = os.path.dirname(d)
    d = os.path.normpath(d)
    parent = os.path.dirname(d)
    if os.path.basename(parent) == SOURCE_SUBDIR:
        base = os.path.dirname(parent)      # 录制备份/自定义根时也能对上
    else:
        base = base or recordings_root()
    return os.path.join(base, DANMAKU_SUBDIR, streamer_of(d))


def is_danmaku_path(path: str) -> bool:
    """路径是不是弹幕版产物（判据：路径里含 ``（弹幕版）``）。"""
    try:
        return DM_MARK in os.path.normpath(str(path or ''))
    except Exception:                                          # noqa: BLE001
        return False


def p(*parts: str) -> str:
    """拼出项目内的绝对路径。``p('tools', 'ffmpeg.exe')``"""
    return os.path.join(ROOT, *parts)


def ensure(*parts: str) -> str:
    """拼路径并确保目录存在，返回该路径。"""
    path = os.path.join(ROOT, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def enter_root() -> str:
    """把工作目录切到项目根，并让 ``import tools.xxx`` 可用。幂等。

    ⚠️ 为什么还要 chdir：
        配置里的 ``output_dir: ./直播回放`` 是相对路径，其他模块里也有大量
        ``open('.temp/xxx')`` 这样的写法。chdir 到根目录后它们才对。
        新代码请尽量用 ``p()``，但不必为了这个去改历史代码。

    ⚠️ 必须在**任何**入口（main.py / dryrun.py / render_only.py /
        tools/*.py）最早处调用，否则从计划任务启动时会写错位置。
    """
    os.chdir(ROOT)
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    return ROOT


def data_root() -> str:
    """录制数据的总根目录。

    可用环境变量 ``DMR_DATA_ROOT`` 指到别的盘（录制盘独立于项目盘时用）。
    默认就是项目根。
    """
    return os.environ.get('DMR_DATA_ROOT') or ROOT


def snapshot_dir(auto_migrate_legacy=True) -> str:
    """配置快照目录。

    默认 ``<ROOT>/backups/配置快照``（跟着项目走，换机器一起搬）。
    可用 ``DMR_SNAPSHOT_DIR`` 覆盖到别的盘。

    ``auto_migrate_legacy``：旧版本把快照写在 ``F:\\_DMR配置快照``（项目外）。
    首次在新位置找不到历史时，如果旧位置存在就**复制**一份过来 —— 只复制
    不删除，用户的旧数据永远还在原地。
    """
    path = os.environ.get('DMR_SNAPSHOT_DIR') or os.path.join(BACKUPS, '配置快照')
    os.makedirs(path, exist_ok=True)

    if auto_migrate_legacy:
        legacy = 'F:\\_DMR配置快照'
        try:
            empty = not any(os.scandir(path))
        except OSError:
            empty = False
        if empty and os.path.isdir(legacy) and os.path.abspath(legacy) != os.path.abspath(path):
            import shutil
            for name in os.listdir(legacy):
                src = os.path.join(legacy, name)
                dst = os.path.join(path, name)
                if os.path.exists(dst):
                    continue
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                except OSError:
                    pass
    return path


def backup_dir() -> str:
    """大件备份目录（整包发布、云端打包等）。默认 ``<ROOT>/backups``。"""
    path = os.environ.get('DMR_BACKUP_DIR') or BACKUPS
    os.makedirs(path, exist_ok=True)
    return path


def release_dir() -> str:
    """发布产物目录。默认 ``<ROOT>/backups/release``。"""
    path = os.environ.get('DMR_RELEASE_DIR') or os.path.join(BACKUPS, 'release')
    os.makedirs(path, exist_ok=True)
    return path
