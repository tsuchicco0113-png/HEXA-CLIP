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
| `ocr_cam.py` | 本体アプリ (クリップ / 履歴 / QR / 設定) |
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

### 本体アプリ (ホーム画面の HEXA CLIP)

```bash
hexaclip-ocr               # ホーム画面のアイコンと同じ
```

上のタブで4つのモードを切り替えます。画面下に、その画面で使えるキーが常に出ます。

| モード | 内容 | キー |
|---|---|---|
| 1 クリップ | カメラで読み取り → 修正 → 送る | `ENTER` 読取 / `↑↓` ズーム x1〜x3。枠の色とFOCUSバーでピント確認 (緑=良) |
| 2 履歴 | 受信・送信した内容の一覧 | `↑↓` 選ぶ / `ENTER` 全文表示 → `E` 編集 `S` ファイル保存 (`~/clip/`) `D` 削除 |
| 3 QR | スマホ接続用QR (読むだけでログイン) | SSHからは `czclip qr` |
| 4 設定 | OCR言語 / 高精細撮影 / 起動時ズーム / 受信したら表示 | `↑↓` 選ぶ / `←→` 変える (すぐ保存) |

共通の操作:

- `TAB` 次のモード、`1`〜`4` で直接選択、`ESC` 戻る (一番上の画面では終了)
- 方向キーは本体の流儀と同じ **F(↑) X(↓) Z(←) C(→)**。文字を打つ編集画面では `fn` を押しながら
- 編集画面: 英数字で挿入 / `BS` 削除 / `TAB` 改行 / `ENTER` 送る / `ESC` 破棄
- スマホなどから届くと「受信」と通知。設定で本文を自動表示にもできます

メモ:

- カメラ (IMX219) は固定焦点。30〜40cm 程度離し、小さい文字やPC画面はズームで読みます
- 撮影は全画素 (3280x2464)。暗い背景 (ダークモードの画面) は自動で白黒反転します
- 本体に日本語IMEは無いため、日本語の修正は削除のみ (挿入は英数字・記号)
- クリップ以外のモードではカメラを止めて電池を節約します
- 起動中はランチャー(APPLaunch)を一時停止し、自動ロック/スリープを防ぎます (`--no-pause` で無効)。
  万一ランチャーが固まったままになったら `sudo pkill -CONT -f APPLaunch`
- キーが効かないとき: `hexaclip-ocr --debug-keys` でコードを確認、`--keys keys.json` で上書き

## セキュリティ

- Web画面は同一LAN向け (HTTP)。6桁PIN、5回失敗で60秒ロック
- `127.0.0.1` からのアクセスは認証不要 (CLI / OCR用)
- PIN再発行と全端末ログアウト: `python3 ~/hexaclip/czclipd.py --reset-pin` → `sudo systemctl restart hexaclip`

## 動作環境

- M5Stack CardputerZero (Raspberry Pi CM0 / Debian aarch64)
- Python 3、Picamera2、Pillow、numpy、Tesseract (`tesseract-ocr-jpn`)
