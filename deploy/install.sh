#!/usr/bin/env bash
# نصب خودکار سامانه اسکرپر HAR روی Ubuntu 22.04 / 24.04
# استفاده:  sudo bash deploy/install.sh [domain]
#   بدون دامنه: پنل روی http://IP-سرور باز می‌شود
#   با دامنه:   Nginx برای آن دامنه تنظیم و در صورت امکان SSL گرفته می‌شود
set -euo pipefail

DOMAIN="${1:-}"
APP_USER="harscraper"
APP_DIR="/opt/har-scraper"
DATA_DIR="/var/lib/har-scraper"
ENV_FILE="/etc/har-scraper.env"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-AMin1535@12}"

[ "$(id -u)" -eq 0 ] || { echo "با sudo اجرا کنید"; exit 1; }

echo "==> نصب پیش‌نیازها"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx ufw curl

echo "==> ساخت کاربر سرویس و پوشه‌ها"
id "$APP_USER" &>/dev/null || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR"
cp -r "$SRC_DIR/app" "$SRC_DIR/requirements.txt" "$APP_DIR/"
chown -R root:root "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
chmod 700 "$DATA_DIR"

echo "==> ساخت محیط پایتون"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip || true
if ! "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" --timeout 30; then
  echo "    دسترسی به PyPI نشد؛ نصب از بسته‌های آفلاین داخل پروژه"
  "$APP_DIR/venv/bin/pip" install -q --no-index --find-links "$SRC_DIR/deploy/wheels" -r "$APP_DIR/requirements.txt"
fi

echo "==> ذخیره تنظیمات و رمز ادمین"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
HARSCRAPER_USER=$ADMIN_USER
HARSCRAPER_PASS=$ADMIN_PASS
HARSCRAPER_DATA=$DATA_DIR
HARSCRAPER_TIMEOUT=30
HARSCRAPER_UA_SUFFIX=
EOF
else
  echo "    $ENV_FILE از قبل وجود دارد؛ دست نخورد"
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "==> سرویس systemd"
cat > /etc/systemd/system/har-scraper.service <<EOF
[Unit]
Description=HAR Scraper panel
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
# فقط یک worker: زمان‌بند داخل همین پروسه است
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now har-scraper
systemctl restart har-scraper

echo "==> Nginx"
SERVER_NAME="${DOMAIN:-_}"
cat > /etc/nginx/sites-available/har-scraper <<EOF
server {
    listen 80;
    server_name $SERVER_NAME;
    client_max_body_size 200M;   # فایل‌های HAR می‌توانند بزرگ باشند

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/har-scraper /etc/nginx/sites-enabled/har-scraper
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo "==> فایروال"
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null

if [ -n "$DOMAIN" ]; then
  echo "==> گرفتن گواهی SSL برای $DOMAIN"
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect \
    || echo "    گرفتن SSL ناموفق بود (بخش «SSL» راهنما را ببینید). پنل فعلاً روی http بالاست."
fi

echo "==> مرورگر ضبط (اختیاری)"
bash "$SRC_DIR/deploy/install-recorder.sh" || echo "    نصب مرورگر ضبط ناموفق بود؛ بعداً: sudo bash deploy/install-recorder.sh"

sleep 2
if curl -s -o /dev/null -w "%{http_code}" -u "$ADMIN_USER:$ADMIN_PASS" http://127.0.0.1/ | grep -q 200; then
  IP=$(curl -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
  echo
  echo "✅ نصب کامل شد"
  echo "   آدرس پنل:  http://${DOMAIN:-$IP}"
  echo "   نام کاربری: $ADMIN_USER"
  echo "   رمز در فایل $ENV_FILE ذخیره شده است"
else
  echo "❌ پنل جواب نداد. لاگ:  journalctl -u har-scraper -n 50"
  exit 1
fi
