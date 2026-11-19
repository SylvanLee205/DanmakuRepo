# -*- coding: utf-8 -*-
"""一条命令把两个仓库都推上去。

两个仓库的分工
--------------
* **公开仓库**（`origin` → DanmakuRepo，分支 `main`）
  代码 + 文档，**脱敏**：不含 configs/、不含状态库、不含内网地址。
  内容 = 开发主干 `main` 的树 + 10 个发布脚手架文件（示例配置、脱敏工具等）。

* **私有仓库**（`full` → DanmakuRepo-Full，分支 `main` = 本地 full-backup）
  完全体：开发主干 `main` 的全部内容 **加上** `configs/`、`.temp/`、`logs/dmr_state.db`。
  克隆下来放到 `F:\\DanmakuRepo` 就能直接跑，跟生产机一模一样。

用法
----
    python tools/push_all.py             # 预览：只报告会推什么，不动任何东西
    python tools/push_all.py --apply     # 真做：同步、提交、推送两个仓库

前置条件：开发主干 `main` 工作区干净（先 commit）。否则中止 —— 宁可让你先整理，
也不要推上去半截内容。

分支命名（2026-11-19 起统一，远端两个仓库都只有 `main`）
    main          开发主干，生产目录就在这个分支上，也是同步的唯一来源
    public        公开树的本地分支，只存在于 -prodsync worktree，推送为 origin/main
    full-backup   私有完整副本，只存在于 -full worktree，推送为 full/main

两个 worktree（由 `git worktree` 维护，不在生产目录里切分支）：
    <项目根>-prodsync   公开分支（public）
    <项目根>-full       私有完整副本（full-backup）
例如项目在 F:\\DanmakuRepo 时，两个工作树就在 F:\\DanmakuRepo-prodsync
和 F:\\DanmakuRepo-full。换成别的盘/目录会自动跟着变。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录，自动识别（可放在任意路径）


try:
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')
except Exception:
    pass

PROD = _PROJECT_ROOT
WT_PUBLIC = _PROJECT_ROOT + '-prodsync'
WT_FULL = _PROJECT_ROOT + '-full'

# 三个分支名的分工（2026-11-19 统一命名后）
#   DEV_BRANCH     本地开发主干 = main，生产目录所在分支，也是同步的唯一来源
#   PUBLIC_LOCAL   公开树的本地分支 = public（在 -prodsync worktree 里）
#   PUBLIC_REMOTE  公开仓库的远端分支 = main（对外只暴露这一个名字）
DEV_BRANCH = 'main'
PUBLIC_LOCAL = 'public'
PUBLIC_REMOTE = 'main'
FULL_BRANCH = 'full-backup'
FULL_REMOTE = 'full'

# 公开仓库**独有**、开发主干里没有的文件。
# 这些是 v5 基线带过来的发布脚手架和示例配置，同步时绝不能删。
PUBLIC_EXTRAS = {
    'configs/global.example.yml',
    'configs/temp/example-直播录制.yml',
    'configs/temp/example-视频下载.yml',
    'notify_content.txt',
    'notify_title.txt',
    'tools/create_release.py',
    'tools/find_private_info.py',
    'tools/make_example_config.py',
    'tools/mask_private_info.py',
    'tools/redo_release.py',
}


def sh(cmd, cwd, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if check and r.returncode != 0:
        raise RuntimeError('%s 失败(%d): %s%s'
                           % (' '.join(cmd), r.returncode,
                              (r.stdout or '')[-500:], (r.stderr or '')[-800:]))
    return (r.stdout or '').strip()


def git(cwd, *args, check=True):
    return sh(['git'] + list(args), cwd, check=check)


def git_push(cwd, remote, refspec):
    """推送并**检查退出码**。

    ⚠️ 2026-11-16 修正（审查报告 S-3）：原实现用 ``git(..., check=False)``，
    而 ``sh()`` 只返回 stdout —— git push 的结果和错误全在 stderr，于是
    认证失败 / 网络断 / 非快进时 stdout 为空，照样打印 "ok"、返回 'pushed'，
    Push_All.bat 显示 "[OK] Both repos up to date"。假成功比明确失败危险。
    """
    r = subprocess.run(['git', 'push', remote, refspec], cwd=cwd,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    lines = [l for l in ((r.stderr or '') + '\n' + (r.stdout or '')).splitlines()
             if l.strip()]
    tail = lines[-1].strip() if lines else ''
    return r.returncode == 0, tail


#: 不允许进公开仓库的敏感模式。命中即要求显式 --allow-sensitive。
SENSITIVE_PATTERNS = [
    (re.compile(r'sctp[A-Za-z0-9]{12,}'), 'Server酱 key'),
    (re.compile(r'https?://100\.(?:1?\d?\d|2[0-4]\d|25[0-5])\.\d{1,3}\.\d{1,3}'),
     'Tailscale IP'),
    (re.compile(r'live\.douyin\.com/\d{4,}'), '抖音房间号'),
    (re.compile(r'live\.bilibili\.com/\d{4,}'), 'B站房间号'),
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '手机号'),
]

#: 只扫这些后缀（二进制文件没有"泄露房间号"的语义）
_SCAN_SUFFIX = ('.py', '.md', '.yml', '.yaml', '.json', '.txt', '.bat', '.cmd',
                '.ps1', '.html', '.css', '.js', '.sh', '.toml', '.cfg', '.ini')

#: 第三方 SDK / 压缩库：里面的长数字是协议常量或示例号码，不是我们的隐私。
#: 扫它们只会制造噪音，让人对告警麻木。
_SKIP_FILES = {
    'DMR/LiveAPI/danmaku/douyin/webmssdk.js',
}

#: 明显的示例占位符 / 测试用假号（文档和单测里本来就该长这样）
_PLACEHOLDER_IDS = {
    '123456', '1234567', '12345678', '000000', '111111',
    # 2026-11-16 把真实房间号脱敏后统一换成的假号，见 docs/maintainer-log.md
    '123456789012', '10000001', '9999', '111111111', '222222222',
}


def _is_placeholder(frag: str) -> bool:
    """判断命中的房间号/号码是不是占位符。"""
    digits = re.sub(r'\D', '', frag)
    return digits in _PLACEHOLDER_IDS


def scan_sensitive():
    """扫一遍"即将进入公开仓库"的 main 树，返回 [(文件, 行号, 类型, 片段)]。

    为什么要扫：``sync_public`` 用 ``git checkout main -- .`` 整树同步，
    从不做脱敏检查；而同步过去的 docs 里确实含有真实抖音房间号、Tailscale
    地址和 B 站房间号（审查报告 S-2 已确认这些内容在 origin/main 上是公开的）。
    """
    hits = []
    files = git(PROD, 'ls-tree', '-r', '--name-only', DEV_BRANCH,
                check=False).splitlines()
    for rel in files:
        rel = rel.replace('\\', '/')
        if not rel.lower().endswith(_SCAN_SUFFIX) or rel in _SKIP_FILES:
            continue
        p = os.path.join(PROD, rel)
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat, kind in SENSITIVE_PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                if _is_placeholder(m.group(0)):
                    break
                hits.append((rel, lineno, kind, m.group(0)[:40]))
                break
    return hits


def dev_dirty():
    return bool(git(PROD, 'status', '--porcelain', check=False))


# ---------------------------------------------------------------- 公开仓库
def sync_public(apply):
    """让公开树的树 == 开发主干 + PUBLIC_EXTRAS。"""
    print('【公开仓库】%s (%s → origin/%s)' % (WT_PUBLIC, PUBLIC_LOCAL, PUBLIC_REMOTE))
    if not os.path.isdir(WT_PUBLIC):
        print('  ! worktree 不存在，跳过')
        return None

    # 预览模式不碰 worktree：只看"会删什么、会推什么"，不改一个字节。
    # 2026-11-04 实踩：这段 checkout 原本无条件执行，预览一次就把公开
    # worktree 里的文件覆盖成 main 版本了（虽然可还原，但"预览"不该有副作用）。
    if apply:
        git(WT_PUBLIC, 'checkout', DEV_BRANCH, '--', '.')

    # `git checkout main -- .` 只更新/新增，不删除开发主干里已不存在的文件。
    # 这里补上删除，但保留 PUBLIC_EXTRAS。
    #
    # ⚠️ 删除只在 apply=True 时执行。
    # 2026-11-04 实踩：预览模式（默认）也会真的 `git rm` + `os.remove` 掉文件，
    # 只是不提交 —— 等于"看一眼会发生什么"就把公开 worktree 弄脏了，
    # 且删掉的文件要 `git checkout HEAD --` 才能找回。预览必须零副作用。
    dev_files = set(git(PROD, 'ls-tree', '-r', '--name-only', DEV_BRANCH).splitlines())
    tracked = [p for p in git(WT_PUBLIC, 'ls-files').splitlines() if p]
    to_delete = [p for p in tracked
                 if p not in dev_files and p not in PUBLIC_EXTRAS]
    for p in to_delete:
        if not apply:
            print('  [预览] 将删除 %s' % p)
            continue
        fp = os.path.join(WT_PUBLIC, p)
        git(WT_PUBLIC, 'rm', '-q', '--cached', '--', p)
        if os.path.isfile(fp):
            os.remove(fp)
        print('  删除 %s' % p)

    if apply:
        # 真正同步之后，用 worktree 的 status 判断需要提交什么
        changed = git(WT_PUBLIC, 'status', '--porcelain')
        if not changed:
            print('  无变化')
            return 'nochange'
    else:
        # 预览模式：worktree 没被碰过，status 必然是干净的，不能拿它判断。
        # 改成直接比树差异（HEAD 对 main），这才是 apply 时会被带进来的内容。
        #
        # 注意：HEAD..main 里会包含那些"公开树独有、开发主干没有"的文件
        # （configs/global.example.yml、tools/*release*.py 等）—— 它们在 apply
        # 时会被 PUBLIC_EXTRAS 保护住**不删**，所以预览也必须过滤掉，
        # 否则会误报成"待提交 10 项"（2026-11-04 实踩）。
        diff = git(WT_PUBLIC, 'diff', '--name-status', 'HEAD', DEV_BRANCH, check=False)
        lines = []
        for l in diff.splitlines():
            if not l.strip():
                continue
            parts = l.split('\t')
            path = parts[-1]
            if path in PUBLIC_EXTRAS:
                continue        # 白名单文件，apply 时会保留，不算待同步
            lines.append(l)
        for p in to_delete:
            lines.append('D\t%s' % p)
        if not lines:
            print('  无变化')
            return 'nochange'
        changed = '\n'.join(lines)

    subject = git(PROD, 'log', '-1', '--format=%s', DEV_BRANCH)
    print('  待提交 %d 项（提交信息：%s）' % (len(changed.splitlines()), subject))
    if not apply:
        return 'pending'

    git(WT_PUBLIC, 'add', '-A')
    git(WT_PUBLIC, 'commit', '-q', '-m', subject)
    print('  已提交 %s' % git(WT_PUBLIC, 'log', '-1', '--format=%h %s'))
    ok, msg = git_push(WT_PUBLIC, 'origin',
                       '%s:%s' % (PUBLIC_LOCAL, PUBLIC_REMOTE))
    print('  push: %s' % (msg or ('ok' if ok else '未知错误')))
    return 'pushed' if ok else 'push_failed'


# ---------------------------------------------------------------- 私有仓库
def sync_full(apply):
    """让 full 分支 == 开发主干 + configs/.temp/状态库，并推送。"""
    print()
    print('【私有仓库】%s (%s)' % (WT_FULL, FULL_BRANCH))
    if not os.path.isdir(WT_FULL):
        print('  ! worktree 不存在，跳过')
        return None

    if not git(WT_FULL, 'status', '--porcelain'):
        behind = git(WT_FULL, 'rev-list', '--count', 'HEAD..' + DEV_BRANCH)
        if behind != '0':
            print('  落后 main %s 个提交' % behind)
            if apply:
                git(WT_FULL, 'merge', '--no-edit', DEV_BRANCH)
                print('  已合并 main')
        else:
            print('  代码已是最新')

    # 复用 sync_full_backup 的同步函数（configs / .temp / 状态库 / 待删清单）
    sys.path.insert(0, PROD)
    from tools.sync_full_backup import (sync_configs, sync_temp,
                                        sync_state_db, sync_logs)
    if apply:
        sync_configs()
        sync_temp()
        sync_state_db()
        sync_logs()

    changed = git(WT_FULL, 'status', '--porcelain')
    if not changed:
        print('  无变化')
        return 'nochange'

    print('  待提交 %d 项' % len(changed.splitlines()))
    if not apply:
        return 'pending'

    git(WT_FULL, 'add', '-A')
    git(WT_FULL, 'commit', '-q', '-m', '完整副本快照')
    print('  已提交 %s' % git(WT_FULL, 'log', '-1', '--format=%h %s'))
    ok, msg = git_push(WT_FULL, FULL_REMOTE, '%s:main' % FULL_BRANCH)
    print('  push: %s' % (msg or ('ok' if ok else '未知错误')))
    return 'pushed' if ok else 'push_failed'


def main():
    ap = argparse.ArgumentParser(description='同步并推送两个仓库')
    ap.add_argument('--apply', action='store_true', help='真做（默认只预览）')
    ap.add_argument('--allow-sensitive', action='store_true',
                    help='即使扫描到敏感信息（房间号/手机号/key/IP）也继续推送')
    ap.add_argument('--skip-scan', action='store_true',
                    help='跳过敏感扫描（不推荐）')
    args = ap.parse_args()

    if dev_dirty():
        print('main 工作区有未提交改动，先处理掉再推：')
        print(git(PROD, 'status', '--short'))
        return 1

    head = git(PROD, 'log', '-1', '--format=%h %s', DEV_BRANCH)
    print('main HEAD: %s' % head)
    print()

    # ── 公开仓库脱敏闸门（审查报告 S-2）──────────────────────────
    # 公开仓库是"整树同步"，没有任何脱敏过滤。这里在同步之前扫一遍，
    # 命中就要求人确认 —— 已经公开出去的内容需要改历史才能清理，所以
    # "先拦住"比"推完再发现"重要得多。
    if not args.skip_scan:
        hits = scan_sensitive()
        if hits:
            print('⚠️ 公开仓库将带入 %d 处敏感信息：' % len(hits))
            for rel, lineno, kind, frag in hits[:20]:
                print('   %-42s :%-5d %s  %s' % (rel, lineno, kind, frag))
            if len(hits) > 20:
                print('   ... 还有 %d 处' % (len(hits) - 20))
            if not args.allow_sensitive:
                print()
                print('已中止。处理方式二选一：')
                print('  1) 先清理上面这些文件（推荐）')
                print('  2) 确认可以公开，加 --allow-sensitive 重跑')
                print('注意：已经推到 origin/main 的内容需要改历史才能清除，'
                      '光删本地文件不够。')
                return 2
            print('（--allow-sensitive 已指定，继续）')
        else:
            print('✅ 敏感扫描通过（房间号/手机号/key/内网 IP 均未命中）')
        print()

    pub = sync_public(args.apply)
    full = sync_full(args.apply)

    print()
    if not args.apply:
        print('预览完成。加 --apply 才会提交并推送。')
        return 0
    print('完成：公开=%s  私有=%s' % (pub, full))
    if 'push_failed' in (pub, full):
        print('❌ 有仓库推送失败，请看上面的 push 输出。')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
