"""一键生成"瘦身交接包"—— 给下一个 AI / 同事的最小完整快照。

为什么要这个
------------
生产目录 ``F:\\DanmakuRepo`` 有 **14 GB**（绝大部分是录播视频），直接发给
别人既跑不动也不安全（里面有主播房间号、Server酱 key、登录态）。
但"来龙去脉"真正需要的只有三样：

    1. 代码 + 文档（含 HANDOVER.md / AGENTS.md）
    2. **.git 历史**（只有 10 MB，80 个提交，来龙去脉全在里面）
    3. 任务配置（脱敏后）—— 让人知道在录哪些主播

本脚本把这三样打包成一个 zip（默认约 15 MB），并对敏感信息脱敏。

用法
----
    python tools/make_handover_bundle.py                :: 出 zip 到项目父目录
    python tools/make_handover_bundle.py --out-dir D:/handover  :: 出文件夹
    python tools/make_handover_bundle.py --with-configs       :: 带上完整配置（不脱敏）
    python tools/make_handover_bundle.py --no-git             :: 不带 .git 历史
    python tools/make_handover_bundle.py --with-logs          :: 附带最近 3 天日志

脱敏规则
--------
    live.douyin.com/<房间号>    → live.douyin.com/<房间号>
    live.bilibili.com/<房间号>  → live.bilibili.com/<房间号>
    sctpXXXX（Server酱 key）     → <Server酱key已脱敏>
    11 位手机号 / 邮箱           → <已脱敏>

⚠️ **脱敏不能替代重置凭据**：Server酱 key 曾经硬编码在 notify.py 里并进了
git 历史，换机/交接后应去 Server酱³ 后台**重置** key，本脚本只保证新发的
包里不含明文。
"""
import argparse
import os
import re
import shutil
import sys
import time
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 整个目录不进包
EXCLUDE_DIRS = {
    '.venv', '__pycache__', '直播回放', '.temp', '.pytest_cache',
    '.mypy_cache', '.idea', '.vscode', 'node_modules', '.login_info',
    'login_info', 'backups',
}
#: 单个文件/后缀不进包（.exe 是二进制工具，AI 读不了也没必要）
EXCLUDE_SUFFIX = ('.pyc', '.exe', '.dll', '.pyd', '.zip', '.7z', '.rar')
#: 单独排除的文件名
EXCLUDE_NAMES = {'.DS_Store', 'thumbs.db'}

#: 保留的日志天数（--with-logs 时）
LOG_DAYS = 3

_PATTERNS = [
    (re.compile(r'(live\.douyin\.com/)\d+'), r'\1<房间号>'),
    (re.compile(r'(live\.bilibili\.com/)\d+'), r'\1<房间号>'),
    (re.compile(r'sctp[A-Za-z0-9]{10,}'), '<Server酱key已脱敏>'),
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '<手机号已脱敏>'),
    (re.compile(r'[\w.\-]+@[\w\-]+\.[a-z]{2,}'), '<邮箱已脱敏>'),
]

TEXT_SUFFIX = ('.py', '.md', '.yml', '.yaml', '.json', '.txt', '.cfg', '.ini',
               '.bat', '.cmd', '.ps1', '.xml', '.html', '.css', '.js', '.sh',
               '.sql', '.toml', '')


def _mask(text: str) -> str:
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


def _is_text(name: str) -> bool:
    return name.lower().endswith(TEXT_SUFFIX)


def _skip_dir(rel: str) -> bool:
    """rel 是相对项目根的目录路径。"""
    parts = rel.replace('\\', '/').split('/')
    for p in parts:
        if p in EXCLUDE_DIRS:
            return True
    return False


def _skip_file(rel: str) -> bool:
    parts = rel.replace('\\', '/').split('/')
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in EXCLUDE_NAMES:
        return True
    if name.lower().endswith(EXCLUDE_SUFFIX):
        return True
    # 日志默认不带（--with-logs 单独处理）
    if '/logs/' in '/' + rel.replace('\\', '/') + '/':
        return True
    return False


def _iter_files(include_git=True, with_logs=False):
    """产出 (绝对路径, 包内相对路径)。"""
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        rel_dir = os.path.relpath(dirpath, PROJECT_ROOT).replace('\\', '/')
        if rel_dir == '.':
            rel_dir = ''
        # 目录级排除
        dirnames[:] = [
            d for d in dirnames
            if not _skip_dir(os.path.join(rel_dir, d) if rel_dir else d)
        ]
        if rel_dir.startswith('.git'):
            if not include_git:
                dirnames[:] = []
                continue
        for fn in filenames:
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            if rel_dir.startswith('.git'):
                if not include_git:
                    continue
            else:
                if _skip_file(rel):
                    continue
            yield os.path.join(dirpath, fn), rel.replace('\\', '/')


def _recent_logs():
    """最近 N 天的 DMR 日志（--with-logs）。"""
    logdir = os.path.join(PROJECT_ROOT, 'logs')
    if not os.path.isdir(logdir):
        return []
    cut = time.time() - LOG_DAYS * 86400
    out = []
    for fn in sorted(os.listdir(logdir)):
        p = os.path.join(logdir, fn)
        if not os.path.isfile(p):
            continue
        if not fn.startswith('DMR-') or not fn.endswith('.log'):
            continue
        try:
            if os.path.getmtime(p) < cut:
                continue
        except OSError:
            continue
        out.append((p, f'logs/{fn}'))
    return out


def _config_files(with_configs):
    """任务配置：默认脱敏后加入包。"""
    cfgdir = os.path.join(PROJECT_ROOT, 'configs')
    if not os.path.isdir(cfgdir):
        return []
    out = []
    for fn in sorted(os.listdir(cfgdir)):
        p = os.path.join(cfgdir, fn)
        if not os.path.isfile(p):
            continue
        if fn in ('global.yml', 'global.yaml'):
            out.append((p, f'configs/{fn}', True))
        elif fn.endswith(('.yml', '.yaml')):
            out.append((p, f'configs/{fn}', True))
    if not with_configs:
        return out
    return out


def build(out_path, include_git=True, with_logs=False, with_configs=False,
          mask=True):
    """写出交接包。返回 (文件数, 字节数)。"""
    n = 0
    total = 0
    seen = set()

    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        def add_file(src, arcname, do_mask=True):
            nonlocal n, total
            if arcname in seen:
                return
            seen.add(arcname)
            try:
                with open(src, 'rb') as f:
                    data = f.read()
            except OSError as e:
                print(f'  [跳过] {arcname}: {e}')
                return
            if do_mask and mask and _is_text(src):
                try:
                    text = data.decode('utf-8')
                except UnicodeDecodeError:
                    text = data.decode('utf-8', errors='replace')
                data = _mask(text).encode('utf-8')
            z.writestr(arcname, data)
            n += 1
            total += len(data)

        for src, rel in _iter_files(include_git=include_git,
                                    with_logs=with_logs):
            add_file(src, rel)

        # 任务配置（脱敏）
        for src, rel, do_mask in _config_files(with_configs):
            add_file(src, rel, do_mask=do_mask)

        # 最近日志（也脱敏：日志里可能有房间号/URL）
        if with_logs:
            for src, rel in _recent_logs():
                add_file(src, rel, do_mask=True)

    return n, total


def build_dir(out_dir, include_git=True, with_logs=False, with_configs=False,
              mask=True):
    """直接产出文件夹（本地给 AI 读比 zip 方便）。"""
    n = 0
    total = 0
    for src, rel in _iter_files(include_git=include_git, with_logs=with_logs):
        dst = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
        total += os.path.getsize(src)
    for src, rel, do_mask in _config_files(with_configs):
        dst = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, 'rb') as f:
            data = f.read()
        if mask and do_mask and _is_text(src):
            data = _mask(data.decode('utf-8', errors='replace')).encode('utf-8')
        with open(dst, 'wb') as f:
            f.write(data)
        n += 1
        total += len(data)
    if with_logs:
        for src, rel in _recent_logs():
            dst = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, 'rb') as f:
                data = f.read()
            data = _mask(data.decode('utf-8', errors='replace')).encode('utf-8')
            with open(dst, 'wb') as f:
                f.write(data)
            n += 1
            total += len(data)
    return n, total


def scan_out(path):
    """扫一遍产出物，确认没有残留明文 key。"""
    import zipfile
    hits = []
    pat = re.compile(r'sctp[A-Za-z0-9]{10,}')
    if os.path.isfile(path):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not _is_text(name):
                    continue
                try:
                    txt = z.read(name).decode('utf-8', errors='replace')
                except Exception:
                    continue
                if pat.search(txt):
                    hits.append(name)
    else:
        for dirpath, _d, files in os.walk(path):
            for fn in files:
                if not _is_text(fn):
                    continue
                try:
                    txt = open(os.path.join(dirpath, fn), encoding='utf-8',
                               errors='replace').read()
                except OSError:
                    continue
                if pat.search(txt):
                    hits.append(os.path.join(dirpath, fn))
    return hits


def main():
    ap = argparse.ArgumentParser(description='生成 DanmakuRepo 瘦身交接包')
    ap.add_argument('--out', default=None,
                    help='输出 zip 路径（默认：项目父目录/DanmakuRepo-交接-<时间戳>.zip）')
    ap.add_argument('--out-dir', default=None,
                    help='输出文件夹路径（与 --out 二选一）')
    ap.add_argument('--no-git', action='store_true', help='不带 .git 历史')
    ap.add_argument('--with-logs', action='store_true',
                    help=f'附带最近 {LOG_DAYS} 天日志')
    ap.add_argument('--with-configs', action='store_true',
                    help='带上完整配置（默认仍脱敏，此开关仅表示"要带配置"）')
    ap.add_argument('--no-mask', action='store_true',
                    help='不脱敏（只在完全信任接收方时用）')
    args = ap.parse_args()

    stamp = time.strftime('%Y%m%d-%H%M')
    if args.out_dir:
        out_dir = os.path.abspath(args.out_dir)
        if os.path.exists(out_dir):
            print(f'目录已存在，先删除：{out_dir}')
            return 1
        n, total = build_dir(out_dir, not args.no_git, args.with_logs,
                             args.with_configs, not args.no_mask)
        print(f'✅ 交接文件夹：{out_dir}')
        print(f'   文件 {n} 个，{total/1024/1024:.1f} MB')
        target = out_dir
    else:
        out = args.out or os.path.join(
            os.path.dirname(PROJECT_ROOT),
            f'DanmakuRepo-交接-{stamp}.zip')
        n, total = build(out, not args.no_git, args.with_logs,
                         args.with_configs, not args.no_mask)
        print(f'✅ 交接包：{out}')
        print(f'   文件 {n} 个，压缩前 {total/1024/1024:.1f} MB，'
              f'压缩后 {os.path.getsize(out)/1024/1024:.1f} MB')
        target = out

    hits = scan_out(target)
    if hits:
        print('⚠️ 仍有明文 key 残留，请检查：')
        for h in hits[:10]:
            print('   ', h)
    else:
        print('✅ 脱敏自检通过：包内无明文 Server酱 key')

    print('\n包里有什么：HANDOVER.md / AGENTS.md / README.md / docs/ / 源码 / '
          '工具脚本 / 任务配置(脱敏)' + ('' if args.no_git else ' / 完整 git 历史'))
    print('接手方第一句话：读 HANDOVER.md 和 AGENTS.md。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
