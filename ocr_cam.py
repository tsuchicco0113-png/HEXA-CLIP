#!/usr/bin/env python3
"""HEXA CLIP — CardputerZero 用アプリ (v1.1)

スマホやPCから送ったコード・資料を受け取って、本体で使う (fn+V で打ち込む / ~/clip/ に置く)。

起動するとハニカムのホーム画面。六角形を選んで ENTER (1〜4 キーで直接も):
  1 履歴   : 受け取ったものの一覧 → ENTER で全文 → ENTER でメニュー
             (貼付用 / 編集 / 保存 / 実行 / QRで表示 / ピン留め / 削除)
  2 接続   : スマホ接続用のQRコード (読むだけでログイン)
  3 カメラ : おまけ。本体カメラで文字やQRを読む (カメラは この画面でだけ動く)
  4 設定   : 受信時の動き / fn+V の改行 / カメラの設定
ホームには fn+V で貼れる内容と新着の数、assets/mascot/ に絵があればゴマエビちゃんが出る

共通の操作:
  ESC = ホームへ戻る (ホームでは確認のうえ終了)   1〜4 = 画面を直接選択   TAB = 次の画面
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
import math
import os
import select
import signal
import struct
import subprocess
import time
import urllib.request

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

VERSION = "1.1"
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
    ("auto_show", "受信したら", [False, True], ["通知だけ", "本文を開く"]),
    ("paste_newline", "fn+V の改行", ["enter", "none"], ["ENTERで打つ", "打たない (1行に)"]),
    ("lang", "カメラ: 言語", ["jpn", "jpn+eng", "eng", "code"],
     ["日本語", "日本語+英語", "英語", "コード"]),
    ("hires", "カメラ: 高精細", [True, False], ["ON (小さい文字)", "OFF (速い)"]),
    ("zoom", "カメラ: ズーム", [0, 1, 2], ["x1", "x2", "x3"]),
]
# 画面からは外した設定 (この値に固定)
FIXED_SETTINGS = {"burst": 3, "low_conf": "mark", "paste_erase": True}


def load_settings():
    st = {k: vals[0] for k, _, vals, _ in SETTING_DEFS}
    try:
        for k, v in json.load(open(SETTINGS_PATH)).items():
            if k in st:
                st[k] = v
    except (OSError, ValueError):
        pass
    st.update(FIXED_SETTINGS)
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


def find_font(size, bold=False):
    """日本語フォントを探す(細すぎるウェイトは後回し)。bold=True なら太いウェイトを優先"""
    cands = []
    for p in ["/usr/share/APPLaunch/**/*.tt[fc]", "/usr/share/APPLaunch/**/*.otf",
              "/usr/share/fonts/**/*CJK*", "/usr/share/fonts/**/*[Ii][Pp][Aa]*",
              "/usr/share/fonts/**/*wqy*", "/usr/share/fonts/**/*Droid*Fallback*"]:
        cands += glob.glob(p, recursive=True)

    def rank(f):
        n = os.path.basename(f).lower()
        if "serif" in n:
            return 50
        order = (["bold", "85", "heavy", "75", "black", "medium", "65", "regular", "55", "light", "thin"]
                 if bold else
                 ["medium", "65", "regular", "55", "bold", "75", "black", "light", "thin"])
        for i, w in enumerate(order):
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
    return cam                      # 起動はカメラ画面を開いた時 (set_cam)


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


def shutil_which(cmd):
    import shutil
    return shutil.which(cmd)


def decode_qr_file(path):
    """zbarimg で読む。中身はバイト列で受け取り UTF-8 → Shift_JIS の順に解釈"""
    r = subprocess.run(["zbarimg", "--raw", "-q", "-Sbinary", path],
                       capture_output=True, timeout=20)
    if r.returncode != 0 or not r.stdout:
        return ""
    raw = r.stdout[:-1] if r.stdout.endswith(b"\n") else r.stdout
    for enc in ("utf-8", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def read_qr(cam):
    """プレビュー画角の全体から QR/バーコードを探す。見つからなければ全画素でもう一度"""
    tries = [("main", MAIN_W, MAIN_H)]
    if STILL_CFG is not None:
        tries.append(("still", FULL_W, FULL_H))
    for kind, W, H in tries:
        if kind == "main":
            y = cam.capture_array("main")
        else:
            y = cam.switch_mode_and_capture_array(STILL_CFG, "main")
        img = Image.fromarray(np.ascontiguousarray(y[:H, :W]))
        path = "/tmp/ocr_cam_qr.png"
        img.save(path)
        text = decode_qr_file(path)
        if text:
            return text
    return ""


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


CLIP_LATEST = [0]          # fn+V で貼られる項目のID (検索中でも分かるように)


def clip_items(limit=50, preview=200, q=""):
    """一覧 (本文は先頭 preview 文字だけ。全文は clip_get で)。q: 検索語"""
    from urllib.parse import quote
    try:
        r = clip_call("GET", f"/api/items?limit={limit}&preview={preview}"
                      + (f"&q={quote(q)}" if q else ""), timeout=1.5)
        items = r["items"]
        CLIP_LATEST[0] = r.get("latest", items[0]["id"] if items else 0)
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


def fsize(n):
    n = n or 0
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB" if n < 1048576 else f"{n / 1048576:.1f}MB"


def demote_kwargs():
    """sudo で動いているときは、実行するコードを元のユーザーの権限で動かす"""
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
        env = dict(os.environ, HOME=HOME, USER=os.environ.get("SUDO_USER", ""))
        return {"user": uid, "group": gid, "extra_groups": [], "env": env}
    return {}


def stop_proc(proc):
    """プロセスグループごと止める (子プロセスも)"""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(1.5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


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


# ---------------------------------------------------------------- HEXA STAGE 風の部品
def hexpts(cx, cy, r, rot=30):
    """六角形の頂点 (rot=30 で上下が尖る向き)"""
    return [(cx + r * math.cos(math.radians(60 * i + rot)),
             cy + r * math.sin(math.radians(60 * i + rot))) for i in range(6)]


def honeycomb_bg():
    """ホーム画面の背景 (うっすらハニカム)。起動時に1回だけ作る"""
    img = Image.new("RGB", (LCD_W, LCD_H), BG)
    d = ImageDraw.Draw(img)
    for row in range(-1, 8):
        for col in range(-1, 12):
            cx = col * 30 + (15 if row % 2 else 0)
            cy = 20 + row * 26
            d.polygon(hexpts(cx, cy, 17), outline=(20, 24, 34))
    return img


def glow_hex(img, cx, cy, r, col):
    """光る六角形の枠 (ぼかした太線 + 細線)"""
    g = Image.new("RGB", img.size, (0, 0, 0))
    pts = hexpts(cx, cy, r)
    ImageDraw.Draw(g).line(pts + [pts[0]], fill=col, width=6)
    g = g.filter(ImageFilter.GaussianBlur(3))
    img.paste(ImageChops.lighter(img, g))
    ImageDraw.Draw(img).line(pts + [pts[0]], fill=col, width=2)


def draw_icon(d, kind, cx, cy, s, col):
    """ホームの六角形の中の線画アイコン"""
    if kind == M_HIST:                       # 一覧
        for i, w in enumerate((1.0, 0.75, 0.9)):
            y = cy - s * 0.45 + i * s * 0.45
            d.rounded_rectangle((cx - s * 0.55, y - s * 0.1, cx - s * 0.55 + s * 1.1 * w, y + s * 0.1),
                                2, fill=col)
    elif kind == M_QR:                       # つながる
        for ox in (-0.28, 0.28):
            d.rounded_rectangle((cx + ox * s - s * 0.38, cy - s * 0.2, cx + ox * s + s * 0.38,
                                 cy + s * 0.2), int(s * 0.2), outline=col, width=2)
    elif kind == M_CLIP:                     # カメラ
        d.rounded_rectangle((cx - s * 0.6, cy - s * 0.38, cx + s * 0.6, cy + s * 0.45), 4,
                            outline=col, width=2)
        d.rectangle((cx - s * 0.22, cy - s * 0.55, cx + s * 0.22, cy - s * 0.38), fill=col)
        d.ellipse((cx - s * 0.24, cy - s * 0.18, cx + s * 0.24, cy + s * 0.3), outline=col, width=2)
    else:                                    # 歯車
        for i in range(8):
            a = math.radians(45 * i)
            d.line((cx + math.cos(a) * s * 0.3, cy + math.sin(a) * s * 0.3,
                    cx + math.cos(a) * s * 0.58, cy + math.sin(a) * s * 0.58), fill=col, width=3)
        d.ellipse((cx - s * 0.38, cy - s * 0.38, cx + s * 0.38, cy + s * 0.38), outline=col, width=2)
        d.ellipse((cx - s * 0.12, cy - s * 0.12, cx + s * 0.12, cy + s * 0.12), fill=col)


MASCOT_H = LCD_H - TOP_H - BOT_H - 2      # 全身を縦いっぱいに (顔が分かる大きさ)
MASCOT_MAX_W = 66                          # これより横に広いポーズは小さめに


def load_mascot():
    """assets/mascot/ の立ち絵 (ゴマエビちゃん)。idle.png = ふだん / happy.png = 受信した時
    無ければ何も出さない。白ふち付きの透過PNGをそのまま置けば、本体用に縮める
    (横に広いポーズは幅を抑えて、説明文の場所を残す)"""
    out = {}
    for key in ("idle", "happy"):
        p = os.path.join(ASSET_DIR, "mascot", key + ".png")
        try:
            im = Image.open(p).convert("RGBA")
            bbox = im.getbbox()
            if bbox:
                im = im.crop(bbox)
            k = min(MASCOT_H / im.height, MASCOT_MAX_W / im.width)
            out[key] = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
        except (OSError, ValueError):
            pass
    if "happy" not in out and "idle" in out:
        out["happy"] = out["idle"]
    return out


def battery_percent():
    for p in glob.glob("/sys/class/power_supply/*/capacity"):
        try:
            return int(open(p).read().strip())
        except (OSError, ValueError):
            pass
    return None


# ---------------------------------------------------------------- app
M_HOME, M_HIST, M_QR, M_CLIP, M_SET = range(5)
SCREENS = [M_HIST, M_QR, M_CLIP, M_SET]          # ホームの六角形の並び (1〜4キーでも直接)
SCREEN_NAMES = {M_HIST: "履歴", M_QR: "接続", M_CLIP: "カメラ", M_SET: "設定"}
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
GLOW = (0, 60, 36)                                # 選択中の六角形の内側


class App:
    def __init__(self, fb, keys, cam, args):
        self.fb, self.keys, self.cam, self.args = fb, keys, cam, args
        self.font, font_path = find_font(16)
        self.font_s, _ = find_font(12)
        print(f"HEXA CLIP {VERSION}  font: {font_path or '(default)'}", flush=True)
        # 矢印の字形が無いフォントなら英字で代用
        ok = self.font_s.getmask("↑").getbbox() is not None
        self.AR = dict(u="↑", d="↓", l="←", r="→") if ok else dict(u="F", d="X", l="Z", r="C")
        self.STAR = "★" if self.font_s.getmask("★").getbbox() is not None else "*"
        self.st = load_settings()
        if args.lang:
            self.st["lang"] = args.lang
        self.pv = Preview(self.st["zoom"])
        self.mode = M_HOME                  # 最初はホーム (ハニカム)
        self.home_sel = 0
        self.unseen = 0                     # ホームの「新着」 (履歴を開くと0)
        self.happy_until = 0.0              # ゴマエビちゃんが喜んでいる時間
        self.status = {"t": 0.0}
        self.home_cache = (None, None)
        self.font_b, _ = find_font(19, bold=True)
        self.font_bs, _ = find_font(11, bold=True)
        self.font_t, _ = find_font(10)
        self.mascot = load_mascot()
        self.honey = honeycomb_bg()
        self.cam_on = False                 # カメラは「カメラ」画面でだけ動かす
        self.toast = ("", 0.0)
        self.items, self.sel, self.top = [], 0, 0
        self.query = ""                     # 履歴の検索語
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
        if tabs:                            # HEXA STAGE 風のヘッダー
            d.polygon(hexpts(9, 9, 6), outline=ACC, width=2)
            d.text((19, 1), "HEXA CLIP", fill=ACC, font=self.font_bs)
            x = 19 + self.font_bs.getlength("HEXA CLIP") + 6
            if self.mode != M_HOME:            # 「›」はフォントに無いことがあるので線で描く
                d.line((x + 1, 5, x + 4, 9, x + 1, 13), fill=SUB, width=2)
                d.text((x + 10, 1), SCREEN_NAMES[self.mode], fill=FG, font=self.font_s)
            st = self.status_text()
            d.text((LCD_W - 4 - self.font_t.getlength(st), 2), st, fill=SUB, font=self.font_t)
        else:
            d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=BAR)
            d.text((4, 1), title or "", fill=(255, 255, 255), font=self.font_s)
        d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
        d.text((4, LCD_H - BOT_H + 1), hint, fill=(210, 210, 210), font=self.font_s)
        msg, until = self.toast
        if msg and time.time() < until:      # お知らせは下のキー案内のすぐ上に
            w = int(self.font_s.getlength(msg)) + 16
            y = LCD_H - BOT_H - 24
            d.rounded_rectangle(((LCD_W - w) // 2, y, (LCD_W + w) // 2, y + 20), 8,
                                fill=(0, 120, 70), outline=ACC)
            d.text(((LCD_W - w) // 2 + 8, y + 2), msg, fill=(255, 255, 255), font=self.font_s)
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
        if on == self.cam_on or self.cam is None:
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
        """画面を切り替える。カメラはカメラ画面でだけ動かす"""
        self.mode = m
        if m == M_HIST:
            self.unseen = 0
        self.set_cam(m == M_CLIP)
        self.qr_img = None
        self.next_list = 0.0

    def open_screen(self, m):
        """ホームや 1〜4 キーから各画面へ"""
        self.home_sel = SCREENS.index(m)
        self.set_mode(m)

    # ---- ホーム (ハニカム)
    HOME_POS = [(40, 62), (92, 62), (66, 108), (118, 108)]    # 2x2 のハニカム
    HOME_DESC = {M_HIST: "受け取ったもの", M_QR: "スマホ・PCと",
                 M_CLIP: "文字・QR (おまけ)", M_SET: "受信・fn+V・カメラ"}

    def status_text(self):
        """右上: Wi-Fi (IPの末尾) と電池。15秒ごとに取り直す"""
        if time.time() - self.status["t"] > 15:
            self.status = {"t": time.time(), "bat": battery_percent()}
            try:
                info = clip_call("GET", "/api/pin", timeout=0.8)
                ip = info["url"].split("//")[1].split(":")[0]
                self.status["net"] = ("Wi-Fi ." + ip.split(".")[-1]) if info.get("online") else "Wi-Fi なし"
            except Exception:
                self.status["net"] = "CLIP停止中"
        st = self.status.get("net", "")
        if self.status.get("bat") is not None:
            st += f"  {self.status['bat']}%"
        return st

    def draw_home(self):
        latest = None
        if self.items:
            latest = next((i for i in self.items if i["id"] == CLIP_LATEST[0]), None)
        happy = time.time() < self.happy_until
        key = (self.home_sel, self.unseen, latest and latest["id"], happy, len(self.items or []),
               self.status_text(), self.cam is None, self.items is None)
        if self.home_cache[0] == key:
            img = self.home_cache[1].copy()
            return self.chrome(img, self.home_hint())
        img = self.honey.copy()
        d = ImageDraw.Draw(img)
        sel = SCREENS[self.home_sel]
        for i, m in enumerate(SCREENS):
            cx, cy = self.HOME_POS[i]
            on = m == sel
            pts = hexpts(cx, cy, 27)
            d.polygon(pts, fill=(20, 32, 30) if on else (18, 21, 29))
            if on:
                glow_hex(img, cx, cy, 27, ACC)
                d = ImageDraw.Draw(img)
            else:
                d.polygon(pts, outline=(44, 50, 64), width=2)
            draw_icon(d, m, cx, cy - 4, 15, ACC if on else SUB)
            name = SCREEN_NAMES[m]
            tw = self.font_t.getlength(name)
            d.text((cx - tw / 2, cy + 8), name, fill=FG if on else SUB, font=self.font_t)
            if m == M_HIST and self.unseen:      # 新着の数
                bx, by = cx + 17, cy - 21
                d.ellipse((bx - 8, by - 8, bx + 8, by + 8), fill=(255, 170, 40))
                n = str(min(self.unseen, 99))
                d.text((bx - self.font_t.getlength(n) / 2, by - 7), n, fill=(20, 20, 20), font=self.font_t)
        # 右の説明パネル (ゴマエビちゃんがいれば、その左までに収める)
        x0 = 156
        mw = max(m.width for m in self.mascot.values()) if self.mascot else 0
        right = LCD_W - 6 - (mw + 2 if mw else 0)
        d.text((x0, 22), SCREEN_NAMES[sel], fill=FG, font=self.font_b)
        desc = self.HOME_DESC[sel]
        if sel == M_HIST and self.items is not None:
            desc = f"{len(self.items)}件"
        if sel == M_CLIP and self.cam is None:
            desc = "カメラなし"
        if self.items is None:
            desc = "HEXA CLIP が停止中"
        d.text((x0, 50), self.ellipsize(desc, right - x0), fill=SUB, font=self.font_s)
        d.line((x0, 68, right, 68), fill=(44, 50, 64))
        d.text((x0, 72), "fn+V で貼れる", fill=ACC, font=self.font_t)
        d.rounded_rectangle((x0, 87, max(x0 + 60, right), 117), 4, fill=(18, 22, 30), outline=(44, 50, 64))
        if latest:
            head = latest.get("name") or latest["text"]
            d.text((x0 + 5, 89), self.ellipsize(head, max(40, right - x0 - 10)), fill=FG, font=self.font_s)
            when = datetime.datetime.fromtimestamp(latest["created"]).strftime("%H:%M")
            d.text((x0 + 5, 104), f"{latest['source']} · {when}", fill=SUB, font=self.font_t)
        else:
            d.text((x0 + 5, 95), "まだありません", fill=SUB, font=self.font_s)
        if self.unseen:
            d.text((x0, 124), f"● 新着 {self.unseen}件", fill=(255, 190, 70), font=self.font_bs)
        if self.mascot:                          # ゴマエビちゃん
            m = self.mascot["happy" if happy else "idle"]
            img.paste(m, (LCD_W - 3 - (mw + m.width) // 2, LCD_H - BOT_H - m.height - 1), m)
        self.home_cache = (key, img.copy())
        return self.chrome(img, self.home_hint())

    def home_hint(self):
        A = self.AR
        return f"{A['u']}{A['d']}{A['l']}{A['r']}:選ぶ  ENTER:開く  1〜4:直接  ESC:終了"

    def home_move(self, val):
        r, c = divmod(self.home_sel, 2)
        if val in ("LEFT", "RIGHT"):
            c = 1 - c
        elif val in ("UP", "DOWN"):
            r = 1 - r
        self.home_sel = r * 2 + c

    # ---- 1 クリップ
    def draw_clip(self):
        A = self.AR
        if self.cam is None:
            img = self.canvas()
            d = ImageDraw.Draw(img)
            d.text((10, 44), "カメラが見つかりません", fill=FG, font=self.font)
            d.text((10, 74), "文字の読み取りは、スマホ画面の", fill=SUB, font=self.font_s)
            d.text((10, 92), "「撮って読む」を使ってください", fill=SUB, font=self.font_s)
            return self.chrome(img, "ESC:ホーム")
        img = self.pv.frame(self.cam)
        self.chrome(img, f"ENTER:文字 Q:QR {A['u']}{A['d']}:x{self.pv.zoom + 1} ESC")
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
        items = clip_items(50, q=self.query)
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
        elif not self.items and self.query:
            d.text((8, 50), "見つかりません", fill=SUB, font=self.font)
            d.text((8, 78), f"「{self.query}」  ESCで検索を解除", fill=SUB, font=self.font_s)
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
                if it.get("pinned"):            # ピン留めは出どころの代わりに★
                    d.text((10, y + 2), self.STAR, fill=(232, 193, 74), font=self.font_s)
                else:
                    d.text((6, y + 3), it["source"][:3], fill=colors.get(it["source"], SUB),
                           font=self.font_s)
                top = it["id"] == CLIP_LATEST[0] and not it.get("binary")
                if top:                         # fn+V で貼り付けられる項目
                    d.text((LCD_W - 40, y + 3), "fn+V", fill=ACC, font=self.font_s)
                lim = LCD_W - (84 if top else 44)
                x = 34
                if it.get("name"):              # ファイル: 名前を水色で先に出す
                    nm = self.ellipsize(it["name"], lim * 0.6)
                    d.text((x, y + 3), nm, fill=(120, 200, 255), font=self.font_s)
                    w = self.font_s.getlength(nm) + 6
                    x, lim = x + w, lim - w
                body = fsize(it.get("size", 0)) if it.get("binary") else it["text"]
                d.text((x, y + 3), self.ellipsize(body, lim), fill=FG if not it.get("binary") else SUB,
                       font=self.font_s)
            if len(self.items) > self.ROWS:
                h = self.ROWS * self.ROW_H
                bh = max(8, h * self.ROWS // len(self.items))
                by = TOP_H + 2 + (h - bh) * self.top // max(1, len(self.items) - self.ROWS)
                d.rectangle((LCD_W - 3, by, LCD_W - 1, by + bh), fill=(90, 98, 115))
        n = len(self.items) if self.items else 0
        if self.query:
            return self.chrome(img, f"検索「{self.query}」{n}件  /:変更 ESC:解除")
        return self.chrome(img, f"{A['u']}{A['d']}:選ぶ ENTER:開く /:検索 ESC:ホーム {n}件")

    def toggle_pin(self, item):
        on = not item.get("pinned")
        try:
            clip_call("POST", f"/api/items/{item['id']}/pin", {"on": on})
            item["pinned"] = on
            self.say("★ ピン留めしました (履歴が流れても残る)" if on else "ピン留めを外しました")
        except Exception:
            self.say("できませんでした")
        self.next_list = 0.0

    def run_search(self):
        """検索語の入力 (モーダル)。ENTER で確定、ESC で取りやめ"""
        q = self.query
        self.keys.flush(0.15)
        while True:
            img = self.draw_hist()
            d = ImageDraw.Draw(img)
            d.rectangle((0, 0, LCD_W, TOP_H + 22), fill=(0, 0, 0))
            d.rectangle((4, 3, LCD_W - 4, TOP_H + 18), outline=ACC, fill=(26, 30, 38))
            d.text((10, 6), "検索:", fill=SUB, font=self.font_s)
            x = 44
            d.text((x, 4), q, fill=FG, font=self.font)
            cx = x + int(self.font.getlength(q)) + 1
            d.rectangle((cx, 6, cx + 1, TOP_H + 14), fill=ACC)
            d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
            d.text((4, LCD_H - BOT_H + 1), "英数字で入力  ENTER:検索  ESC:やめる  BS:消す",
                   fill=(210, 210, 210), font=self.font_s)
            self.fb.show(img)
            for kind, val in self.keys.poll(0.5):
                if kind == "CHAR":
                    q += val
                elif val == "BS":
                    q = q[:-1]
                elif val == "ENTER":
                    self.query, self.sel, self.top = q.strip(), 0, 0
                    self.refresh_items()
                    self.keys.flush(0.1)
                    return
                elif val == "ESC":
                    self.keys.flush(0.1)
                    return

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

    def menu(self, title, options, start=0):
        """一覧から選ぶ (モーダル)。options=[(表示, 戻り値, 近道キー)]。ESC で None
        近道キーを直接押しても選べる (覚えたら速い)"""
        sel, rh = start, 18
        rows = (LCD_H - TOP_H - BOT_H - 2) // rh
        A = self.AR
        self.keys.flush(0.15)
        while True:
            img = self.canvas()
            d = ImageDraw.Draw(img)
            top = min(max(0, sel - rows + 1), max(0, len(options) - rows))
            for r in range(min(rows, len(options) - top)):
                i = top + r
                label, _, short = options[i]
                y = TOP_H + 2 + r * rh
                if i == sel:
                    d.rectangle((0, y, LCD_W, y + rh - 1), fill=(34, 48, 58))
                    d.rectangle((0, y, 2, y + rh - 1), fill=ACC)
                d.text((10, y + 2), label, fill=FG if i == sel else (200, 205, 215), font=self.font_s)
                if short:
                    d.text((LCD_W - 22, y + 2), short.upper(), fill=SUB, font=self.font_s)
            self.fb.show(self.chrome(img, f"{A['u']}{A['d']}:選ぶ  ENTER:決定  ESC:戻る",
                                     tabs=False, title=title))
            for kind, val in nav(self.keys.poll(0.5)):
                if kind == "CHAR":
                    for _, ret, short in options:
                        if short and val.lower() == short:
                            self.keys.flush(0.1)
                            return ret
                elif val == "UP":
                    sel = (sel - 1) % len(options)
                elif val == "DOWN":
                    sel = (sel + 1) % len(options)
                elif val in ("ENTER", "RIGHT"):
                    self.keys.flush(0.1)
                    return options[sel][1]
                elif val in ("ESC", "LEFT"):
                    self.keys.flush(0.1)
                    return None

    def confirm(self, title, yes):
        # カーソルは「やめる」から (ENTER 連打で消えないように)
        return self.menu(title, [(yes, True, None), ("やめる", False, None)], start=1) is True

    def run_detail(self, item):
        """全文表示(モーダル)"""
        item = clip_get(item)
        if item.get("binary"):
            return self.run_file_detail(item)
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
            head = item.get("name") or f"#{item['id']}  {item['source']}"
            star = (self.STAR + " ") if item.get("pinned") else ""
            title = note or f"{star}{head}  {when}  {len(text)}文字"
            hint = ("もう一度 D で削除 / 他のキーで中止" if armed
                    else f"ENTER:メニュー  {A['u']}{A['d']}:送る  ESC:戻る")
            self.fb.show(self.chrome(img, hint, tabs=False, title=title))
            for kind, val in nav(self.keys.poll(0.5)):
                key = val.lower() if kind == "CHAR" else val
                if armed:
                    armed = False
                    if key == "d":
                        return self.delete_item(item)
                    note = ""
                    continue
                note = ""
                if key == "ENTER":
                    key = self.menu(f"#{item['id']}  " + (item.get("name") or "メニュー"), [
                        ("貼付用にする (本体で fn+V)", "v", "v"),
                        ("編集", "e", "e"),
                        ("ファイルに保存 (~/clip/)", "s", "s"),
                        ("本体で実行 (python3 / sh)", "r", "r"),
                        ("QRで表示 (スマホで読む)", "q", "q"),
                        ("ピン留めを外す" if item.get("pinned") else "ピン留め ★", "p", "p"),
                        ("削除", "del", "d")])
                    if key == "del":
                        if self.confirm(f"#{item['id']} を削除しますか?", "削除する"):
                            return self.delete_item(item)
                        continue
                    if key is None:
                        continue
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
                    if item.get("name"):
                        note = "保存済み: ~/clip/" + item["name"]
                    else:
                        p = save_file(text, f"clip_{item['id']}.txt")
                        note = "保存: ~/clip/" + os.path.basename(p)
                elif key == "e":
                    out = self.run_editor(text)
                    if out and out.strip() and out != text:
                        self.say(send_clip(out, "edit"))
                        return
                    self.keys.flush(0.1)
                elif key == "p":
                    self.toggle_pin(item)
                elif key == "q":
                    self.show_qr(text, f"#{item['id']}")
                elif key == "r":
                    self.run_code(item, text)

    # ---- QR: 本体の画面に出してスマホで読む (Wi-Fiなしで渡せる)
    def show_qr(self, text, label=""):
        try:
            import qrcode
        except ImportError:
            self.say("QRには sudo apt install python3-qrcode", 4)
            return
        data = text.rstrip("\n").encode("utf-8")
        try:
            qr = qrcode.QRCode(border=2, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
            qr.add_data(data)
            qr.make(fit=True)
            m = np.array(qr.get_matrix(), dtype=np.uint8)
        except Exception:
            m = None
        scale = LCD_H // m.shape[0] if m is not None else 0
        if scale < 2:                        # 1ドット1マスではスマホで読めない
            self.say(f"長すぎてQRにできません ({len(data)}バイト / 700バイト程度まで)", 4)
            return
        q = Image.fromarray((1 - m) * 255).resize((m.shape[1] * scale, m.shape[0] * scale),
                                                   Image.NEAREST).convert("RGB")
        img = Image.new("RGB", (LCD_W, LCD_H), (255, 255, 255))
        img.paste(q, (0, (LCD_H - q.height) // 2))
        d = ImageDraw.Draw(img)
        x = q.width + 8
        d.rectangle((q.width + 2, 0, LCD_W, LCD_H), fill=BG)
        for i, (t, col) in enumerate([("スマホのカメラで", FG), ("読み取れます", FG), ("", FG),
                                      (label, SUB), (f"{len(text)}文字", SUB), ("", FG),
                                      ("Wi-Fi不要", ACC), ("", FG), ("ESC:戻る", SUB)]):
            d.text((x, 8 + i * 17), t, fill=col, font=self.font_s)
        self.fb.show(img)
        self.keys.flush(0.2)
        while not any(k == "ACT" and v == "ESC" for k, v in self.keys.poll(0.5)):
            pass
        self.keys.flush(0.1)

    # ---- QR: 本体のカメラで読む (OCRと違って誤読がない)
    def do_qr(self):
        if not shutil_which("zbarimg"):
            self.say("QR読取には sudo apt install zbar-tools", 4)
            return
        self.message("クリップ", "QRを探しています...")
        try:
            text = read_qr(self.cam)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.recover_camera()
            self.say(f"読み取り失敗: {type(e).__name__}", 4)
            return
        if not text:
            self.say("QRが見つかりません (枠に入れて、もう少し離して)", 3)
            self.keys.flush(0.2)
            return
        out = self.run_editor(text, 0.0)
        if out and out.strip():
            self.say(send_clip(out, "qr"))
            it = clip_items(1)
            if it:
                self.last_id = max(self.last_id, it[0]["id"])
        else:
            self.say("破棄しました")

    # ---- 受け取ったコードを本体で実行
    RUN_MAX_OUT = 200_000

    @staticmethod
    def guess_runner(name, text):
        """python3 か sh か (拡張子 → #! 行 → 中身の雰囲気)"""
        n = (name or "").lower()
        if n.endswith(".py"):
            return "python3"
        if n.endswith((".sh", ".bash")):
            return "sh"
        first = text.lstrip().split("\n", 1)[0]
        if first.startswith("#!"):
            return "python3" if "python" in first else "sh"
        py = ("import ", "from ", "def ", "print(", "class ", "for ", "while ", "if __name__")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        hits = sum(1 for ln in lines if ln.startswith(py) or ln.endswith(":"))
        return "python3" if lines and hits * 3 >= len(lines) else "sh"

    def run_code(self, item, text):
        """確認 → 実行 → 出力を表示。ESC で停止。S で出力を HEXA CLIP へ送る"""
        runner = self.guess_runner(item.get("name"), text)
        # 1) 確認 (TAB で python3 / sh を切り替え)
        self.keys.flush(0.15)
        while True:
            img = self.canvas()
            d = ImageDraw.Draw(img)
            d.text((10, TOP_H + 10), "このコードを本体で実行しますか?", fill=FG, font=self.font)
            d.text((10, TOP_H + 40), f"実行するもの:  {runner}", fill=ACC, font=self.font)
            d.text((10, TOP_H + 66), "入力(input)は使えません / ESCで途中で止められます",
                   fill=SUB, font=self.font_s)
            d.text((10, TOP_H + 84), "フォルダ: ~/clip/", fill=SUB, font=self.font_s)
            self.fb.show(self.chrome(img, "ENTER:実行  TAB:python3/sh 切替  ESC:やめる",
                                     tabs=False, title="実行"))
            ev = self.keys.poll(0.5)
            go = None
            for kind, val in ev:
                if kind == "ACT" and val == "TAB":
                    runner = "sh" if runner == "python3" else "python3"
                elif kind == "ACT" and val == "ENTER":
                    go = True
                elif kind == "ACT" and val == "ESC":
                    go = False
            if go is False:
                self.keys.flush(0.1)
                return
            if go:
                break
        # 2) ファイルにして実行 (名前付きは ~/clip/<名前>、なければ run_<ID>.py/.sh)
        os.makedirs(CLIP_DIR, exist_ok=True)
        chown_user(CLIP_DIR)
        name = item.get("name") or f"run_{item['id']}.{'py' if runner == 'python3' else 'sh'}"
        path = save_file(text, os.path.basename(name))
        cmd = ["python3", "-u", path] if runner == "python3" else ["sh", path]
        out, done = [], {"code": None}
        t0 = time.time()
        try:
            proc = subprocess.Popen(cmd, cwd=CLIP_DIR, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True, **demote_kwargs())
        except OSError as e:
            self.say(f"実行できません: {e.strerror}", 4)
            return

        def reader():
            size = 0
            for raw in iter(lambda: proc.stdout.read1(4096) if hasattr(proc.stdout, "read1")
                            else proc.stdout.read(4096), b""):
                if size < self.RUN_MAX_OUT:
                    out.append(raw.decode("utf-8", "replace"))
                size += len(raw)
            code = proc.wait()
            done["dt"] = time.time() - t0
            done["code"] = code
        import threading
        th = threading.Thread(target=reader, daemon=True)
        th.start()
        # 3) 出力を表示 (最後を追いかける。↑↓でスクロールすると追いかけを止める)
        scroll, follow, note = 0, True, ""
        rows = (LCD_H - TOP_H - BOT_H - 2) // 15
        self.keys.flush(0.1)
        while True:
            text_out = "".join(out)
            if len(text_out) >= self.RUN_MAX_OUT:
                text_out += "\n…(出力が多いため省略)"
            lines = layout(text_out.replace("\t", "    "), self.font_s, LCD_W - 12)
            if follow:
                scroll = max(0, len(lines) - rows)
            scroll = max(0, min(scroll, max(0, len(lines) - rows)))
            img = self.canvas()
            d = ImageDraw.Draw(img)
            for r in range(rows):
                if scroll + r >= len(lines):
                    break
                a, b = lines[scroll + r]
                d.text((5, TOP_H + 2 + r * 15), text_out[a:b], fill=FG, font=self.font_s)
            if not text_out:
                d.text((5, TOP_H + 4), "(出力なし)", fill=SUB, font=self.font_s)
            dt = time.time() - t0
            if done["code"] is None:
                title = f"実行中 {runner} {os.path.basename(path)}  {dt:.0f}s"
                hint = "ESC:停止  ↑↓:スクロール"
            else:
                ok = done["code"] == 0
                title = note or (f"終了 OK ({done['dt']:.1f}s)" if ok
                                 else f"終了 エラー code={done['code']} ({done['dt']:.1f}s)")
                hint = "S:出力をCLIPへ送る  ESC:閉じる  ↑↓:スクロール"
            self.fb.show(self.chrome(img, hint.replace("↑↓", self.AR["u"] + self.AR["d"]),
                                     tabs=False, title=title))
            for kind, val in nav(self.keys.poll(0.2)):
                key = val.lower() if kind == "CHAR" else val
                if key == "ESC":
                    if done["code"] is None:
                        stop_proc(proc)
                        note = "停止しました"
                        th.join(2)
                    else:
                        self.keys.flush(0.1)
                        return
                elif key == "UP":
                    follow, scroll = False, scroll - 1
                elif key == "DOWN":
                    scroll += 1
                    follow = scroll >= len(lines) - rows
                elif key == "s" and done["code"] is not None:
                    note = send_clip(text_out or "(出力なし)", "run") if text_out else "出力がありません"

    def run_file_detail(self, item):
        """バイナリ (PDF・画像など): 内容は表示せず、場所と大きさだけ"""
        when = datetime.datetime.fromtimestamp(item["created"]).strftime("%m/%d %H:%M")
        path = item.get("path") or ""
        if not path or not os.path.exists(path):
            warn = "※ 本体のファイルが見つかりません"
        elif os.path.getsize(path) != item.get("size"):
            warn = "※ 同じ名前の新しいファイルで上書き済み"
        else:
            warn = ""
        armed = False
        self.keys.flush(0.2)
        while True:
            img = self.canvas()
            d = ImageDraw.Draw(img)
            rows = [(item.get("name") or "", (120, 200, 255), self.font),
                    (f"{fsize(item.get('size', 0))}  {item['source']}  {when}", SUB, self.font_s),
                    ("保存先:", SUB, self.font_s),
                    ("~/clip/" + (item.get("name") or ""), FG, self.font_s),
                    (warn, (255, 170, 60), self.font_s),
                    ("スマホ/PCの履歴から DL できます", SUB, self.font_s)]
            y = TOP_H + 6
            for txt, col, f in rows:
                d.text((8, y), self.ellipsize(txt, LCD_W - 16) if txt else "", fill=col, font=f)
                y += 22 if f is self.font else 18
            hint = "もう一度 D で履歴から削除 / 他のキーで中止" if armed else "ENTER:メニュー  ESC:戻る"
            self.fb.show(self.chrome(img, hint, tabs=False, title=f"#{item['id']}  ファイル"))
            for kind, val in nav(self.keys.poll(0.5)):
                key = val.lower() if kind == "CHAR" else val
                if armed:
                    armed = False
                    if key == "d":
                        return self.delete_item(item)
                    continue
                if key == "ENTER":
                    key = self.menu(f"#{item['id']}  {item.get('name') or ''}", [
                        ("ピン留めを外す" if item.get("pinned") else "ピン留め ★", "p", "p"),
                        ("履歴から削除 (ファイルは残る)", "del", "d")])
                    if key == "del":
                        if self.confirm("履歴から削除しますか?", "削除する"):
                            return self.delete_item(item)
                        continue
                if key in ("ESC", "LEFT"):
                    return
                if key == "p":
                    self.toggle_pin(item)
                if key == "d":
                    armed = True

    def delete_item(self, item):
        try:
            clip_call("DELETE", f"/api/items/{item['id']}")
            self.say(f"#{item['id']} を削除")
        except Exception:
            self.say("削除できませんでした")
        self.next_list = 0.0

    def make_paste_target(self, item):
        if item.get("binary"):
            self.say("ファイルは fn+V できません (~/clip/ にあります)", 3)
            return
        note = paste_note(clip_get(item)["text"])
        if item["id"] == CLIP_LATEST[0]:
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
        return self.chrome(self.qr_img.copy(), "スマホでQRを読む (同じWi-Fi)   ESC:ホーム")

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
        return self.chrome(img, f"{A['u']}{A['d']}:選ぶ {A['l']}{A['r']}:変える(すぐ保存) ESC:ホーム")

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
        if self.mode != M_HOME:              # ホームでは新着の数字とゴマエビちゃんで知らせる
            self.say(f"受信 {it[0]['name']}" if it[0].get("name")
                     else f"受信 #{it[0]['id']} ({it[0]['source']})", 3)
        if self.mode != M_HIST:
            self.unseen += 1
        self.happy_until = time.time() + 8
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
            if self.mode == M_HOME:
                if time.time() > self.next_list:
                    self.refresh_items()
                    self.next_list = time.time() + 30
                img = self.draw_home()
            elif self.mode == M_CLIP:
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

            live = self.mode == M_CLIP and self.cam is not None
            for kind, val in nav(self.keys.poll(0.0 if live else 0.25)):
                if kind == "ACT" and val == "ESC" and self.mode == M_HIST and self.query:
                    self.query, self.sel, self.top = "", 0, 0      # まず検索を解除
                    self.refresh_items()
                    break
                if kind == "ACT" and val == "ESC":
                    if self.mode != M_HOME:                 # 各画面 → ホームへ
                        self.home_sel = SCREENS.index(self.mode)
                        self.set_mode(M_HOME)
                        break
                    if self.confirm_quit(img):
                        return f"ESC / frames={frames}"
                    break
                if kind == "CHAR" and val in "1234":
                    self.open_screen(SCREENS[int(val) - 1])
                    break
                if kind == "ACT" and val == "TAB" and self.mode != M_HOME:
                    i = SCREENS.index(self.mode)
                    self.open_screen(SCREENS[(i + 1) % len(SCREENS)])
                    break
                if self.mode == M_HOME:
                    if kind == "ACT" and val in ("UP", "DOWN", "LEFT", "RIGHT"):
                        self.home_move(val)
                    elif kind == "ACT" and val == "ENTER":
                        self.open_screen(SCREENS[self.home_sel])
                        break
                    elif kind == "ACT" and val == "TAB":
                        self.home_sel = (self.home_sel + 1) % 4
                    continue
                if (kind == "CHAR" and val.lower() == "v" and self.mode == M_HIST
                        and self.items):
                    self.make_paste_target(self.items[self.sel])
                    break
                if kind == "CHAR" and val.lower() == "q" and self.mode == M_CLIP and self.cam:
                    self.do_qr()
                    break
                if kind == "CHAR" and val == "/" and self.mode == M_HIST and self.items is not None:
                    self.run_search()
                    break
                if kind == "CHAR" and val.lower() == "p" and self.mode == M_HIST and self.items:
                    self.toggle_pin(self.items[self.sel])
                    break
                if kind != "ACT":
                    continue
                if self.mode == M_CLIP and self.cam is not None:
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
    try:
        cam = open_camera()
    except Exception as e:                  # CardputerZero-Lite などカメラなし
        print("no camera:", e, flush=True)
        cam = None
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
