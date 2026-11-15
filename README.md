# HEXA CLIP

M5Stack **CardputerZero** 用の共有クリップボード + カメラOCR。

- スマホ / PC のブラウザから CardputerZero へテキストやコードを送る
- CardputerZero からスマホへ送り返してコピー
- 本体カメラで文字を読み取り (Tesseract)、本体キーボードで修正してクリップへ送る
- **スマホで撮影 → 本体で読み取り → スマホで直して送る** (本体カメラより高画素・AF付きで小さい文字に強い)
- 履歴をスマホで編集 (本体に日本語IMEが無くても日本語を直せる)
- **ファイルを名前付きで送る** (コード・PDF・画像など) → 本体の `~/clip/` に保存。本体→スマホ/PCへも
- **PC用コマンド `hexaclip`** (Windows / Mac / Linux): ファイル送信、PCのクリップボードとの受け渡し

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
| `czclip` | CLI (`paste` / `push` / `send` / `list` / `get` / `rm` / `top` / `pin` / `qr` / `token` / `logout-all`) |
| `pc/hexaclip.py` | PC用コマンド (標準ライブラリのみ)。本体の `http://<CPZ>:8080/hexaclip.py` からもダウンロードできる |
| `ocr_cam.py` | 本体アプリ (クリップ / 履歴 / QR / 設定) |
| `czpasted.py` | fn+V で最新のクリップをキー入力として貼り付ける常駐プログラム |
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
czclip send log.txt        # ファイルとして送る (スマホ/PCの履歴からDL)
czclip list                # 履歴
czclip paste 3             # 3番目に新しいもの
czclip top 12              # #12 を最新に移す (fn+V の対象にする)
czclip find wifi           # 履歴を検索
czclip star 12             # ピン留め★ (履歴が500件を超えても消えない)。unstar で外す
czclip pin                 # 接続URLとPINを表示
czclip token               # 共有シート/ショートカット用のトークンと送り方を表示
```

### スマホの画面

| 操作 | 内容 |
|---|---|
| **撮影して文字を読む** | スマホのカメラで撮る → 読みたい所を指でなぞって囲む → `読み取る`。OCRは本体 (Tesseract) で行い、結果が入力欄に入る。直して `CPZへ送る` |
| 言語 | 日本語 / 日+英 / 英語・コード (選択は記憶) |
| `回転` / `撮り直し` / `範囲を選び直す` | 横向きの写真や、読み取りがいまいちな時に |
| `編集` | 履歴の内容を入力欄で直して `保存` (位置は変わらない) |
| `貼付用` | その項目を一番上へ移す → 本体の `fn+V` で打ち込まれる |
| `コピー` / `削除` | スマホのクリップボードへ / 削除 |

- 本体側の追加・削除・並び替えは2秒以内にスマホへ反映されます
- 範囲を囲まずに読むと、写真全体 (長辺2000pxに縮小) を読みます。囲んだ方が速く正確です
- コードの字下げは文字の位置から復元し、“ ” ‘ ’ は " ' にそろえます (fn+V で打てる文字に)

### 本体アプリ (ホーム画面の HEXA CLIP)

```bash
hexaclip-ocr               # ホーム画面のアイコンと同じ
```

上のタブで4つのモードを切り替えます。画面下に、その画面で使えるキーが常に出ます。

| モード | 内容 | キー |
|---|---|---|
| 1 クリップ | カメラで読み取り → 修正 → 送る | `ENTER` 文字を読取 (連写して一番シャープな1枚を使用) / `Q` QR・バーコードを読取 / `↑↓` ズーム x1〜x3。枠の色とFOCUSバーでピント確認 (緑=良) |
| 2 履歴 | 受信・送信した内容の一覧 (`fn+V` 印が貼り付け対象、★=ピン留め) | `↑↓` 選ぶ / `V` 貼付用にする / `P` ピン留め / `/` 検索 (ESCで解除) / `ENTER` 全文表示 → `E` 編集 `S` 保存 `R` 実行 `Q` QRで表示 `P` ピン留め `D` 削除 |
| 3 QR | スマホ接続用QR (読むだけでログイン) | SSHからは `czclip qr` |
| 4 設定 | OCR言語 (日本語/日+英/英語/コード) / 高精細撮影 / 連写枚数 / 自信のない文字 / 起動時ズーム / 受信したら表示 / fn+V の動作 | `↑↓` 選ぶ / `←→` 変える (すぐ保存) |

共通の操作:

- `TAB` 次のモード、`1`〜`4` で直接選択、`ESC` 戻る (一番上の画面では確認ウィンドウ → `ENTER` で終了)
- 方向キーは本体の流儀と同じ **F(↑) X(↓) Z(←) C(→)**。文字を打つ編集画面では `fn` を押しながら
- 編集画面: 英数字で挿入 / `BS` 削除 / `TAB` 改行 / `ENTER` 送る / `ESC` を2回で破棄
- 読み取りに自信のない文字は橙色+下線で残ります (直すと色が消える)。設定で「消す」にもできます
- 言語「コード」は、字下げに加えて単語間の空白の数も復元します
- スマホなどから届くと「受信」と通知。設定で本文を自動表示にもできます

メモ:

- カメラ (IMX219) は固定焦点。30〜40cm 程度離し、小さい文字やPC画面はズームで読みます
- 撮影は全画素 (3280x2464)。暗い背景 (ダークモードの画面) は自動で白黒反転します
- 本体に日本語IMEは無いため、日本語の修正は削除のみ (挿入は英数字・記号)
- クリップ以外のモードではカメラを止めて電池を節約します
- 起動中はランチャー(APPLaunch)を一時停止し、自動ロック/スリープを防ぎます (`--no-pause` で無効)。
  万一ランチャーが固まったままになったら `sudo pkill -CONT -f APPLaunch`
- キーが効かないとき: `hexaclip-ocr --debug-keys` でコードを確認、`--keys keys.json` で上書き

### fn+V で貼り付け (実験的)

本体のどのアプリでも、**fn を押しながら V** で、HEXA CLIP の最新 (一番上) の内容をキー入力として打ち込みます。
古い項目を貼りたいときは、履歴で選んで `V` (またはSSHで `czclip top ID`) を押して一番上に移します
(`czpasted.py` / `hexaclip-paste.service`)。

- 打てるのは英数字と記号だけ。日本語は飛ばします
- 改行は ENTER として打たれます (CLIでは1行ずつ実行される)。末尾の改行は打ちません。
  設定「fn+Vの改行」を「打たない」にすると、空白でつないで1行にまとめます
- 履歴で `V` を押したとき、打てない文字 (日本語など) があれば件数を表示します
- 打ち込み中に `ESC` で中止。最大4000文字
- 本体は fn+V で `V` が入力されるため、貼り付け前に1文字消します (設定「fn+Vで入るVを消す」で OFF にできます)
- 止める: `sudo systemctl disable --now hexaclip-paste`

### ファイルを送る

- スマホ: `ファイルを送る` で選ぶ (複数可)。PCのブラウザなら画面にドラッグ&ドロップ
- 貼り付けた文字に `ファイル名` を付けて送ると、その名前で保存
- 本体の `~/clip/<名前>` に保存。同じ名前があれば1つ前を `~/clip/.prev/` に残します
- UTF-8のテキスト (256KB以下) は履歴に本文も入り、本体で表示・編集・fn+V できます。
  Windowsの改行 (CRLF) とBOMは自動で直します
- PDF・画像などは履歴の `DL` でダウンロード (本体ではファイルの場所を表示)。上限100MB
- 履歴を削除しても `~/clip/` のファイルは残ります

### PCから使う (hexaclip コマンド)

Python 3 があれば Windows / Mac / Linux で使えます。スマホ画面の一番下「PCから使う」に、
ダウンロードリンクと設定コマンド (トークン入り) が出ます。

```bash
# 本体から取ってくる
curl -O http://<CPZ>:8080/hexaclip.py
# 最初に1回だけ (トークンは czclip token か、スマホ画面の「PCから使う」)
python hexaclip.py setup http://<CPZ>:8080 <TOKEN>

python hexaclip.py send main.py 資料.pdf   # ファイルを送る
git diff | python hexaclip.py push         # 標準入力を送る
python hexaclip.py push --clip             # PCのクリップボードを送る
python hexaclip.py paste --clip            # 最新をPCのクリップボードへ
python hexaclip.py paste > memo.txt        # 最新をファイルへ (UTF-8)
python hexaclip.py get                     # 最新のファイルを保存 (同名は (1) を付けて上書きしない)
python hexaclip.py get 12 -o downloads     # ID指定
python hexaclip.py list
python hexaclip.py find main.py            # 履歴を検索
```

- Windows のクリップボードは PowerShell 経由 (日本語も文字化けしない)。Mac は pbcopy/pbpaste、
  Linux は wl-clipboard か xclip
- 設定の保存先: Windows `%APPDATA%\hexaclip\config.json` / その他 `~/.config/hexaclip/pc.json`

### 受け取ったコードを本体で実行

履歴の全文表示で `R` → 確認画面 (`TAB` で python3 / sh を切り替え) → `ENTER` で実行します。

- 名前付きは `~/clip/<名前>`、名前なしは `~/clip/run_<ID>.py` (.sh) に保存してから実行。作業フォルダは `~/clip/`
- 出力は画面に流れて表示 (`↑↓` でスクロール)。`ESC` で停止 (子プロセスごと)
- 終わったら `S` で出力を HEXA CLIP へ送れます (スマホ/PCで結果を見る)
- 入力 (`input()` など) は使えません。アプリが sudo で動いている場合も、コードは元のユーザーの権限で動きます

### QRで渡す / 読む (Wi-Fiなし)

- **本体 → スマホ**: 全文表示で `Q`。画面にQRを出すので、スマホのカメラで読めます (700バイト程度まで)
- **紙・画面 → 本体**: クリップモードで `Q`。QRやバーコードを本体カメラで読みます。OCRと違って誤読がありません
  (`zbar-tools` を使用。install.sh で導入)

### ピン留め・検索

- よく使うコマンドや設定を ★ ピン留め。スマホ画面では上に「ピン留め」欄として出ます。
  履歴が500件を超えて古いものが消えても、ピン留めは残ります
- 検索は本文とファイル名が対象。空白で区切ると全部を含むものだけ (AND)

### 共有シート / ショートカットから送る

ブラウザを開かずに送受信できます。`czclip token` で表示される内容を、iOSのショートカット
(「URLの内容を取得」) や Android の HTTP 送信アプリに設定します。

```
送る:      POST http://<CPZ>:8080/api/items     本文=送りたい文字
           Authorization: Bearer <TOKEN> / Content-Type: text/plain
受け取る:  GET  http://<CPZ>:8080/api/latest    (同じ Authorization)
```

avahi が動いていれば `http://<ホスト名>.local:8080/` でも届きます (QR画面と `czclip pin` に表示)。

## セキュリティ

- Web画面は同一LAN向け (HTTP)。6桁PIN、5回失敗で60秒ロック
- トークン (`czclip token`) を知っている端末は読み書きできます。漏れたら `czclip token reset`
- `hexaclip-paste` は一般ユーザー (input グループ) で動きます
- `127.0.0.1` からのアクセスは認証不要 (CLI / OCR用)
- PIN再発行と全端末ログアウト: `python3 ~/hexaclip/czclipd.py --reset-pin` → `sudo systemctl restart hexaclip`

## 動作環境

- M5Stack CardputerZero (Raspberry Pi CM0 / Debian aarch64)
- Python 3、Picamera2、Pillow、numpy、Tesseract (`tesseract-ocr-jpn`)
