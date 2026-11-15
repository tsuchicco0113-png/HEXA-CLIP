#!/usr/bin/env python3
"""HEXA CLIP — CardputerZero 用アプリ (v0.5)

4つのモードを上のタブで切り替える:
  1 クリップ : カメラで文字を読み取り → 修正 → HEXA CLIPへ送る
  2 履歴     : 受信/送信した内容の一覧・全文表示・編集・保存・削除
  3 QR       : スマホ接続用のQRコード (読むだけでログイン)
  4 設定     : OCR言語 / 全画素撮影 / ズーム / 受信時の自動表示

共通の操作:
  TAB = 次のモード   1〜4 = モードを直接選択   ESC = 戻る (一番上の画面では終了)
  方向キー = F(↑) X(↓) Z(←) C(→)   ※本体の流儀と同じ。文字入力画面では fn を押しながら
  画面下に、その画面で使えるキーを常に表示

usage:
  hexaclip-ocr                 # 通常起動 (ホーム画面のアイコンと同じ)
  hexaclip-ocr --debug-keys    # 押したキーのコードを表示
  hexaclip-ocr --keys keys.json  # キー割り当ての上書き {"fn":[464],"fn_map":{"33":"UP"}}
  hexaclip-ocr --no-pause      # ランチャーを一時停止しない

起動中はランチャー(APPLaunch)を一時停止し、自動ロック/スリープと画面の取り合いを防ぐ。
万一ランチャーが固まったままになったら: sudo pkill -CONT -f APPLaunch
"""
import argparse
import datetime
import fcntl
import glob
import json
import os
import select
import signal
import struct
import subprocess
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

VERSION = "0.6.1"
LCD_W, LCD_H = 320, 170
TOP_H, BOT_H = 18, 16                # タブバー / キー案内バー
MAIN_W, MAIN_H = 1640, 1232          # センサー全画角(2x2ビニング)
FULL_W, FULL_H = 3280, 2464          # 撮影用の全画素
LORES_W, LORES_H = 640, 480          # プレビュー用(同じ画角)
ZOOMS = [0.6, 0.3, 0.2]              # プレビューに映す横幅(全画角に対する割合) x1 / x2 / x3
STILL_CFG = None                     # 全画素撮影の設定(open_cameraで作る)
BOX = (24, 60, 296, 114)             # 読み取り枠(LCD座標)

HOME = os.path.expanduser("~" + os.environ.get("SUDO_USER", ""))
CLIP_URL = os.environ.get("HEXACLIP_URL", "http://127.0.0.1:8080")
CLIP_DIR = os.path.join(HOME, "clip")
SETTINGS_PATH = os.path.join(HOME, ".config", "hexaclip", "settings.json")

BG, FG, SUB, ACC = (14, 16, 22), (235, 238, 245), (150, 158, 175), (47, 214, 123)
BAR = (0, 90, 60)


def view_rect(cw):
    """映す範囲 (x0, y0, w, h) を全画角に対する割合で返す"""
    ch = cw * (MAIN_W / MAIN_H) / (LCD_W / LCD_H)
    return (1 - cw) / 2, (1 - ch) / 2, cw, ch


def chown_user(*paths):
    if os.environ.get("SUDO_UID"):
        for p in paths:
            os.chown(p, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))


# ---------------------------------------------------------------- settings
SETTING_DEFS = [
    ("lang", "OCRの言語", ["jpn", "jpn+eng", "eng"], ["日本語", "日本語+英語", "英語"]),
    ("hires", "高精細撮影", [True, False], ["ON (小さい文字向き)", "OFF (速い)"]),
    ("zoom", "起動時のズーム", [0, 1, 2], ["x1", "x2", "x3"]),
    ("auto_show", "受信したら表示", [False, True], ["OFF (通知のみ)", "ON (本文を開く)"]),
    ("paste_erase", "fn+Vで入るVを消す", [True, False], ["ON", "OFF"]),
]


def load_settings():
    st = {k: vals[0] for k, _, vals, _ in SETTING_DEFS}
    try:
        for k, v in json.load(open(SETTINGS_PATH)).items():
            if k in st:
                st[k] = v
    except (OSError, ValueError):
        pass
    return st


def save_settings(st):
    try:
        d = os.path.dirname(SETTINGS_PATH)
        os.makedirs(d, exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(st, f)
        chown_user(d, SETTINGS_PATH)
    except OSError as e:
        print("settings save failed:", e)


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
KNOWN = set(CHARS) | set(ACTIONS) | {K_LSHIFT, K_RSHIFT, K_CAPS}
FN_ARROWS = {33: "UP", 45: "DOWN", 44: "LEFT", 46: "RIGHT"}     # fn + F / X / Z / C
LETTER_NAV = {"f": "UP", "x": "DOWN", "z": "LEFT", "c": "RIGHT"}


def nav(events):
    """文字入力のない画面用: F/X/Z/C 単独を方向キーとして扱う"""
    out = []
    for kind, val in events:
        if kind == "CHAR" and val.lower() in LETTER_NAV:
            out.append(("ACT", LETTER_NAV[val.lower()]))
        else:
            out.append((kind, val))
    return out


class Keys:
    """キーボードを横取りして、押下を ('CHAR', 'a') / ('ACT', 'LEFT') に変換"""

    def __init__(self, dev, debug=False, cfg=None):
        self.fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        except OSError as e:
            os.close(self.fd)
            if e.errno == 16:               # EBUSY: 別のプログラムがキーボードを独占中
                raise SystemExit(
                    "HEXA CLIP はすでに起動しています (キーボードが使用中)。\n"
                    "本体の画面で ESC を押して終了するか、次を実行してください:\n"
                    "  pkill -f ocr_cam.py; sleep 1; sudo pkill -CONT -f APPLaunch")
            raise
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
                print("key", code, flush=True)
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
            if code in self.fn_codes or code not in KNOWN:
                continue                         # fn / sym など(修飾キーとして held に残す)
            # fn = 設定で指定したキー、または「知らないキー」が押されている状態
            fn = any(c in self.fn_codes or c not in KNOWN for c in self.held if c != code)
            if fn and code in self.fn_map:
                events.append(("ACT", self.fn_map[code]))
                continue
            if fn and code in FN_ARROWS:
                events.append(("ACT", FN_ARROWS[code]))
                continue
            if code in ACTIONS:
                events.append(("ACT", ACTIONS[code]))
                continue
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
        if "serif" in n:
            return 50
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
    global STILL_CFG
    cam = Picamera2()
    try:
        STILL_CFG = cam.create_still_configuration(
            main={"size": (FULL_W, FULL_H), "format": "YUV420"},
            transform=Transform(hflip=1, vflip=1), buffer_count=1)
    except Exception as e:
        print("hires config failed:", e)
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
    def __init__(self, zoom=0):
        self.peak = 1.0
        self.zoom = zoom
        self.ratio = 0.0

    @property
    def cw(self):
        return ZOOMS[self.zoom]

    def set_zoom(self, z):
        z = max(0, min(len(ZOOMS) - 1, z))
        if z != self.zoom:
            self.zoom, self.peak = z, 1.0

    def frame(self, cam):
        y = cam.capture_array("lores")[:LORES_H, :LORES_W]
        vx, vy, vw_, vh_ = view_rect(self.cw)
        x0, y0 = int(vx * LORES_W), int(vy * LORES_H)
        x1, y1 = int((vx + vw_) * LORES_W), int((vy + vh_) * LORES_H)
        view = np.ascontiguousarray(y[y0:y1, x0:x1])
        vh, vw = view.shape
        bx0, by0, bx1, by1 = BOX
        sub = view[by0 * vh // LCD_H:by1 * vh // LCD_H, bx0 * vw // LCD_W:bx1 * vw // LCD_W]
        f = sharpness(sub) if sub.size > 16 else 0.0
        self.peak = max(self.peak * 0.995, f, 1.0)
        self.ratio = min(f / self.peak, 1.0)
        img = Image.fromarray(view).resize((LCD_W, LCD_H)).convert("RGB")
        ImageDraw.Draw(img).rectangle(BOX, outline=self.color(), width=2)
        return img

    def color(self):
        r = self.ratio
        return (0, 255, 120) if r > 0.8 else (255, 200, 0) if r > 0.5 else (255, 80, 80)


def box_frac(cw):
    """読み取り枠の範囲を全画角に対する割合で返す"""
    vx, vy, vw, vh = view_rect(cw)
    bx0, by0, bx1, by1 = BOX
    return (vx + bx0 / LCD_W * vw, vy + by0 / LCD_H * vh,
            vx + bx1 / LCD_W * vw, vy + by1 / LCD_H * vh)


def prepare(img):
    """OCR向けの前処理: 白地に黒へそろえる / 小さい切り出しは拡大 / 大きすぎれば縮小"""
    if np.asarray(img).mean() < 110:            # 暗い背景(ダークモードの画面など)は反転
        img = ImageOps.invert(img)
    if img.width < 900:                         # 小さい文字: 2倍に拡大
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    elif img.width > 1400:
        img = img.resize((1400, int(img.height * 1400 / img.width)), Image.LANCZOS)
    return ImageOps.autocontrast(img, cutoff=1)


def capture_crop(cam, cw, hires=True):
    """枠内を切り出す。hires=True なら全画素モードに切り替えて撮る"""
    y, W, H = None, MAIN_W, MAIN_H
    if hires and STILL_CFG is not None:
        try:
            y = cam.switch_mode_and_capture_array(STILL_CFG, "main")
            W, H = FULL_W, FULL_H
        except Exception as e:
            print("hires capture failed, fallback:", e)
            y = None
    if y is None:
        y = cam.capture_array("main")
        W, H = MAIN_W, MAIN_H
    f = box_frac(cw)
    x0, y0, x1, y1 = int(f[0] * W), int(f[1] * H), int(f[2] * W), int(f[3] * H)
    img = Image.fromarray(np.ascontiguousarray(y[:H, :W][y0:y1, x0:x1]))
    img.save("/tmp/ocr_cam_raw.png")
    return prepare(img)


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


# ---------------------------------------------------------------- clip API
def clip_call(method, path, body=None, timeout=2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CLIP_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def clean(text):
    return text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")


def clip_items(limit=50):
    try:
        items = clip_call("GET", f"/api/items?limit={limit}", timeout=1.0)["items"]
        for it in items:
            it["text"] = clean(it["text"])
        return items
    except Exception:
        return None                      # None = サーバー停止中


def to_top(item_id):
    """最新に移す → fn+V の貼り付け対象になる。新しいIDを返す"""
    try:
        return clip_call("POST", f"/api/items/{item_id}/top", {})["id"]
    except Exception:
        return None


def save_file(text, name):
    os.makedirs(CLIP_DIR, exist_ok=True)
    p = os.path.join(CLIP_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    chown_user(CLIP_DIR, p)
    return p


def send_clip(text, source="ocr"):
    """HEXA CLIPへ送る。失敗したらファイル保存。戻り値: 表示用メッセージ"""
    try:
        r = clip_call("POST", "/api/items", {"text": text, "source": source}, 3.0)
        return f"CLIPへ送信 #{r['id']}"
    except Exception:
        save_file(text, datetime.datetime.now().strftime("ocr_%Y%m%d_%H%M%S.txt"))
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
        self.title = f"編集  OCR {elapsed:.1f}s" if elapsed else "編集"
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
        d.text((4, 1), f"{self.title}  {len(self.text)}文字",
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
        d.text((4, LCD_H - 15), "ENTER:送る ESC:破棄 TAB:改行 fn+FXZC:移動",
               fill=(200, 200, 200), font=self.font_s)
        return img


# ---------------------------------------------------------------- app
MODES = ["クリップ", "履歴", "QR", "設定"]
M_CLIP, M_HIST, M_QR, M_SET = range(4)


class App:
    def __init__(self, fb, keys, cam, args):
        self.fb, self.keys, self.cam, self.args = fb, keys, cam, args
        self.font, font_path = find_font(16)
        self.font_s, _ = find_font(12)
        print(f"HEXA CLIP {VERSION}  font: {font_path or '(default)'}", flush=True)
        # 矢印の字形が無いフォントなら英字で代用
        ok = self.font_s.getmask("↑").getbbox() is not None
        self.AR = dict(u="↑", d="↓", l="←", r="→") if ok else dict(u="F", d="X", l="Z", r="C")
        self.st = load_settings()
        if args.lang:
            self.st["lang"] = args.lang
        self.pv = Preview(self.st["zoom"])
        self.mode = M_CLIP
        self.cam_on = True
        self.toast = ("", 0.0)
        self.items, self.sel, self.top = [], 0, 0
        self.set_sel = 0
        self.qr_img = None
        it = clip_items(1)
        self.last_id = it[0]["id"] if it else 0
        self.next_check = time.time() + 2
        self.next_list = 0.0

    # ---- drawing
    def canvas(self):
        return Image.new("RGB", (LCD_W, LCD_H), BG)

    def chrome(self, img, hint, tabs=True, title=None):
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=(0, 0, 0))
        if tabs:
            w = LCD_W // len(MODES)
            for i, name in enumerate(MODES):
                on = i == self.mode
                if on:
                    d.rectangle((i * w, 0, (i + 1) * w - 1, TOP_H - 1), fill=BAR)
                label = f"{i + 1} {name}"
                tw = self.font_s.getlength(label)
                d.text((i * w + (w - tw) / 2, 1), label, fill=(255, 255, 255) if on else SUB,
                       font=self.font_s)
        else:
            d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=BAR)
            d.text((4, 1), title or "", fill=(255, 255, 255), font=self.font_s)
        d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
        d.text((4, LCD_H - BOT_H + 1), hint, fill=(210, 210, 210), font=self.font_s)
        msg, until = self.toast
        if msg and time.time() < until:
            w = int(self.font_s.getlength(msg)) + 16
            d.rounded_rectangle(((LCD_W - w) // 2, TOP_H + 4, (LCD_W + w) // 2, TOP_H + 24), 8,
                                fill=(0, 120, 70))
            d.text(((LCD_W - w) // 2 + 8, TOP_H + 6), msg, fill=(255, 255, 255), font=self.font_s)
        return img

    def say(self, msg, sec=2.5):
        self.toast = (msg, time.time() + sec)

    def message(self, title, body):
        img = self.canvas()
        ImageDraw.Draw(img).text((8, 60), body, fill=FG, font=self.font)
        self.fb.show(self.chrome(img, "", tabs=False, title=title))

    # ---- camera on/off (クリップ以外では止めて電池を節約)
    def set_cam(self, on):
        if on == self.cam_on:
            return
        try:
            if on:
                self.cam.start()
                time.sleep(0.4)
            else:
                self.cam.stop()
        except Exception as e:
            print("camera switch failed:", e)
        self.cam_on = on

    def set_mode(self, m):
        self.mode = m % len(MODES)
        self.set_cam(self.mode == M_CLIP)
        self.qr_img = None
        self.next_list = 0.0

    # ---- 1 クリップ
    def draw_clip(self):
        img = self.pv.frame(self.cam)
        A = self.AR
        self.chrome(img, f"ENTER:読取  {A['u']}{A['d']}:ズーム x{self.pv.zoom + 1}")
        d = ImageDraw.Draw(img)
        bx = 226
        d.text((bx - 40, LCD_H - BOT_H + 1), "FOCUS", fill=SUB, font=self.font_s)
        d.rectangle((bx, LCD_H - 12, bx + 90, LCD_H - 4), outline=(120, 120, 120))
        d.rectangle((bx, LCD_H - 12, bx + int(90 * self.pv.ratio), LCD_H - 4), fill=self.pv.color())
        return img

    def do_ocr(self):
        self.message("クリップ", "読み取り中...")
        t0 = time.time()
        text = run_ocr(capture_crop(self.cam, self.pv.cw, self.st["hires"]),
                       self.st["lang"], self.args.psm)
        dt = time.time() - t0
        print(f"----- OCR ({dt:.1f}s)\n{text}\n-----", flush=True)
        out = self.run_editor(text, dt)
        if out and out.strip():
            self.say(send_clip(out, "ocr"))
            it = clip_items(1)
            if it:
                self.last_id = max(self.last_id, it[0]["id"])
        else:
            self.say("破棄しました")

    def run_editor(self, text, elapsed=0.0):
        """編集画面(モーダル)。送る→文字列 / 破棄→None"""
        ed = Editor(text, self.font, self.font_s, elapsed)
        self.keys.flush(0.2)
        while True:
            self.fb.show(ed.render())
            for kind, val in self.keys.poll(0.5):
                r = ed.handle(kind, val)
                if r:
                    self.keys.flush(0.2)
                    return ed.text if r == "SEND" else None

    # ---- 2 履歴
    ROWS, ROW_H = 7, 19

    def refresh_items(self):
        items = clip_items(50)
        if items is None:
            self.items = None
            return
        cur = self.items[self.sel]["id"] if self.items and self.sel < len(self.items) else None
        self.items = items
        if cur is not None:                 # 選択中の項目を追いかける
            for i, it in enumerate(items):
                if it["id"] == cur:
                    self.sel = i
                    break
        self.sel = max(0, min(self.sel, len(items) - 1))

    def draw_hist(self):
        img = self.canvas()
        d = ImageDraw.Draw(img)
        A = self.AR
        if self.items is None:
            d.text((8, 50), "HEXA CLIP が停止中です", fill=(255, 120, 120), font=self.font)
            d.text((8, 78), "sudo systemctl start hexaclip", fill=SUB, font=self.font_s)
        elif not self.items:
            d.text((8, 50), "まだ何もありません", fill=SUB, font=self.font)
            d.text((8, 78), "スマホから送るか、クリップで読み取り", fill=SUB, font=self.font_s)
        else:
            if self.sel < self.top:
                self.top = self.sel
            elif self.sel >= self.top + self.ROWS:
                self.top = self.sel - self.ROWS + 1
            colors = {"ocr": (47, 214, 123), "web": (90, 170, 255), "cli": (255, 190, 70)}
            for r in range(self.ROWS):
                i = self.top + r
                if i >= len(self.items):
                    break
                it = self.items[i]
                y = TOP_H + 2 + r * self.ROW_H
                if i == self.sel:
                    d.rectangle((0, y, LCD_W - 5, y + self.ROW_H - 1), fill=(34, 48, 58))
                    d.rectangle((0, y, 2, y + self.ROW_H - 1), fill=ACC)
                d.text((6, y + 3), it["source"][:3], fill=colors.get(it["source"], SUB),
                       font=self.font_s)
                if i == 0:                      # fn+V で貼り付けられる項目
                    d.text((LCD_W - 40, y + 3), "fn+V", fill=ACC, font=self.font_s)
                line = " ".join(it["text"].split())
                lim = LCD_W - (84 if i == 0 else 44)
                while line and self.font_s.getlength(line) > lim:
                    line = line[:-2] + "…" if not line.endswith("…") else line[:-2] + "…"
                d.text((34, y + 3), line, fill=FG, font=self.font_s)
            if len(self.items) > self.ROWS:
                h = self.ROWS * self.ROW_H
                bh = max(8, h * self.ROWS // len(self.items))
                by = TOP_H + 2 + (h - bh) * self.top // max(1, len(self.items) - self.ROWS)
                d.rectangle((LCD_W - 3, by, LCD_W - 1, by + bh), fill=(90, 98, 115))
        n = len(self.items) if self.items else 0
        return self.chrome(img, f"{A['u']}{A['d']}:選ぶ ENTER:開く V:貼付用にする  {n}件")

    def run_detail(self, item):
        """全文表示(モーダル)"""
        A = self.AR
        text, scroll, armed, note = item["text"], 0, False, ""
        rows = (LCD_H - TOP_H - BOT_H - 2) // 20
        self.keys.flush(0.2)
        while True:
            lines = layout(text, self.font, LCD_W - 12)
            scroll = max(0, min(scroll, max(0, len(lines) - rows)))
            img = self.canvas()
            d = ImageDraw.Draw(img)
            for r in range(rows):
                if scroll + r >= len(lines):
                    break
                s, e = lines[scroll + r]
                d.text((5, TOP_H + 2 + r * 20), text[s:e], fill=FG, font=self.font)
            if len(lines) > rows:
                h = rows * 20
                bh = max(8, h * rows // len(lines))
                by = TOP_H + 2 + (h - bh) * scroll // max(1, len(lines) - rows)
                d.rectangle((LCD_W - 3, by, LCD_W - 1, by + bh), fill=(90, 98, 115))
            when = datetime.datetime.fromtimestamp(item["created"]).strftime("%m/%d %H:%M")
            title = note or f"#{item['id']}  {item['source']}  {when}  {len(text)}文字"
            hint = ("もう一度 D で削除 / 他のキーで中止" if armed
                    else f"{A['u']}{A['d']}:送り V:貼付用 E:編集 S:保存 D:削除")
            self.fb.show(self.chrome(img, hint, tabs=False, title=title))
            for kind, val in nav(self.keys.poll(0.5)):
                key = val.lower() if kind == "CHAR" else val
                if armed:
                    armed = False
                    if key == "d":
                        try:
                            clip_call("DELETE", f"/api/items/{item['id']}")
                            self.say(f"#{item['id']} を削除")
                        except Exception:
                            self.say("削除できませんでした")
                        return
                    note = ""
                    continue
                note = ""
                if key in ("ESC", "LEFT"):
                    return
                if key == "UP":
                    scroll -= 1
                elif key == "DOWN":
                    scroll += 1
                elif key == "d":
                    armed = True
                elif key == "v":
                    self.make_paste_target(item)
                    return
                elif key == "s":
                    p = save_file(text, f"clip_{item['id']}.txt")
                    note = "保存: ~/clip/" + os.path.basename(p)
                elif key == "e":
                    out = self.run_editor(text)
                    if out and out.strip() and out != text:
                        self.say(send_clip(out, "edit"))
                        return
                    self.keys.flush(0.1)

    def make_paste_target(self, item):
        if self.items and item["id"] == self.items[0]["id"]:
            self.say("貼付用です  ESCで閉じて fn+V", 3)
            return
        new = to_top(item["id"])
        if new:
            self.last_id = max(self.last_id, new)
            self.sel = 0
            self.say(f"#{new} を貼付用に  ESCで閉じて fn+V", 3)
            self.refresh_items()
            self.sel = 0
        else:
            self.say("できませんでした")

    # ---- 3 QR
    def draw_qr(self):
        if self.qr_img is None:
            self.qr_img = self.build_qr()
        return self.chrome(self.qr_img.copy(), "スマホでQRを読む(同じWi-Fi) TAB:次へ ESC:終了")

    def build_qr(self):
        img = self.canvas()
        d = ImageDraw.Draw(img)
        try:
            info = clip_call("GET", "/api/pin", timeout=1.0)
        except Exception:
            d.text((8, 50), "HEXA CLIP が停止中です", fill=(255, 120, 120), font=self.font)
            d.text((8, 78), "sudo systemctl start hexaclip", fill=SUB, font=self.font_s)
            return img
        url, pin = info["url"], info["pin"]
        x = 8
        side = LCD_H - TOP_H - BOT_H - 4
        try:
            import qrcode
            qr = qrcode.QRCode(border=2, box_size=1,
                               error_correction=qrcode.constants.ERROR_CORRECT_L)
            qr.add_data(url + "#pin=" + pin)
            qr.make(fit=True)
            m = np.array(qr.get_matrix(), dtype=np.uint8)
            scale = max(1, side // m.shape[0])
            q = Image.fromarray((1 - m) * 255).resize(
                (m.shape[1] * scale, m.shape[0] * scale), Image.NEAREST)
            img.paste(q, (4, TOP_H + 2 + (side - q.height) // 2))
            x = 4 + q.width + 10
        except ImportError:
            d.text((8, 118), "QR: sudo apt install python3-qrcode", fill=(255, 200, 0),
                   font=self.font_s)
        host = url.replace("http://", "").rstrip("/")
        d.text((x, TOP_H + 6), "または URL", fill=SUB, font=self.font_s)
        for i, part in enumerate(host.split(":")):
            d.text((x, TOP_H + 24 + i * 17), part if i == 0 else ":" + part, fill=FG,
                   font=self.font_s)
        d.text((x, TOP_H + 66), "PIN", fill=SUB, font=self.font_s)
        d.text((x, TOP_H + 82), pin, fill=ACC, font=self.font)
        return img

    # ---- 4 設定
    def draw_set(self):
        img = self.canvas()
        d = ImageDraw.Draw(img)
        A = self.AR
        for i, (key, label, vals, names) in enumerate(SETTING_DEFS):
            y = TOP_H + 3 + i * 26
            on = i == self.set_sel
            if on:
                d.rectangle((0, y, LCD_W, y + 23), fill=(34, 48, 58))
                d.rectangle((0, y, 2, y + 23), fill=ACC)
            d.text((8, y + 5), label, fill=FG, font=self.font_s)
            try:
                name = names[vals.index(self.st[key])]
            except ValueError:
                name = str(self.st[key])
            txt = f"{A['l']} {name} {A['r']}" if on else name
            d.text((LCD_W - 8 - self.font_s.getlength(txt), y + 5), txt,
                   fill=ACC if on else SUB, font=self.font_s)
        return self.chrome(img, f"{A['u']}{A['d']}:選ぶ {A['l']}{A['r']}:変える(すぐ保存) ESC:終了")

    def change_setting(self, step):
        key, _, vals, _ = SETTING_DEFS[self.set_sel]
        try:
            i = vals.index(self.st[key])
        except ValueError:
            i = 0
        self.st[key] = vals[(i + step) % len(vals)]
        save_settings(self.st)
        if key == "zoom":
            self.pv.set_zoom(self.st["zoom"])

    # ---- 受信の通知
    def check_incoming(self):
        if time.time() < self.next_check:
            return
        self.next_check = time.time() + 2
        it = clip_items(1)
        if not it or it[0]["id"] <= self.last_id:
            return
        self.last_id = it[0]["id"]
        if it[0]["source"] in ("ocr", "edit"):
            return
        self.say(f"受信 #{it[0]['id']} ({it[0]['source']})", 3)
        self.next_list = 0.0
        if self.st["auto_show"]:
            self.set_mode(M_HIST)
            self.refresh_items()
            self.sel = 0
            self.run_detail(it[0])
            self.next_list = 0.0

    # ---- main loop
    def run(self):
        self.keys.flush(1.0)
        print("ready.", flush=True)
        frames = 0
        while True:
            if self.mode == M_CLIP:
                img = self.draw_clip()
            elif self.mode == M_HIST:
                if time.time() > self.next_list:
                    self.refresh_items()
                    self.next_list = time.time() + 2
                img = self.draw_hist()
            elif self.mode == M_QR:
                img = self.draw_qr()
            else:
                img = self.draw_set()
            self.fb.show(img)
            frames += 1

            for kind, val in nav(self.keys.poll(0.0 if self.mode == M_CLIP else 0.25)):
                if kind == "ACT" and val == "ESC":
                    return f"ESC / frames={frames}"
                if kind == "ACT" and val == "TAB":
                    self.set_mode(self.mode + 1)
                    break
                if kind == "CHAR" and val in "1234":
                    self.set_mode(int(val) - 1)
                    break
                if (kind == "CHAR" and val.lower() == "v" and self.mode == M_HIST
                        and self.items):
                    self.make_paste_target(self.items[self.sel])
                    break
                if kind != "ACT":
                    continue
                if self.mode == M_CLIP:
                    if val == "ENTER":
                        self.do_ocr()
                        break
                    if val in ("UP", "DOWN"):
                        self.pv.set_zoom(self.pv.zoom + (1 if val == "UP" else -1))
                elif self.mode == M_HIST and self.items:
                    if val == "UP":
                        self.sel = max(0, self.sel - 1)
                    elif val == "DOWN":
                        self.sel = min(len(self.items) - 1, self.sel + 1)
                    elif val in ("ENTER", "RIGHT"):
                        self.run_detail(self.items[self.sel])
                        self.next_list = 0.0
                        break
                elif self.mode == M_SET:
                    if val == "UP":
                        self.set_sel = max(0, self.set_sel - 1)
                    elif val == "DOWN":
                        self.set_sel = min(len(SETTING_DEFS) - 1, self.set_sel + 1)
                    elif val in ("LEFT", "RIGHT", "ENTER"):
                        self.change_setting(-1 if val == "LEFT" else 1)
            self.check_incoming()


# ---------------------------------------------------------------- launcher
def launcher_pids():
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read()
        except OSError:
            continue
        if b"M5CardputerZero-APPLaunch" in cmd:
            pids.append(int(d))
    return pids


def signal_launcher(sig):
    for pid in launcher_pids():
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-l", "--lang", help="OCR言語を一時的に指定 (設定より優先)")
    ap.add_argument("--psm", type=int, default=6)
    ap.add_argument("--debug-keys", action="store_true")
    ap.add_argument("--keys", help="キー割り当てJSON")
    ap.add_argument("--no-pause", action="store_true", help="ランチャーを一時停止しない")
    a = ap.parse_args()
    cfg = json.load(open(a.keys)) if a.keys else None

    fb = FrameBuffer()
    keys = Keys(find_keyboard(), a.debug_keys, cfg)
    cam = open_camera()
    paused = False
    if not a.no_pause and launcher_pids():
        signal_launcher(signal.SIGSTOP)
        paused = True
        # 強制終了(SIGTERM/SIGHUP)でも必ずランチャーを再開する
        for sg in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sg, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        print("launcher paused", flush=True)
    reason = "unknown"
    try:
        reason = App(fb, keys, cam, a).run()
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    except Exception:
        import traceback
        traceback.print_exc()
        reason = "error"
    finally:
        print(f"exit: {reason}", flush=True)
        try:
            cam.stop()
        except Exception:
            pass
        keys.close()
        fb.clear()
        if paused:
            signal_launcher(signal.SIGCONT)
            print("launcher resumed", flush=True)


if __name__ == "__main__":
    main()
