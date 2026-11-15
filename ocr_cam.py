#!/usr/bin/env python3
"""HEXA CLIP — OCR camera for CardputerZero (v0.3)

ﾌﾟﾚﾋﾞｭｰ → ENTERで撮影・OCR → 編集画面で修正 → ENTERでHEXA CLIPへ送る

  プレビュー : ENTER=読み取り  ESC=終了
               枠の色/FOCUSバー 緑=ピント良 黄=まあまあ 赤=ボケ
  編集画面   : ←→↑↓=カーソル  BS=1文字削除  DEL=後ろを削除  TAB=改行
               英数字・記号=挿入 (日本語の入力は不可)
               ENTER=送る  ESC=破棄してプレビューへ

usage:
  sudo python3 ocr_cam.py                 # 既定: jpn
  sudo python3 ocr_cam.py -l jpn+eng
  sudo python3 ocr_cam.py --debug-keys    # 押したキーのコードを表示
  sudo python3 ocr_cam.py --keys keys.json  # キー割り当ての上書き(下記)

keys.json の例 (fnキー+文字で矢印にしたい場合):
  {"fn": [464], "fn_map": {"39": "UP", "52": "DOWN", "51": "LEFT", "53": "RIGHT"}}
"""
import argparse
import datetime
import fcntl
import glob
import json
import os
import select
import struct
import subprocess
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

VERSION = "0.3"
LCD_W, LCD_H = 320, 170
MAIN_W, MAIN_H = 1640, 1232          # センサー全画角(2x2ビニング)
LORES_W, LORES_H = 400, 300          # プレビュー用(同じ画角)

VIEW_CW = 0.6                        # プレビューに映す範囲(中央を少しズーム)
VIEW_CH = VIEW_CW * (MAIN_W / MAIN_H) / (LCD_W / LCD_H)
VIEW_X0 = (1 - VIEW_CW) / 2
VIEW_Y0 = (1 - VIEW_CH) / 2
BOX = (24, 58, 296, 112)             # 読み取り枠(LCD座標)

CLIP_URL = os.environ.get("HEXACLIP_URL", "http://127.0.0.1:8080")
CLIP_DIR = os.path.expanduser("~" + os.environ.get("SUDO_USER", "") + "/clip")

# ---------------------------------------------------------------- keys (Linux input codes)
EVIOCGRAB = 0x40044590
EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)

K_ESC, K_BS, K_TAB, K_ENTER = 1, 14, 15, 28
K_LSHIFT, K_RSHIFT, K_CAPS = 42, 54, 58
K_UP, K_LEFT, K_RIGHT, K_DOWN = 103, 105, 106, 108
K_HOME, K_END, K_DEL = 102, 107, 111

CHARS = {
    2: "1!", 3: "2@", 4: "3#", 5: "4$", 6: "5%", 7: "6^", 8: "7&", 9: "8*", 10: "9(", 11: "0)",
    12: "-_", 13: "=+", 16: "qQ", 17: "wW", 18: "eE", 19: "rR", 20: "tT", 21: "yY", 22: "uU",
    23: "iI", 24: "oO", 25: "pP", 26: "[{", 27: "]}", 30: "aA", 31: "sS", 32: "dD", 33: "fF",
    34: "gG", 35: "hH", 36: "jJ", 37: "kK", 38: "lL", 39: ";:", 40: "'\"", 41: "`~", 43: "\\|",
    44: "zZ", 45: "xX", 46: "cC", 47: "vV", 48: "bB", 49: "nN", 50: "mM", 51: ",<", 52: ".>",
    53: "/?", 57: "  ",
}
ACTIONS = {K_UP: "UP", K_DOWN: "DOWN", K_LEFT: "LEFT", K_RIGHT: "RIGHT", K_HOME: "HOME",
           K_END: "END", K_DEL: "DEL", K_BS: "BS", K_TAB: "TAB", K_ENTER: "ENTER", K_ESC: "ESC"}


class Keys:
    """キーボードを横取りして、押下を ('CHAR', 'a') / ('ACT', 'LEFT') に変換"""

    def __init__(self, dev, debug=False, cfg=None):
        self.fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        self.debug = debug
        self.held = set()
        self.shift_once = False
        self.caps = False
        cfg = cfg or {}
        self.fn_codes = set(cfg.get("fn", []))
        self.fn_map = {int(k): v for k, v in cfg.get("fn_map", {}).items()}

    def _raw(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, EV_SIZE * 64)
        except BlockingIOError:
            return []
        out = []
        for i in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
            _, _, etype, code, value = struct.unpack(EV_FMT, data[i:i + EV_SIZE])
            if etype == 1:
                out.append((code, value))
        return out

    def poll(self, timeout=0.0):
        events = []
        for code, value in self._raw(timeout):
            if self.debug and value == 1:
                print("key", code)
            if value == 0:
                self.held.discard(code)
                continue
            if value == 1:
                self.held.add(code)
            if code in (K_LSHIFT, K_RSHIFT):
                if value == 1:
                    self.shift_once = True       # 押して離すと次の1文字だけ大文字
                continue
            if code == K_CAPS:
                if value == 1:
                    self.caps = not self.caps
                continue
            if code in self.fn_codes:
                continue
            fn = any(c in self.held for c in self.fn_codes)
            if fn and code in self.fn_map:
                events.append(("ACT", self.fn_map[code]))
                continue
            if code in ACTIONS:
                events.append(("ACT", ACTIONS[code]))
                continue
            if code in CHARS:
                shift = (K_LSHIFT in self.held or K_RSHIFT in self.held
                         or self.shift_once) != self.caps
                events.append(("CHAR", CHARS[code][1 if shift else 0]))
                self.shift_once = False
        return events

    def flush(self, sec=0.6):
        end = time.time() + sec
        while time.time() < end:
            self._raw(0.05)
        self.held.clear()
        self.shift_once = False

    def close(self):
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
        finally:
            os.close(self.fd)


def find_keyboard():
    name = None
    for line in open("/proc/bus/input/devices"):
        if line.startswith("N: Name="):
            name = line.strip().split("=", 1)[1].strip('"')
        elif line.startswith("H: Handlers=") and name and "tca8418" in name:
            for h in line.split("=", 1)[1].split():
                if h.startswith("event"):
                    return "/dev/input/" + h
    raise RuntimeError("keyboard (tca8418) not found")


# ---------------------------------------------------------------- display
class FrameBuffer:
    def __init__(self, dev="/dev/fb0"):
        base = f"/sys/class/graphics/{os.path.basename(dev)}"
        self.bpp = int(open(f"{base}/bits_per_pixel").read())
        try:
            self.stride = int(open(f"{base}/stride").read())
        except OSError:
            self.stride = LCD_W * self.bpp // 8
        self.f = open(dev, "r+b", buffering=0)

    def show(self, img):
        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        if self.bpp == 16:
            buf = (((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)).astype("<u2")
        else:
            buf = np.zeros((LCD_H, LCD_W, 4), np.uint8)
            buf[..., 0], buf[..., 1], buf[..., 2], buf[..., 3] = a[..., 2], a[..., 1], a[..., 0], 255
        row = LCD_W * self.bpp // 8
        raw = buf.tobytes()
        if self.stride == row:
            self.f.seek(0)
            self.f.write(raw)
        else:
            for y in range(LCD_H):
                self.f.seek(y * self.stride)
                self.f.write(raw[y * row:(y + 1) * row])

    def clear(self):
        self.show(Image.new("RGB", (LCD_W, LCD_H)))


def find_font(size):
    """日本語フォントを探す(細すぎるウェイトは後回し)"""
    cands = []
    for p in ["/usr/share/APPLaunch/**/*.tt[fc]", "/usr/share/APPLaunch/**/*.otf",
              "/usr/share/fonts/**/*CJK*", "/usr/share/fonts/**/*[Ii][Pp][Aa]*",
              "/usr/share/fonts/**/*wqy*", "/usr/share/fonts/**/*Droid*Fallback*"]:
        cands += glob.glob(p, recursive=True)

    def rank(f):
        n = os.path.basename(f).lower()
        for i, w in enumerate(["medium", "65", "regular", "55", "bold", "75", "black", "light", "thin"]):
            if w in n:
                return i
        return 5
    for f in sorted(set(cands), key=rank):
        try:
            font = ImageFont.truetype(f, size)
            if font.getlength("あ") > 0:
                return font, f
        except Exception:
            pass
    return ImageFont.load_default(), None


# ---------------------------------------------------------------- camera / OCR
def open_camera():
    from libcamera import Transform
    from picamera2 import Picamera2
    cam = Picamera2()
    cam.configure(cam.create_preview_configuration(
        main={"size": (MAIN_W, MAIN_H), "format": "YUV420"},
        lores={"size": (LORES_W, LORES_H), "format": "YUV420"},
        transform=Transform(hflip=1, vflip=1),       # 180°回転
        buffer_count=3))
    cam.start()
    time.sleep(0.8)
    return cam


def sharpness(a):
    a = a.astype(np.float32)
    lap = a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var())


class Preview:
    def __init__(self, font_s):
        self.font_s = font_s
        self.peak = 1.0
        self.toast = ("", 0.0)

    def frame(self, cam):
        y = cam.capture_array("lores")[:LORES_H, :LORES_W]
        x0, y0 = int(VIEW_X0 * LORES_W), int(VIEW_Y0 * LORES_H)
        x1, y1 = int((VIEW_X0 + VIEW_CW) * LORES_W), int((VIEW_Y0 + VIEW_CH) * LORES_H)
        view = np.ascontiguousarray(y[y0:y1, x0:x1])
        vh, vw = view.shape
        bx0, by0, bx1, by1 = BOX
        sub = view[by0 * vh // LCD_H:by1 * vh // LCD_H, bx0 * vw // LCD_W:bx1 * vw // LCD_W]
        f = sharpness(sub) if sub.size > 16 else 0.0
        self.peak = max(self.peak * 0.995, f, 1.0)
        ratio = min(f / self.peak, 1.0)

        img = Image.fromarray(view).resize((LCD_W, LCD_H)).convert("RGB")
        d = ImageDraw.Draw(img)
        col = (0, 255, 120) if ratio > 0.8 else (255, 200, 0) if ratio > 0.5 else (255, 80, 80)
        d.rectangle(BOX, outline=col, width=2)
        d.rectangle((0, LCD_H - 16, LCD_W, LCD_H), fill=(0, 0, 0))
        d.text((4, LCD_H - 15), "ENTER:read ESC:quit", fill=(255, 255, 255), font=self.font_s)
        bx = 190
        d.text((bx - 34, LCD_H - 15), "FOCUS", fill=(200, 200, 200), font=self.font_s)
        d.rectangle((bx, LCD_H - 12, bx + 120, LCD_H - 4), outline=(120, 120, 120))
        d.rectangle((bx, LCD_H - 12, bx + int(120 * ratio), LCD_H - 4), fill=col)
        msg, until = self.toast
        if msg and time.time() < until:
            w = int(self.font_s.getlength(msg)) + 16
            d.rounded_rectangle(((LCD_W - w) // 2, 6, (LCD_W + w) // 2, 26), 8, fill=(0, 120, 70))
            d.text(((LCD_W - w) // 2 + 8, 8), msg, fill=(255, 255, 255), font=self.font_s)
        return img


def box_to_main():
    bx0, by0, bx1, by1 = BOX
    return (int((VIEW_X0 + bx0 / LCD_W * VIEW_CW) * MAIN_W),
            int((VIEW_Y0 + by0 / LCD_H * VIEW_CH) * MAIN_H),
            int((VIEW_X0 + bx1 / LCD_W * VIEW_CW) * MAIN_W),
            int((VIEW_Y0 + by1 / LCD_H * VIEW_CH) * MAIN_H))


def capture_crop(cam):
    y = cam.capture_array("main")[:MAIN_H, :MAIN_W]
    x0, y0, x1, y1 = box_to_main()
    img = Image.fromarray(np.ascontiguousarray(y[y0:y1, x0:x1]))
    if img.width > 1000:
        img = img.resize((1000, int(img.height * 1000 / img.width)), Image.BILINEAR)
    return ImageOps.autocontrast(img, cutoff=1)


def run_ocr(img, lang, psm, min_conf=60.0):
    """TSVで単語ごとの信頼度を取り、低いもの(ノイズ)を捨てる"""
    path = "/tmp/ocr_cam_crop.png"
    img.save(path)
    r = subprocess.run(["tesseract", path, "-", "-l", lang, "--psm", str(psm), "tsv"],
                       capture_output=True, text=True)
    lines, order = {}, []
    for row in r.stdout.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            if float(c[10]) < min_conf:
                continue
        except ValueError:
            continue
        key = (c[2], c[3], c[4])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(c[11])
    out = []
    for k in order:
        s = lines[k][0]
        for w in lines[k][1:]:
            s += (" " if s[-1:].isascii() and w[:1].isascii() else "") + w
        out.append(s)
    return "\n".join(out)


# ---------------------------------------------------------------- clip
def send_clip(text):
    """HEXA CLIPへ送る。失敗したらファイル保存。戻り値: 表示用メッセージ"""
    try:
        req = urllib.request.Request(
            CLIP_URL + "/api/items",
            data=json.dumps({"text": text, "source": "ocr"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return f"CLIPへ送信 #{json.loads(r.read())['id']}"
    except Exception:
        os.makedirs(CLIP_DIR, exist_ok=True)
        p = os.path.join(CLIP_DIR, datetime.datetime.now().strftime("ocr_%Y%m%d_%H%M%S.txt"))
        with open(p, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        if os.environ.get("SUDO_UID"):
            for q in (CLIP_DIR, p):
                os.chown(q, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
        return "CLIP停止中 → ファイル保存"


# ---------------------------------------------------------------- editor
def layout(text, font, width):
    """折り返し。[(start, end), ...] 各行のtext上の範囲(改行文字は含まない)"""
    lines, start, cur_w = [], 0, 0.0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            lines.append((start, i))
            start, cur_w = i + 1, 0.0
        else:
            w = font.getlength(ch)
            if cur_w + w > width and i > start:
                lines.append((start, i))
                start, cur_w = i, 0.0
            cur_w += w
        i += 1
    lines.append((start, len(text)))
    return lines


class Editor:
    LINE_H = 20
    TOP, BOTTOM = 20, 16

    def __init__(self, text, font, font_s, elapsed):
        self.text, self.cur = text, len(text)
        self.font, self.font_s = font, font_s
        self.elapsed = elapsed
        self.scroll = 0
        self.width = LCD_W - 10
        self.rows = (LCD_H - self.TOP - self.BOTTOM) // self.LINE_H

    def _pos(self, lines):
        for n, (s, e) in enumerate(lines):
            if s <= self.cur <= e and not (self.cur == e and n + 1 < len(lines)
                                             and lines[n + 1][0] == e):
                return n, self.cur - s
        return len(lines) - 1, self.cur - lines[-1][0]

    def _col_at_x(self, s, e, x):
        acc = 0.0
        for i in range(s, e):
            w = self.font.getlength(self.text[i])
            if acc + w / 2 > x:
                return i
            acc += w
        return e

    def handle(self, kind, val):
        """戻り値: None(継続) / 'SEND' / 'CANCEL'"""
        t = self.text
        if kind == "CHAR":
            self.text = t[:self.cur] + val + t[self.cur:]
            self.cur += 1
        elif val == "TAB":
            self.text = t[:self.cur] + "\n" + t[self.cur:]
            self.cur += 1
        elif val == "BS" and self.cur > 0:
            self.text = t[:self.cur - 1] + t[self.cur:]
            self.cur -= 1
        elif val == "DEL" and self.cur < len(t):
            self.text = t[:self.cur] + t[self.cur + 1:]
        elif val == "LEFT":
            self.cur = max(0, self.cur - 1)
        elif val == "RIGHT":
            self.cur = min(len(t), self.cur + 1)
        elif val in ("UP", "DOWN", "HOME", "END"):
            lines = layout(t, self.font, self.width)
            n, _ = self._pos(lines)
            s, e = lines[n]
            if val == "HOME":
                self.cur = s
            elif val == "END":
                self.cur = e
            else:
                x = self.font.getlength(t[s:self.cur])
                m = n - 1 if val == "UP" else n + 1
                if 0 <= m < len(lines):
                    self.cur = self._col_at_x(*lines[m], x)
        elif val == "ENTER":
            return "SEND"
        elif val == "ESC":
            return "CANCEL"
        return None

    def render(self):
        img = Image.new("RGB", (LCD_W, LCD_H), (14, 16, 22))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, LCD_W, 17), fill=(0, 90, 60))
        d.text((4, 1), f"編集  OCR {self.elapsed:.1f}s  {len(self.text)}文字",
               fill=(255, 255, 255), font=self.font_s)
        lines = layout(self.text, self.font, self.width)
        n, _ = self._pos(lines)
        if n < self.scroll:
            self.scroll = n
        elif n >= self.scroll + self.rows:
            self.scroll = n - self.rows + 1
        for r in range(self.rows):
            li = self.scroll + r
            if li >= len(lines):
                break
            s, e = lines[li]
            y = self.TOP + r * self.LINE_H
            d.text((5, y), self.text[s:e], fill=(235, 238, 245), font=self.font)
            if li == n:
                cx = 5 + int(self.font.getlength(self.text[s:self.cur]))
                d.rectangle((cx, y + 1, cx + 1, y + self.LINE_H - 2), fill=(47, 214, 123))
        if not self.text:
            d.text((5, self.TOP), "(空です)", fill=(110, 118, 135), font=self.font)
        if len(lines) > self.rows:     # スクロールバー
            h = LCD_H - self.TOP - self.BOTTOM
            bh = max(8, h * self.rows // len(lines))
            by = self.TOP + (h - bh) * self.scroll // max(1, len(lines) - self.rows)
            d.rectangle((LCD_W - 3, by, LCD_W - 1, by + bh), fill=(90, 98, 115))
        d.rectangle((0, LCD_H - self.BOTTOM, LCD_W, LCD_H), fill=(0, 0, 0))
        d.text((4, LCD_H - 15), "ENTER:送る  ESC:破棄  TAB:改行  BS:削除",
               fill=(200, 200, 200), font=self.font_s)
        return img


def message(fb, font, title, body):
    img = Image.new("RGB", (LCD_W, LCD_H), (14, 16, 22))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, LCD_W, 17), fill=(0, 90, 60))
    d.text((4, 1), title, fill=(255, 255, 255), font=font)
    d.text((6, 30), body, fill=(235, 238, 245), font=font)
    fb.show(img)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-l", "--lang", default="jpn")
    ap.add_argument("--psm", type=int, default=6)
    ap.add_argument("--debug-keys", action="store_true")
    ap.add_argument("--keys", help="キー割り当てJSON")
    a = ap.parse_args()

    font, font_path = find_font(16)
    font_s, _ = find_font(12)
    print(f"HEXA CLIP OCR {VERSION}  font: {font_path or '(default)'}")
    cfg = json.load(open(a.keys)) if a.keys else None

    fb = FrameBuffer()
    keys = Keys(find_keyboard(), a.debug_keys, cfg)
    cam = open_camera()
    pv = Preview(font_s)
    keys.flush(1.0)
    print("ready. ENTER=read / ESC=quit")
    reason, frames = "unknown", 0
    try:
        while True:
            fb.show(pv.frame(cam))
            frames += 1
            acts = [v for k, v in keys.poll(0.0) if k == "ACT"]
            if "ESC" in acts:
                reason = "ESC"
                break
            if "ENTER" not in acts:
                continue

            message(fb, font, "OCR", "読み取り中...")
            t0 = time.time()
            text = run_ocr(capture_crop(cam), a.lang, a.psm)
            ed = Editor(text, font, font_s, time.time() - t0)
            print(f"----- OCR ({ed.elapsed:.1f}s)\n{text}\n-----")
            keys.flush(0.2)
            result = None
            while result is None:
                fb.show(ed.render())
                for kind, val in keys.poll(0.5):
                    result = ed.handle(kind, val)
                    if result:
                        break
            if result == "SEND" and ed.text.strip():
                msg = send_clip(ed.text)
                print(f"{msg}\n{ed.text}\n-----")
            else:
                msg = "破棄しました"
            pv.toast = (msg, time.time() + 2.0)
            keys.flush(0.2)
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    except Exception:
        import traceback
        traceback.print_exc()
        reason = "error"
    finally:
        print(f"exit: {reason} / frames={frames}")
        try:
            cam.stop()
        except Exception:
            pass
        keys.close()
        fb.clear()


if __name__ == "__main__":
    main()
