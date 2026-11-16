"""分块渲染 + 拼接：绕过文件后段的坏数据。

已验证：03点39分.ts 的前 600 秒用 QSV 和 x265 带字幕都能正常渲染。
崩在更后面（源文件后段有坏帧，x265 会直接访问冲突）。

策略：
  · 每块 600 秒单独渲染（每块独立 ffmpeg 进程 -> 坏块最多毁自己）
  · 每块加 -err_detect ignore_err 跳过坏帧
  · 最后用 concat demuxer 拼成一个 mp4（-c copy，不重编码）
"""
import os
import subprocess
import sys

try:                                                             # GBK 控制台下 emoji 打印不崩
    from _guard import ensure_utf8_stdout as _ensure_utf8
    _ensure_utf8()
except Exception:                                                # noqa: BLE001
    pass

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录，自动识别（可放在任意路径）


os.chdir(_PROJECT_ROOT)

STEM = '哭哭不嘻嘻-2026年10月19日03点39分'
SRC = f'直播回放/哭哭不嘻嘻/{STEM}.ts'
ASS = f'./直播回放/哭哭不嘻嘻/{STEM}.ass'
OUTDIR = '直播回放/哭哭不嘻嘻（弹幕版）'
FINAL = os.path.join(OUTDIR, f'{STEM}（弹幕版）.mp4')
CHUNKDIR = '.temp/_chunks'
CHUNK = 600          # 每块秒数

QSV = ['-c:v', 'hevc_qsv', '-preset', 'medium', '-global_quality:v', '30',
       '-tag:v', 'hvc1']
X265 = ['-c:v', 'libx265', '-preset', 'veryfast', '-crf', '28', '-tag:v', 'hvc1']
AUD = ['-c:a', 'libopus', '-b:a', '96k']


def duration(p):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'default=nw=1:nk=1', p],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=300)
    try:
        return float((r.stdout or '0').strip() or 0)
    except ValueError:
        return 0.0


def main():
    os.makedirs(CHUNKDIR, exist_ok=True)
    os.makedirs(OUTDIR, exist_ok=True)

    total = duration(SRC)
    if total <= 0:
        print(f'  ❌ 读不出源时长: {SRC}')
        return 1
    n = int(total // CHUNK) + (1 if total % CHUNK else 0)
    print('=' * 78)
    print(f'分块渲染 {STEM}')
    print('=' * 78)
    print(f'  源时长 {total:.0f}s -> {n} 块 × {CHUNK}s')
    print()

    chunks = []
    for i in range(n):
        ss = i * CHUNK
        dur = min(CHUNK, total - ss)
        out = os.path.join(CHUNKDIR, f'c{i:03d}.mp4')
        chunks.append(out)
        if os.path.exists(out) and duration(out) >= dur * 0.9:
            print(f'  [{i+1}/{n}] {ss}s-{ss+dur}s  已存在，跳过')
            continue

        ok = False
        for enc_name, enc in (('qsv', QSV), ('x265', X265)):
            if os.path.exists(out):
                os.remove(out)
            cmd = (['ffmpeg', '-v', 'error', '-y',
                    '-ss', str(ss), '-t', str(dur),
                    '-err_detect', 'ignore_err',
                    '-fflags', '+discardcorrupt+genpts',
                    '-i', SRC,
                    # ⚠️ 2026-10-19 修：输入侧 -ss 会把 filter 看到的 PTS 归零，
                    #    于是 subtitles 滤镜**每一块都从 0 秒开始**取弹幕 ——
                    #    实测 showinfo：`-ss 600 -i file` 后首帧 pts_time=0，
                    #    第 2 块起弹幕全部错位（相当于把前 600 秒的弹幕重播一遍）。
                    #    修法：进滤镜前把 PTS 平移回绝对时间，滤镜后再归零，
                    #    这样字幕对得上，拼出来的整体时间轴也从 0 开始（concat 安全）。
                    '-vf', f"setpts=PTS+{ss}/TB,subtitles='{ASS}',setpts=PTS-STARTPTS",
                    '-noautoscale', *enc, *AUD,
                    '-movflags', '+faststart', out])
            t0 = __import__('time').time()
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=3600)
            el = __import__('time').time() - t0
            d = duration(out)
            if r.returncode == 0 and d >= dur * 0.85:
                print(f'  [{i+1}/{n}] {ss}s-{ss+dur}s  ✅ {enc_name}  '
                      f'{os.path.getsize(out)/1024**2:.1f} MB  {d:.0f}s  {el:.0f}s')
                ok = True
                break
            err = [l for l in (r.stderr or '').split('\n') if l.strip()]
            print(f'  [{i+1}/{n}] {ss}s-{ss+dur}s  ❌ {enc_name} '
                  f'exit={r.returncode} d={d:.0f}s  {(err[0][:80] if err else "")}')
        if not ok:
            print(f'       ⚠️ 这一块两种编码器都失败，将跳过（拼接时缺这一段）')

    good = [c for c in chunks if os.path.exists(c) and duration(c) > 0]
    print()
    print(f'  可用块: {len(good)}/{n}')
    if not good:
        print('  ❌ 没有可用块')
        return 1

    lst = os.path.join(CHUNKDIR, 'list.txt')
    with open(lst, 'w', encoding='utf-8') as f:
        for c in good:
            f.write(f"file '{os.path.abspath(c).replace(chr(92), '/')}'\n")

    # ⚠️ 不能先删 FINAL：FINAL 是「（弹幕版）」成品，属项目红线（永不删除）。
    #    先拼到临时名，校验通过才原子替换，拼接失败时旧成品原样保留。
    tmp_final = FINAL + '.partial.mp4'
    if os.path.exists(tmp_final):
        os.remove(tmp_final)
    print(f'  拼接 -> {FINAL}（先写临时文件）')
    r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat',
                        '-safe', '0', '-i', lst, '-c', 'copy',
                        '-movflags', '+faststart', tmp_final],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=1800)
    if r.returncode != 0:
        print(f'  ❌ 拼接失败: {(r.stderr or "")[:300]}')
        print(f'  旧成品未改动: {FINAL}' if os.path.exists(FINAL) else '  （此前没有旧成品）')
        try:
            if os.path.exists(tmp_final):
                os.remove(tmp_final)
        except OSError:
            pass
        return 1

    d = duration(tmp_final)
    sz = os.path.getsize(tmp_final) / 1024**2
    print()
    print('=' * 78)
    print(f'✅ 完成  {sz:.1f} MB  时长 {d:.0f}s / 源 {total:.0f}s = {d/total*100:.1f}%')
    print('=' * 78)

    # 完整性验证：能完整解码吗
    r2 = subprocess.run(['ffmpeg', '-v', 'error', '-i', tmp_final, '-f', 'null', '-'],
                        capture_output=True, text=True, encoding='utf-8',
                        errors='replace', timeout=1800)
    nerr = len([l for l in (r2.stderr or '').split('\n') if l.strip()])
    print(f'  解码校验: exit={r2.returncode} 错误行 {nerr}  '
          f'{"✅ 可正常播放" if r2.returncode == 0 else "❌ 有问题"}')
    if r2.returncode != 0:
        try:
            os.remove(tmp_final)
        except OSError:
            pass
        print('  校验未通过，临时文件已删除，旧成品保留。')
        return 1
    os.replace(tmp_final, FINAL)
    print(f'  已替换成品: {FINAL}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
