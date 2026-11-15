#!/usr/bin/env python3
"""hexaclip — PC から HEXA CLIP (CardputerZero) へコードや資料を送る / 受け取る

Python 3.8+ の標準ライブラリだけで動く (Windows / Mac / Linux)。

最初に1回だけ (接続先とトークンを保存。トークンは本体で `czclip token`、
またはスマホ画面の「PCから使う」に出る):
  python hexaclip.py setup http://192.168.x.x:8080 TOKEN

使い方:
  hexaclip send FILE...        ファイルを名前付きで送る (本体の ~/clip/ に保存)
  hexaclip push [TEXT...]      テキストを送る (省略時は標準入力)   例: git diff | hexaclip push
  hexaclip push --clip         PCのクリップボードの内容を送る
  hexaclip paste [N]           最新 (またはN番目) を出力           例: hexaclip paste > memo.txt
  hexaclip paste --clip [N]    最新をPCのクリップボードへ
  hexaclip get [ID] [-o DIR]   ファイルとして保存 (省略時は最新。名前付きはその名前で)
  hexaclip list [N]            履歴の一覧
  hexaclip info                接続先と状態を表示

環境変数 HEXACLIP_URL / HEXACLIP_TOKEN があれば設定ファイルより優先。
"""
import base64
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, unquote

VERSION = "0.1"
SYSTEM = platform.system()


# ------------------------------------------------------------------ config
def config_path():
    if SYSTEM == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "hexaclip", "config.json")
    return os.path.join(os.path.expanduser("~"), ".config", "hexaclip", "pc.json")


def load_config():
    cfg = {}
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    if os.environ.get("HEXACLIP_URL"):
        cfg["url"] = os.environ["HEXACLIP_URL"]
    if os.environ.get("HEXACLIP_TOKEN"):
        cfg["token"] = os.environ["HEXACLIP_TOKEN"]
    if not cfg.get("url") or not cfg.get("token"):
        die("接続先が未設定です。最初に:\n  python hexaclip.py setup http://<CPZのIP>:8080 <TOKEN>\n"
            "(TOKEN は本体で czclip token、またはスマホ画面の「PCから使う」に表示)")
    cfg["url"] = cfg["url"].rstrip("/")
    return cfg


def die(msg, code=1):
    print("hexaclip: " + msg, file=sys.stderr)
    sys.exit(code)


def info(msg):
    print(msg, file=sys.stderr)


# ------------------------------------------------------------------ http
def call(cfg, method, path, body=None, ctype="application/json", raw=False, timeout=30):
    data = body
    if body is not None and ctype == "application/json":
        data = json.dumps(body).encode()
    headers = {"Authorization": "Bearer " + cfg["token"]}
    if data is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(cfg["url"] + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read()
            if raw:
                return content, r.headers
            return json.loads(content or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "")
        except Exception:
            msg = ""
        if e.code == 401:
            die("トークンが違います (本体で czclip token を確認し、setup し直してください)")
        die(f"エラー {e.code} {msg}".strip())
    except urllib.error.URLError as e:
        die(f"{cfg['url']} に接続できません ({e.reason})\n"
            "  同じWi-Fiか / 本体の電源 / IPが変わっていないか を確認してください")


# ------------------------------------------------------------------ PC clipboard
def clip_get():
    """PCのクリップボード → 文字列"""
    if SYSTEM == "Windows":
        # 文字化けしないよう UTF-8 → base64 で受け取る
        ps = ("$t = Get-Clipboard -Raw; if ($t -eq $null) { $t = '' }; "
              "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t))")
        out = run_clip(["powershell", "-NoProfile", "-Command", ps])
        return base64.b64decode(out.strip() or b"").decode("utf-8")
    if SYSTEM == "Darwin":
        return run_clip(["pbpaste"], env={"LANG": "en_US.UTF-8"}).decode("utf-8", "replace")
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        if shutil.which(cmd[0]):
            return run_clip(cmd).decode("utf-8", "replace")
    die("クリップボードを読めません (wl-clipboard か xclip を入れてください)")


def clip_set(text):
    """文字列 → PCのクリップボード"""
    data = text.encode("utf-8")
    if SYSTEM == "Windows":
        ps = ("$b = [Console]::In.ReadToEnd(); "
              "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)))")
        run_clip(["powershell", "-NoProfile", "-Command", ps], base64.b64encode(data))
        return
    if SYSTEM == "Darwin":
        run_clip(["pbcopy"], data, env={"LANG": "en_US.UTF-8"})
        return
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard", "-i"],
                ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            run_clip(cmd, data)
            return
    die("クリップボードに書けません (wl-clipboard か xclip を入れてください)")


def run_clip(cmd, data=None, env=None):
    e = dict(os.environ, **(env or {}))
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, env=e, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as ex:
        die(f"{cmd[0]} を実行できません ({ex})")
    if r.returncode != 0:
        die(f"{cmd[0]} が失敗しました: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


# ------------------------------------------------------------------ output
def write_text(text):
    """端末なら表示、リダイレクト/パイプなら UTF-8 のバイト列で (Windows で cp932 にしない)"""
    if sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(errors="replace")
        except AttributeError:
            pass
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def read_stdin():
    raw = sys.stdin.buffer.read()
    for enc in ("utf-8-sig", "cp932" if SYSTEM == "Windows" else "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def human(n):
    n = n or 0
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB" if n < 1048576 else f"{n / 1048576:.1f}MB"


def one_line(s, n=60):
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[:n - 1] + "…"


def unique_path(path):
    """同名があれば name (1).ext のようにずらす (PC側のファイルを上書きしない)"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


# ------------------------------------------------------------------ commands
def cmd_setup(args):
    if len(args) < 2:
        die("使い方: python hexaclip.py setup http://<CPZのIP>:8080 <TOKEN>")
    url, token = args[0].rstrip("/"), args[1].strip()
    if not url.startswith("http"):
        url = "http://" + url
    cfg = {"url": url, "token": token}
    r = call(cfg, "GET", "/api/ping", timeout=5)
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    info(f"OK: HEXA CLIP {r.get('version', '?')} に接続しました\n設定: {path}")


def cmd_send(cfg, args):
    if not args:
        die("送るファイルを指定してください (例: hexaclip send main.py)")
    for path in args:
        if not os.path.isfile(path):
            die(f"{path}: ファイルがありません")
        with open(path, "rb") as f:
            data = f.read()
        name = os.path.basename(path)
        r = call(cfg, "POST", f"/api/files?source=pc&name={quote(name)}", data,
                 ctype="application/octet-stream", timeout=300)
        kind = "ファイル" if r.get("binary") else "テキスト"
        info(f"sent #{r['id']} {r['name']} ({human(r['size'])}, {kind}) → 本体 ~/clip/{r['name']}")


def cmd_push(cfg, args):
    if args[:1] == ["--clip"]:
        text = clip_get()
        if not text.strip():
            die("PCのクリップボードが空です")
    elif args:
        text = " ".join(args)
    else:
        if sys.stdin.isatty():
            info("送る内容を入力して、最後に Ctrl+D (Windowsは Ctrl+Z → Enter)")
        text = read_stdin()
    if not text.strip():
        die("空なので送りません")
    r = call(cfg, "POST", "/api/items", {"text": text, "source": "pc"})
    info(f"pushed #{r['id']} ({len(text)}文字)")


def items(cfg, n):
    return call(cfg, "GET", f"/api/items?limit={max(1, n)}")["items"]


def cmd_paste(cfg, args):
    to_clip = "--clip" in args
    args = [a for a in args if a != "--clip"]
    n = int(args[0]) if args else 1
    its = [i for i in items(cfg, n + 20) if not i.get("binary")]
    if len(its) < n:
        die("テキストの履歴がありません")
    text = its[n - 1]["text"]
    if to_clip:
        clip_set(text)
        info(f"#{its[n - 1]['id']} をPCのクリップボードにコピーしました ({len(text)}文字)")
    else:
        write_text(text)


def cmd_get(cfg, args):
    out = "."
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            die("-o の後に保存先フォルダを指定してください")
        out = args[i + 1]
        args = args[:i] + args[i + 2:]
    if args:
        item_id = int(args[0].lstrip("#"))
    else:                                     # 最新の「名前付き」を優先、なければ最新
        its = items(cfg, 20)
        if not its:
            die("履歴がありません")
        named = [i for i in its if i.get("name")]
        item_id = (named or its)[0]["id"]
    body, headers = call(cfg, "GET", f"/api/files/{item_id}", raw=True, timeout=300)
    name = None
    disp = headers.get("Content-Disposition", "")
    if "filename*=UTF-8''" in disp:
        name = unquote(disp.split("filename*=UTF-8''", 1)[1].split(";")[0])
    name = os.path.basename(name or f"clip_{item_id}.txt")
    os.makedirs(out, exist_ok=True)
    path = unique_path(os.path.join(out, name))
    with open(path, "wb") as f:
        f.write(body)
    info(f"saved #{item_id} → {path} ({human(len(body))})")


def cmd_list(cfg, args):
    n = int(args[0]) if args else 10
    for it in items(cfg, n):
        if it.get("binary"):
            body = f"({human(it.get('size'))})"
        else:
            body = one_line(it["text"])
        name = f"{it['name']}: " if it.get("name") else ""
        print(f"#{it['id']:<5} [{it['source']:<4}] {one_line(name + body, 70)}")


def cmd_info(cfg):
    r = call(cfg, "GET", "/api/ping", timeout=5)
    print(f"接続先: {cfg['url']}\n本体:   HEXA CLIP {r.get('version', '?')}  (OK)\n"
          f"設定:   {config_path()}\nPC:     {SYSTEM} / Python {platform.python_version()}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "setup":
        return cmd_setup(args)
    if cmd == "version":
        print(VERSION)
        return
    cmds = {"send": cmd_send, "push": cmd_push, "paste": cmd_paste, "get": cmd_get,
            "list": cmd_list, "info": lambda cfg, _: cmd_info(cfg)}
    if cmd not in cmds:
        die(f"知らないコマンドです: {cmd} (hexaclip help)")
    cfg = load_config()
    try:
        cmds[cmd](cfg, args)
    except ValueError as e:
        if "invalid literal for int" in str(e):
            die("番号の指定が正しくありません")
        raise
    except KeyboardInterrupt:
        die("中止しました", 130)


if __name__ == "__main__":
    main(sys.argv[1:])
