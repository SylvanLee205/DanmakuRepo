"""清理"已确认上传成功"的本地文件（源视频 + 源弹幕）。

⚠️⚠️ 2026-10-18 修正：本工具**只删源视频 + 源弹幕，永不删弹幕版**。

    用户明确需求：弹幕版必须永久保留在本地（同时上传云端做备份）。
    原版本会连带删除 dm_video —— 那是个错误，已修正。

安全前提（缺一不删）：
  1. 该分段的 dm_video 状态必须是 uploaded
  2. dm_video 文件名必须在云端**确实存在**（实时 rclone 核对）
  3. 待删文件必须位于 直播回放/ 目录下
  4. 源视频/源弹幕最后修改时间必须早于 --min-age 小时
     （**默认 24 小时**，与 DMR 的 clean_args 后悔期一致）
  5. 跳过 rule 命中（src_video 状态 skipped）的文件**不动** —— 那是"不上传、留本地"的语义
  6. 文件在 logs/pending_clean.json 的 24h 延迟队列里 → 不动（DMR 会到期自己删）
  7. 每个文件先记日志再删；删除失败只告警不中断

默认 dry-run，加 --apply 才真删。删完把状态库对应产物标成 cleaned（云端有备份）。

⚠️⚠️ 2026-11-16 修正（审查报告 D-5）：原实现 ``MIN_AGE_H = 1.0`` 且完全不读
``pending_clean.json``，等于绕过 24 小时后悔期 —— 刚上传 1 小时、用户还来得及
发现问题的文件会被直接删源。现在默认 24h + 队列保护，``--min-age`` 显式调小时
会打印警告。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter

try:
    from _guard import (PROJECT_ROOT, dmr_running, ensure_utf8_stdout,
                        require_not_running)
except ImportError:                                              # 从项目根 import tools.xxx
    from tools._guard import (PROJECT_ROOT, dmr_running, ensure_utf8_stdout,
                              require_not_running)

PROD = PROJECT_ROOT
REPLAY = os.path.join(PROD, '直播回放')
DB = os.path.join(PROD, 'logs', 'dmr_state.db')
PENDING = os.path.join(PROD, 'logs', 'pending_clean.json')

#: 与 DMR clean_args 的 delay（86400 秒）对齐：没过后悔期不许删
DEFAULT_MIN_AGE_H = 24.0


def _tool(name):
    """定位外部工具：环境变量 -> tools/ -> PATH -> 注册表 PATH -> 安装目录。

    见 DMR/utils/toolsmgr.py。不写死版本号和安装位置 ——
    换个 rclone 版本、换台电脑都不用改代码。
    """
    root = PROJECT_ROOT
    if root not in sys.path:
        sys.path.insert(0, root)
    from DMR.utils.toolsmgr import find_tool
    return find_tool(name) or name


def _under_replay(path: str) -> bool:
    """路径是否真的在 直播回放/ 下（按目录边界，不用裸字符串前缀）。

    裸 ``startswith(REPLAY)`` 会把 ``直播回放备份/`` 也放过去。
    """
    try:
        root = os.path.normcase(os.path.abspath(REPLAY))
        target = os.path.normcase(os.path.abspath(path))
        return target == root or target.startswith(root + os.sep)
    except Exception:                                            # noqa: BLE001
        return False


def _pending_names():
    """24h 延迟队列里的文件名集合；队列读不出来返回 None（按全部保护处理）。"""
    if not os.path.exists(PENDING):
        print('  [警告] 未找到 pending_clean.json，按"全部保护"处理，不删任何文件。')
        return None
    try:
        with open(PENDING, encoding='utf-8') as f:
            data = json.load(f)
        names = set()
        for entry in data or []:
            for pair in entry.get('pairs') or []:
                for item in pair:
                    if isinstance(item, str) and item:
                        names.add(os.path.basename(item.replace('\\', '/')))
        return names
    except Exception as e:                                       # noqa: BLE001
        print(f'  [警告] 读取延迟清理队列失败（{e}），按"全部保护"处理。')
        return None


def _abs(path: str) -> str:
    return os.path.normpath(os.path.join(PROD, str(path).replace('/', os.sep)))


def main() -> int:
    apply = '--apply' in sys.argv
    force = '--force' in sys.argv
    min_age_h = DEFAULT_MIN_AGE_H
    for i, a in enumerate(sys.argv):
        if a == '--min-age' and i + 1 < len(sys.argv):
            try:
                min_age_h = float(sys.argv[i + 1])
            except ValueError:
                print(f'--min-age 需要数字，收到 {sys.argv[i + 1]!r}')
                return 2

    print('=' * 78)
    print(f'清理已确认上传成功的本地文件 {"（实际删除）" if apply else "（DRY-RUN）"}')
    print('=' * 78)
    print(f'  最小文件年龄: {min_age_h} 小时（更新的文件不删）')
    if min_age_h < DEFAULT_MIN_AGE_H:
        print(f'  ⚠️ 已低于默认的 {DEFAULT_MIN_AGE_H} 小时后悔期 —— '
              f'提前删除意味着发现问题时源视频已不可恢复。')

    if apply and not require_not_running('删除源文件并改状态库', force=force):
        return 3

    pending = _pending_names()
    if pending is None:
        print('  中止：读不到延迟清理队列时无法判断哪些文件还在后悔期内。')
        return 3

    # ── 1. 云端核对 ──
    print('  读取云端文件列表...')
    rclone = _tool('rclone')
    r = subprocess.run([rclone, 'lsf', 'cd2:百度网盘/DMR录播',
                        '--max-depth', '2', '--files-only'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=300)
    if r.returncode != 0:
        print(f'  ❌ rclone 失败: {(r.stderr or "")[:200]}')
        return 1
    cloud = {os.path.basename(x.strip())
             for x in (r.stdout or '').split('\n') if x.strip()}
    print(f'  云端 {len(cloud)} 个文件')

    # ── 2. 读状态库并按 (session, idx) 配对 ──
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('SELECT * FROM artifacts')
    groups = {}
    for row in cur.fetchall():
        a = dict(row)
        groups.setdefault((a['session_id'], a['segment_idx']), {})[a['kind']] = a

    now = time.time()
    to_delete = []      # [(path, kind, size, artifact)]
    skipped_reasons = {}

    def skip(reason, name):
        skipped_reasons.setdefault(reason, []).append(name)

    for key, g in sorted(groups.items()):
        dm = g.get('dm_video')
        if not dm or dm['state'] != 'uploaded':
            continue
        dd = json.loads(dm['data'] or '{}')
        dp = dd.get('path')
        if not dp:
            continue
        dabs = _abs(dp)
        dbase = os.path.basename(dabs)
        if dbase not in cloud:
            skip('云端核对不通过', dbase)
            continue
        if not os.path.exists(dabs):
            continue
        if dbase in pending:
            skip('还在 24h 后悔期内（pending_clean 队列）', dbase)
            continue
        if now - os.path.getmtime(dabs) < min_age_h * 3600:
            skip('成品文件太新', dbase)
            continue
        # ⚠️ 绝对不删弹幕版！下面这行原实现已禁用（弹幕版必须永久保留本地）
        # to_delete.append((dabs, 'dm_video', os.path.getsize(dabs), dm))

        # 源视频：只有不是 skipped 才删（skipped = 命中跳过规则、要留本地）
        sv = g.get('src_video')
        if sv and sv['state'] != 'skipped':
            sd = json.loads(sv['data'] or '{}')
            sp = sd.get('path')
            if sp:
                sabs = _abs(sp)
                if os.path.basename(sabs) in pending:
                    skip('源视频还在 24h 后悔期内', os.path.basename(sabs))
                elif os.path.exists(sabs) and \
                        now - os.path.getmtime(sabs) >= min_age_h * 3600:
                    to_delete.append((sabs, 'src_video', os.path.getsize(sabs), sv))
                # 源弹幕 .ass —— 同样要过年龄检查（可能还在被追加写入）
                dmp = sd.get('danmaku_path')
                if dmp:
                    aabs = _abs(dmp)
                    if os.path.basename(aabs) in pending:
                        skip('源弹幕还在 24h 后悔期内', os.path.basename(aabs))
                    elif os.path.exists(aabs) and \
                            now - os.path.getmtime(aabs) >= min_age_h * 3600:
                        to_delete.append(
                            (aabs, 'src_danmaku', os.path.getsize(aabs), sv))
    conn.close()

    # ── 3. 安全过滤 ──
    safe = []
    for p, kind, sz, art in to_delete:
        rp = os.path.abspath(p)
        if not _under_replay(rp):
            print(f'  ⚠️ 跳过（不在直播回放目录）: {rp}')
            continue
        safe.append((rp, kind, sz, art))

    # 去重（同一路径可能被多个产物引用）
    seen = set()
    uniq = []
    for p, kind, sz, art in safe:
        k = os.path.normcase(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((p, kind, sz, art))

    # ── 4. 报告 ──
    LAB = {'dm_video': '弹幕版', 'src_video': '源视频', 'src_danmaku': '源弹幕'}
    cnt = Counter(k for _, k, _, _ in uniq)
    siz = Counter()
    for _, k, sz, _ in uniq:
        siz[k] += sz
    print()
    print('  待删除统计:')
    for k in ('src_video', 'src_danmaku'):
        if cnt[k]:
            print(f'    {LAB[k]:8} {cnt[k]:4} 个  {siz[k]/1024**3:7.2f} GB')
    print(f'    {"合计":8} {len(uniq):4} 个  {sum(siz.values())/1024**3:7.2f} GB')

    if skipped_reasons:
        print()
        print('  被拦下的:')
        for reason, lst in skipped_reasons.items():
            print(f'    {reason}: {len(lst)} 个')
            for x in lst[:3]:
                print(f'        {x[:64]}')

    if not apply:
        print()
        print('  ── 前 25 个待删文件 ──')
        for p, kind, sz, _ in sorted(uniq, key=lambda x: -x[2])[:25]:
            rel = os.path.relpath(p, PROD)
            print(f'    {LAB[kind]:8} {sz/1024**2:9.1f} MB  {rel[:74]}')
        print()
        print('  DRY-RUN 结束。确认无误后加 --apply 执行。')
        return 0

    # ── 5. 执行删除 ──
    print()
    print('  开始删除...')
    ok = fail = 0
    deleted_bytes = 0
    cleaned_arts = []
    for p, kind, sz, art in uniq:
        rel = os.path.relpath(p, PROD)
        try:
            os.remove(p)
            ok += 1
            deleted_bytes += sz
            print(f'    ✅ {rel[:74]}')
            if kind == 'src_video':
                cleaned_arts.append((art['artifact_id'], kind, p))
        except Exception as e:                                   # noqa: BLE001
            fail += 1
            print(f'    ❌ {rel[:60]}  {type(e).__name__}: {e}')

    print()
    print(f'  删除完成: 成功 {ok}，失败 {fail}，释放 {deleted_bytes/1024**3:.2f} GB')

    # ── 6. 更新状态库为 cleaned（需要写连接）──
    if cleaned_arts:
        ts = int(time.time())
        wconn = sqlite3.connect(DB, timeout=60)
        try:
            for aid, kind, p in cleaned_arts:
                try:
                    wconn.execute(
                        'UPDATE artifacts SET state=?, updated_at=? WHERE artifact_id=?',
                        ('cleaned', ts, aid))
                except Exception as e:                           # noqa: BLE001
                    print(f'    ⚠️ 状态更新失败 {aid}: {e}')
            wconn.commit()
            print(f'  状态库: {len(cleaned_arts)} 条产物标为 cleaned')
        finally:
            wconn.close()

    # ── 7. 清理空目录 ──
    for dp, dn, fn in os.walk(REPLAY, topdown=False):
        try:
            if not os.listdir(dp):
                os.rmdir(dp)
                print(f'    🗑 删除空目录: {os.path.relpath(dp, PROD)}')
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    ensure_utf8_stdout()
    sys.exit(main())
