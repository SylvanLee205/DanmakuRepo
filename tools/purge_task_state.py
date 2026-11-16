# -*- coding: utf-8 -*-
"""把「已停用任务」的历史状态从 dmr_state.db 里清掉。

为什么需要
--------
`DMR/__init__.py::_check_task_set_consistency()` 有一道保护：状态库里记录的任务
若不在当前配置里，就拒绝启动（防止配置目录指错、误删历史状态）。

把主播配置挪进 `configs/temp/` 停用时（用户常用做法），这道校验就会拦住启动，
DMR 陷入「崩溃 → 10 秒后重启 → 再崩溃」的循环。正确做法不是删整个库
（那会连在用的 31 个任务的状态一起毁掉），而是只清掉停用任务的行。

安全措施
--------
1. **先备份**整个 .db 到 logs/dmr_state.db.bak-<时间戳>，出问题直接拷回来。
2. 默认只预览；加 --apply 才真写。
3. 只删 sessions / artifacts / upload_groups 三张表里属于这些任务的行，
   磁盘上的录像文件一个都不碰。

用法
----
    .venv\\Scripts\\python.exe tools\\purge_task_state.py            # 预览
    .venv\\Scripts\\python.exe tools\\purge_task_state.py --apply   # 执行
    .venv\\Scripts\\python.exe tools\\purge_task_state.py 小沐头 灰蒙蒙   # 指定任务
"""
import argparse
import glob
import os
import shutil
import sqlite3
import sys
import time

try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass

UP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(UP, 'logs', 'dmr_state.db')
CONFIGS = os.path.join(UP, 'configs')


def config_tasknames():
    """当前配置里的任务名（取文件名的 DMR- 前缀之后部分）。"""
    out = set()
    for f in glob.glob(os.path.join(CONFIGS, 'DMR-*.yml')):
        out.add(os.path.basename(f)[4:-4])
    return out


def db_tasknames(conn):
    rows = conn.execute("select distinct taskname from sessions").fetchall()
    return {r[0] for r in rows if r[0]}


def main():
    ap = argparse.ArgumentParser(description='清理已停用任务的状态库记录')
    ap.add_argument('names', nargs='*', help='任务名；不给则自动取"库里有、配置里没有"的')
    ap.add_argument('--apply', action='store_true', help='真的写库；不加则只预览')
    ap.add_argument('--force', action='store_true',
                    help='允许清理**仍在配置里**的活跃任务（危险，会让该任务状态失明）')
    args = ap.parse_args()

    if not os.path.exists(DB):
        print('找不到状态库:', DB)
        return 1
    conn = sqlite3.connect(DB)

    cfg = config_tasknames()
    db = db_tasknames(conn)
    stale = sorted(db - cfg)
    if args.names:
        targets = [n for n in args.names if n in db]
        unknown = [n for n in args.names if n not in db]
        if unknown:
            print('（库里本来就没有，忽略：%s）' % ', '.join(unknown))
    else:
        targets = stale
        if stale:
            print('库里有、配置里没有的任务: %s' % ', '.join(stale))
            print('（这些就是拦住启动的元凶；配置在 %s 里）' % os.path.relpath(
                os.path.join(CONFIGS, 'temp'), UP))

    if not targets:
        print('没有需要清理的任务。')
        conn.close()
        return 0

    # ⚠️ 2026-11-16：显式传名时不检查该任务是否仍在 configs/ 里。
    #    手滑传了活跃主播名 → 它的历史状态全被删（磁盘录像还在，但状态失明：
    #    已上传的分段会被当成没传过、重新上传/重新渲染）。
    active = sorted(n for n in targets if n in cfg)
    if active and not args.force:
        print('拒绝：以下任务是**仍在配置里**的活跃任务，清理会让它们状态失明：')
        for n in active:
            print('   %s' % n)
        print('如果确实要清：先把配置移到 configs/temp/ 停用，'
              '或加 --force（不推荐）。')
        conn.close()
        return 1

    print()
    print('将清理以下任务的状态记录：')
    total = 0
    for name in targets:
        s = conn.execute("select count(*) from sessions where taskname=?",
                         (name,)).fetchone()[0]
        a = conn.execute(
            "select count(*) from artifacts where session_id in "
            "(select session_id from sessions where taskname=?)", (name,)).fetchone()[0]
        u = conn.execute("select count(*) from upload_groups where taskname=?",
                         (name,)).fetchone()[0]
        total += s + a + u
        print('   %-14s sessions=%d  artifacts=%d  upload_groups=%d'
              % (name, s, a, u))
    print()
    print('合计删除 %d 行。磁盘录像文件不受影响。' % total)

    if not args.apply:
        print()
        print('这是预览。确认后加 --apply 执行（会自动先备份数据库）。')
        conn.close()
        return 0

    # DMR 运行时会缓存状态，写库后可能被内存里的旧值覆盖回去
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            '_guard', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   '_guard.py'))
        _g = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_g)
        if not _g.require_not_running('清理任务状态', force=args.force):
            conn.close()
            return 1
        bak = DB + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
        if not _g.sqlite_backup(DB, bak):
            conn.close()
            return 1
    except Exception as e:                                       # noqa: BLE001
        print(f'  [警告] 运行检测/在线备份不可用（{e}），退回直接复制。')
        bak = DB + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
        shutil.copy2(DB, bak)
    print()
    print('已备份: %s (%.2f MB)' % (os.path.basename(bak),
                                    os.path.getsize(bak) / 1048576))

    for name in targets:
        conn.execute(
            "delete from artifacts where session_id in "
            "(select session_id from sessions where taskname=?)", (name,))
        conn.execute("delete from upload_groups where taskname=?", (name,))
        conn.execute("delete from sessions where taskname=?", (name,))
    conn.commit()
    conn.close()
    print('已清理: %s' % ', '.join(targets))
    print()
    print('现在可以重启 DMR 了。想把某个主播加回来：把配置从 configs\\temp\\ '
          '挪回 configs\\ 即可（DMR 会热加载，但历史状态已不在库里）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
