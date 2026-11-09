# -*- coding: utf-8 -*-
"""修复状态库里 `data` 列的非法 JSON 转义（老版本路径拼接留下的脏数据）。

问题
----
部分 ``artifacts.data`` 载荷里有形如::

    "./直播回放/弹幕版\\vvu\\\\vvu-...mp4"

的路径 —— 单个反斜杠 ``\\v`` 在 JSON 里是**非法转义**（``\\`` 合法，``\\v``
不合法）。根源是旧版本用字符串拼接跨盘符路径时，一边用了 ``\\``、一边用了
``\\\\``，序列化前没统一。

后果
----
``decode_payload`` 有容错：解析失败就 ``logger.warning`` 然后**按空载荷处理**。
所以不会导致启动失败 —— 但那条记录的全部字段（path / size / upload 信息）
会被静默丢弃，等于状态库对这条记录"瞎了"。

当前实盘：53 条受影响，**全部是终态**（cleaned 33 / uploaded 14 /
skipped 5 / missing 1），不会被重新处理，所以暂时不影响生产。
但若哪天出现非终态的坏记录，重启恢复就会拿不到路径。

修法
----
把非法转义的单反斜杠补成双反斜杠（在 JSON 文本层面修，不动语义），
修完能被正常解析即算成功。真修不了的（结构性损坏）如实报告、不硬改。

安全措施
--------
1. 先备份整个 .db。
2. 默认只预览；``--apply`` 才写。
3. 只改 ``artifacts.data`` 一列，且只在"修完确实能解析"时才写。
"""
import argparse
import json
import os
import re
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

#: JSON 里合法的单字符转义
_VALID_ESC = set('"\\/bfnrtu')


def _fix_escapes(raw: str):
    """把非法转义 ``\\x`` 补成 ``\\\\x``。返回修好的文本，失败返回 None。"""
    out = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch != '\\':
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            out.append('\\\\')
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in _VALID_ESC:
            out.append(ch)
            out.append(nxt)
            i += 2
            continue
        # 非法转义 → 这个反斜杠本该是字面量，转义成 \\
        out.append('\\\\')
        i += 1
    text = ''.join(out)
    try:
        json.loads(text)
    except Exception:
        return None
    return text


def main():
    ap = argparse.ArgumentParser(description='修复状态库载荷的非法 JSON 转义')
    ap.add_argument('--apply', action='store_true', help='真正写库（默认预览）')
    ap.add_argument('--db', default=DB)
    opt = ap.parse_args()

    if not os.path.exists(opt.db):
        print(f'状态库不存在：{opt.db}')
        return 2

    con = sqlite3.connect(opt.db)
    con.row_factory = sqlite3.Row

    print('=' * 78)
    print(f'载荷转义修复（{"实际写库" if opt.apply else "DRY-RUN 仅预览"}）')
    print('=' * 78)

    targets, unfixable = [], []
    for r in con.execute('SELECT artifact_id, kind, state, data FROM artifacts'):
        raw = r['data']
        if not raw:
            continue
        try:
            json.loads(raw)
            continue                      # 本来就好的，不用修
        except json.JSONDecodeError:
            pass                          # 确认是坏载荷，落到下面的 _fix_escapes
        fixed = _fix_escapes(raw)
        if fixed is None:
            unfixable.append((r['artifact_id'], r['state']))
        else:
            targets.append((r['artifact_id'], r['kind'], r['state'], fixed))

    print(f'\n  可修复 {len(targets)} 条，修不了 {len(unfixable)} 条')
    if unfixable:
        for a, s in unfixable[:10]:
            print(f'    [修不了] {a} ({s})')
    if not targets:
        print('\n  没有需要修复的记录。')
        con.close()
        return 0

    import collections
    print('  按状态：')
    for k, v in collections.Counter(t[2] for t in targets).most_common():
        print(f'    {k:<10} {v}')

    if not opt.apply:
        print('\n  这是 DRY-RUN。确认无误后加 --apply 真正写库。')
        con.close()
        return 0

    bak = f'{opt.db}.bak-{time.strftime("%Y%m%d-%H%M%S")}'
    shutil.copy2(opt.db, bak)
    print(f'\n  已备份状态库 → {bak}')

    for aid, kind, state, fixed in targets:
        con.execute('UPDATE artifacts SET data=? WHERE artifact_id=?',
                    (fixed, aid))
    con.commit()
    print(f'  已修复 {len(targets)} 条。')

    # 复核
    still = 0
    for r in con.execute('SELECT data FROM artifacts'):
        try:
            json.loads(r['data'] or '{}')
        except Exception:
            still += 1
    print(f'  复核：仍有 {still} 条解析失败')
    con.close()
    return 0 if still == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
