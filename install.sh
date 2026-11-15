#!/bin/sh
# HEXA CLIP installer for CardputerZero
# 使い方: cd ~/hexaclip && sh install.sh   (sudo は付けずに実行)
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
U=$(id -un)
if [ "$U" = "root" ]; then
  echo "sudo を付けずに実行してください (sh install.sh)"; exit 1
fi

echo "== packages"
sudo apt-get install -y python3-pil tesseract-ocr tesseract-ocr-jpn tesseract-ocr-eng >/dev/null

echo "== files"
chmod +x "$DIR/czclip" "$DIR/czclipd.py" "$DIR/ocr_cam.py"
sudo ln -sf "$DIR/czclip" /usr/local/bin/czclip

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
sleep 1.5

echo "== done"
czclip pin
echo "スマホのブラウザで上のURLを開いて PIN を入力してください"
