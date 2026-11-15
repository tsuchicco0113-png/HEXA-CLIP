#!/usr/bin/env python3
"""HEXA CLIP のランチャー用アイコンを生成する

usage: python3 make_icon.py OUT.png [SIZE | REFERENCE.png]
  REFERENCE.png を渡すと、そのPNGと同じサイズで作る(既存アイコンに合わせる)
"""
import math
import sys

from PIL import Image, ImageDraw, ImageFilter


def hexpts(cx, cy, r):
    return [(cx + r * math.cos(math.radians(60 * i + 30)), cy + r * math.sin(math.radians(60 * i + 30)))
            for i in range(6)]


def make(size):
    """HEXA STAGE 風: 光る緑の六角形の中にクリップボード"""
    s = size * 4                                    # 4倍で描いて縮小(アンチエイリアス)
    u = s / 100
    green = (47, 214, 123, 255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # 外側の光
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pts = hexpts(50 * u, 50 * u, 44 * u)
    ImageDraw.Draw(glow).line(pts + [pts[0]], fill=(47, 214, 123, 200), width=int(10 * u))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4 * u)))
    d = ImageDraw.Draw(img)
    d.polygon(hexpts(50 * u, 50 * u, 44 * u), fill=(16, 22, 26, 255))
    d.line(pts + [pts[0]], fill=green, width=int(4 * u), joint="curve")
    # クリップボード
    d.rounded_rectangle((31 * u, 27 * u, 69 * u, 76 * u), 5 * u, outline=green, width=int(4 * u))
    d.rounded_rectangle((41 * u, 21 * u, 59 * u, 31 * u), 3 * u, fill=green)
    for y, w in ((40, 26), (50, 20), (60, 14)):
        d.rounded_rectangle((38 * u, y * u, (38 + w) * u, (y + 4) * u), 2 * u, fill=(232, 234, 240, 255))
    return img.resize((size, size), Image.LANCZOS)


def main():
    out = sys.argv[1]
    size = 64
    if len(sys.argv) > 2:
        arg = sys.argv[2]
        if arg.lower().endswith(".png"):
            try:
                size = Image.open(arg).size[0]
            except OSError:
                pass
        else:
            size = int(arg)
    make(size).save(out)
    print(f"icon {size}x{size} -> {out}")


if __name__ == "__main__":
    main()
