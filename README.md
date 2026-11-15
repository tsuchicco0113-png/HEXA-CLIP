# HEXA CLIP

M5Stack **CardputerZero** 用の共有クリップボード + カメラOCR。

- スマホ / PC のブラウザから CardputerZero へテキストやコードを送る
- CardputerZero からスマホへ送り返してコピー
- 本体カメラで文字を読み取り (Tesseract)、本体キーボードで修正してクリップへ送る

```
 入力                     中核                        出力
 カメラOCR ──┐        ┌─────────────┐        ┌─ czclip paste (CLI / Python)
 スマホ(Web) ─┼──────→ │  czclipd     │ ──────┼─ ファイル保存
 PC (SSH)  ──┘        │  SQLite履歴  │        └─ スマホ(Web)でコピー
                       └─────────────┘
```

## 構成

| ファイル | 役割 |
|---|---|
| `czclipd.py` | 常駐サーバー。SQLite に履歴保存、`:8080` でスマホ用Web画面 (PIN認証)。標準ライブラリのみ |
| `czclip` | CLI (`paste` / `push` / `list` / `get` / `rm` / `pin` / `logout-all`) |
| `ocr_cam.py` | カメラOCRアプリ。プレビュー → OCR → 編集 → クリップへ送信 |
| `hexaclip-ocr` | OCRアプリの起動スクリプト (ランチャーから起動時は `~/.local/share/hexaclip/ocr.log` にログ) |
| `make_icon.py` | ランチャー用アイコン生成 |
| `install.sh` | 依存パッケージ導入、`czclip` / `hexaclip-ocr` コマンド、systemd `hexaclip.service`、ホーム画面への登録 |

## インストール (CardputerZero)

```bash
git clone https://github.com/tsuchicco0113-png/HEXA-CLIP.git ~/hexaclip
cd ~/hexaclip && sh install.sh      # sudo は付けない
```

最後に表示される URL を同じWi-Fiのスマホで開き、PIN を入力します (ログインは30日間有効)。
初回は `sudo reboot` で再起動すると、ホーム画面に **HEXA CLIP** アイコンが追加されます。

## 使い方

```bash
czclip paste > main.py     # スマホから送ったコードを保存
czclip paste | python3     # そのまま実行
czclip push < log.txt      # スマホへ送る
czclip list                # 履歴
czclip pin                 # 接続URLとPINを表示
```

### カメラOCR

```bash
hexaclip-ocr               # ホーム画面の HEXA CLIP アイコンと同じ
hexaclip-ocr -l jpn+eng
```

| 画面 | キー |
|---|---|
| プレビュー | `ENTER` 読み取り / `Z` ズーム切替 (x1/x2) / `Q` スマホ接続QR / `ESC` 終了。枠の色とFOCUSバーでピント確認 (緑=良) |
| 編集 | `←→↑↓` カーソル / `BS` 削除 / 英数字で挿入 / `TAB` 改行 / `ENTER` 送る / `ESC` 破棄 |

- カメラ (IMX219) は固定焦点。30〜40cm 程度離して撮るとピントが合います
- 撮影は全画素 (3280x2464) で行います。小さい文字やPC画面は、離れたまま `Z` で x2 にして読みます
- 暗い背景 (ダークモードの画面) は自動で白黒反転します
- `Q` で接続用QRを表示。スマホで読むだけでログインできます (SSHからは `czclip qr`)
- スマホなどから届くと、プレビューに「受信」と通知が出ます
- 本体に日本語IMEは無いため、日本語の修正は削除のみ (挿入は英数字・記号)
- キー割り当ての確認: `--debug-keys`、上書き: `--keys keys.json`
- 起動中はランチャー(APPLaunch)を一時停止し、自動ロック/スリープと画面の取り合いを防ぎます (`--no-pause` で無効)。
  万一ランチャーが固まったままになったら `sudo pkill -CONT -f APPLaunch`

## セキュリティ

- Web画面は同一LAN向け (HTTP)。6桁PIN、5回失敗で60秒ロック
- `127.0.0.1` からのアクセスは認証不要 (CLI / OCR用)
- PIN再発行と全端末ログアウト: `python3 ~/hexaclip/czclipd.py --reset-pin` → `sudo systemctl restart hexaclip`

## 動作環境

- M5Stack CardputerZero (Raspberry Pi CM0 / Debian aarch64)
- Python 3、Picamera2、Pillow、numpy、Tesseract (`tesseract-ocr-jpn`)
