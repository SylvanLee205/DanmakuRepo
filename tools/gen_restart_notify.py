"""重启通知：生成内容 + 发送（由 DMR 在启动成功后调用）。

⚠️ 换行格式见 DMR/notify.py 的说明：Server酱³ 按 Markdown 解析，
   必须用"两空格 + \n"（硬换行），纯 \n 和 <br> 都不行（实测）。

也可以单独运行做手动测试：
    python tools\\gen_restart_notify.py
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from DMR.notify import send, disk_free_gb          # noqa: E402

try:                                                             # GBK 控制台下 emoji 打印不崩
    from _guard import ensure_utf8_stdout as _ensure_utf8
    _ensure_utf8()
except Exception:                                                # noqa: BLE001
    pass

STATE_FILE = os.path.join(ROOT, '.temp', 'restart_history.json')
VERSION_FILE = os.path.join(ROOT, 'DMR', '__init__.py')

FLAP_WINDOW = 3600      # 统计窗口（秒）
FLAP_THRESHOLD = 3      # 窗口内超过这个次数算频繁重启
MIN_COUNT_GAP = 600     # 人工重启：距上次计数不足这个秒数就不重复计数（防调试误报）
CRASH_GAP = 120         # 崩溃重启：间隔小于这个秒数视为崩溃循环，**照常计数**
MAX_TIMES = 200         # 状态文件最多保留多少条，防止崩溃循环把 json 写爆
DISK_WARN_GB = 30       # 剩余磁盘低于这个值就在通知里告警

RCLONE_DEFAULT_DEST = 'cd2:百度网盘/DMR录播'


def version():
    """读版本号：优先 DMR 包里的 VERSION，退回 DMR/__init__.py 里的字面量。"""
    try:
        from DMR import VERSION as v
        return str(v)
    except Exception:
        pass
    try:
        t = open(VERSION_FILE, encoding='utf-8', errors='replace').read()
        m = re.search(r"VERSION\s*=\s*['\"]([^'\"]+)['\"]", t)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ''


def _rclone_dest():
    """从 configs/global.yml 的 upload_args.rclone.command 里抠出目标路径。

    生产上传实际是 `rclone copy {PATH} cd2:百度网盘/DMR录播/{TASKNAME}`，
    这里取 `cd2:百度网盘/DMR录播` 作为探活目标。读不到配置就退回默认值
    （default.yml 里写的就是这个）。
    """
    try:
        p = os.path.join(ROOT, 'configs', 'global.yml')
        t = open(p, encoding='utf-8', errors='replace').read()
        m = re.search(r"""command:\s*\[[^\]]*?['"]rclone['"][^\]]*?['"]([A-Za-z0-9_\-]+:[^'"]*)['"]""", t)
        if m:
            d = re.sub(r'/\{[A-Za-z_]+\}.*$', '', m.group(1)).rstrip('/')
            if ':' in d:
                return d
    except Exception:
        pass
    return RCLONE_DEFAULT_DEST


def cloud_ok():
    """云端通道是否可达：rclone 能列出目标目录就算正常。

    ⚠️ 2026-11-16 修（审查报告 R-13）：
        CloudDrive2 服务版已于 2026-11-02 下架，上传改为 rclone 直连 WebDAV。
        原来查 `Get-Service CloudDrive2` + 19798 端口，本机根本没这个服务，
        于是**每次**启动成功通知都带一条「⚠️ 云端通道：CD2 服务不存在」的假告警。
        现在直接探真实链路：`rclone lsd <dest>`，15 秒超时，不会拖慢启动。
    """
    dest = _rclone_dest()
    try:
        r = subprocess.run(
            ['rclone', 'lsd', dest, '--max-depth', '1',
             '--timeout', '15s', '--contimeout', '10s'],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=30)
    except FileNotFoundError:
        return False, 'rclone 未安装或不在 PATH'
    except subprocess.TimeoutExpired:
        return False, '连接超时（30s）'
    except Exception as e:
        return False, f'查询失败({type(e).__name__})'
    if r.returncode == 0:
        return True, '正常'
    lines = [x.strip() for x in ((r.stderr or '') + (r.stdout or '')).splitlines() if x.strip()]
    tail = lines[-1][:80] if lines else f'rc={r.returncode}'
    return False, f'不可达（{tail}）'


def bump_and_get_restarts():
    """记录本次启动，返回 (总次数, 窗口内次数)。

    计数规则（2026-11-16 修，审查报告 R-13）：
      - 间隔 >= MIN_COUNT_GAP（10 分钟）：正常计数。手动调试重启几次不会误报。
      - 间隔 <  CRASH_GAP（2 分钟）  ：**照常计数**。这是崩溃循环的特征
        （进程自己 10 秒后重试），必须立刻累计才能及时告警。
      - 中间区间（2~10 分钟）：不计数，按人工重启处理。

    旧逻辑是"不足 10 分钟一律不计数"，与它自己的注释
    「真正的崩溃循环（10 秒重试）间隔远小于 10 分钟，仍能被识别」**矛盾**：
    10 秒崩溃循环每小时最多只计 6 次，要 20~30 分钟才凑够 3 次阈值，
    告警严重滞后。改成按间隔分流后，崩溃循环 30 秒内就能触发告警。
    """
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    now = time.time()
    try:
        d = json.load(open(STATE_FILE, encoding='utf-8')) if os.path.exists(STATE_FILE) else {}
    except Exception:
        d = {}
    times = [t for t in (d.get('times') or []) if isinstance(t, (int, float))]
    times = [t for t in times if now - t < 7 * 86400]
    last = times[-1] if times else 0
    gap = now - last
    if gap >= MIN_COUNT_GAP or (last and gap < CRASH_GAP):
        times.append(now)
        times = times[-MAX_TIMES:]
        try:
            json.dump({'times': times, 'count': len(times), 'last': now},
                      open(STATE_FILE, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass
    return len(times), len([t for t in times if now - t < FLAP_WINDOW])


def build(task_names=None, task_count=None):
    """生成 (标题, [正文行...])。

    设计取舍（2026-10-07 按用户反馈调整）：
      ✗ 启动时间     -> 删。通知本身自带时间，重复没意义
      ✗ 近 24h 录制  -> 删。启动时看它没有行动价值
      ✗ 正在录制     -> 删。启动瞬间必然还没开始录，只会显示"暂无"
      ✓ 主播加载     -> 从"32 个"改成**列出名字**，否则不知道是谁
      ✓ 云端通道     -> 留。启动时最该确认的就是"上传通道通不通"
                        （2026-11-16 起查 rclone remote 可达性，不再查已下架的 CD2 服务）
      ✓ 磁盘剩余     -> 新增。快满了会导致录制失败，是真实风险
    """
    _, recent = bump_and_get_restarts()
    ok, cloud_txt = cloud_ok()
    # 磁盘剩余：不传路径 = 自动取数据所在盘（原来是写死的 F:\）
    free_gb = disk_free_gb()
    ver = version()

    head = 'DanmakuRepo 已成功复活' + (f'（v{ver}）' if ver else '')
    L = [head, '']

    names = list(task_names or [])
    n = task_count if task_count is not None else len(names)
    if names:
        L.append(f'主播加载：{n} 个，全部成功')
        L.append('、'.join(names))
    else:
        L.append(f'主播加载：{n} 个')

    L.append('')
    L.append('云端通道：正常' if ok else f'⚠️ 云端通道：{cloud_txt}')

    if free_gb is not None:
        if free_gb < DISK_WARN_GB:
            L.append(f'⚠️ 磁盘剩余：{free_gb:.1f} GB（偏低，请清理）')
        else:
            L.append(f'磁盘剩余：{free_gb:.0f} GB')

    if recent >= FLAP_THRESHOLD:
        L.append('')
        L.append(f'⚠️ 频繁重启：最近 1 小时 {recent} 次，请检查日志')

    title = (f'⚠️ DanmakuRepo 频繁重启（{recent}次/时）'
             if recent >= FLAP_THRESHOLD else 'DanmakuRepo 已启动')
    return title, L


def notify(task_names=None, task_count=None, quiet=True):
    """生成并发送启动通知。返回 (ok, title, lines)。"""
    try:
        title, lines = build(task_names=task_names, task_count=task_count)
        ok = send(title, lines)
        if not quiet:
            print(f'[notify] {"已发送" if ok else "发送失败"}  标题: {title}')
            for l in lines:
                print(f'    {l}')
        return ok, title, lines
    except Exception as e:
        if not quiet:
            print(f'[notify] 异常: {type(e).__name__}: {e}')
        return False, '', []


if __name__ == '__main__':
    # 手动测试：不带参数就用一个假的示例名单，方便看排版
    ok, title, lines = notify(
        task_names=['苏苏没烦恼', 'vvu', '掉了颗兔牙', '相扑猫', '小鱼大王',
                    '年狗大号', '荞麦拌面', '小玟不是小玫', '哭哭不嘻嘻'],
        task_count=32, quiet=False)
    print(f'\n  结果: {"✅ 发送成功" if ok else "❌ 发送失败"}')
