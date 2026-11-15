#!/usr/bin/env python3
"""czpasted — fn+V で HEXA CLIP の最新を貼り付け / fn+H でクイックパネル (どのアプリの上でも)

CardputerZero には共通のクリップボードが無いため、本体キーボードのデバイスに
キーイベントを書き込み、いま開いているアプリに1文字ずつ打ち込む。

  - 打てるのは英数字と記号 (US配列)。日本語などは飛ばす
  - 改行は ENTER として打つ (設定で「打たない」にすると空白に置き換えて1行にまとめる)。
    末尾の改行は打たない (シェルで勝手に実行されないように)
  - 打ち込み中に ESC を押すと中止
  - HEXA CLIP アプリの起動中は動かない (アプリがキーボードを独占するため)

fn+H: 今の画面の上に小さな履歴パネルを出す (quickpanel.py)。↑↓ で選んで ENTER で、
元のアプリに打ち込む。スマホ/PCから何か届いたら、上部に1〜2秒「受信」の帯を出す。

input グループのユーザーで動かす (install.sh が hexaclip-paste.service として登録)。
"""
import argparse
import fcntl
import json
import os
import select
import signal
import struct
import threading
import time
import urllib.request

EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)
EV_SYN, EV_KEY = 0, 1
K_ESC, K_BS, K_TAB, K_ENTER, K_LSHIFT, K_SPACE = 1, 14, 15, 28, 42, 57
MAX_CHARS = 4000
EVIOCGKEY = 0x80000000 | (96 << 16) | (ord("E") << 8) | 0x18    # いま押されているキーの一覧

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


def text_to_keys(text, newline="enter"):
    """文字列 → [(keycode, shift)], 打てなかった文字数

    newline: "enter" = 改行を ENTER として打つ / "none" = 空白に置き換える (1行にまとめる)
    末尾の改行は打たない"""
    keys, skipped = [], 0
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if newline == "none":
        text = " ".join(ln.strip() for ln in text.split("\n") if ln.strip())
    for ch in text[:MAX_CHARS]:
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
    def __init__(self, fd, fn_code, key_code, get_text, erase=lambda: False, delay=0.004,
                 newline=lambda: "enter", panel_code=None, open_panel=None):
        self.fd, self.fn, self.key = fd, fn_code, key_code
        self.get_text, self.erase, self.delay = get_text, erase, delay
        self.newline = newline
        self.panel_code, self.open_panel = panel_code, open_panel
        self.held = set()
        self.lock = threading.Lock()            # 打ち込み / パネル / 帯 を同時にしない

    def key_down(self, code):
        """キーが本当に押されているか (デバイスの実状態)。

        HEXA CLIP アプリがキーボードを独占している間はイベントが届かず、その間に fn を
        離すと「押しっぱなし」と誤認して V だけで貼り付けが走るのを防ぐ"""
        try:
            buf = fcntl.ioctl(self.fd, EVIOCGKEY, bytes(96))
            return bool(buf[code // 8] & (1 << (code % 8)))
        except OSError:
            return code in self.held        # 取れない環境では従来どおり

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
        data = (struct.pack(EV_FMT, 0, 0, EV_KEY, code, value)
                + struct.pack(EV_FMT, 0, 0, EV_SYN, 0, 0))
        for _ in range(50):
            try:
                os.write(self.fd, data)
                break
            except BlockingIOError:          # 一時的に書けない → 少し待って再試行
                time.sleep(0.01)
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
        """押下状態を更新し、fn+V なら "paste"、fn+H なら "panel" を返す"""
        hit = None
        for code, value in events:
            if value == 0:
                self.held.discard(code)
            elif value == 1:
                self.held.add(code)
                if code in (self.key, self.panel_code) and self.fn in self.held:
                    if self.key_down(self.fn):
                        hit = "paste" if code == self.key else "panel"
                    else:
                        self.held.discard(self.fn)
        return hit

    def drain(self):
        """たまったイベント(自分の打鍵の戻りを含む)を全部読み、ESC が押されていたら True"""
        esc = False
        while True:
            ev = self.read(0)
            if not ev:
                return esc
            esc = esc or any(c == K_ESC and v == 1 for c, v in ev)

    def wait_release(self, timeout=3.0, key=None):
        """fn と V (または H) が離されるまで待つ (fn を押したまま打つと別の操作になるため)"""
        end = time.time() + timeout
        key = self.key if key is None else key
        def down():
            return self.key_down(self.fn) or self.key_down(key)
        while down() and time.time() < end:
            self.track(self.read(0.05))
        if down():
            return False
        self.held.discard(self.fn)
        self.held.discard(key)
        return True

    def paste(self):
        if not self.wait_release():
            return "timeout (fn を離してください)"
        text = self.get_text()
        if not text:
            return "empty"
        return self.type_text(text)

    def panel(self):
        """fn+H: クイックパネル → 選んだものを元のアプリに打ち込む"""
        if not self.wait_release(key=self.panel_code):
            return "timeout (fn を離してください)"
        try:
            text = self.open_panel()
        except Exception as e:
            resume_launcher()
            return f"panel error: {e!r}"
        time.sleep(0.12)                         # 再開したランチャーが落ち着くのを待つ
        if not text:
            if self.erase():
                self.drain()
                self.tap(K_BS)                   # fn+H で入ってしまった h を消す
            return "closed"
        return self.type_text(text)

    def type_text(self, text):
        keys, skipped = text_to_keys(text, self.newline())
        cut = max(0, len(text.rstrip("\n")) - MAX_CHARS)
        self.drain()
        if self.erase():
            self.tap(K_BS)                       # ホットキーで入ってしまった v / h を消す
        for n, (code, shift) in enumerate(keys):
            if n % 8 == 0 and self.drain():      # 自分の打鍵を読み捨てつつ、ESC(中止)を見る
                self.held.clear()
                return f"aborted at {n}/{len(keys)}"
            self.tap(code, shift)
        time.sleep(0.05)
        self.drain()
        self.held.clear()
        return (f"typed {len(keys)} keys, skipped {skipped}"
                + (f", cut {cut} (max {MAX_CHARS})" if cut else ""))

    def run(self):
        while True:
            hit = self.track(self.read(5.0))
            if hit:
                with self.lock:
                    r = self.paste() if hit == "paste" else self.panel()
                print(f"{hit}:", r, flush=True)


# ---------------------------------------------------------------- ランチャー
LAUNCHER = "M5CardputerZero-APPLaunch"


def resume_launcher():
    """ランチャーを必ず動く状態に戻す (パネル中に落ちても本体が固まらないように)"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
        except OSError:
            continue
        if LAUNCHER.encode() in cmd and b"pkill" not in cmd:
            try:
                os.kill(int(pid), signal.SIGCONT)
            except OSError:
                pass


# ---------------------------------------------------------------- 受信の帯
def watch_incoming(url, paster, notify, painter_box):
    """サーバーの変更通知 (SSE) を聞いて、届いたら上部に帯を出す"""
    def latest():
        with urllib.request.urlopen(url + "/api/items?limit=1&preview=80", timeout=3) as r:
            its = json.loads(r.read())["items"]
        return its[0] if its else None
    last = None
    while True:
        try:
            it = latest()
            last = it["id"] if it else 0
            with urllib.request.urlopen(url + "/api/events", timeout=60) as r:
                for raw in r:
                    if not raw.startswith(b"data:"):
                        continue
                    it = latest()
                    if not it or it["id"] <= last:
                        continue
                    last = it["id"]
                    if it.get("moved") or it["source"] in ("ocr", "edit", "cli", "run"):
                        continue                  # 本体で自分がしたこと
                    if not notify():
                        continue
                    show_banner(it, paster, painter_box)
        except Exception as e:
            print("watch:", repr(e), flush=True)
            time.sleep(5)


def show_banner(it, paster, painter_box):
    import quickpanel as qp
    if qp.app_running() or not paster.lock.acquire(blocking=False):
        return                                    # HEXA CLIP アプリ中 / 打ち込み・パネル中は出さない
    try:
        body = it.get("name") or " ".join(it["text"].split())
        qp.banner("受信: " + body, painter_box.get("p"))
    except Exception as e:
        resume_launcher()
        print("banner:", repr(e), flush=True)
    finally:
        paster.lock.release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fn", type=int, default=389, help="fnキーのコード")
    ap.add_argument("--key", type=int, default=47, help="貼り付けキーのコード (V)")
    ap.add_argument("--panel-key", type=int, default=35, help="クイックパネルのキーのコード (H)")
    ap.add_argument("--no-panel", action="store_true", help="クイックパネルと受信の帯を使わない")
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

    def setting(key, default):
        try:
            return json.load(open(a.settings)).get(key, default)
        except Exception:
            return default

    def erase():
        return bool(setting("paste_erase", True))   # 本体は fn+V で V が入力されるため、既定で消す

    def newline():
        return "none" if setting("paste_newline", "enter") == "none" else "enter"

    def api(path):
        with urllib.request.urlopen(a.url + path, timeout=3) as r:
            return json.loads(r.read())

    resume_launcher()                            # 前回パネル中に止まっていても、まず戻す
    dev = find_keyboard()
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except PermissionError:
        raise SystemExit(f"czpasted: {dev} を開けません。input グループのユーザーか root で実行してください")
    print(f"czpasted: {dev}  fn+V=paste  fn+H=panel  uid={os.getuid()}", flush=True)

    painter_box = {}

    def warm():
        """パネルの部品 (numpy / Pillow / フォント) を先に読み込んでおく (初回だけ遅いので)"""
        try:
            import quickpanel as qp
            painter_box["p"] = qp.Painter()
            print("panel ready", flush=True)
        except Exception as e:
            print("panel unavailable:", repr(e), flush=True)

    def open_panel():
        import quickpanel as qp
        if qp.app_running():
            return None
        return qp.run_panel(dev, api, painter=painter_box.get("p"))

    paster = Paster(fd, a.fn, a.key, get_text, erase, newline=newline,
                    panel_code=None if a.no_panel else a.panel_key, open_panel=open_panel)
    if not a.no_panel:
        threading.Thread(target=warm, daemon=True).start()
        threading.Thread(target=watch_incoming, daemon=True,
                         args=(a.url, paster, lambda: bool(setting("notify_banner", True)),
                               painter_box)).start()
    paster.run()


if __name__ == "__main__":
    main()
