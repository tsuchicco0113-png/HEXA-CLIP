#!/bin/sh
# HEXA CLIP installer for CardputerZero
# 使い方: cd ~/hexaclip && sh install.sh   (sudo は付けずに実行)
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
U=$(id -un)
if [ "$U" = "root" ]; then
  echo "sudo を付けずに実行してください (sh install.sh)"; exit 1
fi
APPL=/usr/share/APPLaunch

echo "== packages"
sudo apt-get install -y python3-pil python3-numpy python3-qrcode tesseract-ocr tesseract-ocr-jpn tesseract-ocr-eng zbar-tools >/dev/null

echo "== files"
chmod +x "$DIR/czclip" "$DIR/czclipd.py" "$DIR/czpasted.py" "$DIR/ocr_cam.py" "$DIR/hexaclip-ocr" "$DIR/pc/hexaclip.py"
sudo ln -sf "$DIR/pc/hexaclip.py" /usr/local/bin/hexaclip      # PCと同じコマンド
sudo ln -sf "$DIR/czclip" /usr/local/bin/czclip                # 旧コマンド (互換用)
sudo ln -sf "$DIR/hexaclip-ocr" /usr/local/bin/hexaclip-ocr

echo "== 日本語フォント (画面の字が□にならないか確認)"
if ! python3 "$DIR/ocr_cam.py" --check-font; then
  echo "   日本語の字がそろったフォントが無いので fonts-noto-cjk を入れます (少し時間がかかります)"
  sudo apt-get install -y fonts-noto-cjk >/dev/null
  python3 "$DIR/ocr_cam.py" --check-font || echo "   ※ それでも見つかりません。表示が□になる場合はお知らせください"
fi

echo "== permissions (画面とキーボードを sudo なしで使う)"
NEED_REBOOT=0
for g in video input; do
  if ! id -nG "$U" | tr ' ' '\n' | grep -qx "$g"; then
    sudo usermod -aG "$g" "$U"
    NEED_REBOOT=1
  fi
done

echo "== service (user: $U)"
sudo tee /etc/systemd/system/hexaclip.service >/dev/null <<EOF
[Unit]
Description=HEXA CLIP daemon
After=network-online.target
Wants=network-online.target

[Service]
User=$U
ExecStart=/usr/bin/python3 $DIR/czclipd.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now hexaclip
sudo systemctl restart hexaclip

echo "== paste service (fn+V で貼り付け)"
# まず一般ユーザー(inputグループ)で動かす。キーボードを開けない場合だけ root に戻す
write_paste_unit() {   # $1 = "user" または "root"
  if [ "$1" = "user" ]; then
    RUNAS="User=$U
SupplementaryGroups=input video"
  else
    RUNAS=""
  fi
  sudo tee /etc/systemd/system/hexaclip-paste.service >/dev/null <<EOF
[Unit]
Description=HEXA CLIP paste hotkey (fn+V)
After=hexaclip.service

[Service]
$RUNAS
ExecStart=/usr/bin/python3 $DIR/czpasted.py --settings /home/$U/.config/hexaclip/settings.json
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable hexaclip-paste >/dev/null 2>&1
  sudo systemctl restart hexaclip-paste
}
write_paste_unit user
sleep 2
if ! systemctl is-active --quiet hexaclip-paste; then
  echo "   (一般ユーザーではキーボードを開けないため root で動かします)"
  write_paste_unit root
fi

if [ -d "$APPL/applications" ]; then
  echo "== launcher (APPLaunch)"
  # 内蔵Cameraアプリのアイコンと同じサイズで作る
  REF="$APPL/share/images/camera1.png"
  [ -f "$REF" ] || REF=64
  python3 "$DIR/make_icon.py" /tmp/hexaclip.png "$REF"
  sudo install -m 644 /tmp/hexaclip.png "$APPL/share/images/hexaclip.png"
  sudo tee "$APPL/applications/hexaclip.desktop" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=HEXA CLIP
Comment=Camera OCR + shared clipboard
Exec=/usr/local/bin/hexaclip-ocr
Icon=share/images/hexaclip.png
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
  NEED_REBOOT=1
fi

sleep 1.5
echo "== done"
hexaclip pin
echo "スマホのブラウザで上のURLを開いて PIN を入力してください"
if [ "$NEED_REBOOT" = 1 ]; then
  echo ""
  echo "*** ホーム画面へのアイコン追加と権限の反映のため、再起動してください: sudo reboot"
fi
