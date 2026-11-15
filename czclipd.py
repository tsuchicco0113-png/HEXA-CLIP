#!/usr/bin/env python3
"""HEXA CLIP daemon (czclipd) v0.1 — CardputerZero 共有クリップボード

- 履歴を SQLite に保存
- http://<CPZ>:8080/ でスマホ/PC用の画面 (PIN認証)
- 127.0.0.1 からのアクセスは認証不要 (czclip コマンド / ocr_cam 用)

標準ライブラリのみ。追加インストール不要。
"""
import argparse
import hmac
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.2"
DATA_DIR = os.path.expanduser(os.environ.get("HEXACLIP_DIR", "~/.local/share/hexaclip"))
DB_PATH = os.path.join(DATA_DIR, "clip.db")
PIN_PATH = os.path.join(DATA_DIR, "pin")
MAX_TEXT = 256 * 1024          # 1件の上限 256KB
MAX_ITEMS = 500                # 履歴の上限
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
        self.db.commit()

    def add(self, text, source):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO items(text, source, created) VALUES(?,?,?)",
                (text, source, time.time()))
            self.db.execute(
                "DELETE FROM items WHERE id NOT IN "
                "(SELECT id FROM items ORDER BY id DESC LIMIT ?)", (MAX_ITEMS,))
            self.db.commit()
            return cur.lastrowid

    def list(self, limit=50, after=0):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, text, source, created FROM items WHERE id > ? "
                "ORDER BY id DESC LIMIT ?", (after, limit)).fetchall()
        return [dict(id=r[0], text=r[1], source=r[2], created=r[3]) for r in rows]

    def get(self, item_id):
        with self.lock:
            r = self.db.execute(
                "SELECT id, text, source, created FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(id=r[0], text=r[1], source=r[2], created=r[3]) if r else None

    def delete(self, item_id):
        with self.lock:
            n = self.db.execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount
            self.db.commit()
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


def load_pin(reset=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    if reset or not os.path.exists(PIN_PATH):
        pin = f"{secrets.randbelow(10**6):06d}"
        fd = os.open(PIN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pin)
        return pin
    return open(PIN_PATH).read().strip()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------------------ web page
PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HEXA CLIP</title>
<style>
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--fg:#e8eaf0;--sub:#8a93a6;--acc:#2fd67b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:8px;z-index:2}
header b{font-size:17px;letter-spacing:.04em}header span{color:var(--sub);font-size:12px;margin-left:auto}
main{max-width:720px;margin:0 auto;padding:12px 16px 40px}
textarea{width:100%;min-height:96px;background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:10px;padding:10px;font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;resize:vertical}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;
padding:8px 14px;font:inherit;cursor:pointer}
button.pri{background:var(--acc);color:#04130a;border-color:var(--acc);font-weight:600}
.row{display:flex;gap:8px;margin:8px 0 18px}.row .grow{flex:1}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}
.item pre{margin:0;padding:10px 12px;white-space:pre-wrap;word-break:break-word;
font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;max-height:240px;overflow:auto}
.meta{display:flex;align-items:center;gap:8px;padding:6px 8px 6px 12px;border-top:1px solid var(--line);
color:var(--sub);font-size:12px}.meta .src{padding:1px 6px;border:1px solid var(--line);border-radius:6px}
.meta .sp{flex:1}.meta button{padding:4px 10px;font-size:13px}
#login{max-width:320px;margin:15vh auto;text-align:center}
#login input{width:100%;font-size:28px;letter-spacing:.4em;text-align:center;padding:10px;
background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;margin:12px 0}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--acc);color:#04130a;
padding:8px 16px;border-radius:20px;font-weight:600;opacity:0;transition:.2s;pointer-events:none}
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
  <textarea id="t" placeholder="ここに貼り付けて「送る」→ CardputerZero で czclip paste"></textarea>
  <div class="row"><button class="grow pri" onclick="send()">CPZへ送る</button>
  <button onclick="document.getElementById('t').value=''">クリア</button></div>
  <div id="list"></div>
</main></div>
<div id="toast"></div>
<script>
const $=id=>document.getElementById(id);let last=0,items=[];
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1400)}
async function api(p,o={}){const r=await fetch(p,Object.assign({credentials:'same-origin',
 headers:{'Content-Type':'application/json'}},o));if(r.status==401){show(false);throw 401}
 if(!r.ok)throw await r.text();return r.json()}
function show(ok){$('login').hidden=ok;$('app').hidden=!ok;if(!ok)$('pin').focus()}
async function login(){try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({pin:$('pin').value.trim()})});if(r.ok){show(true);load(true)}else{$('lerr').textContent=
 r.status==429?'ロック中 しばらく待ってください':'PINが違います'}}catch(e){$('lerr').textContent='接続エラー'}}
$('pin').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
function fmt(t){const d=new Date(t*1000);return d.toLocaleString('ja-JP',{month:'numeric',day:'numeric',
 hour:'2-digit',minute:'2-digit'})}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function render(){$('list').innerHTML=items.length?items.map(i=>`<div class="item"><pre>${esc(i.text)}</pre>
 <div class="meta"><span class="src">${esc(i.source)}</span><span>${fmt(i.created)}</span><span class="sp"></span>
 <button onclick="del(${i.id})">削除</button><button class="pri" onclick="cp(${i.id})">コピー</button></div></div>`).join('')
 :'<div class="empty">まだ何もありません</div>'}
async function load(full){try{const r=await api('/api/items?limit=50'+(full?'':'&after='+last));
 if(full)items=r.items;else items=r.items.concat(items);if(items.length)last=Math.max(...items.map(i=>i.id));
 if(full||r.items.length)render();$('st').textContent='● 接続中';$('st').style.color='var(--acc)'}
 catch(e){if(e!==401){$('st').textContent='● 切断';$('st').style.color='#ff6b6b'}}}
async function send(){const v=$('t').value;if(!v.trim())return;try{await api('/api/items',{method:'POST',
 body:JSON.stringify({text:v,source:'web'})});$('t').value='';toast('送りました');load(false)}catch(e){toast('失敗')}}
async function del(id){await api('/api/items/'+id,{method:'DELETE'});items=items.filter(i=>i.id!==id);render()}
function cp(id){const s=items.find(i=>i.id===id).text;
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(s).then(()=>toast('コピーしました'))}
 else{const ta=document.createElement('textarea');ta.value=s;ta.style.position='fixed';ta.style.opacity='0';
 document.body.appendChild(ta);ta.focus();ta.select();ta.setSelectionRange(0,s.length);
 try{document.execCommand('copy');toast('コピーしました')}catch(e){toast('コピー失敗')}ta.remove()}}
(async()=>{const m=location.hash.match(/pin=(\d{6})/);
 if(m){history.replaceState(null,'',location.pathname);$('pin').value=m[1];await login();if($('app').hidden)show(false);return}
 try{await api('/api/ping');show(true);load(true)}catch(e){}})();
setInterval(()=>{if(!$('app').hidden)load(false)},3000);
</script></body></html>
"""


# ------------------------------------------------------------------ server
class App:
    def __init__(self, store, pin):
        self.store = store
        self.pin = pin
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

    def authed(self):
        return self.is_local() or self.app.store.valid_session(self.token())

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
        return json.loads(self.rfile.read(n) or b"{}")

    # routes
    def do_GET(self):
        u = urlparse(self.path)
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
            return self.send_json({"ok": True, "version": VERSION})
        if u.path == "/api/items":
            q = parse_qs(u.query)
            limit = min(int(q.get("limit", ["50"])[0]), MAX_ITEMS)
            after = int(q.get("after", ["0"])[0])
            return self.send_json({"items": self.app.store.list(limit, after)})
        if u.path.startswith("/api/items/"):
            it = self.app.store.get(int(u.path.rsplit("/", 1)[1]))
            return self.send_json(it) if it else self.send_json({"error": "not found"}, 404)
        if u.path == "/api/pin" and self.is_local():
            return self.send_json({"pin": self.app.pin, "url": f"http://{lan_ip()}:{self.server.server_port}/"})
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
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
        if u.path == "/api/logout_all" and self.is_local():
            self.app.store.clear_sessions()
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        u = urlparse(self.path)
        if u.path.startswith("/api/items/"):
            ok = self.app.store.delete(int(u.path.rsplit("/", 1)[1]))
            return self.send_json({"ok": ok}, 200 if ok else 404)
        self.send_json({"error": "not found"}, 404)


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
    Handler.app = App(store, pin)
    srv = DualStackServer((a.bind, a.port), Handler)
    print(f"HEXA CLIP {VERSION}  http://{lan_ip()}:{a.port}/  PIN={pin}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
