#!/usr/bin/env bash
# نصب مرورگر ضبط (Chromium + Playwright) برای دکمه «ضبط با مرورگر» در پنل
# استفاده (از داخل پوشه پروژه):  sudo bash deploy/install-recorder.sh
#
# اگر سرور به اینترنت خارج دسترسی ضعیف دارد، به‌ترتیب امتحان می‌شود:
#   ۱) PyPI و سرور Playwright با ادامه دانلود قطع‌شده و چند بار تلاش
#   ۲) میرورها (Runflare، Tsinghua، Aliyun برای پایتون؛ npmmirror برای مرورگر)
#   ۳) فایل‌های آفلاین در deploy/offline/ (راهنما در انتهای خروجی اگر همه ناموفق بود)
set -uo pipefail
APP_DIR="/opt/har-scraper"
BROWSERS="$APP_DIR/browsers"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OFFLINE="$SRC_DIR/deploy/offline"
PW_VER="1.56.0"
PIP="$APP_DIR/venv/bin/pip"
PW="$APP_DIR/venv/bin/playwright"
[ "$(id -u)" -eq 0 ] || { echo "با sudo اجرا کنید"; exit 1; }
export PLAYWRIGHT_BROWSERS_PATH="$BROWSERS"
export PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000

offline_help() {
  cat <<HELP

❌ $1
روی کامپیوتری که اینترنت آزاد دارد این فایل‌ها را دانلود کنید و در پوشه
  $OFFLINE
روی سرور بگذارید (مثلاً با scp)، بعد همین دستور را دوباره اجرا کنید:
  ۱) پکیج‌های پایتون (روی همان کامپیوتر):
     pip download --only-binary=:all: --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux1_x86_64 --python-version $(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")') -d offline playwright==$PW_VER
  ۲) مرورگر:
     $SHELL_URL
HELP
  exit 1
}

echo "==> نصب Playwright $PW_VER"
RESUME=""
"$PIP" install --help 2>/dev/null | grep -q -- "--resume-retries" && RESUME="--resume-retries 20"
installed=0
if ls "$OFFLINE"/playwright-*.whl >/dev/null 2>&1; then
  "$PIP" install --no-index --find-links "$OFFLINE" "playwright==$PW_VER" && installed=1
fi
if [ $installed -eq 0 ]; then
  for idx in "https://pypi.org/simple" "https://mirror-pypi.runflare.com/simple" \
             "https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.aliyun.com/pypi/simple"; do
    echo "    تلاش از $idx"
    if "$PIP" install --timeout 60 --retries 8 $RESUME -i "$idx" "playwright==$PW_VER"; then installed=1; break; fi
  done
fi
SHELL_URL="https://cdn.playwright.dev/dbazure/download/playwright/builds/chromium/<نسخه>/chromium-headless-shell-linux.zip"
[ $installed -eq 1 ] || offline_help "دانلود Playwright از هیچ منبعی ممکن نشد"

# Where Playwright expects the headless Chromium, and from which URL
INFO="$("$PW" install --dry-run --only-shell chromium 2>/dev/null)"
SHELL_DIR="$(echo "$INFO" | awk '/chromium-headless-shell/{f=1} f&&/Install location/{print $3; exit}')"
SHELL_URL="$(echo "$INFO" | awk '/chromium-headless-shell/{f=1} f&&/Download url/{print $3; exit}')"

echo "==> کتابخانه‌های سیستمی لازم برای Chromium (از مخزن Ubuntu)"
"$PW" install-deps chromium || echo "    هشدار: نصب بعضی کتابخانه‌ها ناموفق بود"

echo "==> دانلود Chromium (حدود ۱۰۰ مگابایت؛ ممکن است چند دقیقه طول بکشد)"
mkdir -p "$BROWSERS"
got=0
if [ -f "$OFFLINE/chromium-headless-shell-linux.zip" ] && [ -n "$SHELL_DIR" ]; then
  echo "    از فایل آفلاین"
  rm -rf "$SHELL_DIR" && mkdir -p "$SHELL_DIR" && \
    python3 -c "import zipfile,sys;zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$OFFLINE/chromium-headless-shell-linux.zip" "$SHELL_DIR" && \
    chmod +x "$SHELL_DIR"/chrome-linux/headless_shell && \
    touch "$SHELL_DIR/INSTALLATION_COMPLETE" "$SHELL_DIR/DEPENDENCIES_VALIDATED" && got=1
fi
if [ $got -eq 0 ]; then
  for host in "" "https://cdn.npmmirror.com/binaries/playwright"; do
    echo "    تلاش از ${host:-سرور اصلی Playwright}"
    for try in 1 2 3; do
      if PLAYWRIGHT_DOWNLOAD_HOST="$host" "$PW" install --only-shell chromium; then got=1; break 2; fi
      sleep 5
    done
  done
fi
[ $got -eq 1 ] || offline_help "دانلود Chromium ممکن نشد"
chmod -R a+rX "$BROWSERS"

echo "==> تنظیم سرویس"
mkdir -p /etc/systemd/system/har-scraper.service.d
cat > /etc/systemd/system/har-scraper.service.d/recorder.conf <<CONF
[Service]
Environment=PLAYWRIGHT_BROWSERS_PATH=$BROWSERS
CONF
systemctl daemon-reload
systemctl restart har-scraper

echo "==> آزمایش اجرای مرورگر"
if sudo -u harscraper env PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" HOME=/var/lib/har-scraper "$APP_DIR/venv/bin/python" -c \
  "from playwright.sync_api import sync_playwright as s
with s() as p: b=p.chromium.launch(); b.close()"; then
  echo "✅ مرورگر ضبط نصب شد. در پنل، داخل هر بخش «🎥 ضبط با مرورگر» را بزنید."
else
  echo "❌ مرورگر نصب شد ولی اجرا نشد؛ خروجی بالا را بفرستید."
  exit 1
fi
