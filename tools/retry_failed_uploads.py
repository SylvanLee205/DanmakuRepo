"""CD2 已重启，补传之前失败的文件。

⚠️ 2026-11-16 按审查报告 L-19 重写三处：
   ① 云端目标名必须过 ``safe_remote_name``（含 emoji/特殊字符的文件上传时被
      改名，补传若用原名，CD2/WebDAV 会 errno -7 但 rclone 仍返回 0 → 静默丢失）。
   ② 字节核对改用 ``rclone lsjson --stat`` 直查远端对象，不再用 ``lsf`` 的
      目录列表（历史上 lsf 会返回幻影条目，把没传上去的文件报成"已存在"）。
   ③ 按任务自己的 upload_args 分流：只传网盘的任务才补传到网盘，
      B 站任务/R2 任务不再被无脑推到百度网盘。
"""
import json
import os
import subprocess
import time

try:                                                             # GBK 控制台下 emoji 打印不崩
    from _guard import ensure_utf8_stdout as _ensure_utf8
    _ensure_utf8()
except Exception:                                                # noqa: BLE001
    pass


def _tool(name):
    """定位外部工具：环境变量 -> tools/ -> PATH -> 注册表 PATH -> 安装目录。

    见 DMR/utils/toolsmgr.py。不写死版本号和安装位置 ——
    换个 rclone 版本、换台电脑都不用改代码。
    """
    import os as _o
    import sys as _s
    _root = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
    if _root not in _s.path:
        _s.path.insert(0, _root)
    from DMR.utils.toolsmgr import find_tool
    return find_tool(name) or name

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录，自动识别（可放在任意路径）


UP = _PROJECT_ROOT
RCLONE = _tool('rclone')
os.chdir(UP)

# 复用上传层的清洗逻辑：审计/补传判断必须和真正上传用同一套规则，
# 否则含 emoji 的文件（上传时被改名）会被永远误报为"云端没有"。
try:
    import sys as _sys
    if UP not in _sys.path:
        _sys.path.insert(0, UP)
    from DMR.Uploader.subprocess_uploader import safe_remote_name
except Exception:                                                # noqa: BLE001
    def safe_remote_name(name):
        return name

REMOTE_ROOT = 'cd2:百度网盘/DMR录播'

print('=' * 78)
print('1. 云端连通确认')
print('=' * 78)


r = subprocess.run([RCLONE, 'lsd', REMOTE_ROOT, '--max-depth', '1'],
                   capture_output=True, text=True, encoding='utf-8',
                   errors='replace', timeout=300)
print(f'  云端连通: exit={r.returncode}  '
      f'{len([x for x in r.stdout.split(chr(10)) if x.strip()])} 个目录')
if r.returncode != 0:
    print(f'    {(r.stderr or "")[:300]}')
    raise SystemExit('云端不可达，先别补传')


def _task_upload_rules(taskname):
    """读出该任务实际生效的 upload_args 规则列表 [(key, rule), ...]。

    读不到配置时返回空列表（调用方按"不确定"处理，仍然补传）。
    """
    try:
        from DMR.Config import Config
        cfg = Config(os.path.join(UP, 'configs', 'global.yml'))
        rc = cfg.get_replay_config(taskname) or {}
        args = rc.get('upload_args') or {}
    except Exception as e:                                       # noqa: BLE001
        print(f'      ⚠ 读取任务 {taskname} 的上传配置失败（{type(e).__name__}），按网盘任务处理')
        return []
    rules = []
    for key, val in args.items():
        for rule in (val or []):
            if isinstance(rule, dict):
                rules.append((key, rule))
    return rules


def _rule_targets_disk(rule) -> bool:
    """该条上传规则是否把文件传到网盘（命令里出现 cd2: remote）。"""
    cmd = rule.get('command') or []
    if isinstance(cmd, str):
        return 'cd2:' in cmd
    return any('cd2:' in str(x) for x in cmd)


def _remote_size(dest):
    """直查远端对象大小（字节）。不存在返回 None。

    用 lsjson --stat 而不是 lsf：lsf 列目录在 CD2 时代会返回幻影条目。
    """
    try:
        rr = subprocess.run([RCLONE, 'lsjson', '--stat', dest],
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=300)
        if rr.returncode != 0:
            return None
        j = json.loads(rr.stdout or '{}')
        return int(j.get('Size')) if j.get('Size') is not None else None
    except Exception:                                            # noqa: BLE001
        return None


print()
print('=' * 78)
print('2. 失败队列里的文件，逐个补传')
print('=' * 78)
p = os.path.join(UP, '.temp', 'failed_uploads.json')
if not os.path.exists(p):
    raise SystemExit(f'没有失败上传清单：{p}')
d = json.loads(open(p, encoding='utf-8', errors='replace').read())

uploaded, failed, skipped = [], [], []
for uuid, task in d.items():
    files = task.get('files') or []
    src = task.get('source') or ''
    # source 形如 'replay/掉了颗兔牙' → 任务名
    taskname = src.split('/', 1)[1] if '/' in src else src
    # 按任务自己的上传规则分流：不传网盘的任务不该被推到百度网盘
    rules = _task_upload_rules(taskname)
    if rules and not any(_rule_targets_disk(rule) for _, rule in rules):
        print(f'\n  --- 任务 {taskname} 的上传目标不含网盘（规则: '
              f'{", ".join(k for k, _ in rules)}），跳过')
        skipped.extend(f.get('path') if isinstance(f, dict) else str(f) for f in files)
        continue
    for f in files:
        fp = f.get('path') if isinstance(f, dict) else str(f)
        if not fp:
            continue
        # 修掉相对路径里混用的反斜杠（Windows 能处理 /，但混用会出问题）
        real = os.path.normpath(os.path.join(UP, fp.lstrip('./\\')))
        remote_name = safe_remote_name(os.path.basename(real))
        print(f'\n  --- {os.path.basename(fp)}')
        print(f'      任务名    : {taskname}')
        print(f'      实际路径  : {real}')
        print(f'      存在      : {os.path.exists(real)}')
        if not os.path.exists(real):
            print(f'      ⚠ 找不到文件，跳过')
            failed.append(fp)
            continue
        size = os.path.getsize(real)
        print(f'      大小      : {size} 字节 ({size/1024/1024:.1f} MB)')
        dest = f'{REMOTE_ROOT}/{taskname}/{remote_name}'
        print(f'      目标      : {dest}')
        # 用 copyto 明确指定目标名，避免受云端目录解析影响
        rr = subprocess.run([RCLONE, 'copyto', real, dest, '--retries', '5',
                             '--low-level-retries', '10'],
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=3600)
        print(f'      rclone exit={rr.returncode}')
        if rr.returncode != 0:
            print(f'        {(rr.stderr or "")[:400]}')
            failed.append(fp)
        else:
            uploaded.append((real, size, dest))

print()
print('=' * 78)
print('3. 等 60 秒后逐个核对云端对象大小')
print('=' * 78)
time.sleep(60)
ok = 0
for real, size, dest in uploaded:
    cs = _remote_size(dest)
    nm = dest.rsplit('/', 1)[-1]
    if cs == size:
        print(f'  ✅ {nm}  字节一致 ({size})')
        ok += 1
    elif cs is not None:
        print(f'  ⚠ {nm}  云端 {cs} vs 本地 {size}')
    else:
        print(f'  ❌ {nm}  云端查不到（rclone 报成功但对象不存在 —— 名称或路径被拒）')

print()
print('=' * 78)
print(f'补传结果: {ok}/{len(uploaded)} 成功　跳过 {len(skipped)} 个（非网盘任务）')
if failed:
    print(f'仍有 {len(failed)} 个未处理:')
    for f in failed:
        print(f'  {f}')
print('=' * 78)
