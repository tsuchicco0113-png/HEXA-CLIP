#!/usr/bin/env python3
"""HEXA CLIP のランチャー用アイコンを生成する

usage: python3 make_icon.py OUT.png [SIZE | REFERENCE.png]
  REFERENCE.png を渡すと、そのPNGと同じサイズで作る(既存アイコンに合わせる)
"""
import sys

from PIL import Image, ImageDraw


def make(size):
    s = size * 4                                    # 4倍で描いて縮小(アンチエイリアス)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 100
    d.rounded_rectangle((4 * u, 4 * u, 96 * u, 96 * u), 20 * u, fill=(22, 26, 34, 255))
    green = (47, 214, 123, 255)
    # クリップボード
    d.rounded_rectangle((22 * u, 20 * u, 78 * u, 86 * u), 8 * u, outline=green, width=int(6 * u))
    d.rounded_rectangle((36 * u, 12 * u, 64 * u, 28 * u), 5 * u, fill=green)
    # テキスト行
    for y, w in ((42, 40), (54, 32), (66, 22)):
        d.rounded_rectangle((32 * u, y * u, (32 + w) * u, (y + 5) * u), 2.5 * u,
                            fill=(232, 234, 240, 255))
    # カメラレンズ
    d.ellipse((58 * u, 58 * u, 84 * u, 84 * u), fill=(22, 26, 34, 255), outline=green, width=int(5 * u))
    d.ellipse((67 * u, 67 * u, 75 * u, 75 * u), fill=green)
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
