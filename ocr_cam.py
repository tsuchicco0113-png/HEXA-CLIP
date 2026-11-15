#!/usr/bin/env python3
"""HEXA CLIP — CardputerZero 用アプリ (v0.7.2)

4つのモードを上のタブで切り替える:
  1 クリップ : カメラで文字を読み取り → 修正 → HEXA CLIPへ送る
  2 履歴     : 受信/送信した内容の一覧・全文表示・編集・保存・削除
  3 QR       : スマホ接続用のQRコード (読むだけでログイン)
  4 設定     : OCR言語 / 全画素撮影 / ズーム / 受信時の自動表示

共通の操作:
  TAB = 次のモード   1〜4 = モードを直接選択   ESC = 戻る (一番上の画面では確認のうえ終了)
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

VERSION = "0.7.2"
LCD_W, LCD_H = 320, 170
TOP_H, BOT_H = 18, 16                # タブバー / キー案内バー
MAIN_W, MAIN_H = 1640, 1232          # センサー全画角(2x2ビニング)
FULL_W, FULL_H = 3280, 2464          # 撮影用の全画素
LORES_W, LORES_H = 640, 480          # プレビュー用(同じ画角)
ZOOMS = [0.6, 0.3, 0.2]              # プレビューに映す横幅(全画角に対する割合) x1 / x2 / x3
STILL_CFG = None                     # 全画素撮影の設定(open_cameraで作る)
BOX = (24, 60, 296, 114)             # 読み取り枠(LCD座標)
MAX_OCR_W = 3000                     # OCRに渡す最大幅(全画素の枠はこれ未満なので縮小されない)

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
    ("lang", "OCRの言語", ["jpn", "jpn+eng", "eng", "code"],
     ["日本語", "日本語+英語", "英語", "コード(空白保持)"]),
    ("hires", "高精細撮影", [True, False], ["ON (小さい文字向き)", "OFF (速い)"]),
    ("burst", "連写して選ぶ", [3, 1, 5], ["3枚", "1枚 (速い)", "5枚"]),
    ("low_conf", "自信のない文字", ["mark", "drop"], ["色付きで残す", "消す"]),
    ("zoom", "起動時のズーム", [0, 1, 2], ["x1", "x2", "x3"]),
    ("auto_show", "受信したら表示", [False, True], ["OFF (通知のみ)", "ON (本文を開く)"]),
    ("paste_erase", "fn+Vで入るVを消す", [True, False], ["ON", "OFF"]),
    ("paste_newline", "fn+Vの改行", ["enter", "none"], ["ENTERで打つ", "打たない (1行に)"]),
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


PASTE_MAX = 4000                                               # czpasted.py と同じ
PASTEABLE = set("".join(CHARS.values())) | {" ", "\n", "\t"}


def paste_note(text):
    """fn+V で打てない分の説明 ("" なら全部打てる)"""
    text = text.rstrip("\n")
    bad = sum(1 for ch in text[:PASTE_MAX] if ch not in PASTEABLE)
    notes = []
    if bad:
        notes.append(f"日本語など{bad}字は打てません")
    if len(text) > PASTE_MAX:
        notes.append(f"{PASTE_MAX}字まで")
    return " / ".join(notes)


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
            shift = K_LSHIFT in self.held or K_RSHIFT in self.held or self.shift_once
            if self.caps and CHARS[code][0].isalpha():
                shift = not shift                # CapsLock は英字だけに効かせる (1 → ! にしない)
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
    """OCR向けの前処理: 白地に黒へそろえる / 小さい切り出しは拡大 / 極端に大きければ縮小

    全画素撮影の枠(x1で約1670px)は縮小しない。以前は1400pxへ縮めて解像度を捨てていた。"""
    if np.asarray(img).mean() < 110:            # 暗い背景(ダークモードの画面など)は反転
        img = ImageOps.invert(img)
    if img.width < 900:                         # 小さい文字: 2倍に拡大
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    elif img.width > MAX_OCR_W:
        img = img.resize((MAX_OCR_W, int(img.height * MAX_OCR_W / img.width)), Image.LANCZOS)
    return ImageOps.autocontrast(img, cutoff=1)


def capture_crop(cam, cw, hires=True, burst=3, settle=0.3):
    """枠内を切り出す。hires=True なら全画素モードに切り替えて撮る

    ENTER を押した揺れが収まるまで settle 秒待ち、burst 枚撮って一番シャープな1枚を使う。
    戻り値: (前処理した画像, 情報文字列)"""
    time.sleep(settle)
    f = box_frac(cw)

    def crop(y, W, H):
        x0, y0, x1, y1 = int(f[0] * W), int(f[1] * H), int(f[2] * W), int(f[3] * H)
        return np.ascontiguousarray(y[:H, :W][y0:y1, x0:x1])

    burst = max(1, int(burst))
    frames = []
    if hires and STILL_CFG is not None:
        prev = getattr(cam, "camera_config", None)
        try:
            if burst == 1 or prev is None:
                frames.append(crop(cam.switch_mode_and_capture_array(STILL_CFG, "main"),
                                   FULL_W, FULL_H))
            else:
                cam.switch_mode(STILL_CFG)
                try:
                    for _ in range(burst):
                        frames.append(crop(cam.capture_array("main"), FULL_W, FULL_H))
                finally:
                    cam.switch_mode(prev)       # プレビューに戻す
        except Exception as e:
            print("hires capture failed, fallback:", e, flush=True)
    if not frames:
        for _ in range(burst):
            frames.append(crop(cam.capture_array("main"), MAIN_W, MAIN_H))
    scores = [sharpness(fr) for fr in frames]
    best = int(np.argmax(scores))
    img = Image.fromarray(frames[best])
    img.save("/tmp/ocr_cam_raw.png")
    info = f"{best + 1}/{len(frames)}枚目" if len(frames) > 1 else ""
    return prepare(img), info


OCR_LANGS = {"jpn": "jpn", "jpn+eng": "jpn+eng", "eng": "eng", "code": "eng"}


def run_ocr(img, lang, psm, min_conf=60.0, path="/tmp/ocr_cam_crop.png", timeout=90):
    """文字列だけ返す (信頼度の低い単語は捨てる)"""
    return run_ocr_ex(img, lang, psm, min_conf, keep_low=None, path=path, timeout=timeout)[0]


def run_ocr_ex(img, lang, psm, min_conf=60.0, keep_low=30.0, path="/tmp/ocr_cam_crop.png",
               timeout=90):
    """TSVで単語ごとの信頼度を取る → (テキスト, 自信のない範囲 [(start, end), ...])

    min_conf 未満の単語: keep_low 以上なら「自信なし」として残し範囲を返す。
                         keep_low=None または keep_low 未満なら捨てる (反射などのノイズ)
    lang: jpn / jpn+eng / eng / code (英語 + 単語間の空白を位置から復元)
    path: 一時ファイル。czclipd (スマホ撮影のOCR) からは別名で呼ぶ
          (sudo で動いた本体アプリの /tmp ファイルを上書きできず失敗するのを防ぐ)"""
    img.save(path)
    r = subprocess.run(["tesseract", path, "-", "-l", OCR_LANGS.get(lang, lang),
                        "--psm", str(psm), "tsv"],
                       capture_output=True, text=True, timeout=timeout)
    lines, order = {}, []
    for row in r.stdout.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            conf = float(c[10])
            left, width = int(c[6]), int(c[8])
        except ValueError:
            continue
        low = conf < min_conf
        if low and (keep_low is None or conf < keep_low):
            continue
        key = (c[2], c[3], c[4])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append((c[11], left, width, low))
    return join_lines([lines[k] for k in order], exact_spaces=(lang == "code"))


QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
                        "\u2018": "'", "\u2019": "'", "\u2032": "'", "\u00b4": "'"})


def join_lines(lines, exact_spaces=False):
    """単語 [(文字, left, width[, low])] の行リスト → (テキスト, 自信のない範囲)

    - 行頭の位置からインデントを復元する (コードの字下げが消えないように)
    - exact_spaces: 単語間の空白の数も位置から復元する (コードの桁揃えなど)
    - “ ” ‘ ’ を " ' にそろえる (コードで使えない & fn+V で打てないため)
    """
    if not lines:
        return "", []
    # 半角1文字の幅: ASCIIだけの単語から推定
    ws = [(w[0], w[2]) for ln in lines for w in ln if w[0].isascii() and len(w[0]) >= 2]
    cw = sum(wd for _, wd in ws) / max(1, sum(len(w) for w, _ in ws)) if ws else 0
    base = min(ln[0][1] for ln in lines)
    out, marks, pos = [], [], 0
    for ln in lines:
        indent = int(round((ln[0][1] - base) / cw)) if cw else 0
        s = " " * indent if 1 <= indent <= 40 else ""
        prev = None
        for w in ln:
            text, left, width = w[0], w[1], w[2]
            if prev is not None:
                if exact_spaces and cw:
                    s += " " * max(1, min(40, int(round((left - prev[1] - prev[2]) / cw))))
                elif s[-1:].isascii() and text[:1].isascii():
                    s += " "
            if len(w) > 3 and w[3]:
                marks.append((pos + len(s), pos + len(s) + len(text)))
            s += text
            prev = w
        out.append(s.translate(QUOTES))
        pos += len(s) + 1
    return "\n".join(out), marks


# ---------------------------------------------------------------- clip API
def clip_call(method, path, body=None, timeout=2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CLIP_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def clean(text):
    return text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")


def clip_items(limit=50, preview=200):
    """一覧 (本文は先頭 preview 文字だけ。全文は clip_get で)"""
    try:
        items = clip_call("GET", f"/api/items?limit={limit}&preview={preview}", timeout=1.0)["items"]
        for it in items:
            it["text"] = clean(it["text"])
        return items
    except Exception:
        return None                      # None = サーバー停止中


def clip_get(item):
    """全文が必要なとき (一覧はプレビューなので)"""
    if not item.get("truncated"):
        return item
    try:
        full = clip_call("GET", f"/api/items/{item['id']}", timeout=2.0)
        full["text"] = clean(full["text"])
        return full
    except Exception:
        return item


def clip_rev():
    try:
        return clip_call("GET", "/api/ping", timeout=1.0).get("rev")
    except Exception:
        return None


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
_LAYOUT_CACHE = {}


def layout(text, font, width):
    """折り返し(同じ入力なら前回の結果を使う。大きいクリップで毎フレーム計算して重くなるのを防ぐ)"""
    key = (id(font), width, text)
    r = _LAYOUT_CACHE.get(key)
    if r is None:
        if len(_LAYOUT_CACHE) > 8:
            _LAYOUT_CACHE.clear()
        r = _LAYOUT_CACHE[key] = _layout(text, font, width)
    return r


def _layout(text, font, width):
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
    LOW = (255, 170, 60)                     # 自信のない文字の色

    def __init__(self, text, font, font_s, elapsed, marks=None):
        self.text, self.cur = text, len(text)
        self.orig = text
        self.font, self.font_s = font, font_s
        self.elapsed = elapsed
        self.title = f"編集  OCR {elapsed:.1f}s" if elapsed else "編集"
        self.scroll = 0
        self.width = LCD_W - 10
        self.rows = (LCD_H - self.TOP - self.BOTTOM) // self.LINE_H
        self.marks = [list(m) for m in (marks or []) if 0 <= m[0] < m[1] <= len(text)]
        self.armed = False                   # ESC をもう一度押すと破棄

    # ---- 自信のない範囲を編集に合わせてずらす (触った範囲は「確認済み」として消す)
    def _ins(self, p):
        keep = []
        for m in self.marks:
            if m[0] >= p:
                m[0] += 1
                m[1] += 1
            elif m[0] < p < m[1]:
                continue
            keep.append(m)
        self.marks = keep

    def _del(self, p):
        keep = []
        for m in self.marks:
            if m[0] <= p < m[1]:
                continue
            if m[0] > p:
                m[0] -= 1
                m[1] -= 1
            keep.append(m)
        self.marks = keep

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
        if self.armed:
            self.armed = False
            if kind == "ACT" and val == "ESC":
                return "CANCEL"
            return None                      # 他のキーは「やめる」だけ (入力はしない)
        t = self.text
        if kind == "CHAR":
            self._ins(self.cur)
            self.text = t[:self.cur] + val + t[self.cur:]
            self.cur += 1
        elif val == "TAB":
            self._ins(self.cur)
            self.text = t[:self.cur] + "\n" + t[self.cur:]
            self.cur += 1
        elif val == "BS" and self.cur > 0:
            self._del(self.cur - 1)
            self.text = t[:self.cur - 1] + t[self.cur:]
            self.cur -= 1
        elif val == "DEL" and self.cur < len(t):
            self._del(self.cur)
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
            if self.text == self.orig and not self.elapsed:
                return "CANCEL"              # 何も変えていない (履歴からの編集)
            self.armed = True
        return None

    def _segments(self, s, e):
        """行 [s, e) を (文字列, 自信なしか) に分ける"""
        cuts = {s, e}
        for a, b in self.marks:
            if a < e and b > s:
                cuts.update((max(a, s), min(b, e)))
        pts = sorted(cuts)
        out = []
        for a, b in zip(pts, pts[1:]):
            low = any(m[0] <= a and b <= m[1] for m in self.marks)
            out.append((self.text[a:b], low))
        return out

    def render(self):
        img = Image.new("RGB", (LCD_W, LCD_H), (14, 16, 22))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, LCD_W, 17), fill=(120, 30, 30) if self.armed else (0, 90, 60))
        if self.armed:
            title = "もう一度ESCで破棄 / 他のキーで戻る"
        else:
            title = f"{self.title}  {len(self.text)}文字"
            if self.marks:
                title += f"  要確認{len(self.marks)}"
        d.text((4, 1), title, fill=(255, 255, 255), font=self.font_s)
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
            if self.marks:
                x = 5.0
                for seg, low in self._segments(s, e):
                    w = self.font.getlength(seg)
                    if low:
                        d.line((x, y + self.LINE_H - 2, x + w, y + self.LINE_H - 2),
                               fill=self.LOW, width=1)
                    d.text((x, y), seg, fill=self.LOW if low else (235, 238, 245), font=self.font)
                    x += w
            else:
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
        hint = ("橙=自信なし ENTER:送る ESC:破棄 TAB:改行" if self.marks
                else "ENTER:送る ESC:破棄 TAB:改行 fn+FXZC:移動")
        d.text((4, LCD_H - 15), hint, fill=(200, 200, 200), font=self.font_s)
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
        it = clip_items(1, 80)
        self.last_id = it[0]["id"] if it else 0
        self.rev = clip_rev()
        self.next_check = time.time() + 1
        self.next_list = 0.0                # 0 = 一覧を取り直す

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

    def confirm_quit(self, base):
        """終了の確認ウィンドウ。ENTER で True、それ以外のキーで False (戻る)"""
        img = Image.blend(base, Image.new("RGB", base.size), 0.65)
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = 40, 42, LCD_W - 40, LCD_H - 38
        d.rounded_rectangle((x0, y0, x1, y1), 8, fill=(26, 30, 38), outline=ACC, width=2)
        for i, (txt, font, col) in enumerate([
                ("HEXA CLIP を終了しますか?", self.font, FG),
                ("ENTER: 終了    ESC: 戻る", self.font_s, ACC)]):
            w = font.getlength(txt)
            d.text(((LCD_W - w) / 2, y0 + 16 + i * 32), txt, fill=col, font=font)
        self.fb.show(img)
        self.keys.flush(0.2)
        while True:
            for kind, val in self.keys.poll(0.5):
                self.keys.flush(0.1)
                return kind == "ACT" and val == "ENTER"

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
        try:
            img, info = capture_crop(self.cam, self.pv.cw, self.st["hires"], self.st["burst"])
            text, marks = run_ocr_ex(img, self.st["lang"], self.args.psm,
                                     keep_low=30.0 if self.st["low_conf"] == "mark" else None)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.recover_camera()
            self.say(f"読み取り失敗: {type(e).__name__}", 4)
            self.keys.flush(0.2)
            return
        dt = time.time() - t0
        print(f"----- OCR ({dt:.1f}s {info})\n{text}\n-----", flush=True)
        out = self.run_editor(text, dt, marks)
        if out and out.strip():
            self.say(send_clip(out, "ocr"))
            it = clip_items(1)
            if it:
                self.last_id = max(self.last_id, it[0]["id"])
        else:
            self.say("破棄しました")

    def recover_camera(self):
        """撮影の失敗後、プレビューを動く状態に戻す"""
        try:
            self.cam.stop()
        except Exception:
            pass
        try:
            self.cam.start()
            time.sleep(0.4)
            self.cam_on = True
        except Exception as e:
            print("camera restart failed:", e, flush=True)

    def run_editor(self, text, elapsed=0.0, marks=None):
        """編集画面(モーダル)。送る→文字列 / 破棄→None"""
        ed = Editor(text, self.font, self.font_s, elapsed, marks)
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
            colors = {"ocr": (47, 214, 123), "web": (90, 170, 255), "cli": (255, 190, 70),
                      "cam": (200, 140, 255)}
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
                lim = LCD_W - (84 if i == 0 else 44)
                d.text((34, y + 3), self.ellipsize(it["text"], lim), fill=FG, font=self.font_s)
            if len(self.items) > self.ROWS:
                h = self.ROWS * self.ROW_H
                bh = max(8, h * self.ROWS // len(self.items))
                by = TOP_H + 2 + (h - bh) * self.top // max(1, len(self.items) - self.ROWS)
                d.rectangle((LCD_W - 3, by, LCD_W - 1, by + bh), fill=(90, 98, 115))
        n = len(self.items) if self.items else 0
        return self.chrome(img, f"{A['u']}{A['d']}:選ぶ ENTER:開く V:貼付用にする  {n}件")

    def ellipsize(self, text, lim):
        """1行に収まるように省略。先頭だけ見て二分探索 (長いクリップで固まらない)"""
        line = " ".join(text[:400].split())
        if self.font_s.getlength(line) <= lim:
            return line
        lo, hi = 0, len(line)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.font_s.getlength(line[:mid] + "…") <= lim:
                lo = mid
            else:
                hi = mid - 1
        return line[:lo] + "…"

    def run_detail(self, item):
        """全文表示(モーダル)"""
        item = clip_get(item)
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
        note = paste_note(clip_get(item)["text"])
        if self.items and item["id"] == self.items[0]["id"]:
            self.say(note or "貼付用です  ESCで閉じて fn+V", 4 if note else 3)
            return
        new = to_top(item["id"])
        if new:
            self.last_id = max(self.last_id, new)
            self.sel = 0
            self.say(f"#{new} を貼付用に  " + (note or "ESCで閉じて fn+V"), 4 if note else 3)
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
        if info.get("online") is False:
            d.text((8, 40), "Wi-Fi に接続されていません", fill=(255, 200, 0), font=self.font)
            d.text((8, 68), "本体の設定で Wi-Fi につなぐか、", fill=SUB, font=self.font_s)
            d.text((8, 86), "スマホのテザリングに接続してください", fill=SUB, font=self.font_s)
            d.text((8, 112), f"PIN {pin}", fill=ACC, font=self.font)
            return img
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
            d.text((8, LCD_H - BOT_H - 16), "QR: sudo apt install python3-qrcode", fill=(255, 200, 0),
                   font=self.font_s)
        host = url.replace("http://", "").rstrip("/")
        d.text((x, TOP_H + 6), "または URL", fill=SUB, font=self.font_s)
        for i, part in enumerate(host.split(":")):
            d.text((x, TOP_H + 24 + i * 17), part if i == 0 else ":" + part, fill=FG,
                   font=self.font_s)
        d.text((x, TOP_H + 62), "PIN", fill=SUB, font=self.font_s)
        d.text((x, TOP_H + 76), pin, fill=ACC, font=self.font)
        if info.get("mdns"):                # IPが変わっても使える名前
            name = info["mdns"].replace("http://", "").rstrip("/")
            d.text((x, TOP_H + 102), name, fill=SUB, font=self.font_s)
        return img

    # ---- 4 設定
    def draw_set(self):
        img = self.canvas()
        d = ImageDraw.Draw(img)
        A = self.AR
        rh = 22
        rows = (LCD_H - TOP_H - BOT_H - 2) // rh
        top = min(max(0, self.set_sel - rows + 1), max(0, len(SETTING_DEFS) - rows))
        top = min(top, self.set_sel)
        for r in range(rows):
            i = top + r
            if i >= len(SETTING_DEFS):
                break
            key, label, vals, names = SETTING_DEFS[i]
            y = TOP_H + 2 + r * rh
            on = i == self.set_sel
            if on:
                d.rectangle((0, y, LCD_W - 5, y + rh - 2), fill=(34, 48, 58))
                d.rectangle((0, y, 2, y + rh - 2), fill=ACC)
            d.text((8, y + 4), label, fill=FG, font=self.font_s)
            try:
                name = names[vals.index(self.st[key])]
            except ValueError:
                name = str(self.st[key])
            txt = f"{A['l']} {name} {A['r']}" if on else name
            d.text((LCD_W - 10 - self.font_s.getlength(txt), y + 4), txt,
                   fill=ACC if on else SUB, font=self.font_s)
        if len(SETTING_DEFS) > rows:
            h = rows * rh
            bh = max(8, h * rows // len(SETTING_DEFS))
            by = TOP_H + 2 + (h - bh) * top // max(1, len(SETTING_DEFS) - rows)
            d.rectangle((LCD_W - 3, by, LCD_W - 1, by + bh), fill=(90, 98, 115))
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
        self.next_check = time.time() + 1
        rev = clip_rev()
        if rev is None or rev == self.rev:
            return
        self.rev = rev
        self.next_list = 0.0                # 何か変わった → 履歴を取り直す
        it = clip_items(1, 80)
        if not it or it[0]["id"] <= self.last_id:
            return
        self.last_id = it[0]["id"]
        if it[0]["source"] in ("ocr", "edit") or it[0].get("moved"):
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
                if time.time() > self.next_list:   # 変更通知が来た時 (+念のため30秒ごと)
                    self.refresh_items()
                    self.next_list = time.time() + 30
                img = self.draw_hist()
            elif self.mode == M_QR:
                img = self.draw_qr()
            else:
                img = self.draw_set()
            self.fb.show(img)
            frames += 1

            for kind, val in nav(self.keys.poll(0.0 if self.mode == M_CLIP else 0.25)):
                if kind == "ACT" and val == "ESC":
                    if self.confirm_quit(img):
                        return f"ESC / frames={frames}"
                    break
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
