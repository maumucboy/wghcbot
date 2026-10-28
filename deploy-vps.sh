#!/bin/bash
# Deploy bot + Mini App trên chính VPS chạy AmneziaWG (chạy với quyền root).
# Yêu cầu: đã có /root/awgbot/bot.env (copy từ .env.example rồi điền token/pass).
set -e
DIR=/root/awgbot          # giữ env, db, venv, cert
APP=/root/wghcbot         # mã nguồn (git clone) + index.html

if [ ! -f "$DIR/bot.env" ]; then
  echo "Thiếu $DIR/bot.env — tạo từ .env.example rồi điền token, mật khẩu."; exit 1
fi
mkdir -p "$DIR"

# config mới dùng tên miền thay vì IP trần
DOMAIN=$(grep -E '^AWG_ENDPOINT_DOMAIN=' "$DIR/bot.env" | cut -d= -f2)
if [ -n "$DOMAIN" ] && grep -q "^export AWG_ENDPOINT=" /root/awg/awgsetup_cfg.init 2>/dev/null; then
  sed -i "s|^export AWG_ENDPOINT=.*|export AWG_ENDPOINT='$DOMAIN'|" /root/awg/awgsetup_cfg.init
fi

# mở TCP 443 cho panel (UFW của installer chỉ mở UDP 443)
command -v ufw >/dev/null && ufw status | grep -q active && ufw allow 443/tcp >/dev/null 2>&1 || true

# môi trường Python
apt-get install -y -qq python3-venv openssl >/dev/null 2>&1 || true
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -U pip
"$DIR/venv/bin/pip" install -q -r "$APP/requirements.txt"

# chứng chỉ tự ký cho panel (Cloudflare proxy sẽ cấp HTTPS thật cho khách; SSL mode = Full)
if [ ! -f "$DIR/panel.crt" ]; then
  CN=${DOMAIN:-panel}; PANELCN="panel.${CN#*.}"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$DIR/panel.key" -out "$DIR/panel.crt" -subj "/CN=$PANELCN" >/dev/null 2>&1
  chmod 600 "$DIR/panel.key"
fi

# --- systemd: bot ---
cat > /etc/systemd/system/awgbot.service <<SVC
[Unit]
Description=AWG Telegram Bot
After=network-online.target awg-quick@awg0.service
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$DIR/bot.env
WorkingDirectory=$APP
ExecStart=$DIR/venv/bin/python $APP/bot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SVC

# --- systemd: panel (Mini App) ---
cat > /etc/systemd/system/awgpanel.service <<SVC
[Unit]
Description=AWG Mini App Panel
After=network-online.target awg-quick@awg0.service
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$DIR/bot.env
WorkingDirectory=$APP
ExecStart=$DIR/venv/bin/uvicorn api:app --host 0.0.0.0 --port 443 --ssl-keyfile $DIR/panel.key --ssl-certfile $DIR/panel.crt
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable --now awgbot.service awgpanel.service
sleep 2
echo "== Bot =="; systemctl status awgbot.service --no-pager | head -6
echo "== Panel =="; systemctl status awgpanel.service --no-pager | head -6
echo
echo "Xong."
echo " - Telegram: /start cho bot, bam '📱 Mo Mini App'."
echo " - Cloudflare: SSL/TLS -> Overview -> Full (de chap nhan cert tu ky)."
