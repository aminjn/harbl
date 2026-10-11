#!/usr/bin/env bash
# نصب مرورگر ضبط (Chromium + Playwright) برای دکمه «ضبط با مرورگر» در پنل
# استفاده (از داخل پوشه پروژه):  sudo bash deploy/install-recorder.sh
set -euo pipefail
APP_DIR="/opt/har-scraper"
BROWSERS="$APP_DIR/browsers"
[ "$(id -u)" -eq 0 ] || { echo "با sudo اجرا کنید"; exit 1; }

echo "==> نصب Playwright"
"$APP_DIR/venv/bin/pip" install -q "playwright==1.56.0"

echo "==> دانلود Chromium و کتابخانه‌های لازم (چند دقیقه طول می‌کشد)"
mkdir -p "$BROWSERS"
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" "$APP_DIR/venv/bin/playwright" install --with-deps chromium
chmod -R a+rX "$BROWSERS"

echo "==> تنظیم سرویس"
mkdir -p /etc/systemd/system/har-scraper.service.d
cat > /etc/systemd/system/har-scraper.service.d/recorder.conf <<CONF
[Service]
Environment=PLAYWRIGHT_BROWSERS_PATH=$BROWSERS
CONF
systemctl daemon-reload
systemctl restart har-scraper
echo "✅ مرورگر ضبط نصب شد. در پنل، داخل هر بخش «🎥 ضبط با مرورگر» را بزنید."
