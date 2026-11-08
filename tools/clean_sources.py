"""安全的源文件清理工具（带完整防护）。

⚠️⚠️ 为什么需要这个工具（2026-10-07 事故）：
    为了清理 7 个滞留的 .mkv，我临时写了个一次性脚本，结果它：
      1. **没有过滤 `[正在录制]` 前缀** —— 删掉了正在录制的 .ass
      2. **只对 .mkv 做了"弹幕版存在"检查**，.ass 直接删
    所幸删的是 0 字节文件，DMR 继续写入，没有实际损失。
    但这是个真实的教训：**一次性脚本绕过了正式流程的保护。**

    本工具把所有防护固化下来，以后清理一律用它。

防护清单（每一条都对应一次真实教训）：
    1. 跳过 `[正在录制]` 前缀的文件        <- 2026-10-07 事故
    2. 跳过 `.part` / `.tmp` / `.converting` 等中间文件
    3. 只处理超过 N 分钟没被修改的文件（默认 30 分钟）  <- 防正在写入
    4. 必须有对应的渲染产物（`（弹幕版）.mp4`）才允许删   <- 防删掉还没渲染的
    5. 默认 dry-run，加 --apply 才真删
    6. 删除前打印清单让用户确认

用法：
    python tools\\clean_sources.py            # 只报告（dry-run）
    python tools\\clean_sources.py --apply    # 真正删除
    python tools\\clean_sources.py --apply --min-age 120   # 只清 2 小时以上的
"""
import argparse
import os
import re
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录，自动识别（可放在任意路径）


ROOT = _PROJECT_ROOT
REPLAY = os.path.join(ROOT, '直播回放')

# 这些前缀/后缀表示"正在使用中"，绝对不能碰
ACTIVE_MARKERS = ('[正在录制]', '[正在渲染]', '[正在上传]')
SKIP_SUFFIX = ('.part', '.tmp', '.converting', '.temp', '.ytdl', '.download')
CLEANABLE_SUFFIX = ('.mkv', '.ass', '.flv', '.ts')

# 渲染产物目录的后缀
RENDERED_DIR_SUFFIX = '（弹幕版）'

# CD2 服务版缓存（云端真实内容）
CD2_CACHE = r'C:\Windows\System32\config\systemprofile\Waytech\CloudDrive2\dir_cache.sqlite'


def cloud_files(task):
    """从 CD2 缓存读某任务的云端文件集合。查不到返回 None（表示未知）。

    ⚠️ 为什么要查云端：
        本地弹幕版可能已被清理（正常行为），这时源文件其实可以安全删掉，
        因为内容已经在云端了。只按"本地渲染产物存在"判断会过于保守，
        导致源文件永久滞留（实测 12 个文件 1.11 GB 因此没被清理）。
    """
    import shutil
    import sqlite3
    if not os.path.exists(CD2_CACHE):
        return None
    tmp = os.path.join(os.environ.get('TEMP', '.'), 'clean_sources_cache')
    try:
        os.makedirs(tmp, exist_ok=True)
        for suf in ('', '-wal', '-shm'):
            src = CD2_CACHE + suf
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(tmp, 'dir_cache.sqlite' + suf))
                except Exception:
                    pass
        q = os.path.join(tmp, 'dir_cache.sqlite')
        con = sqlite3.connect(f'file:{q}?mode=ro', uri=True)
        cur = con.cursor()
        cur.execute("SELECT id FROM cached_item WHERE path LIKE ?",
                    (f'%/DMR录播/{task}',))
        out = set()
        for (did,) in cur.fetchall():
            cur.execute('SELECT name FROM files WHERE parent_id=?', (did,))
            out |= {r[0] for r in cur.fetchall()}
        con.close()
        return out
    except Exception:
        return None


def _scan_layouts():
    """按当前磁盘布局枚举 (原视频目录, 弹幕版目录) 配对。

    ⚠️⚠️ 2026-11-09 修：原来只认老布局（``直播回放/<主播>/`` +
    ``直播回放/<主播>（弹幕版）/``）。11-02 改成新布局后，``直播回放/`` 下
    只剩 ``原视频`` 和 ``弹幕版`` 两个目录，老逻辑把这两个当"主播名"去找
    ``原视频（弹幕版）``、``弹幕版（弹幕版）``，永远找不到 → 整个工具
    静默变成"没有可清理的文件"。实盘后果：4 个已上传分段的源视频
    （2.2GB）在磁盘上躺了一周没人清。

    新布局：``直播回放/原视频/<主播>/`` + ``直播回放/弹幕版/<主播>/``
    老布局：``直播回放/<主播>/``      + ``直播回放/<主播>（弹幕版）/``
    两种都返回，互不冲突（新布局的两级目录名不会撞上老布局的单级模式）。
    """
    pairs = []
    if not os.path.isdir(REPLAY):
        return pairs

    # ── 新布局 ──
    src_root = os.path.join(REPLAY, '原视频')
    out_root = os.path.join(REPLAY, '弹幕版')
    if os.path.isdir(src_root):
        for task in sorted(os.listdir(src_root)):
            src_dir = os.path.join(src_root, task)
            if not os.path.isdir(src_dir):
                continue
            out_dir = os.path.join(out_root, task)
            pairs.append((src_dir, out_dir if os.path.isdir(out_dir) else None,
                          task))

    # ── 老布局（兼容残留，避免迁移未完成时漏清）──
    for task in sorted(os.listdir(REPLAY)):
        sub = os.path.join(REPLAY, task)
        if not os.path.isdir(sub):
            continue
        if task in ('原视频', '弹幕版'):
            continue                      # 新布局容器，不是主播目录
        if task.endswith(RENDERED_DIR_SUFFIX):
            continue                      # 弹幕版目录是产物，不动
        out_dir = os.path.join(REPLAY, task + RENDERED_DIR_SUFFIX)
        pairs.append((sub, out_dir if os.path.isdir(out_dir) else None, task))
    return pairs


def _exists_rendered(stem, rendered_files):
    """判断某个源分段是否有对应成品，容忍 ``(n)`` 重名后缀。

    为什么不能只做一次精确匹配：
    同名的分段重录时，DMR 会把产物命名成 ``xxx(2)（弹幕版）.mp4``。原实现
    拿 ``stem + （弹幕版）.mp4`` 精确比对，于是 ``掉了颗兔牙-...00点20分.ts``
    永远对不上 ``掉了颗兔牙-...00点20分(2)（弹幕版）.mp4``，源文件永久滞留
    （实测 755MB 躺了一周）。
    """
    for suffix in ('', ) + tuple(f'({i})' for i in range(1, 10)):
        if f'{stem}{suffix}{RENDERED_DIR_SUFFIX}.mp4' in rendered_files:
            return True
    # 兜底：成品名以「源名 + 可选(n)」开头即算命中，防止 DMR 用了别的后缀
    for name in rendered_files:
        if not name.endswith(RENDERED_DIR_SUFFIX + '.mp4'):
            continue
        base = name[:-len(RENDERED_DIR_SUFFIX + '.mp4')]
        if base == stem:
            return True
        if base.startswith(stem) and re.fullmatch(r'\(\d+\)', base[len(stem):]):
            return True
    return False


def _exists_in_cloud(stem, cloud_set):
    """判断源分段的内容是否真的在云端（严格同名，不容忍前缀巧合）。

    ⚠️⚠️ 2026-11-09 修：原实现是

        cloud_hit = any(n.startswith(stem[:len(stem) - 6]) for n in cloud_set)

    截掉末尾 6 个字符后做前缀比对。这个 6 是照 ``14点19分`` 的长度猜的，
    结果把时间戳削成了 ``...11月07日`` —— 只要那天**任意**一场录制上了云，
    任何分段都会被判成"云端已有"。实盘险情：
        vvu-...11月07日14点19分.ts   被匹配到 ...11月07日15点19分（弹幕版）
        君君.-...11月06日18点52分.ts 被匹配到 ...11月06日18点03分（弹幕版）
    而这两段恰恰是**渲染失败、从未上传**的（产物时长异常被删）。
    若不修，--apply 会把它们删掉，那两段直播就永久没有弹幕版了。

    现在改为严格比对：云端名必须是 ``<stem>（弹幕版）.mp4`` 或带 ``(n)``
    重名变体。emoji 清洗可能改动文件名，所以额外允许"清洗后仍以完整 stem
    的日期时间部分结尾"这一种弱匹配 —— 但**绝不截断 stem**。
    """
    for name in cloud_set:
        base = name[:-4] if name.endswith('.mp4') else name
        if base == f'{stem}{RENDERED_DIR_SUFFIX}':
            return True
        if base.startswith(stem):
            rest = base[len(stem):]
            if rest == RENDERED_DIR_SUFFIX or \
                    re.fullmatch(r'\(\d+\)' + re.escape(RENDERED_DIR_SUFFIX), rest):
                return True
    return False


def _pending_clean_names():
    """读取 24h 延迟清理队列里的文件名（按 basename 比对，跨布局都通用）。

    ⚠️⚠️ 2026-11-09 加：这是本工具**最重要的一道防护**，之前完全没有。

    为什么必须有：``find_candidates`` 的判据是"本地有渲染产物就能删"。
    但 DMR 的正式流程不是这样 —— 渲染完成后还要**上传**，上传成功才进
    ``pending_clean.json`` 等 24 小时再删（给用户留出发现问题的窗口）。
    "已渲染"和"已上传"是两个完全不同的阶段。

    实盘：11-09 那一刻 34 条队列全是**未到期**的（0 条过期），而本工具
    把其中 66 个文件（40.9GB）列成了"可安全清理"。谁要是顺手 ``--apply``，
    就把刚渲染完、还没上传的内容全删了 —— 而且是绕过 24h 保护窗口。
    这类误删不可恢复（源视频删了就再也渲染不出来）。

    返回 ``None`` 表示队列读不出来 —— 此时按"全部保护"处理，宁可不清。
    """
    import json
    q = os.path.join(ROOT, 'logs', 'pending_clean.json')
    if not os.path.exists(q):
        return set()
    try:
        with open(q, encoding='utf-8') as f:
            data = json.load(f)
        names = set()
        for entry in data or []:
            for pair in entry.get('pairs') or []:
                for item in pair:
                    if isinstance(item, str) and item:
                        names.add(os.path.basename(item.replace('\\', '/')))
        return names
    except Exception as e:
        print(f'  [警告] 读取延迟清理队列失败（{e}），'
              f'按"全部保护"处理，不删除任何文件。')
        return None


def find_candidates(min_age_minutes=30):
    """找出可以安全清理的文件，返回 [(路径, 大小, 原因) ...] 和跳过列表。"""
    ok, skipped = [], []
    if not os.path.isdir(REPLAY):
        return ok, skipped

    pending = _pending_clean_names()

    now = time.time()
    for src_dir, out_dir, task in _scan_layouts():
        rendered_files = set()
        if out_dir and os.path.isdir(out_dir):
            rendered_files = {f for f in os.listdir(out_dir)
                              if f.endswith('.mp4')}

        # 云端文件集合（用于判断"内容是否已在云端"），查不到为 None
        cloud_set = cloud_files(task)

        for f in sorted(os.listdir(src_dir)):
            p = os.path.join(src_dir, f)
            if not os.path.isfile(p):
                continue

            # 防护 1/2：名字里带"正在..."标记，或扩展名是中间格式
            if any(m in f for m in ACTIVE_MARKERS):
                skipped.append((p, '文件名含"正在录制/渲染/上传"标记'))
                continue
            if f.lower().endswith(SKIP_SUFFIX):
                skipped.append((p, '中间文件（.part/.tmp 等）'))
                continue
            if not f.lower().endswith(CLEANABLE_SUFFIX):
                continue

            # 防护 3：太新的不动（可能还在写）
            try:
                age_min = (now - os.path.getmtime(p)) / 60
            except Exception:
                continue
            if age_min < min_age_minutes:
                skipped.append((p, f'刚修改过（{age_min:.0f} 分钟前 < '
                                   f'{min_age_minutes} 分钟）'))
                continue

            # 防护 3.5：还在 24h 延迟清理队列里 → 一律不动。
            # 这是 DMR 自己会做的事，工具不该抢在它前面删。
            if pending is None or f in pending:
                skipped.append((p, '在 24 小时延迟清理队列中（交给 DMR 到期自动删）'))
                continue

            # 防护 4：必须有"内容已在别处"的证据才能删，二选一：
            #   a) 本地有渲染产物（（弹幕版）.mp4，含 (n) 重名变体）
            #   b) 云端已有该分段（内容已备份，重渲染没意义）
            stem = os.path.splitext(f)[0]
            if _exists_rendered(stem, rendered_files):
                ok.append((p, os.path.getsize(p), age_min, '本地已渲染'))
                continue
            # 云端校验
            if cloud_set is not None:
                if _exists_in_cloud(stem, cloud_set):
                    ok.append((p, os.path.getsize(p), age_min, '云端已有'))
                    continue
                skipped.append((p, '本地无渲染产物，云端也没有'))
            else:
                skipped.append((p, '本地无渲染产物（云端查询不可用）'))
            continue

    return ok, skipped


def main():
    ap = argparse.ArgumentParser(description='安全清理源文件/弹幕文件')
    ap.add_argument('--apply', action='store_true',
                    help='真正删除（默认只报告，不删）')
    ap.add_argument('--min-age', type=int, default=30, metavar='MIN',
                    help='只清理最后修改早于 MIN 分钟的文件（默认 30）')
    ap.add_argument('--show-skipped', action='store_true',
                    help='也列出被跳过保护的文件')
    opt = ap.parse_args()

    print('=' * 78)
    print(f'源文件清理（min_age={opt.min_age} 分钟，'
          f'{"实际删除" if opt.apply else "DRY-RUN 仅报告"}）')
    print('=' * 78)

    ok, skipped = find_candidates(min_age_minutes=opt.min_age)

    if not ok:
        print('\n  没有可清理的文件。')
    else:
        total = sum(sz for _, sz, _, _ in ok)
        print(f'\n  可安全清理 {len(ok)} 个文件，共 {total/1024/1024/1024:.2f} GB：')
        for p, sz, age, why in sorted(ok, key=lambda x: -x[1]):
            rel = os.path.relpath(p, REPLAY)
            print(f'    {sz/1024/1024:>9.1f} MB  {age:>7.0f} 分钟前  '
                  f'[{why}]  {rel}')

    if opt.show_skipped and skipped:
        print(f'\n  被保护跳过的 {len(skipped)} 个：')
        for p, reason in skipped[:40]:
            rel = os.path.relpath(p, REPLAY)
            print(f'    [跳过] {rel}  <- {reason}')
        if len(skipped) > 40:
            print(f'    ... 还有 {len(skipped)-40} 个')

    if ok and not opt.apply:
        print('\n  这是 DRY-RUN。确认无误后加 --apply 真正删除。')
        return 0

    if ok and opt.apply:
        print('\n  开始删除...')
        n, freed, failed = 0, 0, 0
        for p, sz, _, _ in ok:
            # 删除前再确认一次：文件还在、且仍然"老"
            if not os.path.exists(p):
                continue
            try:
                if (time.time() - os.path.getmtime(p)) / 60 < opt.min_age:
                    print(f'    跳过（刚刚又被修改了）: {os.path.basename(p)}')
                    continue
                os.remove(p)
                n += 1
                freed += sz
            except Exception as e:
                failed += 1
                print(f'    删除失败 {os.path.basename(p)}: {e}')
        print(f'\n  已删除 {n} 个，释放 {freed/1024/1024/1024:.2f} GB'
              + (f'，失败 {failed} 个' if failed else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
