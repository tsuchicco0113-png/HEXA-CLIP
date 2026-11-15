#!/usr/bin/env python3
"""czpasted — fn+V で HEXA CLIP の最新の内容を「キー入力」として貼り付ける (実験的)

CardputerZero には共通のクリップボードが無いため、本体キーボードのデバイスに
キーイベントを書き込み、いま開いているアプリに1文字ずつ打ち込む。

  - 打てるのは英数字と記号 (US配列)。日本語などは飛ばす
  - 改行は ENTER として打つ
  - 打ち込み中に ESC を押すと中止
  - HEXA CLIP アプリの起動中は動かない (アプリがキーボードを独占するため)

root で動かす (install.sh が hexaclip-paste.service として登録)。
"""
import argparse
import json
import os
import select
import struct
import time
import urllib.request

EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)
EV_SYN, EV_KEY = 0, 1
K_ESC, K_BS, K_TAB, K_ENTER, K_LSHIFT, K_SPACE = 1, 14, 15, 28, 42, 57
MAX_CHARS = 4000

_PAIRS = {
    2: "1!", 3: "2@", 4: "3#", 5: "4$", 6: "5%", 7: "6^", 8: "7&", 9: "8*", 10: "9(", 11: "0)",
    12: "-_", 13: "=+", 16: "qQ", 17: "wW", 18: "eE", 19: "rR", 20: "tT", 21: "yY", 22: "uU",
    23: "iI", 24: "oO", 25: "pP", 26: "[{", 27: "]}", 30: "aA", 31: "sS", 32: "dD", 33: "fF",
    34: "gG", 35: "hH", 36: "jJ", 37: "kK", 38: "lL", 39: ";:", 40: "'\"", 41: "`~", 43: "\\|",
    44: "zZ", 45: "xX", 46: "cC", 47: "vV", 48: "bB", 49: "nN", 50: "mM", 51: ",<", 52: ".>",
    53: "/?",
}
CHARMAP = {" ": (K_SPACE, False), "\n": (K_ENTER, False), "\t": (K_TAB, False)}
for _code, _p in _PAIRS.items():
    CHARMAP[_p[0]] = (_code, False)
    CHARMAP[_p[1]] = (_code, True)


def text_to_keys(text):
    """文字列 → [(keycode, shift)], 打てなかった文字数"""
    keys, skipped = [], 0
    for ch in text.replace("\r\n", "\n").replace("\r", "\n")[:MAX_CHARS]:
        if ch in CHARMAP:
            keys.append(CHARMAP[ch])
        else:
            skipped += 1
    return keys, skipped


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


class Paster:
    def __init__(self, fd, fn_code, key_code, get_text, erase=lambda: False, delay=0.004):
        self.fd, self.fn, self.key = fd, fn_code, key_code
        self.get_text, self.erase, self.delay = get_text, erase, delay
        self.held = set()

    # ---- I/O
    def read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, EV_SIZE * 64)
        except (BlockingIOError, InterruptedError):
            return []
        out = []
        for i in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
            _, _, etype, code, value = struct.unpack(EV_FMT, data[i:i + EV_SIZE])
            if etype == EV_KEY:
                out.append((code, value))
        return out

    def emit(self, code, value):
        os.write(self.fd, struct.pack(EV_FMT, 0, 0, EV_KEY, code, value)
                 + struct.pack(EV_FMT, 0, 0, EV_SYN, 0, 0))
        time.sleep(self.delay)

    def tap(self, code, shift=False):
        if shift:
            self.emit(K_LSHIFT, 1)
        self.emit(code, 1)
        self.emit(code, 0)
        if shift:
            self.emit(K_LSHIFT, 0)

    # ---- logic
    def track(self, events):
        """押下状態を更新し、fn+V が押されたら True"""
        hit = False
        for code, value in events:
            if value == 0:
                self.held.discard(code)
            elif value == 1:
                self.held.add(code)
                if code == self.key and self.fn in self.held:
                    hit = True
        return hit

    def wait_release(self, timeout=3.0):
        """fn と V が離されるまで待つ (fn を押したまま打つと別の操作になるため)"""
        end = time.time() + timeout
        while (self.fn in self.held or self.key in self.held) and time.time() < end:
            self.track(self.read(0.05))
        return not (self.fn in self.held or self.key in self.held)

    def paste(self):
        if not self.wait_release():
            return "timeout (fn を離してください)"
        text = self.get_text()
        if not text:
            return "empty"
        keys, skipped = text_to_keys(text)
        if self.erase():
            self.tap(K_BS)                       # ホットキーで入ってしまった v を消す
        for n, (code, shift) in enumerate(keys):
            if n % 8 == 0:                       # 自分の打鍵を読み捨てつつ、ESC(中止)を見る
                for c, v in self.read(0):
                    if c == K_ESC and v == 1:
                        self.held.clear()
                        return f"aborted at {n}/{len(keys)}"
            self.tap(code, shift)
        time.sleep(0.05)
        self.read(0)
        self.held.clear()
        return f"typed {len(keys)} keys, skipped {skipped}"

    def run(self):
        while True:
            if self.track(self.read(5.0)):
                print("paste:", self.paste(), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fn", type=int, default=389, help="fnキーのコード")
    ap.add_argument("--key", type=int, default=47, help="貼り付けキーのコード (V)")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--settings", help="アプリの settings.json (paste_erase を読む)")
    a = ap.parse_args()

    def get_text():
        try:
            with urllib.request.urlopen(a.url + "/api/items?limit=1", timeout=2) as r:
                items = json.loads(r.read())["items"]
            return items[0]["text"].replace("\ufeff", "") if items else ""
        except Exception as e:
            print("fetch failed:", e, flush=True)
            return ""

    def erase():
        try:
            return bool(json.load(open(a.settings)).get("paste_erase"))
        except Exception:
            return False

    dev = find_keyboard()
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    print(f"czpasted: {dev}  hotkey fn({a.fn})+key({a.key})", flush=True)
    Paster(fd, a.fn, a.key, get_text, erase).run()


if __name__ == "__main__":
    main()
