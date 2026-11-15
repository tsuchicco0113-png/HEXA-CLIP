#!/usr/bin/env python3
"""HEXA CLIP daemon (czclipd) — CardputerZero 共有クリップボード

- 履歴を SQLite に保存
- http://<CPZ>:8080/ でスマホ/PC用の画面 (PIN認証)
  スマホで撮影 → 本体の Tesseract で読み取り → スマホで直して送る / 履歴の編集
- 127.0.0.1 からのアクセスは認証不要 (czclip コマンド / ocr_cam 用)

本体は標準ライブラリのみ。スマホ撮影のOCRだけ Pillow / numpy / tesseract を使う
(install.sh で導入済み。無ければその機能だけ 503 になる)。
"""
import argparse
import hmac
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

VERSION = "0.7"
DATA_DIR = os.path.expanduser(os.environ.get("HEXACLIP_DIR", "~/.local/share/hexaclip"))
DB_PATH = os.path.join(DATA_DIR, "clip.db")
PIN_PATH = os.path.join(DATA_DIR, "pin")
TOKEN_PATH = os.path.join(DATA_DIR, "api_token")
FILES_DIR = os.path.expanduser(os.environ.get("HEXACLIP_FILES", "~/clip"))   # 受け取ったファイルの置き場
MAX_FILE = 100 * 1024 * 1024   # ファイル1つの上限 100MB
PC_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc", "hexaclip.py")
MAX_SSE = 8                    # 同時にリアルタイム接続できる数
MAX_TEXT = 256 * 1024          # 1件の上限 256KB
MAX_ITEMS = 500                # 履歴の上限
MAX_IMAGE = 12 * 1024 * 1024   # スマホ撮影の画像 12MB
OCR_LANGS = {"jpn", "eng", "jpn+eng", "code"}
OCR_MAX_SIDE = 3000            # これより大きい画像は縮小してから読む
SESSION_DAYS = 30
LOCAL_ADDRS = {"127.0.0.1", "::1"}


# ------------------------------------------------------------------ store
class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'unknown',
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY,
                expires REAL NOT NULL);
        """)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(items)")]
        if "moved" not in cols:          # 0.5で追加: 「貼付用」で移しただけの項目 (受信通知しない)
            self.db.execute("ALTER TABLE items ADD COLUMN moved INTEGER NOT NULL DEFAULT 0")
        for col, typ in (("name", "TEXT"), ("size", "INTEGER"), ("path", "TEXT"),
                         ("binary", "INTEGER NOT NULL DEFAULT 0"),    # 0.6で追加: ファイル
                         ("pinned", "INTEGER NOT NULL DEFAULT 0")):   # 0.7で追加: ピン留め
            if col not in cols:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
        self.db.commit()
        # 追加/削除/並び替え/編集のたびに増える番号。画面はこれが変わったら一覧を取り直す
        self.rev = int(time.time() * 1000)
        self.changed = threading.Condition()

    def _bump(self):
        with self.changed:
            self.rev += 1
            self.changed.notify_all()

    def wait_change(self, rev, timeout):
        """rev から変わるまで待つ (リアルタイム通知用)。変わった後の rev を返す"""
        with self.changed:
            self.changed.wait_for(lambda: self.rev != rev, timeout)
            return self.rev

    def add(self, text, source, name=None, path=None, size=None, binary=False, trim=True):
        """trim=False: ファイルの中身はそのまま (末尾の改行を落とさない)"""
        text = normalize(text) if trim else text
        if size is None:
            size = len(text.encode())
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO items(text, source, created, name, path, size, binary) "
                "VALUES(?,?,?,?,?,?,?)",
                (text, source, time.time(), name, path, size, int(binary)))
            self.db.execute(                 # 上限を超えたら古い順に消す (ピン留めは残す)
                "DELETE FROM items WHERE pinned=0 AND id NOT IN "
                "(SELECT id FROM items ORDER BY id DESC LIMIT ?)", (MAX_ITEMS,))
            self.db.commit()
            self._bump()
            return cur.lastrowid

    COLS = "id, text, source, created, moved, name, size, binary, path, pinned"

    @staticmethod
    def _row(r, preview=0):
        d = dict(id=r[0], text=r[1], source=r[2], created=r[3], moved=bool(r[4]), length=len(r[1]),
                 name=r[5], size=r[6] if r[6] is not None else len(r[1].encode()),
                 binary=bool(r[7]), path=r[8], pinned=bool(r[9]))
        if preview and len(r[1]) > preview:  # 一覧用: 先頭だけ送る (本体の2秒ごとの取得を軽く)
            d["text"], d["truncated"] = r[1][:preview], True
        return d

    def list(self, limit=50, after=0, preview=0, q="", pinned=False):
        """q: 本文かファイル名に含む (大文字小文字・空白区切りは AND) / pinned: ピン留めだけ"""
        where, args = ["id > ?"], [after]
        for word in q.split()[:5]:
            w = "%" + word.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(lower(text) LIKE ? ESCAPE '\\' OR lower(ifnull(name,'')) LIKE ? ESCAPE '\\')")
            args += [w, w]
        if pinned:
            where.append("pinned=1")
        with self.lock:
            rows = self.db.execute(
                f"SELECT {self.COLS} FROM items WHERE {' AND '.join(where)} "
                "ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
            latest = self.db.execute("SELECT max(id) FROM items").fetchone()[0]
        self.latest = latest or 0
        return [self._row(r, preview) for r in rows]

    def set_pin(self, item_id, on):
        with self.lock:
            n = self.db.execute("UPDATE items SET pinned=? WHERE id=?",
                                (int(bool(on)), item_id)).rowcount
            self.db.commit()
            if n:
                self._bump()
        return n > 0

    def get(self, item_id):
        with self.lock:
            r = self.db.execute(f"SELECT {self.COLS} FROM items WHERE id=?", (item_id,)).fetchone()
        return self._row(r) if r else None

    def to_top(self, item_id):
        """指定の項目を最新(一番上)に移す。新しいIDを返す"""
        with self.lock:
            r = self.db.execute("SELECT text, source, name, path, size, binary, pinned FROM items "
                                "WHERE id=?", (item_id,)).fetchone()
            if not r:
                return None
            self.db.execute("DELETE FROM items WHERE id=?", (item_id,))
            cur = self.db.execute(
                "INSERT INTO items(text, source, created, moved, name, path, size, binary, pinned) "
                "VALUES(?,?,?,1,?,?,?,?,?)", (r[0], r[1], time.time()) + tuple(r[2:]))
            self.db.commit()
            self._bump()
            return cur.lastrowid

    def update(self, item_id, text):
        """本文を書き換える (スマホでの編集)。位置(ID)は変えない。
        名前付きのファイルなら ~/clip/ のファイルも書き換える。バイナリは編集不可"""
        with self.lock:
            r = self.db.execute("SELECT name, path, binary FROM items WHERE id=?",
                                (item_id,)).fetchone()
            if not r or r[2]:
                return False
            if r[0]:                         # ファイル: 改行コードだけそろえ、末尾の改行は保つ
                text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
                if r[1]:
                    write_text_file(r[1], text)
            else:
                text = normalize(text)
            n = self.db.execute("UPDATE items SET text=?, created=?, size=? WHERE id=?",
                                (text, time.time(), len(text.encode()), item_id)).rowcount
            self.db.commit()
            if n:
                self._bump()
        return n > 0

    def delete(self, item_id):
        with self.lock:
            n = self.db.execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount
            self.db.commit()
            if n:
                self._bump()
        return n > 0

    def new_session(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            self.db.execute("INSERT INTO sessions VALUES(?,?)",
                            (token, time.time() + SESSION_DAYS * 86400))
            self.db.commit()
        return token

    def valid_session(self, token):
        if not token:
            return False
        with self.lock:
            r = self.db.execute("SELECT expires FROM sessions WHERE token=?", (token,)).fetchone()
        return bool(r and r[0] > time.time())

    def clear_sessions(self):
        with self.lock:
            self.db.execute("DELETE FROM sessions")
            self.db.commit()


def normalize(text):
    """Windows由来の改行(CRLF)やBOMを除き、末尾の改行1つを落とす"""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    return text[:-1] if text.endswith("\n") and not text.endswith("\n\n") else text


def safe_name(name):
    """ファイル名だけにする (フォルダ指定や危ない文字を除く)。だめなら None"""
    name = str(name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip()
    name = name.lstrip(".").strip()
    return name[:120] or None


def write_text_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def place_file(tmp, name):
    """一時ファイルを ~/clip/<name> へ。同名があれば ~/clip/.prev/ に1つ前を残す"""
    final = os.path.join(FILES_DIR, name)
    if os.path.exists(final):
        prev = os.path.join(FILES_DIR, ".prev")
        os.makedirs(prev, exist_ok=True)
        os.replace(final, os.path.join(prev, name))
    os.replace(tmp, final)
    return final


def load_pin(reset=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    if reset or not os.path.exists(PIN_PATH):
        pin = f"{secrets.randbelow(10**6):06d}"
        fd = os.open(PIN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pin)
        return pin
    return open(PIN_PATH).read().strip()


def load_token(reset=False):
    """共有シート/ショートカット用のAPIトークン (Authorization: Bearer ...)"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if reset or not os.path.exists(TOKEN_PATH):
        tok = secrets.token_urlsafe(24)
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        return tok
    return open(TOKEN_PATH).read().strip()


def mdns_name():
    """avahi が動いていれば <hostname>.local で届く (IPが変わってもURLが同じ)"""
    if os.path.exists("/run/avahi-daemon/pid") or os.path.exists("/var/run/avahi-daemon/pid"):
        return socket.gethostname().split(".")[0] + ".local"
    return None


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------------------ OCR (スマホ撮影)
_OCR_LOCK = threading.Lock()       # CM0 は非力なので1枚ずつ


def ocr_image(data, lang):
    """画像(バイト列) → (テキスト, 自信のない範囲, 秒)。前処理と読み取りは本体アプリ(ocr_cam)と同じ"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from io import BytesIO
    from PIL import Image, ImageOps
    import ocr_cam                  # import だけではカメラ等は開かない

    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("L")
    if max(img.size) > OCR_MAX_SIDE:
        img.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.LANCZOS)
    with _OCR_LOCK:
        t0 = time.time()
        fd, path = tempfile.mkstemp(prefix="hexaclip_ocr_", suffix=".png")
        os.close(fd)
        try:
            text, marks = ocr_cam.run_ocr_ex(ocr_cam.prepare(img), lang, 6, path=path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return text, marks, time.time() - t0


# ------------------------------------------------------------------ web page
PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HEXA CLIP</title>
<style>
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--fg:#e8eaf0;--sub:#8a93a6;--acc:#2fd67b;--cam:#c88cff}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:8px;z-index:2}
header b{font-size:17px;letter-spacing:.04em}header span{color:var(--sub);font-size:12px;margin-left:auto}
main{max-width:720px;margin:0 auto;padding:12px 16px 40px}
textarea{width:100%;min-height:110px;background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:10px;padding:10px;font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;resize:vertical}
textarea.editing{border-color:var(--acc)}
button,select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;
padding:8px 12px;font:inherit;cursor:pointer;white-space:nowrap}
button.pri{background:var(--acc);color:#04130a;border-color:var(--acc);font-weight:600}
button.cam{background:#2a2140;border-color:var(--cam);color:#ecdcff;font-weight:600}
button:disabled{opacity:.5}
.row{display:flex;gap:8px;margin:8px 0 14px;flex-wrap:wrap}.row .grow{flex:1}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin:0 0 14px}
#cv{width:100%;display:block;border-radius:6px;touch-action:none;background:#000}
.hint{color:var(--sub);font-size:12px;margin:6px 0 0}
#editbar{display:flex;align-items:center;gap:8px;color:var(--acc);font-size:13px;margin:0 0 6px}
#editbar .sp{flex:1}#editbar button{padding:3px 10px;font-size:13px}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}
.item.top{border-color:#1f6e47}
.item pre{margin:0;padding:10px 12px;white-space:pre-wrap;word-break:break-word;
font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;max-height:240px;overflow:auto}
.meta{display:flex;align-items:center;gap:4px;padding:6px 6px 6px 10px;border-top:1px solid var(--line);
color:var(--sub);font-size:12px;flex-wrap:wrap}.meta .src{padding:1px 6px;border:1px solid var(--line);border-radius:6px}
.meta .fnv{color:var(--acc);border:1px solid #1f6e47;border-radius:6px;padding:1px 6px}
.meta .sp{flex:1}.meta button{padding:4px 8px;font-size:13px}
#login{max-width:320px;margin:15vh auto;text-align:center}
#login input{width:100%;font-size:28px;letter-spacing:.4em;text-align:center;padding:10px;
background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;margin:12px 0}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--acc);color:#04130a;
padding:8px 16px;border-radius:20px;font-weight:600;opacity:0;transition:.2s;pointer-events:none;z-index:9}
#unsure{font-size:12px;color:var(--sub);margin:6px 0 0;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
#unsure button{padding:2px 8px;font-size:13px;color:#ffaa3c;border-color:#7a5320;background:#2a2116}
.tools{display:flex;gap:6px;margin:8px 0 2px}.tools button{padding:6px 9px;font-size:14px}
.tools .sp{flex:1}button.on{border-color:var(--acc);color:var(--acc)}button.send{padding:12px;font-size:16px}
.fname{display:flex;gap:8px;margin:8px 0 0}.fname input{flex:1;min-width:0;background:var(--card);color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:13px ui-monospace,Menlo,Consolas,monospace}
.fhead{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);
font:13px ui-monospace,Menlo,Consolas,monospace;color:#9fd0ff}.fhead b{flex:1;word-break:break-all;font-weight:600}
.fhead span{color:var(--sub);font-size:12px;white-space:nowrap}
.bin{padding:12px;color:var(--sub);font-size:13px}
#drop{position:fixed;inset:0;background:rgba(47,214,123,.12);border:3px dashed var(--acc);z-index:8;
display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;color:var(--acc);pointer-events:none}
details.pc{margin:24px 0 0;color:var(--sub);font-size:13px}details.pc summary{cursor:pointer;padding:6px 0}
details.pc pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;white-space:pre-wrap;
word-break:break-all;color:var(--fg);font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:6px 0}
.item{position:relative}.item.pin{border-color:#7a6420}
.star{position:absolute;top:4px;right:4px;padding:0 8px;border:0;background:transparent;color:#e8c14a;font-size:18px;z-index:1}
.item .fhead{padding-right:40px}.item pre{padding-right:36px}
.search input{width:100%;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;
padding:9px 12px;font:inherit;margin:4px 0 2px}
.sec{color:var(--sub);font-size:12px;margin:14px 2px 0}
#toast.on{opacity:1}.err{color:#ff6b6b;min-height:1.5em}.empty{color:var(--sub);text-align:center;padding:30px}
</style></head><body>
<div id="login" hidden>
  <b style="font-size:20px">HEXA CLIP</b>
  <p style="color:var(--sub)">CardputerZero の PIN を入力</p>
  <input id="pin" inputmode="numeric" maxlength="6" autocomplete="one-time-code">
  <button class="pri" style="width:100%" onclick="login()">接続</button>
  <p class="err" id="lerr"></p>
</div>
<div id="app" hidden>
<header><b>HEXA CLIP</b><span id="st">●</span></header>
<main>
  <div id="editbar" hidden><span id="edlabel"></span><span class="sp"></span>
    <button onclick="endEdit(true)">編集をやめる</button></div>
  <textarea id="t" placeholder="コードや文章を貼り付けて「CPZへ送る」&#10;→ 本体の fn+V で打ち込めます"></textarea>
  <div class="fname" id="fnameBox" hidden><input id="fname" placeholder="ファイル名 (例: main.py → 本体の ~/clip/ に保存)"
    autocapitalize="off" autocorrect="off" spellcheck="false"></div>
  <div id="unsure" hidden></div>
  <div class="tools">
    <button onclick="pick()" title="スマホで撮って、本体で文字を読む">📷 撮って読む</button>
    <button onclick="$('files').click()" title="コードや資料をファイルのまま送る">📎 ファイル</button>
    <button id="nameBtn" onclick="toggleName()" title="名前を付けてファイルとして保存">🏷 名前</button>
    <span class="sp"></span><button onclick="clr()" title="入力欄をクリア" aria-label="クリア">✕</button>
  </div>
  <input type="file" id="file" accept="image/*" capture="environment" hidden>
  <input type="file" id="files" multiple hidden>
  <div id="crop" class="panel" hidden>
    <canvas id="cv"></canvas>
    <p class="hint" id="chint">読みたい所を指でなぞって囲むと、速く正確になります</p>
    <div class="row" style="margin-bottom:0">
      <select id="lang" aria-label="読み取る言語">
        <option value="jpn">日本語</option><option value="jpn+eng">日+英</option><option value="eng">英語</option>
        <option value="code">コード</option>
      </select>
      <button onclick="rot()">回転</button><button onclick="pick()">撮り直し</button>
      <button onclick="closeCrop()">閉じる</button>
    </div>
    <div class="row" style="margin-bottom:0"><button class="pri grow" id="ocrBtn" onclick="doOcr()">読み取る</button></div>
  </div>
  <div class="row"><button class="grow pri send" id="sendBtn" onclick="send()">CPZへ送る</button>
  <button id="reBtn" onclick="reopen()" hidden>範囲を選び直す</button></div>
  <div class="search"><input id="q" type="search" placeholder="🔍 履歴を検索" oninput="search()"
    autocapitalize="off" autocorrect="off" spellcheck="false"></div>
  <div id="list"></div>
  <details class="pc" id="pc" ontoggle="pcInfo()"><summary>PCから使う (hexaclip コマンド)</summary>
    <p>Python 3 があれば Windows / Mac / Linux で使えます。PCのブラウザなら、この画面にファイルを
    ドラッグ&amp;ドロップしても送れます。</p>
    <p>1. <a href="/hexaclip.py" style="color:var(--acc)">hexaclip.py をダウンロード</a></p>
    <p>2. 最初に1回だけ (接続先とトークンを保存):</p><pre id="pcsetup">…</pre>
    <button onclick="cpText($('pcsetup').textContent)">コピー</button>
    <p>3. 使い方 (本体でも同じ hexaclip コマンドが使えます):</p><pre># ファイルを送る
python hexaclip.py send main.py data.pdf
# PCのクリップボードを送る / 最新をPCのクリップボードへ
python hexaclip.py push --clip
python hexaclip.py paste --clip
# 最新のファイルを保存 / 履歴
python hexaclip.py get
python hexaclip.py list</pre>
  </details>
</main></div>
<div id="drop" hidden>ドロップして CPZ へ送る</div>
<div id="toast"></div>
<script>
const $=id=>document.getElementById(id);let rev=null,items=[],pins=[],latest=0,editing=null,src=null,sel=null,drag=null;
function toast(m,ms){const t=$('toast');t.textContent=m;t.classList.add('on');clearTimeout(t._h);
 t._h=setTimeout(()=>t.classList.remove('on'),ms||1600)}
async function api(p,o={}){const r=await fetch(p,Object.assign({credentials:'same-origin',
 headers:{'Content-Type':'application/json'}},o));if(r.status==401){show(false);throw 401}
 if(!r.ok)throw await r.text();return r.json()}
function show(ok){$('login').hidden=ok;$('app').hidden=!ok;if(!ok)$('pin').focus()}
async function login(){try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({pin:$('pin').value.trim()})});if(r.ok){show(true);load()}else{$('lerr').textContent=
 r.status==429?'ロック中 しばらく待ってください':'PINが違います'}}catch(e){$('lerr').textContent='接続エラー'}}
$('pin').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
function fmt(t){const d=new Date(t*1000),o={hour:'2-digit',minute:'2-digit'};
 if(d.toDateString()!==new Date().toDateString()){o.month='numeric';o.day='numeric'}return d.toLocaleString('ja-JP',o)}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fsize(n){return n<1024?n+'B':n<1048576?(n/1024).toFixed(1)+'KB':(n/1048576).toFixed(1)+'MB'}
function card(i){const top=i.id===latest&&!i.binary,dl=`<a href="/api/files/${i.id}" download><button>DL</button></a>`;
 return `<div class="item${top?' top':''}${i.pinned?' pin':''}">
 <button class="star" onclick="pin(${i.id},${!i.pinned})" title="ピン留め (履歴が流れても残る)">${i.pinned?'★':'☆'}</button>
 ${i.name?`<div class="fhead"><b>${esc(i.name)}</b><span>${fsize(i.size)}</span></div>`:''}
 ${i.binary?`<div class="bin">ファイル (本体の ~/clip/${esc(i.name)})</div>`:`<pre>${esc(i.text)}</pre>`}
 <div class="meta"><span class="src">${esc(i.source)}</span><span>${fmt(i.created)}</span>
 ${top?'<span class="fnv">fn+V</span>':''}<span class="sp"></span>
 <button onclick="del(${i.id})">削除</button>${i.binary?'':`<button onclick="edit(${i.id})">編集</button>`}
 ${!top&&!i.binary?`<button onclick="toTop(${i.id})" title="本体の fn+V で貼り付ける対象にする">貼付用</button>`:''}
 ${i.name?dl:''}${i.binary?'':`<button class="pri" onclick="cp(${i.id})">コピー</button>`}</div></div>`}
function render(){const q=$('q').value.trim(),rest=q?items:items.filter(i=>!i.pinned);let h='';
 if(!q&&pins.length)h+=`<div class="sec">★ ピン留め</div>`+pins.map(card).join('')+`<div class="sec">履歴</div>`;
 if(q)h+=`<div class="sec">「${esc(q)}」 ${items.length}件</div>`;
 h+=rest.length?rest.map(card).join(''):`<div class="empty">${q?'見つかりません':'まだ何もありません'}</div>`;
 $('list').innerHTML=h}
function find(id){return items.find(i=>i.id===id)||pins.find(i=>i.id===id)}
function online(ok){$('st').textContent=ok?'● 接続中':'● 切断';$('st').style.color=ok?'var(--acc)':'#ff6b6b'}
async function load(){try{const q=$('q').value.trim();
 const [r,p]=await Promise.all([api('/api/items?limit=50'+(q?'&q='+encodeURIComponent(q):'')),
  q?Promise.resolve({items:pins}):api('/api/items?limit=50&pinned=1')]);
 items=r.items;pins=p.items;rev=r.rev;latest=r.latest;render();online(true)
 if(editing&&!find(editing)){endEdit(false);toast('編集中の項目が本体で削除されました',2500)}}
 catch(e){if(e!==401)online(false)}}
async function poll(){try{const r=await api('/api/ping');online(true);if(r.rev!==rev)await load()}
 catch(e){if(e!==401)online(false)}}
async function send(){const v=$('t').value;if(!v.trim())return;const b=$('sendBtn');b.disabled=true;
 const nm=$('fname').value.trim();
 try{if(editing){await api('/api/items/'+editing,{method:'PUT',body:JSON.stringify({text:v})});toast('保存しました')}
 else if(nm){const r=await upload(new Blob([v.endsWith('\n')?v:v+'\n'],{type:'text/plain'}),nm);
  if(!r)return;toast('~/clip/'+r.name+' に保存しました');toggleName(false)}
 else{await api('/api/items',{method:'POST',body:JSON.stringify({text:v,source:lastOcr?'cam':'web'})});toast('送りました')}
 $('t').value='';lastOcr=false;unsure=[];showUnsure();endEdit(false);load()}catch(e){toast('失敗')}finally{b.disabled=false}}
function clr(){$('t').value='';lastOcr=false;unsure=[];showUnsure();endEdit(false);toggleName(false)}
function toggleName(on){const b=$('fnameBox'),open=on===undefined?b.hidden:on;b.hidden=!open;
 $('nameBtn').classList.toggle('on',open);if(open)$('fname').focus();else $('fname').value=''}
async function del(id){if(!confirm('#'+id+' を削除しますか?'))return;
 try{await api('/api/items/'+id,{method:'DELETE'})}catch(e){}load()}
async function toTop(id){try{await api('/api/items/'+id+'/top',{method:'POST',body:'{}'});
 toast('貼付用にしました (本体で fn+V)');load()}catch(e){toast('失敗')}}
function edit(id){const it=find(id);if(!it||it.binary)return;editing=id;$('t').value=it.text;
 toggleName(false);$('nameBtn').disabled=true;
 $('t').classList.add('editing');$('editbar').hidden=false;$('edlabel').textContent='#'+id+' を編集中';
 $('sendBtn').textContent='保存';scrollTo({top:0,behavior:'smooth'});$('t').focus()}
function endEdit(clear){if(clear&&editing)$('t').value='';editing=null;$('nameBtn').disabled=false;$('t').classList.remove('editing');
 $('editbar').hidden=true;$('sendBtn').textContent='CPZへ送る'}
function cp(id){cpText(find(id).text)}
async function pin(id,on){try{await api('/api/items/'+id+'/pin',{method:'POST',body:JSON.stringify({on})});
 toast(on?'ピン留めしました':'ピン留めを外しました');load()}catch(e){toast('失敗')}}
let qT=0;function search(){clearTimeout(qT);qT=setTimeout(load,250)}
function cpText(s){
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(s).then(()=>toast('コピーしました'))}
 else{const ta=document.createElement('textarea');ta.value=s;ta.style.position='fixed';ta.style.opacity='0';
 document.body.appendChild(ta);ta.focus();ta.select();ta.setSelectionRange(0,s.length);
 try{document.execCommand('copy');toast('コピーしました')}catch(e){toast('コピー失敗')}ta.remove()}}

// ---- ファイル送信 (ボタン / ドラッグ&ドロップ)。進み具合が出るよう XHR を使う
function upload(blob,name){return new Promise(ok=>{const x=new XMLHttpRequest();
 x.open('POST','/api/files?name='+encodeURIComponent(name)+'&source=web');x.withCredentials=true;
 x.upload.onprogress=e=>{if(e.lengthComputable&&e.total>200000)toast(`${name} ${Math.round(e.loaded/e.total*100)}%`,4000)};
 x.onload=()=>{let j={};try{j=JSON.parse(x.responseText)}catch(e){}
  if(x.status==401){show(false);ok(null)}else if(x.status!=200){toast(`${name}: ${j.error||x.status}`,3500);ok(null)}else ok(j)};
 x.onerror=()=>{toast(name+': 送信失敗',3000);ok(null)};x.send(blob)})}
async function sendFiles(list){let n=0;for(const f of list){if(await upload(f,f.name))n++}
 if(n)toast(n==1?'~/clip/ に保存しました':`${n}個のファイルを ~/clip/ に保存しました`,2500);load()}
$('files').onchange=e=>{const l=[...e.target.files];e.target.value='';if(l.length)sendFiles(l)};
let dragN=0;addEventListener('dragenter',e=>{if(e.dataTransfer.types.includes('Files')&&!$('app').hidden){dragN++;$('drop').hidden=false}});
addEventListener('dragleave',()=>{if(--dragN<=0){dragN=0;$('drop').hidden=true}});
addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('Files'))e.preventDefault()});
addEventListener('drop',e=>{if(!e.dataTransfer.files.length)return;e.preventDefault();dragN=0;$('drop').hidden=true;
 if(!$('app').hidden)sendFiles([...e.dataTransfer.files])});
async function pcInfo(){if(!$('pc').open)return;try{const r=await api('/api/token');
 $('pcsetup').textContent=`python hexaclip.py setup ${location.origin} ${r.token}`}catch(e){}}

// ---- 撮影 → 範囲選択 → 本体でOCR
const MAXSRC=3000,MAXFULL=2000;let lastOcr=false,unsure=[];
// 自信のない語: タップで入力欄のその場所を選択 (位置がずれていたら同じ語を探す)
function showUnsure(){const u=$('unsure');u.hidden=!unsure.length;if(!unsure.length)return;
 u.innerHTML='自信なし:'+unsure.map((x,i)=>`<button onclick="jump(${i})">${esc(x.w)}</button>`).join('')}
function jump(i){const t=$('t'),x=unsure[i];let s=t.value.substr(x.s,x.w.length)===x.w?x.s:t.value.indexOf(x.w);
 if(s<0){unsure.splice(i,1);showUnsure();return}t.focus();t.setSelectionRange(s,s+x.w.length)}
try{$('lang').value=localStorage.getItem('hc_lang')||'jpn'}catch(e){}
$('lang').onchange=()=>{try{localStorage.setItem('hc_lang',$('lang').value)}catch(e){}};
function pick(){$('file').value='';$('file').click()}
$('file').onchange=e=>{const f=e.target.files[0];if(!f)return;const u=URL.createObjectURL(f),im=new Image();
 im.onload=()=>{const k=Math.min(1,MAXSRC/Math.max(im.naturalWidth,im.naturalHeight));
  const c=document.createElement('canvas');c.width=Math.round(im.naturalWidth*k);c.height=Math.round(im.naturalHeight*k);
  c.getContext('2d').drawImage(im,0,0,c.width,c.height);URL.revokeObjectURL(u);src=c;sel=null;
  $('crop').hidden=false;$('reBtn').hidden=true;draw();$('crop').scrollIntoView({behavior:'smooth'})};
 im.onerror=()=>toast('画像を開けませんでした');im.src=u};
function rot(){if(!src)return;const c=document.createElement('canvas');c.width=src.height;c.height=src.width;
 const x=c.getContext('2d');x.translate(c.width,0);x.rotate(Math.PI/2);x.drawImage(src,0,0);src=c;sel=null;draw()}
function draw(){const cv=$('cv'),dpr=window.devicePixelRatio||1,w=cv.clientWidth||300;
 cv.width=Math.round(w*dpr);cv.height=Math.round(w*dpr*src.height/src.width);
 const x=cv.getContext('2d'),k=cv.width/src.width;x.drawImage(src,0,0,cv.width,cv.height);
 if(sel&&sel.w>2&&sel.h>2){x.fillStyle='rgba(0,0,0,.55)';x.beginPath();x.rect(0,0,cv.width,cv.height);
  x.rect(sel.x*k,sel.y*k,sel.w*k,sel.h*k);x.fill('evenodd');x.strokeStyle='#2fd67b';x.lineWidth=2*dpr;
  x.strokeRect(sel.x*k,sel.y*k,sel.w*k,sel.h*k)}
 $('chint').textContent=sel?`範囲 ${Math.round(sel.w)}×${Math.round(sel.h)}px  (なぞり直すと変更)`:
  '読みたい所を指でなぞって囲むと、速く正確になります'}
function pt(e){const r=$('cv').getBoundingClientRect(),k=src.width/r.width;
 return{x:Math.max(0,Math.min(src.width,(e.clientX-r.left)*k)),y:Math.max(0,Math.min(src.height,(e.clientY-r.top)*k))}}
$('cv').addEventListener('pointerdown',e=>{if(!src)return;e.preventDefault();$('cv').setPointerCapture(e.pointerId);
 drag=pt(e);sel={x:drag.x,y:drag.y,w:0,h:0};draw()});
$('cv').addEventListener('pointermove',e=>{if(!drag)return;const p=pt(e);
 sel={x:Math.min(drag.x,p.x),y:Math.min(drag.y,p.y),w:Math.abs(p.x-drag.x),h:Math.abs(p.y-drag.y)};draw()});
$('cv').addEventListener('pointerup',()=>{drag=null;if(sel&&(sel.w<16||sel.h<8))sel=null;draw()});
addEventListener('resize',()=>{if(src&&!$('crop').hidden)draw()});
function closeCrop(){$('crop').hidden=true;$('reBtn').hidden=!src}
function reopen(){$('crop').hidden=false;$('reBtn').hidden=true;draw();$('crop').scrollIntoView({behavior:'smooth'})}
function region(){const r=sel||{x:0,y:0,w:src.width,h:src.height};
 const k=sel?1:Math.min(1,MAXFULL/Math.max(r.w,r.h)),c=document.createElement('canvas');
 c.width=Math.max(1,Math.round(r.w*k));c.height=Math.max(1,Math.round(r.h*k));
 c.getContext('2d').drawImage(src,r.x,r.y,r.w,r.h,0,0,c.width,c.height);return c}
async function doOcr(){if(!src)return;const b=$('ocrBtn');b.disabled=true;b.textContent='本体で読み取り中…';
 try{const blob=await new Promise(ok=>region().toBlob(ok,'image/jpeg',0.92));
  const r=await fetch('/api/ocr?lang='+encodeURIComponent($('lang').value),{method:'POST',
   credentials:'same-origin',headers:{'Content-Type':'image/jpeg'},body:blob});
  if(r.status==401){show(false);return}const j=await r.json();if(!r.ok)throw j.error||r.status;
  if(!j.text.trim()){toast('文字が見つかりませんでした。範囲を狭めるか撮り直してください',3000);return}
  if(editing)endEdit(false);const t=$('t'),off=t.value.trim()?t.value.replace(/\n*$/,'\n').length:0;
  t.value=off?t.value.replace(/\n*$/,'\n')+j.text:j.text;
  unsure=(off?unsure:[]).concat((j.marks||[]).map(m=>({s:m[0]+off,w:j.text.slice(m[0],m[1])})));showUnsure();
  lastOcr=true;closeCrop();t.scrollIntoView({behavior:'smooth',block:'center'});
  toast(`読み取りました (${j.elapsed.toFixed(1)}秒) 直して「CPZへ送る」`,2800)}
 catch(e){toast('読み取り失敗: '+e,3000)}finally{b.disabled=false;b.textContent='読み取る'}}

(async()=>{const m=location.hash.match(/pin=(\d{6})/);
 if(m){history.replaceState(null,'',location.pathname);$('pin').value=m[1];await login();if($('app').hidden)show(false);return}
 try{await api('/api/ping');show(true);load()}catch(e){}})();
// 変更は SSE で即時に受け取る。つながらない間だけ2秒ごとの確認に戻す
let es=null,sseOk=false;
function startSSE(){if(es||!window.EventSource||$('app').hidden)return;es=new EventSource('/api/events');
 es.onopen=()=>{sseOk=true;online(true)};
 es.onmessage=e=>{sseOk=true;online(true);if(+e.data!==rev)load()};
 es.onerror=()=>{sseOk=false;es.close();es=null;setTimeout(startSSE,5000)}}
setInterval(()=>{if($('app').hidden||document.hidden)return;if(!es)startSSE();if(!sseOk)poll()},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!$('app').hidden)poll()});
</script></body></html>
"""


# ------------------------------------------------------------------ server
class App:
    def __init__(self, store, pin, token):
        self.store = store
        self.pin = pin
        self.token = token
        self.sse = 0
        self.sse_lock = threading.Lock()
        self.fail = {}          # ip -> (count, locked_until)
        self.flock = threading.Lock()

    def check_pin(self, ip, pin):
        now = time.time()
        with self.flock:
            cnt, until = self.fail.get(ip, (0, 0))
            if until > now:
                return "locked"
            if hmac.compare_digest(str(pin), self.pin):
                self.fail.pop(ip, None)
                return "ok"
            cnt += 1
            self.fail[ip] = (0, now + 60) if cnt >= 5 else (cnt, 0)
            return "ng"


class Handler(BaseHTTPRequestHandler):
    server_version = f"czclipd/{VERSION}"
    app: App = None

    def log_message(self, fmt, *args):
        pass

    # helpers
    def ip(self):
        return self.client_address[0].replace("::ffff:", "")

    def is_local(self):
        return self.ip() in LOCAL_ADDRS

    def token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["hc"].value if "hc" in c else None

    def bearer(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return bool(tok) and hmac.compare_digest(tok, self.app.token)

    def authed(self):
        return (self.is_local() or self.bearer()
                or self.app.store.valid_session(self.token()))

    def send_text(self, text, code=200):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def events(self):
        """Server-Sent Events: 変更があった瞬間に rev を送る (Web画面の即時反映)"""
        with self.app.sse_lock:
            if self.app.sse >= MAX_SSE:
                return self.send_json({"error": "busy"}, 503)
            self.app.sse += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            rev = self.app.store.rev
            self.wfile.write(f"data: {rev}\n\n".encode())
            self.wfile.flush()
            while True:
                new = self.app.store.wait_change(rev, 25)
                msg = f"data: {new}\n\n" if new != rev else ": keepalive\n\n"
                rev = new
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.app.sse_lock:
                self.app.sse -= 1

    def send_json(self, obj, code=200, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_TEXT + 1024:
            raise ValueError("too large")
        raw = self.rfile.read(n)
        if self.headers.get("Content-Type", "").startswith("text/plain"):
            # ショートカット等から本文をそのまま送る (source は ?source= で指定、既定 share)
            src = parse_qs(urlparse(self.path).query).get("source", ["share"])[0]
            return {"text": raw.decode("utf-8", "replace"), "source": src}
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("json must be an object")
        return data

    # routes
    def _do_GET(self):
        u = urlparse(self.path)
        if u.path == "/hexaclip.py":          # PC用コマンドの配布 (秘密情報は含まない)
            try:
                body = open(PC_TOOL, "rb").read()
            except OSError:
                return self.send_json({"error": "not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/x-python; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="hexaclip.py"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        if u.path == "/api/ping":
            return self.send_json({"ok": True, "version": VERSION, "rev": self.app.store.rev})
        if u.path == "/api/events":
            return self.events()
        if u.path == "/api/items":
            q = parse_qs(u.query)
            limit = max(1, min(int(q.get("limit", ["50"])[0]), MAX_ITEMS))
            after = max(0, int(q.get("after", ["0"])[0]))
            preview = max(0, int(q.get("preview", ["0"])[0]))
            its = self.app.store.list(limit, after, preview, q.get("q", [""])[0],
                                      q.get("pinned", ["0"])[0] == "1")
            # latest = fn+V で貼られる項目のID (検索で絞っても分かるように)
            return self.send_json({"items": its, "rev": self.app.store.rev,
                                   "latest": self.app.store.latest})
        if u.path == "/api/latest":            # 最新の本文だけ (ショートカットで「CPZからコピー」)
            items = self.app.store.list(1)
            return self.send_text(items[0]["text"] if items else "")
        if u.path.startswith("/api/items/"):
            it = self.app.store.get(int(u.path.rsplit("/", 1)[1]))
            return self.send_json(it) if it else self.send_json({"error": "not found"}, 404)
        if u.path == "/api/pin" and self.is_local():
            ip, port, m = lan_ip(), self.server.server_port, mdns_name()
            return self.send_json({"pin": self.app.pin, "url": f"http://{ip}:{port}/",
                                   "online": ip != "127.0.0.1",
                                   "mdns": f"http://{m}:{port}/" if m else None})
        if u.path == "/api/token":             # ログイン済みの画面にも出す (同じ権限なので)
            return self.send_json({"token": self.app.token})
        if u.path.startswith("/api/files/"):
            return self.get_file(int(u.path.rsplit("/", 1)[1]))
        self.send_json({"error": "not found"}, 404)

    def _do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/ocr":
            return self.post_ocr(u)
        if u.path == "/api/files":
            return self.post_file(u)
        try:
            data = self.read_json()
        except Exception:
            return self.send_json({"error": "bad request"}, 400)
        if u.path == "/api/login":
            r = self.app.check_pin(self.ip(), data.get("pin", ""))
            if r == "ok":
                tok = self.app.store.new_session()
                ck = f"hc={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS*86400}"
                return self.send_json({"ok": True}, extra={"Set-Cookie": ck})
            return self.send_json({"error": r}, 429 if r == "locked" else 403)
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        if u.path == "/api/items":
            text = data.get("text", "")
            if not isinstance(text, str) or not text.strip():
                return self.send_json({"error": "empty"}, 400)
            if len(text.encode()) > MAX_TEXT:
                return self.send_json({"error": "too large"}, 413)
            src = str(data.get("source", "web"))[:16]
            return self.send_json({"id": self.app.store.add(text, src)})
        if u.path.startswith("/api/items/") and u.path.endswith("/pin"):
            ok = self.app.store.set_pin(int(u.path.split("/")[3]), data.get("on", True))
            return self.send_json({"ok": ok}, 200 if ok else 404)
        if u.path.startswith("/api/items/") and u.path.endswith("/top"):
            new = self.app.store.to_top(int(u.path.split("/")[3]))
            return self.send_json({"id": new}) if new else self.send_json({"error": "not found"}, 404)
        if u.path == "/api/token/reset" and self.is_local():
            self.app.token = load_token(True)
            return self.send_json({"token": self.app.token})
        if u.path == "/api/logout_all" and self.is_local():
            self.app.store.clear_sessions()
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def post_ocr(self, u):
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return self.send_json({"error": "画像がありません"}, 400)
        if n > MAX_IMAGE:
            return self.send_json({"error": "画像が大きすぎます"}, 413)
        data = self.rfile.read(n)
        lang = parse_qs(u.query).get("lang", ["jpn"])[0].replace(" ", "+")
        if lang not in OCR_LANGS:
            return self.send_json({"error": "lang"}, 400)
        try:
            text, marks, dt = ocr_image(data, lang)
        except ImportError as e:
            return self.send_json({"error": f"OCRの準備ができていません ({e.name})"}, 503)
        except FileNotFoundError:
            return self.send_json({"error": "tesseract がありません"}, 503)
        except subprocess.TimeoutExpired:
            return self.send_json({"error": "時間切れ (範囲を狭めてください)"}, 504)
        except Exception as e:
            print("ocr failed:", repr(e), flush=True)
            return self.send_json({"error": f"読み取れない画像です ({type(e).__name__})"}, 400)
        print(f"ocr {lang} {len(data)//1024}KB {dt:.1f}s {len(text)}chars", flush=True)
        return self.send_json({"text": text, "elapsed": dt, "marks": marks})

    def post_file(self, u):
        """ファイルを名前付きで受け取る: 本文=ファイルの中身、?name=ファイル名
        テキスト(UTF-8, 256KB以下)は履歴に本文も入り、本体の fn+V / 編集 / コピーができる。
        それ以外 (PDF・画像など) は ~/clip/ に保存し、履歴からダウンロードできる"""
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        q = parse_qs(u.query)
        name = safe_name(q.get("name", [""])[0])
        if not name:
            return self.send_json({"error": "ファイル名がありません"}, 400)
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return self.send_json({"error": "空のファイルです"}, 400)
        if n > MAX_FILE:
            return self.send_json({"error": f"{MAX_FILE // 1024 // 1024}MBまでです"}, 413)
        src = str(q.get("source", ["web"])[0])[:16]
        os.makedirs(FILES_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=FILES_DIR, prefix=".upload_")
        try:
            left = n
            with os.fdopen(fd, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 256 * 1024))
                    if not chunk:
                        raise ConnectionError("short body")
                    f.write(chunk)
                    left -= len(chunk)
            text = None
            if n <= MAX_TEXT:
                raw = open(tmp, "rb").read()
                if b"\0" not in raw:
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text = None
            if text is not None:             # Windowsの改行やBOMはそろえる (本体はLinux)
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                write_text_file(tmp, text)
            final = place_file(tmp, name)
        except ConnectionError:
            return self.send_json({"error": "途中で切れました"}, 400)
        except OSError as e:
            print("file save failed:", e, flush=True)
            return self.send_json({"error": "保存できません (容量不足?)"}, 507)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        size = os.path.getsize(final)
        new = self.app.store.add(text if text is not None else "", src, name=name, path=final,
                                 size=size, binary=text is None, trim=False)
        print(f"file {name} {size}B {'text' if text is not None else 'binary'} -> #{new}", flush=True)
        return self.send_json({"id": new, "name": name, "size": size, "binary": text is None,
                               "path": final})

    def get_file(self, item_id):
        """ダウンロード。名前付きはその名前で、名前なしは clip_<ID>.txt"""
        it = self.app.store.get(item_id)
        if not it:
            return self.send_json({"error": "not found"}, 404)
        name = it["name"] or f"clip_{it['id']}.txt"
        ascii_name = "".join(c if c.isascii() and c.isprintable() and c not in '"\\' else "_"
                             for c in name)
        disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
        if not it["binary"]:
            body = it["text"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", disp)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        path = it["path"]
        if not path or not os.path.exists(path):
            return self.send_json({"error": "本体のファイルが削除されています"}, 404)
        if os.path.getsize(path) != it["size"]:
            return self.send_json({"error": "同じ名前の新しいファイルで上書きされています"}, 410)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Disposition", disp)
        self.send_header("Content-Length", str(it["size"]))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _do_PUT(self):
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        u = urlparse(self.path)
        try:
            data = self.read_json()
        except Exception:
            return self.send_json({"error": "bad request"}, 400)
        if u.path.startswith("/api/items/"):
            text = data.get("text", "")
            if not isinstance(text, str) or not text.strip():
                return self.send_json({"error": "empty"}, 400)
            if len(text.encode()) > MAX_TEXT:
                return self.send_json({"error": "too large"}, 413)
            ok = self.app.store.update(int(u.path.rsplit("/", 1)[1]), text)
            return self.send_json({"ok": ok}, 200 if ok else 404)   # バイナリも 404 (編集不可)
        self.send_json({"error": "not found"}, 404)

    def _do_DELETE(self):
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        u = urlparse(self.path)
        if u.path.startswith("/api/items/"):
            ok = self.app.store.delete(int(u.path.rsplit("/", 1)[1]))
            return self.send_json({"ok": ok}, 200 if ok else 404)
        self.send_json({"error": "not found"}, 404)


    def _safe(self, fn):
        """不正なID・数値などは 400 を返す (以前は例外で接続が切れていた)"""
        try:
            fn()
        except (ValueError, TypeError, KeyError, IndexError):
            self.send_json({"error": "bad request"}, 400)

    def do_GET(self):
        self._safe(self._do_GET)

    def do_POST(self):
        self._safe(self._do_POST)

    def do_DELETE(self):
        self._safe(self._do_DELETE)

    def do_PUT(self):
        self._safe(self._do_PUT)


class DualStackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="HEXA CLIP daemon")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--reset-pin", action="store_true",
                    help="PINを作り直して全端末をログアウトし、終了 (その後サービスを再起動)")
    a = ap.parse_args()

    store = Store(DB_PATH)
    if a.reset_pin:
        pin = load_pin(True)
        store.clear_sessions()
        print(f"new PIN: {pin}  (sudo systemctl restart hexaclip で反映)")
        return
    pin = load_pin()
    Handler.app = App(store, pin, load_token())
    srv = DualStackServer((a.bind, a.port), Handler)
    print(f"HEXA CLIP {VERSION}  http://{lan_ip()}:{a.port}/  PIN={pin}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
