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
from urllib.parse import parse_qs, urlparse

VERSION = "0.4"
DATA_DIR = os.path.expanduser(os.environ.get("HEXACLIP_DIR", "~/.local/share/hexaclip"))
DB_PATH = os.path.join(DATA_DIR, "clip.db")
PIN_PATH = os.path.join(DATA_DIR, "pin")
MAX_TEXT = 256 * 1024          # 1件の上限 256KB
MAX_ITEMS = 500                # 履歴の上限
MAX_IMAGE = 12 * 1024 * 1024   # スマホ撮影の画像 12MB
OCR_LANGS = {"jpn", "eng", "jpn+eng"}
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
        self.db.commit()
        # 追加/削除/並び替えのたびに増える番号。Web画面はこれが変わったら一覧を取り直す
        self.rev = int(time.time() * 1000)

    def _bump(self):
        self.rev += 1

    def add(self, text, source):
        text = normalize(text)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO items(text, source, created) VALUES(?,?,?)",
                (text, source, time.time()))
            self.db.execute(
                "DELETE FROM items WHERE id NOT IN "
                "(SELECT id FROM items ORDER BY id DESC LIMIT ?)", (MAX_ITEMS,))
            self.db.commit()
            self._bump()
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

    def to_top(self, item_id):
        """指定の項目を最新(一番上)に移す。新しいIDを返す"""
        with self.lock:
            r = self.db.execute("SELECT text, source FROM items WHERE id=?", (item_id,)).fetchone()
            if not r:
                return None
            self.db.execute("DELETE FROM items WHERE id=?", (item_id,))
            cur = self.db.execute("INSERT INTO items(text, source, created) VALUES(?,?,?)",
                                  (r[0], r[1], time.time()))
            self.db.commit()
            self._bump()
            return cur.lastrowid

    def update(self, item_id, text):
        """本文を書き換える (スマホでの編集)。位置(ID)は変えない"""
        text = normalize(text)
        with self.lock:
            n = self.db.execute("UPDATE items SET text=?, created=? WHERE id=?",
                                (text, time.time(), item_id)).rowcount
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


# ------------------------------------------------------------------ OCR (スマホ撮影)
_OCR_LOCK = threading.Lock()       # CM0 は非力なので1枚ずつ


def ocr_image(data, lang):
    """画像(バイト列) → (テキスト, 秒)。前処理と読み取りは本体アプリ(ocr_cam)と同じ"""
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
            text = ocr_cam.run_ocr(ocr_cam.prepare(img), lang, 6, path=path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return text, time.time() - t0


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
  <div class="row">
    <button class="cam grow" id="camBtn" onclick="pick()">撮影して文字を読む</button>
    <select id="lang" aria-label="OCRの言語">
      <option value="jpn">日本語</option><option value="jpn+eng">日+英</option><option value="eng">英語/コード</option>
    </select>
  </div>
  <input type="file" id="file" accept="image/*" capture="environment" hidden>
  <div id="crop" class="panel" hidden>
    <canvas id="cv"></canvas>
    <p class="hint" id="chint">読みたい所を指でなぞって囲むと、速く正確になります</p>
    <div class="row" style="margin-bottom:0">
      <button onclick="rot()">回転</button><button onclick="pick()">撮り直し</button>
      <button onclick="closeCrop()">閉じる</button>
      <button class="pri grow" id="ocrBtn" onclick="doOcr()">読み取る</button>
    </div>
  </div>
  <div id="editbar" hidden><span id="edlabel"></span><span class="sp"></span>
    <button onclick="endEdit(true)">編集をやめる</button></div>
  <textarea id="t" placeholder="ここに貼り付けて「CPZへ送る」→ 本体で fn+V / czclip paste"></textarea>
  <div class="row"><button class="grow pri" id="sendBtn" onclick="send()">CPZへ送る</button>
  <button id="reBtn" onclick="reopen()" hidden>範囲を選び直す</button>
  <button onclick="clr()">クリア</button></div>
  <div id="list"></div>
</main></div>
<div id="toast"></div>
<script>
const $=id=>document.getElementById(id);let rev=null,items=[],editing=null,src=null,sel=null,drag=null;
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
function render(){$('list').innerHTML=items.length?items.map((i,n)=>`<div class="item${n?'':' top'}"><pre>${esc(i.text)}</pre>
 <div class="meta"><span class="src">${esc(i.source)}</span><span>${fmt(i.created)}</span>
 ${n?'':'<span class="fnv">fn+V</span>'}<span class="sp"></span>
 <button onclick="del(${i.id})">削除</button><button onclick="edit(${i.id})">編集</button>
 ${n?`<button onclick="toTop(${i.id})" title="本体の fn+V で貼り付ける対象にする">貼付用</button>`:''}
 <button class="pri" onclick="cp(${i.id})">コピー</button></div></div>`).join('')
 :'<div class="empty">まだ何もありません</div>'}
function online(ok){$('st').textContent=ok?'● 接続中':'● 切断';$('st').style.color=ok?'var(--acc)':'#ff6b6b'}
async function load(){try{const r=await api('/api/items?limit=50');items=r.items;rev=r.rev;render();online(true)
 if(editing&&!items.some(i=>i.id===editing)){endEdit(false);toast('編集中の項目が本体で削除されました',2500)}}
 catch(e){if(e!==401)online(false)}}
async function poll(){try{const r=await api('/api/ping');online(true);if(r.rev!==rev)await load()}
 catch(e){if(e!==401)online(false)}}
async function send(){const v=$('t').value;if(!v.trim())return;const b=$('sendBtn');b.disabled=true;
 try{if(editing){await api('/api/items/'+editing,{method:'PUT',body:JSON.stringify({text:v})});toast('保存しました')}
 else{await api('/api/items',{method:'POST',body:JSON.stringify({text:v,source:lastOcr?'cam':'web'})});toast('送りました')}
 $('t').value='';lastOcr=false;endEdit(false);load()}catch(e){toast('失敗')}finally{b.disabled=false}}
function clr(){$('t').value='';lastOcr=false;endEdit(false)}
async function del(id){if(!confirm('#'+id+' を削除しますか?'))return;
 try{await api('/api/items/'+id,{method:'DELETE'})}catch(e){}load()}
async function toTop(id){try{await api('/api/items/'+id+'/top',{method:'POST',body:'{}'});
 toast('貼付用にしました (本体で fn+V)');load()}catch(e){toast('失敗')}}
function edit(id){const it=items.find(i=>i.id===id);if(!it)return;editing=id;$('t').value=it.text;
 $('t').classList.add('editing');$('editbar').hidden=false;$('edlabel').textContent='#'+id+' を編集中';
 $('sendBtn').textContent='保存';scrollTo({top:0,behavior:'smooth'});$('t').focus()}
function endEdit(clear){if(clear&&editing)$('t').value='';editing=null;$('t').classList.remove('editing');
 $('editbar').hidden=true;$('sendBtn').textContent='CPZへ送る'}
function cp(id){const s=items.find(i=>i.id===id).text;
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(s).then(()=>toast('コピーしました'))}
 else{const ta=document.createElement('textarea');ta.value=s;ta.style.position='fixed';ta.style.opacity='0';
 document.body.appendChild(ta);ta.focus();ta.select();ta.setSelectionRange(0,s.length);
 try{document.execCommand('copy');toast('コピーしました')}catch(e){toast('コピー失敗')}ta.remove()}}

// ---- 撮影 → 範囲選択 → 本体でOCR
const MAXSRC=3000,MAXFULL=2000;let lastOcr=false;
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
  if(editing)endEdit(false);const t=$('t');t.value=t.value.trim()?t.value.replace(/\n*$/,'\n')+j.text:j.text;
  lastOcr=true;closeCrop();t.scrollIntoView({behavior:'smooth',block:'center'});
  toast(`読み取りました (${j.elapsed.toFixed(1)}秒) 直して「CPZへ送る」`,2800)}
 catch(e){toast('読み取り失敗: '+e,3000)}finally{b.disabled=false;b.textContent='読み取る'}}

(async()=>{const m=location.hash.match(/pin=(\d{6})/);
 if(m){history.replaceState(null,'',location.pathname);$('pin').value=m[1];await login();if($('app').hidden)show(false);return}
 try{await api('/api/ping');show(true);load()}catch(e){}})();
setInterval(()=>{if(!$('app').hidden&&!document.hidden)poll()},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!$('app').hidden)poll()});
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
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("json must be an object")
        return data

    # routes
    def _do_GET(self):
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
            return self.send_json({"ok": True, "version": VERSION, "rev": self.app.store.rev})
        if u.path == "/api/items":
            q = parse_qs(u.query)
            limit = max(1, min(int(q.get("limit", ["50"])[0]), MAX_ITEMS))
            after = max(0, int(q.get("after", ["0"])[0]))
            return self.send_json({"items": self.app.store.list(limit, after),
                                   "rev": self.app.store.rev})
        if u.path.startswith("/api/items/"):
            it = self.app.store.get(int(u.path.rsplit("/", 1)[1]))
            return self.send_json(it) if it else self.send_json({"error": "not found"}, 404)
        if u.path == "/api/pin" and self.is_local():
            return self.send_json({"pin": self.app.pin, "url": f"http://{lan_ip()}:{self.server.server_port}/"})
        self.send_json({"error": "not found"}, 404)

    def _do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/ocr":
            return self.post_ocr(u)
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
        if u.path.startswith("/api/items/") and u.path.endswith("/top"):
            new = self.app.store.to_top(int(u.path.split("/")[3]))
            return self.send_json({"id": new}) if new else self.send_json({"error": "not found"}, 404)
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
            text, dt = ocr_image(data, lang)
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
        return self.send_json({"text": text, "elapsed": dt})

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
            return self.send_json({"ok": ok}, 200 if ok else 404)
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
    Handler.app = App(store, pin)
    srv = DualStackServer((a.bind, a.port), Handler)
    print(f"HEXA CLIP {VERSION}  http://{lan_ip()}:{a.port}/  PIN={pin}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
