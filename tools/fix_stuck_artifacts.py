# -*- coding: utf-8 -*-
"""把产物"卡在运行中状态"的记录挑出来并对账修正。

为什么需要
--------
``DMR/state/reconciler.py::_step0_sanitize_unstable`` 本来就会把
``rendering`` / ``uploading`` 这类"运行中"状态降级（重启时一律不可信）。
但它**只在进程启动时跑一次**。而这个进程是长期常驻的 —— 实盘连续运行
6 天没重启过，于是任何"运行中状态没被正常收敛"的产物就一直卡着：

  · 源视频永远不清理（清理要求终态）
  · 该分段永远不再重试（状态不是 recorded/rendered，进不了 RESUMABLE）

2026-11-09 体检的实际发现（全库就 2 条，属偶发）：
  君君.-2026年11月06日18点52分   state=rendering  attempts=0  last_error=None
  vvu-2026年11月07日14点19分     state=rendering  attempts=0  last_error=None
两条在日志里都明确渲染失败过（产物时长异常，产物已删、源视频保留），
但失败状态没落库 —— 根因已单独修（LiveEvents.onTaskError 现在会写库）。
本工具负责把**已经卡住的历史数据**修回来。

⚠️ 这不是"删数据"，是"按磁盘事实对账"：
  · 源视频文件还在、成品不存在  → 源视频回 recorded（可重新渲染）
  · 源视频文件不在             → 标 missing（如实反映）
  · 成品其实存在（渲染其实成功了）→ 回 rendered（别再重渲）

安全措施
--------
1. 先备份整个 .db 到 ``logs/dmr_state.db.bak-<时间戳>``，出问题直接拷回来。
2. 默认只预览；加 ``--apply`` 才真写。
3. 只动 ``artifacts`` 表里状态为运行中的行，其它行一个不碰。
4. 不删任何磁盘文件。

用法
----
    .venv\\Scripts\\python.exe tools/fix_stuck_artifacts.py            # 预览
    .venv\\Scripts\\python.exe tools/fix_stuck_artifacts.py --apply   # 执行
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

try:
    sys.stdout.reconfigure(errors='replace')
except Exception as e:
    # 无人值守脚本不能因控制台编码问题崩，但铁律禁止静默吞异常
    print(f'[警告] stdout 重配置失败（{type(e).__name__}: {e}），继续运行',
          file=sys.stderr)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'logs', 'dmr_state.db')

#: 这些状态表示"进程认为它正在跑"，重启后一律不可信
UNSTABLE = ('rendering', 'uploading', 'recording')


def _disk_path(root, rel):
    """把库里记的相对路径转成真实路径（兼容 . / 正反斜杠）。"""
    if not rel:
        return None
    p = rel.replace('\\', os.sep).replace('/', os.sep)
    if p.startswith('.' + os.sep):
        p = p[2:]
    if not os.path.isabs(p):
        p = os.path.join(root, p)
    return p


def main():
    ap = argparse.ArgumentParser(
        description='按磁盘事实修正"卡在运行中状态"的产物记录')
    ap.add_argument('--apply', action='store_true',
                    help='真正写库（默认只预览）')
    ap.add_argument('--db', default=DB, help='状态库路径')
    opt = ap.parse_args()

    if not os.path.exists(opt.db):
        print(f'状态库不存在：{opt.db}')
        return 2

    con = sqlite3.connect(opt.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        'SELECT * FROM artifacts WHERE state IN (%s)'
        % ','.join('?' * len(UNSTABLE)), UNSTABLE).fetchall()

    print('=' * 78)
    print(f'运行中状态对账（{"实际写库" if opt.apply else "DRY-RUN 仅预览"}）')
    print(f'状态库：{opt.db}')
    print('=' * 78)

    if not rows:
        print('\n  没有卡在运行中状态的产物。')
        con.close()
        return 0

    import json
    plans = []
    for r in rows:
        try:
            d = json.loads(r['data'] or '{}')
        except Exception:
            d = {}
        path = _disk_path(ROOT, d.get('path'))
        exists = bool(path and os.path.exists(path))
        kind = r['kind']

        if kind == 'dm_video':
            # 成品在 → 其实渲染成功了；不在 → 回到"源已录、待渲染"
            target = 'rendered' if exists else 'recorded'
        elif kind == 'src_video':
            target = 'recorded' if exists else 'missing'
        else:
            target = 'recorded' if exists else 'missing'

        plans.append((r['artifact_id'], r['state'], target, exists,
                      d.get('attempts'), path, d.get('last_error')))

    print(f'\n  发现 {len(plans)} 条：')
    for aid, cur, tgt, exists, att, path, err in plans:
        print(f'    {aid}')
        print(f'      当前 {cur:>9}  →  {tgt:<9}  '
              f'（磁盘{"在" if exists else "不在"}，attempts={att}）')
        print(f'      文件 {path}')
        if err:
            print(f'      旧错误 {str(err)[:100]}')
        print()

    if not opt.apply:
        print('  这是 DRY-RUN。确认无误后加 --apply 真正写库。')
        con.close()
        return 0

    # ── 备份 ──
    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = f'{opt.db}.bak-{stamp}'
    shutil.copy2(opt.db, bak)
    print(f'  已备份状态库 → {bak}')

    now = int(time.time())
    for aid, cur, tgt, exists, att, path, err in plans:
        note = (f'进程长期未重启，运行中状态未收敛；按磁盘事实修正为 {tgt}'
                f'（磁盘{"在" if exists else "不在"}）')
        con.execute(
            'UPDATE artifacts SET state=?, data=?, updated_at=? '
            'WHERE artifact_id=?',
            (tgt, _merge(con, aid, note), now, aid),
        )
    con.commit()

    for aid, cur, tgt, *_ in plans:
        print(f'    {aid}: {cur} → {tgt}')
    print(f'\n  已修正 {len(plans)} 条。')
    print('  说明：源视频已是 recorded 的，DMR 下次重启会由 reconciler '
          '重新派发渲染；不需要手动重跑。')
    con.close()
    return 0


def _merge(con, artifact_id, note):
    """只改 last_error / 清掉渲染残留字段，其它载荷原样保留。"""
    import json
    row = con.execute('SELECT data FROM artifacts WHERE artifact_id=?',
                      (artifact_id,)).fetchone()
    try:
        d = json.loads(row['data'] or '{}') if row else {}
    except Exception as e:
        # 坏载荷（旧版拼路径的非法转义）不该让 --apply 在备份之后、
        # UPDATE 循环中途抛异常退出；也不许当成空载荷悄悄覆盖原始内容。
        print(f'  [警告] {artifact_id} 载荷不可解析（{type(e).__name__}），'
              f'保留原文并跳过字段合并；请先跑 tools/fix_state_payload.py',
              file=sys.stderr)
        return (row['data'] if row else '') or '{}'  # 原样退回
    d['last_error'] = note
    # 渲染中断留下的半成品信息一律清掉，免得下次误用
    d['part_path'] = None
    return json.dumps(d, ensure_ascii=False)


if __name__ == '__main__':
    sys.exit(main())
