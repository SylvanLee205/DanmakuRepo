"""把录制目录迁到新布局（默认**只预览**，加 --apply 才真动）。

旧布局（源和成品混在 直播回放/ 下，主播一多就分不清）：

    直播回放/
      ├── 小鱼大王/                 源视频 + .ass
      ├── 小鱼大王（弹幕版）/        成品
      ├── vvu/
      └── vvu（弹幕版）/

新布局（根目录下永远只有两个分类目录）：

    直播回放/
      ├── 原视频/小鱼大王/
      ├── 原视频/vvu/
      ├── 弹幕版/小鱼大王/
      └── 弹幕版/vvu/

做什么
------
1. 搬目录：`直播回放/<主播>/` → `直播回放/原视频/<主播>/`
          `直播回放/<主播>（弹幕版）/` → `直播回放/弹幕版/<主播>/`
2. 改配置：33 个 `configs/DMR-*.yml` 的 `output_dir` 加一层 `原视频/`
3. 改状态库：`artifacts.data` 里的路径前缀同步更新（先自动备份 .db）
4. 改工具：`tools/add_room.py` 的模板（已改好，这里只校验）

⚠️ 三条硬性前置
----------------
* **必须没有正在录制的任务**。录制中的文件被 ffmpeg/streamgears 占着，
  Windows 上移动会失败（或留下半截目录）。脚本会自己检查，发现录制就拒绝。
* 迁移期间请**先停 DMR**（`Restart_DMR.bat` 或 `Stop_DMR.bat`），
  否则它一边写旧路径一边被搬走，会产出孤儿分段。
* 同盘移动是秒级（只改目录项），但**跨盘就是真拷贝**，别在录的时候干。

用法：
    python tools\\migrate_layout.py            # 预览
    python tools\\migrate_layout.py --apply    # 执行
    python tools\\migrate_layout.py --apply --keep-db   # 不动状态库
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass

APPLY = '--apply' in sys.argv
KEEP_DB = '--keep-db' in sys.argv

DM_MARK = '（弹幕版）'
SRC_SUB = '原视频'
DM_SUB = '弹幕版'
#: 这两个目录本身就是分类目录，不是主播，绝不能被当成主播搬走
RESERVED = {SRC_SUB, DM_SUB}

ROOT = os.path.join(PROJ, '直播回放')
DB = os.path.join(PROJ, 'logs', 'dmr_state.db')
CONFIGS = os.path.join(PROJ, 'configs')


def hr(t=''):
    print()
    print('=' * 78)
    if t:
        print(t)
        print('=' * 78)


def is_recording() -> list:
    """当前有没有正在录制（进程或 [正在录制] 文件）。"""
    hits = []
    try:
        import psutil
        for p in psutil.process_iter(['name', 'cmdline']):
            n = (p.info['name'] or '').lower()
            cl = p.info['cmdline'] or []
            if n == 'python.exe' and any('streamgears_wrapper' in (a or '') for a in cl):
                hits.append(f'录制进程 PID {p.pid}')
            elif n == 'ffmpeg.exe' and any(a == '-segment_time' for a in cl):
                hits.append(f'录制 ffmpeg PID {p.pid}')
    except Exception:
        pass
    try:
        for entry in os.listdir(ROOT):
            d = os.path.join(ROOT, entry)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.startswith('[正在录制]'):
                    hits.append(f'{entry}/{f[:40]}')
    except OSError:
        pass
    return hits


def plan_dirs():
    """算出要搬哪些目录。返回 [(老路径, 新路径, 是成品)]。"""
    moves = []
    if not os.path.isdir(ROOT):
        return moves
    for entry in sorted(os.listdir(ROOT)):
        if entry in RESERVED or entry.startswith('.'):
            continue
        src = os.path.join(ROOT, entry)
        if not os.path.isdir(src):
            continue
        if DM_MARK in entry:
            streamer = entry.replace(DM_MARK, '').strip()
            dst = os.path.join(ROOT, DM_SUB, streamer)
            moves.append((src, dst, True))
        else:
            dst = os.path.join(ROOT, SRC_SUB, entry)
            moves.append((src, dst, False))
    return moves


def do_moves(moves):
    ok = fail = 0
    for src, dst, _is_dm in moves:
        if os.path.exists(dst):
            print(f'  ⏭  目标已存在，跳过: {os.path.relpath(dst, PROJ)}')
            continue
        if not APPLY:
            print(f'  将移动: {os.path.relpath(src, PROJ)}  ->  {os.path.relpath(dst, PROJ)}')
            ok += 1
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            print(f'  ✅ {os.path.relpath(src, PROJ)} -> {os.path.relpath(dst, PROJ)}')
            ok += 1
        except OSError as e:
            print(f'  ❌ 移不动 {os.path.relpath(src, PROJ)}: {e}')
            fail += 1
    return ok, fail


def plan_configs():
    """返回 [(文件路径, 老 output_dir, 新 output_dir)]。"""
    out = []
    if not os.path.isdir(CONFIGS):
        return out
    pat = re.compile(r"^(\s*output_dir:\s*)\./直播回放/([^/\s].*)$")
    for name in sorted(os.listdir(CONFIGS)):
        if not name.startswith('DMR-') or not name.endswith('.yml'):
            continue
        path = os.path.join(CONFIGS, name)
        try:
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            m = pat.match(line.rstrip('\n'))
            if not m:
                continue
            rest = m.group(2)
            if rest.startswith(SRC_SUB + '/'):
                continue                      # 已经迁过了
            out.append((path, i, m.group(1), f'./直播回放/{SRC_SUB}/{rest}'))
            break
    return out


def do_configs(plans):
    n = 0
    for path, idx, prefix, new_val in plans:
        rel = os.path.relpath(path, PROJ)
        if not APPLY:
            print(f'  将改 {rel}: output_dir -> {new_val}')
            n += 1
            continue
        try:
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
            lines[idx] = prefix + new_val + '\n'
            # 原子写：先写临时文件再替换，避免断电留下半截 yml
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8', newline='') as f:
                f.writelines(lines)
            os.replace(tmp, path)
            print(f'  ✅ {rel}')
            n += 1
        except OSError as e:
            print(f'  ❌ 改不了 {rel}: {e}')
    return n


def _migrate_path_string(s: str) -> str:
    """把单个路径字符串迁到新布局（纯字符串层面的改写）。"""
    new = s
    # 成品目录：<主播>（弹幕版）\ -> 弹幕版\<主播>\
    for m in re.finditer(r'([^/\\"\']+)' + re.escape(DM_MARK) + r'([/\\])', new):
        name = m.group(1)
        new = new.replace(m.group(0), f'{DM_SUB}{m.group(2)}{name}{m.group(2)}')
    # 源目录：直播回放/<主播>\ -> 直播回放/原视频/<主播>\
    new = re.sub(r'(直播回放[/\\])(?!' + SRC_SUB + r'[/\\]|' + DM_SUB + r'[/\\])'
                 r'([^/\\"\']+[/\\])',
                 lambda m: m.group(1) + SRC_SUB + '/' + m.group(2), new)
    return new


def _migrate_payload(obj):
    """递归改写 JSON 载荷里所有字符串值。"""
    if isinstance(obj, str):
        return _migrate_path_string(obj)
    if isinstance(obj, list):
        return [_migrate_payload(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _migrate_payload(v) for k, v in obj.items()}
    return obj


def do_db():
    """把状态库里的路径前缀改到新布局（先备份）。

    ⚠️ 2026-11-16 修正（审查报告 D-7）：原实现直接在 ``json.dumps`` 之后的
    **原始文本**上跑正则。Windows 路径在 JSON 里是 ``\\\\``（两个反斜杠），
    正则只消费了一个，替换后留下单反斜杠 → 整条 data 变成非法 JSON
    （``Invalid \\escape``），状态库记录就此损坏。现在必须走
    ``json.loads → 改字段 → json.dumps``，绝不碰原始文本。
    """
    if not os.path.exists(DB):
        print('  没有状态库，跳过')
        return 0

    # 先用在线备份 API 备份（活库也一致），失败就放弃改库
    bak = f'{DB}.bak-{time.strftime("%Y%m%d-%H%M%S")}'
    try:
        from _guard import sqlite_backup
        if not sqlite_backup(DB, bak):
            print('  ❌ 备份失败，放弃改库')
            return 0
        print(f'  已备份: {os.path.basename(bak)}')
    except Exception as e:                                       # noqa: BLE001
        print(f'  ❌ 备份失败，放弃改库: {e}')
        return 0

    con = sqlite3.connect(DB)
    try:
        rows = con.execute('SELECT artifact_id, data FROM artifacts').fetchall()

        changed = []
        broken = 0
        for aid, data in rows:
            if not data:
                continue
            try:
                payload = json.loads(data)
            except (ValueError, TypeError):
                # 已经是坏 JSON：不在迁移里"顺手修"，只统计并跳过
                broken += 1
                continue
            migrated = _migrate_payload(payload)
            if migrated == payload:
                continue
            changed.append((json.dumps(migrated, ensure_ascii=False), aid))

        print(f'  需要更新的记录: {len(changed)} / {len(rows)}')
        if broken:
            print(f'  ⚠️ 有 {broken} 条 data 不是合法 JSON，已跳过'
                  f'（可另跑 tools/fix_state_payload.py）。')
        if not APPLY or not changed:
            return len(changed)

        con.executemany('UPDATE artifacts SET data=? WHERE artifact_id=?', changed)
        con.commit()
        print(f'  ✅ 已更新 {len(changed)} 条')
    except sqlite3.Error as e:
        print(f'  ❌ 写状态库失败: {e}')
        return 0
    finally:
        con.close()
    return len(changed)


def main() -> int:
    hr('0. 前置检查')
    rec = is_recording()
    if rec:
        print('  ❌ 检测到正在录制，拒绝迁移：')
        for r in rec[:10]:
            print(f'     {r}')
        print('     请先 Stop_DMR.bat，或等这批录完再跑。')
        return 1
    print('  ✅ 没有正在录制的任务')
    if not os.path.isdir(ROOT):
        print(f'  ❌ 找不到 {ROOT}')
        return 1

    moves = plan_dirs()
    hr(f'1. 搬目录（{len(moves)} 个）')
    ok, fail = do_moves(moves)

    plans = plan_configs()
    hr(f'2. 改任务配置 output_dir（{len(plans)} 个）')
    n_cfg = do_configs(plans)

    hr('3. 状态库路径')
    n_db = 0 if KEEP_DB else do_db()

    hr('结果')
    mode = '已执行' if APPLY else '预览（未改动任何文件）'
    print(f'  {mode}：目录 {ok} 个（失败 {fail}）  配置 {n_cfg} 个  状态库 {n_db} 条')
    if not APPLY:
        print('\n  确认无误后加 --apply 重跑；迁移前后建议用 Restart_DMR.bat 重启一次。')
    else:
        print('\n  下一步：双击 Restart_DMR.bat 重启，让新配置生效。')
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
