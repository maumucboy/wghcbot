#!/bin/bash
# Deploy bot trên chính VPS chạy AmneziaWG (chạy với quyền root).
# Yêu cầu: đã có /root/awgbot/bot.env (copy từ .env.example rồi điền token/pass).
set -e
DIR=/root/awgbot
if [ ! -f "$DIR/bot.env" ]; then
  echo "Thiếu $DIR/bot.env — tạo từ .env.example rồi điền token, mật khẩu."
  exit 1
fi
mkdir -p "$DIR"
cp awg.py db.py bot.py "$DIR"/
# config mới dùng tên miền thay vì IP trần
DOMAIN=$(grep -E '^AWG_ENDPOINT_DOMAIN=' "$DIR/bot.env" | cut -d= -f2)
if [ -n "$DOMAIN" ] && grep -q "^export AWG_ENDPOINT=" /root/awg/awgsetup_cfg.init 2>/dev/null; then
  sed -i "s|^export AWG_ENDPOINT=.*|export AWG_ENDPOINT='$DOMAIN'|" /root/awg/awgsetup_cfg.init
fi
apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -U pip
"$DIR/venv/bin/pip" install -q -r requirements.txt
cat > /etc/systemd/system/awgbot.service <<SVC
[Unit]
Description=AWG Telegram Bot
After=network-online.target awg-quick@awg0.service
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$DIR/bot.env
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/bot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload
systemctl enable --now awgbot.service
sleep 2
systemctl status awgbot.service --no-pager | head -10
