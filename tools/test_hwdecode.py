"""测试硬解 + subtitles 是否共存。

⚠️ 2026-11-16：配置修改改为**显式 --apply** 才执行。
   原来运行脚本就会往 DMR\\Config\\default.yml 里插 hw_retry 配置块，
   并以 LF 重写整个文件 —— 一个"测试脚本"不该有生产副作用。
   现在默认只做硬解测试，不碰配置。

用法：
    python tools\\test_hwdecode.py            # 只测试，不改任何配置
    python tools\\test_hwdecode.py --apply    # 顺带把降级参数写进 default.yml
"""
import os
import subprocess
import sys
import time

try:                                                             # GBK 控制台下 emoji 打印不崩
    from _guard import ensure_utf8_stdout as _ensure_utf8
    _ensure_utf8()
except Exception:                                                # noqa: BLE001
    pass

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录，自动识别（可放在任意路径）

APPLY = '--apply' in sys.argv


def patch_default_yml() -> None:
    """把降级参数插进 default.yml（保留原文件的行尾风格）。"""
    p = _PROJECT_ROOT + r'\DMR\Config\default.yml'
    with open(p, encoding='utf-8', newline='') as f:
        src = f.read()
    if 'hw_retry' in src:
        print('  default.yml: 已存在，跳过')
        return

    nl = '\r\n' if '\r\n' in src else '\n'
    lines = src.split(nl)
    block = [
        '    # ── 硬件编码失败时的处理（2026-10-18 魔改版新增）──────────────',
        '    # 硬件编码器（*_qsv/_nvenc/_vaapi/_amf）失败时，先用硬件重试，',
        '    # 全部失败才降级到软件编码器。0 = 不重试，直接降级。',
        '    hw_retry: 2',
        '    # 每次硬件重试前等待秒数（给占用 GPU 的程序让路）',
        '    hw_retry_delay: 60',
        '    # 降级用的软件编码器，置空(~)则不降级（libx265 / libx264）',
        '    fallback_vencoder: libx265',
        '    # 软件编码器参数。实测（60 秒 720x1280 素材）：',
        '    #   QSV gq30 7.0MB/958kbps/12.7s vs veryfast crf28 7.7MB/1053kbps/22.3s',
        "    fallback_vencoder_args: ['-preset', 'veryfast', '-crf', '28', '-tag:v', 'hvc1']",
    ]
    anchor = None
    for i, line in enumerate(lines):
        if line.strip().startswith('output_resize:'):
            anchor = i
            break
    if anchor is None:
        print('  default.yml: 没找到 output_resize 锚点')
        return
    lines[anchor + 1:anchor + 1] = block
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write(nl.join(lines))
    print(f'  default.yml: 已在 output_resize 后插入 {len(block)} 行')


def main() -> int:
    if APPLY:
        patch_default_yml()
    else:
        print('  default.yml: 未加 --apply，跳过配置修改（只做硬解测试）')

    os.chdir(_PROJECT_ROOT)
    src = r'直播回放\断尾人类\断尾的人类不会喵-2026年10月18日19点56分.mkv'
    dm = r'直播回放\断尾人类\断尾的人类不会喵-2026年10月18日19点56分.ass'
    out = r'.temp\_hwdec_test.mp4'

    if not os.path.exists(src):
        print(f'\n  ⚠️ 测试素材不存在，跳过硬解测试: {src}')
        print('     这是 2026-10-18 的某一场录制，可能已被清理。')
        print('     换一段本地 ts/mkv + 同名 .ass 即可测试。')
        return 0

    # 复刻 DMR 的转义：反斜杠→正斜杠，冒号→\:
    dm_escaped = os.path.abspath(dm).replace('\\', '/').replace(':', '\\:')
    vf = "subtitles=filename='%s'" % dm_escaped

    print()
    print('=' * 74)
    print('硬解 + subtitles 滤镜 共存测试（60 秒片段）')
    print('=' * 74)
    print(f'  vf = {vf}')

    cases = [
        ('① 软解（当前 global.yml 的 ~）', []),
        ('② 硬解 auto（default.yml 默认）', ['-hwaccel', 'auto']),
        ('③ 硬解 d3d11va', ['-hwaccel', 'd3d11va']),
        ('④ 硬解 qsv', ['-hwaccel', 'qsv']),
    ]

    results = []
    for label, hw in cases:
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        cmd = (['ffmpeg', '-y'] + hw +
               ['-fflags', '+discardcorrupt+genpts',
                '-analyzeduration', '60000000', '-probesize', '100000000',
                '-ss', '600', '-t', '60', '-i', src,
                '-vf', vf,
                '-c:v', 'hevc_qsv', '-preset', 'medium', '-global_quality:v', '30',
                '-c:a', 'aac', '-b:a', '96k', out])
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=900)
            el = time.time() - t0
            ok = (r.returncode == 0 and os.path.exists(out)
                  and os.path.getsize(out) > 50000)
            sz = os.path.getsize(out) / 1024 ** 2 if os.path.exists(out) else 0
            print(f'  {label:34} {"✅" if ok else "❌"}  {el:6.1f}s  {sz:5.1f} MB')
            if not ok:
                for line in reversed((r.stderr or '').split('\n')):
                    if any(k in line for k in ('rror', 'Invalid', 'No device', 'failed')):
                        print(f'       {line.strip()[:120]}')
                        break
            results.append((label, ok, el, sz))
        except subprocess.TimeoutExpired:
            print(f'  {label:34} ⏱ 超时')
            results.append((label, False, 900, 0))

    print()
    base = next((r for r in results if r[1] and '①' in r[0]), None)
    if base:
        print('  相对软解的速度:')
        for label, ok, el, sz in results:
            if ok and '①' not in label:
                print(f'    {label:34} {el/base[2]:.2f}x  体积 {sz/base[3]*100:.0f}%')
    if os.path.exists(out):
        os.remove(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
