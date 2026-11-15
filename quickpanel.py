"""HEXA CLIP クイックパネル — どのアプリの上でも fn+H で出す小さな履歴パネル

CardputerZero にはウィンドウの仕組みが無い (ランチャーもCLIも画面全体を直接描く) ので、
次の順で「上に重ねた」ように見せる:

  1. キーボードを独占 (下のアプリに矢印や ENTER が届かないように)
  2. ランチャーを一時停止 (CLI もランチャーが描いているので、これで描き直しが止まる)
  3. いまの画面を保存して、その上にパネルを描く
  4. 閉じたら画面を元に戻し、ランチャーを再開、キーボードを返す

czpasted (fn+V の常駐プログラム) から呼ばれる。ENTER で選んだ本文を返し、
czpasted がそれを元のアプリに打ち込む。受信の帯 (banner) も同じ仕組み。
"""
import os
import signal
import time

import numpy as np
from PIL import Image, ImageDraw

import ocr_cam as oc

LAUNCHER = "M5CardputerZero-APPLaunch"
PANEL_TIMEOUT = 60                     # 何も押さなければ自動で閉じる (秒)


# ---------------------------------------------------------------- まわりの状態
def _cmdlines():
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            yield int(pid), open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            pass


def launcher_pids():
    return [pid for pid, cmd in _cmdlines() if LAUNCHER in cmd and "pkill" not in cmd]


def app_running():
    """HEXA CLIP 本体アプリが起動中か (その時はアプリ側で表示するので、ここでは何もしない)"""
    return any("ocr_cam.py" in cmd for _, cmd in _cmdlines())


def resume_launcher():
    for pid in launcher_pids():
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError:
            pass


# ---------------------------------------------------------------- 画面を借りる
class Overlay:
    """with Overlay() as ov: の間、ランチャーを止めて画面を借りる。抜ける時に必ず元に戻す"""

    def __init__(self, dev="/dev/fb0"):
        self.fb = oc.FrameBuffer(dev)
        self.pids = []
        self.saved = None

    def __enter__(self):
        self.pids = launcher_pids()
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:
                pass
        time.sleep(0.03)                   # 描きかけの1枚が終わるのを待つ
        size = self.fb.stride * oc.LCD_H
        self.fb.f.seek(0)
        self.saved = self.fb.f.read(size)
        return self

    def background(self):
        """保存した画面を画像にする (パネルの後ろを暗くして見せる用)"""
        bpp, row = self.fb.bpp, oc.LCD_W * self.fb.bpp // 8
        raw = b"".join(self.saved[y * self.fb.stride:y * self.fb.stride + row] for y in range(oc.LCD_H))
        if bpp == 16:
            a = np.frombuffer(raw, "<u2").reshape(oc.LCD_H, oc.LCD_W)
            rgb = np.stack([((a >> 11) & 31) << 3, ((a >> 5) & 63) << 2, (a & 31) << 3], -1)
        else:
            a = np.frombuffer(raw, np.uint8).reshape(oc.LCD_H, oc.LCD_W, 4)
            rgb = a[..., [2, 1, 0]]
        return Image.fromarray(rgb.astype(np.uint8), "RGB")

    def show(self, img):
        self.fb.show(img)

    def __exit__(self, *exc):
        try:
            if self.saved is not None:     # 元の画面に戻す
                self.fb.f.seek(0)
                self.fb.f.write(self.saved)
        finally:
            for pid in self.pids:
                try:
                    os.kill(pid, signal.SIGCONT)
                except OSError:
                    pass
        return False


# ---------------------------------------------------------------- 描画
class Painter:
    def __init__(self):
        self.font, _ = oc.find_font(14)
        self.font_s, _ = oc.find_font(11)
        self.font_b, _ = oc.find_font(12, bold=True)
        ok = self.font_s.getmask("↑").getbbox() is not None
        self.ar = "↑↓" if ok else "FX"

    def fit(self, text, font, width):
        line = " ".join(text[:300].split())
        if font.getlength(line) <= width:
            return line
        lo, hi = 0, len(line)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.getlength(line[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return line[:lo] + "…"


COLORS = {"ocr": (47, 214, 123), "web": (90, 170, 255), "pc": (90, 170, 255), "cli": (255, 190, 70),
          "cam": (200, 140, 255), "share": (90, 170, 255), "qr": (200, 140, 255), "run": (255, 190, 70)}


def panel_items(api):
    """ピン留め → 最近 の順 (重複なし、ファイル(バイナリ)は除く)"""
    try:
        pins = api("/api/items?limit=20&pinned=1&preview=200")["items"]
        recent = api("/api/items?limit=30&preview=200")
    except Exception:
        return None, 0
    latest = recent.get("latest", 0)
    seen, out = set(), []
    for it in pins + recent["items"]:
        if it["id"] in seen or it.get("binary"):
            continue
        seen.add(it["id"])
        out.append(it)
    return out, latest


def draw_panel(p, bg, items, sel, top, latest, note=""):
    W, H = oc.LCD_W, oc.LCD_H
    img = Image.blend(bg, Image.new("RGB", bg.size), 0.6)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = 8, 6, W - 8, H - 6
    d.rounded_rectangle((x0, y0, x1, y1), 8, fill=(16, 20, 26), outline=oc.ACC, width=2)
    d.rounded_rectangle((x0, y0, x1, y0 + 20), 8, fill=(0, 90, 60))
    d.rectangle((x0 + 2, y0 + 12, x1 - 2, y0 + 20), fill=(0, 90, 60))
    d.text((x0 + 8, y0 + 3), "HEXA CLIP", fill=(255, 255, 255), font=p.font_b)
    head = note or f"{len(items)}件"
    d.text((x1 - 8 - p.font_s.getlength(head), y0 + 4), head, fill=(210, 240, 220), font=p.font_s)
    rh, rows = 19, 6
    if not items:
        d.text((x0 + 12, y0 + 44), "まだ何もありません" if items is not None else "HEXA CLIP が停止中",
               fill=oc.SUB, font=p.font)
    for r in range(min(rows, len(items or []) - top)):
        i = top + r
        it = items[i]
        y = y0 + 24 + r * rh
        if i == sel:
            d.rectangle((x0 + 3, y - 1, x1 - 3, y + rh - 2), fill=(30, 48, 44))
            d.rectangle((x0 + 3, y - 1, x0 + 5, y + rh - 2), fill=oc.ACC)
        tag = "★" if it.get("pinned") and p.font_s.getmask("★").getbbox() else it["source"][:3]
        col = (232, 193, 74) if it.get("pinned") else COLORS.get(it["source"], oc.SUB)
        d.text((x0 + 10, y + 2), tag, fill=col, font=p.font_s)
        right = x1 - 10 - (34 if it["id"] == latest else 0)
        body = (it["name"] + "  " if it.get("name") else "") + it["text"]
        d.text((x0 + 38, y + 1), p.fit(body, p.font, right - x0 - 38), font=p.font,
               fill=oc.FG if i == sel else (200, 205, 215))
        if it["id"] == latest:
            d.text((x1 - 40, y + 3), "fn+V", fill=oc.ACC, font=p.font_s)
    if items and len(items) > rows:          # スクロールバー
        h = rows * rh
        bh = max(8, h * rows // len(items))
        by = y0 + 24 + (h - bh) * top // max(1, len(items) - rows)
        d.rectangle((x1 - 5, by, x1 - 3, by + bh), fill=(90, 98, 115))
    d.text((x0 + 8, y1 - 17), f"{p.ar}:選ぶ  ENTER:打ち込む  1〜6:すぐ  ESC:閉じる",
           fill=oc.SUB, font=p.font_s)
    return img


def run_panel(dev, api, keys_cfg=None, painter=None, fbdev="/dev/fb0"):
    """パネルを出して選ばせる。打ち込む本文 (str) か None を返す"""
    p = painter or Painter()
    items, latest = panel_items(api)
    keys = oc.Keys(dev, cfg=keys_cfg)                # キーボードを独占 (下のアプリに届かない)
    try:
        with Overlay(fbdev) as ov:
            bg = ov.background()
            sel, top, rows = 0, 0, 6
            end = time.time() + PANEL_TIMEOUT
            keys.flush(0.15)                         # fn+H の離し分を捨てる
            dirty = True
            while time.time() < end:
                if dirty:
                    top = min(max(top, sel - rows + 1), sel)
                    ov.show(draw_panel(p, bg, items, sel, top, latest))
                    dirty = False
                for kind, val in oc.nav(keys.poll(0.3)):
                    dirty = True
                    end = time.time() + PANEL_TIMEOUT
                    if kind == "ACT" and val == "ESC":
                        return None
                    if not items:
                        continue
                    if kind == "CHAR" and val in "123456":
                        n = top + int(val) - 1
                        if n < len(items):
                            return full_text(api, items[n])
                    elif kind == "ACT" and val == "UP":
                        sel = (sel - 1) % len(items)
                    elif kind == "ACT" and val == "DOWN":
                        sel = (sel + 1) % len(items)
                    elif kind == "ACT" and val in ("ENTER", "RIGHT"):
                        return full_text(api, items[sel])
            return None
    finally:
        keys.close()                                 # キーボードを返す


def full_text(api, item):
    if not item.get("truncated"):
        return item["text"]
    try:
        return api(f"/api/items/{item['id']}")["text"]
    except Exception:
        return item["text"]


def banner(text, painter=None, seconds=1.6, fbdev="/dev/fb0"):
    """どのアプリの上でも、上部に1〜2秒「受信」の帯を出す"""
    p = painter or Painter()
    with Overlay(fbdev) as ov:
        img = ov.background()
        d = ImageDraw.Draw(img)
        msg = p.fit(text, p.font_s, oc.LCD_W - 110)
        w = int(p.font_b.getlength("HEXA CLIP") + p.font_s.getlength(msg)) + 30
        x0 = (oc.LCD_W - w) // 2
        d.rounded_rectangle((x0, 4, x0 + w, 26), 9, fill=(0, 90, 60), outline=oc.ACC)
        d.text((x0 + 10, 7), "HEXA CLIP", fill=(255, 255, 255), font=p.font_b)
        d.text((x0 + 20 + p.font_b.getlength("HEXA CLIP"), 8), msg, fill=(220, 245, 230), font=p.font_s)
        ov.show(img)
        time.sleep(seconds)
