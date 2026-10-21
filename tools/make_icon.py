# -*- coding: utf-8 -*-
r"""生成 NInfer 的图标：桌面用多尺寸 .ico + 托盘用四个状态 PNG。

用法:
    python make_icon.py preview           # 三方案对比预览 -> .workbuddy/assets/icon-preview.png
    python make_icon.py apply A|B|C       # 应用某方案 -> ninfer.ico + tray-*.png

设计要点（为什么要这么写）:
  * 所有绘制在 4 倍超采样画布上完成，最后 LANCZOS 缩回目标尺寸 —— PIL 没有原生
    抗锯齿，这是唯一能得到干净边缘的办法。
  * **笔画粗细按目标尺寸自适应**：16 px 下按比例算只有 1.5 px 宽，必然糊成一团；
    小尺寸要"更粗更满"才和 256 px 有相同的视觉重量。
  * **ICO 手工组装**：Pillow 存 ICO 是"从大图缩出各尺寸"，等于小尺寸白做。这里
    每个尺寸单独渲染，再按 ICO 容器格式（PNG 压缩帧）拼起来。
"""
import os
import shutil
import struct
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

def _find_root():
    """定位项目根：环境变量 > 标记文件探测 > 兜底上一级。不依赖任何硬编码路径。"""
    _env = os.environ.get("NINFER_ROOT")
    if _env and os.path.exists(os.path.join(_env, "start-mtp-slim.bat")):
        return os.path.abspath(_env)
    _cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        _cur = os.path.dirname(_cur)
        if os.path.exists(os.path.join(_cur, "start-mtp-slim.bat")):
            return _cur
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT = _find_root()
def _find_assets(root):
    """图标目录：优先包内 assets/，回退 .workbuddy/assets/；都没有就建前者。"""
    for _rel in (("assets",), (".workbuddy", "assets")):
        _p = os.path.join(root, *_rel)
        if os.path.isdir(_p):
            return _p
    _p = os.path.join(root, "assets")
    os.makedirs(_p, exist_ok=True)
    return _p


ASSETS = _find_assets(PROJECT)
ICO_OUT = os.path.join(PROJECT, "ninfer.ico")
FONT_BOLD = r"C:\Windows\Fonts\segoeuib.ttf"      # Segoe UI Bold（西文/数字）
FONT_CN = r"C:\Windows\Fonts\msyhbd.ttc"           # 微软雅黑 Bold（预览图里的中文）

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
TRAY_PX = 64

SS_MIN = 320          # 超采样画布下限（小尺寸也按 4 倍画）

# 三个方案的配色
SCHEMES = {
    "A": {
        "name": "霓虹 N（深色底 · 发光笔画）",
        "bg": ((26, 40, 68), (8, 13, 26)),        # 顶 -> 底
        "n": ((52, 211, 253), (74, 139, 255)),    # 青 -> 蓝
        "glow": (34, 211, 238),
        "node": (236, 254, 255),
        "border": (60, 84, 128),
        "style": "stroke",
    },
    "B": {
        "name": "极光渐变（蓝紫底 · 白色 N）",
        "bg": ((59, 130, 246), (139, 92, 246)),
        "n": ((255, 255, 255), (255, 255, 255)),
        "glow": (255, 255, 255),
        "node": (255, 255, 255),
        "border": None,
        "style": "font",
    },
    "C": {
        "name": "极简（石墨底 · 纯白 N）",
        "bg": ((45, 52, 66), (24, 28, 38)),
        "n": ((255, 255, 255), (255, 255, 255)),
        "glow": None,
        "node": None,
        "border": None,
        "style": "font",
    },
}

# 托盘四个状态的 N 颜色（形状沿用方案 A 的笔画风格，只换色）
TRAY_ACCENT = {
    "starting": (232, 163, 61),
    "ready": (74, 139, 255),
    "failed": (229, 72, 77),
    "stopped": (128, 138, 150),
}


# ------------------------------------------------------------------ 基础工具
def params_for(px):
    """按目标尺寸给出 N 的几何参数（占边长的比例）。

    小尺寸放大字号、加粗笔画 —— 补偿缩放带来的视觉变细。
    """
    if px <= 20:
        return dict(w=0.205, wsp=0.50, hsp=0.54, fs=0.72)
    if px <= 28:
        return dict(w=0.180, wsp=0.48, hsp=0.52, fs=0.70)
    if px <= 40:
        return dict(w=0.155, wsp=0.465, hsp=0.50, fs=0.68)
    if px <= 72:
        return dict(w=0.135, wsp=0.45, hsp=0.48, fs=0.66)
    return dict(w=0.118, wsp=0.44, hsp=0.465, fs=0.645)


def rounded_mask(size, radius_ratio=0.225):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    r = max(2, int(size * radius_ratio))
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=255)
    return m


def v_gradient(size, c1, c2):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        g.putpixel((0, y), tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return g.resize((size, size), Image.BILINEAR)


def d_gradient(size, c1, c2):
    """对角渐变：先在小画布上算好再放大，比逐像素快几个数量级。"""
    s = 256
    g = Image.new("RGB", (s, s))
    px = g.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2.0 * (s - 1))
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return g.resize((size, size), Image.BICUBIC)


def hex2rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ------------------------------------------------------------------ 绘制 N
def stroke_n_mask(ss, prm, cx=None, cy=None):
    """方案 A 的笔画 N：三条圆头粗线 + 端点节点。返回 (mask, 节点列表)。"""
    cx = ss / 2 if cx is None else cx
    cy = ss / 2 if cy is None else cy
    w = prm["w"] * ss
    half_w = prm["wsp"] * ss / 2
    half_h = prm["hsp"] * ss / 2

    x1, x2 = cx - half_w, cx + half_w
    y0, y1 = cy - half_h, cy + half_h

    m = Image.new("L", (ss, ss), 0)
    d = ImageDraw.Draw(m)
    for a, b in (((x1, y0), (x1, y1)),
                 ((x1, y0), (x2, y1)),
                 ((x2, y0), (x2, y1))):
        d.line([a, b], fill=255, width=int(round(w)))
    # 圆头：line 的端点是方的，补四个圆
    r = w / 2.0
    heads = [(x1, y0), (x1, y1), (x2, y0), (x2, y1)]
    for (px_, py_) in heads:
        d.ellipse((px_ - r, py_ - r, px_ + r, py_ + r), fill=255)
    return m, heads, w


def font_n_mask(ss, prm, cx=None, cy=None):
    """方案 B/C 的字体 N。返回 (mask, [], 0)。"""
    cx = ss / 2 if cx is None else cx
    cy = ss / 2 if cy is None else cy
    m = Image.new("L", (ss, ss), 0)
    d = ImageDraw.Draw(m)
    f = ImageFont.truetype(FONT_BOLD, int(ss * prm["fs"]))
    box = f.getbbox("N")
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text((cx - tw / 2.0 - box[0], cy - th / 2.0 - box[1]), "N", font=f, fill=255)
    return m, [], 0


# ------------------------------------------------------------------ 单尺寸渲染
def render(px, scheme_key, n_colors=None):
    """渲染一个 px x px 的图标。

    n_colors 传入时覆盖方案自带的 N 配色（托盘按状态换色用）。
    """
    sc = SCHEMES[scheme_key]
    prm = params_for(px)
    ss = max(SS_MIN, px * 4)

    c1, c2 = sc["bg"]
    base = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    grad = v_gradient(ss, c1, c2) if sc["style"] == "stroke" else d_gradient(ss, c1, c2)
    base.paste(grad, (0, 0), rounded_mask(ss))

    # 内描边：深色底在深色壁纸上会糊成一片，加一圈亮边把轮廓提出来
    if sc["border"]:
        bd = ImageDraw.Draw(base)
        r = max(2, int(ss * 0.225))
        bd.rounded_rectangle((ss * 0.012, ss * 0.012, ss * 0.988, ss * 0.988),
                             radius=r, outline=sc["border"] + (110,),
                             width=max(1, int(ss * 0.010)))

    n1, n2 = n_colors if n_colors else sc["n"]

    if sc["style"] == "stroke":
        nmask, heads, w = stroke_n_mask(ss, prm)
    else:
        nmask, heads, w = font_n_mask(ss, prm)

    # 外发光（N 的柔光晕），深色底上"通电"的观感就靠它
    if sc.get("glow"):
        gm = nmask.filter(ImageFilter.GaussianBlur(ss * 0.030))
        glow = Image.new("RGBA", (ss, ss), sc["glow"] + (255,))
        glow.putalpha(gm.point(lambda v: int(v * 0.80)))
        base.alpha_composite(glow)

    # N 本体：用渐变填充 mask
    ngrad = v_gradient(ss, n1, n2)
    nlayer = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    nlayer.paste(ngrad, (0, 0), nmask)
    base.alpha_composite(nlayer)

    # 端点节点（只有笔画方案有）
    if heads and sc.get("node"):
        nd = ImageDraw.Draw(base)
        r = w * 0.40
        for (hx, hy) in heads:
            nd.ellipse((hx - r, hy - r, hx + r, hy + r),
                       fill=sc["node"] + (255,))

    # 玻璃高光：柔和的顶部反射（早先用多边形做过，硬边三角很明显，很难看）
    hi_alpha = 0.10 if sc["style"] == "stroke" else 0.17
    hi = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(hi).ellipse(
        (-ss * 0.30, -ss * 0.78, ss * 1.30, ss * 0.50), fill=255)
    hi = hi.filter(ImageFilter.GaussianBlur(ss * 0.060))
    hlayer = Image.new("RGBA", (ss, ss), (255, 255, 255, 255))
    hlayer.putalpha(hi.point(lambda v: int(v * hi_alpha)))
    base.alpha_composite(hlayer)

    # 裁剪回圆角（发光/高光会溢出到方块外）
    a = ImageChops.multiply(base.getchannel("A"), rounded_mask(ss))
    base.putalpha(a)

    return base.resize((px, px), Image.LANCZOS)


# ------------------------------------------------------------------ ICO 组装
def write_ico(path, images):
    """手工拼 ICO：每帧用 PNG 压缩（Vista+ 支持，Win10/11 原生）。

    Pillow 的 ICO 保存只按 sizes 从一张图缩，小尺寸等于白渲染 —— 所以自己拼。
    """
    frames = []
    for im in images:
        import io
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        frames.append((im.width, im.height, buf.getvalue()))

    n = len(frames)
    header = struct.pack("<HHH", 0, 1, n)
    offset = 6 + 16 * n
    entries, blobs = b"", b""
    for (w, h, data) in frames:
        entries += struct.pack("<BBBBHHII",
                               0 if w >= 256 else w,
                               0 if h >= 256 else h,
                               0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + blobs)


def apply_scheme(key):
    os.makedirs(ASSETS, exist_ok=True)
    imgs = [render(s, key) for s in ICO_SIZES]
    write_ico(ICO_OUT, imgs)
    print("ico  -> %s (%d bytes, %d frames)"
          % (ICO_OUT, os.path.getsize(ICO_OUT), len(imgs)))

    # 托盘：沿用方案 A 的笔画形状，只把 N 换成状态色（桌面与托盘同一套视觉语言）
    for phase, accent in TRAY_ACCENT.items():
        light = tuple(min(255, c + 55) for c in accent)
        img = render(TRAY_PX, "A", n_colors=(accent, light))
        p = os.path.join(ASSETS, "tray-%s.png" % phase)
        img.save(p)
        print("tray -> %s (%d px)" % (p, TRAY_PX))
    return True


# ------------------------------------------------------------------ 预览图
def build_preview():
    os.makedirs(ASSETS, exist_ok=True)
    W, PAD, BAND_H, COL_W = 1000, 24, 210, 316
    LIGHT = hex2rgb("F1F4F9")
    DARK = hex2rgb("10151F")
    total_h = PAD * 2 + 34 * 2 + BAND_H * 2 + 18
    canvas = Image.new("RGB", (W, total_h), hex2rgb("20242E"))
    d = ImageDraw.Draw(canvas)
    try:
        f_title = ImageFont.truetype(FONT_CN, 21)
        f_name = ImageFont.truetype(FONT_CN, 15)
        f_small = ImageFont.truetype(FONT_BOLD, 13)
    except Exception:
        f_title = f_name = f_small = ImageFont.load_default()

    ttop = [PAD, PAD + 34 + BAND_H + 18]
    d.text((PAD, ttop[0]), "NInfer 图标候选 · 放在浅色壁纸上", font=f_title,
           fill=(240, 244, 250))
    d.text((PAD, ttop[1]), "NInfer 图标候选 · 放在深色壁纸上", font=f_title,
           fill=(240, 244, 250))

    for band, bgcol in ((0, LIGHT), (1, DARK)):
        top = ttop[band] + 34
        d.rectangle((PAD, top, W - PAD, top + BAND_H), fill=bgcol)
        for i, key in enumerate(("A", "B", "C")):
            x = PAD + i * COL_W
            fg = (30, 36, 48) if band == 0 else (225, 232, 242)
            d.text((x + 4, top + 10), "%s · %s" % (key, SCHEMES[key]["name"]),
                   font=f_name, fill=fg)
            big = render(128, key)
            canvas.paste(big, (x + 4, top + 40), big)
            x2 = x + 144
            for s in (48, 32, 24, 16):
                im = render(s, key)
                canvas.paste(im, (x2, top + 40 + (128 - s) // 2), im)
                x2 += s + 10
            d.text((x + 144, top + 176), "48 / 32 / 24 / 16 px", font=f_small, fill=fg)

    out = os.path.join(ASSETS, "icon-preview.png")
    canvas.save(out)
    print("preview ->", out, os.path.getsize(out), "bytes")
    return out


if __name__ == "__main__":
    act = sys.argv[1] if len(sys.argv) > 1 else "preview"
    if act == "preview":
        build_preview()
    elif act == "apply":
        apply_scheme(sys.argv[2] if len(sys.argv) > 2 else "A")
    else:
        print(__doc__)
