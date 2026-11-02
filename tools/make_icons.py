"""生成桌面快捷方式用的图标（.ico）。

为什么自己做图标，而不是用系统图标库
----------------------------------
`.lnk` 的 `IconLocation` 指向 `shell32.dll,<索引>`。但那些索引：
    * **含义随 Windows 版本变** —— 3 在 Win10 是"文件夹"，换个版本可能是别的
    * **数量有限，找不到"录制/体检/推送"这种语义**
第一次做快捷方式时我随手填了 `shell32.dll,3`，结果 7 个图标全变成文件夹 ——
正是因为这个。

自己画的好处：语义明确、颜色区分、跨 Windows 版本一致、不依赖系统资源。

产物：``assets/icons/*.ico``（每张含 256/64/48/32/16 五个尺寸）

用法：
    python tools\\make_icons.py
由 ``install_desktop_links.py`` 自动调用（图标不存在时）。
"""
import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    print('需要 Pillow：pip install pillow')
    sys.exit(1)

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(PROJ, 'assets', 'icons')

#: 超采样画布尺寸（先画大图再缩，边缘才平滑）
S = 1024
#: 最终尺寸
FINAL = 256

WHITE = (255, 255, 255, 255)

#: 名称 -> (背景色, 符号绘制函数)
#: 符号函数接收 (draw, 画布尺寸)，坐标都用 0~1 的比例，方便随尺寸缩放。
def _play(d, s):
    d.polygon([(0.37 * s, 0.27 * s), (0.37 * s, 0.73 * s), (0.75 * s, 0.5 * s)],
              fill=WHITE)


def _stop(d, s):
    d.rounded_rectangle([0.34 * s, 0.34 * s, 0.66 * s, 0.66 * s],
                        radius=0.025 * s, fill=WHITE)


def _log(d, s):
    for i, w in enumerate((0.46, 0.38, 0.42)):
        y = (0.30 + i * 0.17) * s
        d.rounded_rectangle([0.28 * s, y, (0.28 + w) * s, y + 0.075 * s],
                            radius=0.035 * s, fill=WHITE)


def _add(d, s):
    t = 0.085 * s          # 线宽
    c = 0.5 * s
    ln = 0.24 * s
    d.rounded_rectangle([c - t, c - ln, c + t, c + ln], radius=t, fill=WHITE)
    d.rounded_rectangle([c - ln, c - t, c + ln, c + t], radius=t, fill=WHITE)


def _check(d, s):
    d.line([(0.28 * s, 0.52 * s), (0.44 * s, 0.68 * s), (0.73 * s, 0.34 * s)],
           fill=WHITE, width=int(0.10 * s), joint='curve')


def _health(d, s):
    """体检：心电图折线。

    原来画的是"十字"，结果和"添加主播"的加号几乎一样 —— 缩到 32px 根本分不清。
    心电图既贴合"体检"语义，形状也和其他图标完全不同。
    """
    pts = [(0.20, 0.52), (0.35, 0.52), (0.43, 0.30),
           (0.55, 0.72), (0.63, 0.52), (0.80, 0.52)]
    d.line([(x * s, y * s) for x, y in pts],
           fill=WHITE, width=int(0.085 * s), joint='curve')


def _upload(d, s):
    """推送：向上的箭头 + 底座。"""
    d.polygon([(0.5 * s, 0.24 * s), (0.72 * s, 0.48 * s), (0.58 * s, 0.48 * s),
               (0.58 * s, 0.64 * s), (0.42 * s, 0.64 * s), (0.42 * s, 0.48 * s),
               (0.28 * s, 0.48 * s)], fill=WHITE)
    d.rounded_rectangle([0.28 * s, 0.72 * s, 0.72 * s, 0.79 * s],
                        radius=0.035 * s, fill=WHITE)


def _restart(d, s):
    """重启：带箭头的圆弧（顺时针循环箭头）。"""
    w = int(0.085 * s)
    box = [0.27 * s, 0.27 * s, 0.73 * s, 0.73 * s]
    # 从左下顺时针画到右上，留一个缺口给箭头
    d.arc(box, start=45, end=320, fill=WHITE, width=w)
    # 缺口处的箭头（指向顺时针方向）
    d.polygon([(0.72 * s, 0.20 * s), (0.74 * s, 0.45 * s), (0.49 * s, 0.35 * s)],
              fill=WHITE)


ICONS = {
    'start':   ((0x22, 0xC5, 0x5E), _play),      # 绿
    'stop':    ((0xEF, 0x44, 0x44), _stop),      # 红
    'restart': ((0x0E, 0xA5, 0xE9), _restart),   # 亮蓝
    'log':     ((0x3B, 0x82, 0xF6), _log),       # 蓝
    'add':     ((0x06, 0xB6, 0xD4), _add),       # 青
    'check':   ((0xF5, 0x9E, 0x0B), _check),     # 橙
    'doctor':  ((0x8B, 0x5C, 0xF6), _health),    # 紫
    'push':    ((0x63, 0x66, 0xF1), _upload),    # 靛
}


def build_one(name, color, symbol):
    rgb = (color[0], color[1], color[2], 255)
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = int(S * 0.055)                 # 外边距
    r = int(S * 0.22)                  # 圆角
    d.rounded_rectangle([m, m, S - m, S - m], radius=r, fill=rgb)
    symbol(d, S)

    small = img.resize((FINAL, FINAL), Image.LANCZOS)
    path = os.path.join(OUT_DIR, f'{name}.ico')
    small.save(path, format='ICO',
               sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
    return path


def build_all(force=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    made = []
    for name, (color, symbol) in ICONS.items():
        path = os.path.join(OUT_DIR, f'{name}.ico')
        if not force and os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        made.append(build_one(name, color, symbol))
    return made


if __name__ == '__main__':
    force = '--force' in sys.argv
    got = build_all(force=force)
    if got:
        print(f'生成 {len(got)} 个图标:')
        for g in got:
            print(f'  {os.path.relpath(g, PROJ)}  ({os.path.getsize(g)} bytes)')
    else:
        print('图标已存在，加 --force 强制重建')
