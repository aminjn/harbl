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
# Newer Ubuntu releases (e.g. 26.04) are unknown to this Playwright version: use the 24.04 build of Chromium
PLATFORM_OVERRIDE=""
. /etc/os-release 2>/dev/null || true
if [ "${ID:-}" = "ubuntu" ] && ! printf '%s\n' 20.04 22.04 24.04 | grep -qx "${VERSION_ID:-}"; then
  PLATFORM_OVERRIDE="ubuntu24.04-x64"
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="$PLATFORM_OVERRIDE"
  echo "==> Ubuntu ${VERSION_ID:-?}: مرورگر نسخه Ubuntu 24.04 استفاده می‌شود"
fi

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

# Download speed (bytes/s) of the first MB of each URL, fastest first: "speed url" lines.
# For a PyPI index (ending in /simple) the playwright wheel's own link on that index is measured.
rank() {
  python3 - "$PW_VER" "$@" <<'PY'
import re, sys, time, urllib.request
from urllib.parse import urljoin
ver, urls = sys.argv[1], sys.argv[2:]
out = []
for u in urls:
    target, speed = u, 0
    try:
        if u.endswith("/simple"):
            page = urllib.request.urlopen(u + "/playwright/", timeout=8).read().decode("utf-8", "replace")
            m = re.search(r'href="([^"]*playwright-%s-py3-none-manylinux1_x86_64\.whl[^"]*)"' % re.escape(ver), page)
            if not m:
                raise ValueError("no wheel")
            target = urljoin(u + "/playwright/", m.group(1).replace("&amp;", "&"))
        req = urllib.request.Request(target, headers={"Range": "bytes=0-1048575"})
        t0, got = time.time(), 0
        with urllib.request.urlopen(req, timeout=8) as r:
            while time.time() - t0 < 8:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        speed = int(got / max(time.time() - t0, 0.01))
    except Exception:
        pass
    out.append((speed, u))
for speed, u in sorted(out, reverse=True):
    print(speed, u)
PY
}

echo "==> نصب Playwright $PW_VER"
RESUME=""
"$PIP" install --help 2>/dev/null | grep -q -- "--resume-retries" && RESUME="--resume-retries 20"
installed=0
if "$APP_DIR/venv/bin/python" -c "import importlib.metadata as m,sys;sys.exit(m.version('playwright')!='$PW_VER')" 2>/dev/null; then
  echo "    از قبل نصب است"
  installed=1
fi
if [ $installed -eq 0 ] && ls "$OFFLINE"/playwright-*.whl >/dev/null 2>&1; then
  "$PIP" install --no-index --find-links "$OFFLINE" "playwright==$PW_VER" && installed=1
fi
if [ $installed -eq 0 ]; then
  echo "    سنجش سرعت منبع‌ها…"
  RANKED="$(rank "https://mirror-pypi.runflare.com/simple" "https://pypi.org/simple" \
                 "https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.aliyun.com/pypi/simple")"
  echo "$RANKED" | awk '{printf "      %6d KB/s  %s\n", $1/1024, $2}'
  for idx in $(echo "$RANKED" | awk '{print $2}'); do
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
# another apt/dpkg (e.g. automatic updates) may hold the lock: wait for it
for i in $(seq 1 120); do
  fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || pgrep -x "apt|apt-get|dpkg|unattended-upgr" >/dev/null || break
  [ "$i" = 1 ] && echo "    منتظر تمام شدن apt دیگر…"
  sleep 5
done
if ! "$PW" install-deps chromium; then
  # install them one by one so a renamed package on a newer release does not stop the rest
  apt-get -o DPkg::Lock::Timeout=600 update -y >/dev/null 2>&1 || true
  for pkg in libasound2t64 libasound2 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 \
             libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 \
             libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 libxshmfence1 \
             fonts-liberation fonts-noto-core fonts-noto-color-emoji fonts-vazirmatn; do
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q "$pkg" >/dev/null 2>&1 && echo "      ✓ $pkg" || echo "      - $pkg (نبود)"
  done
fi

echo "==> دانلود Chromium (حدود ۱۰۰ مگابایت؛ ممکن است چند دقیقه طول بکشد)"
mkdir -p "$BROWSERS"
got=0
ZIP="$OFFLINE/chromium-headless-shell-linux.zip"
if [ ! -f "$ZIP" ] && [ -n "$SHELL_URL" ] && [ -n "$SHELL_DIR" ] && [ ! -f "$SHELL_DIR/INSTALLATION_COMPLETE" ]; then
  echo "    دانلود موازی (۸ اتصال هم‌زمان)"
  mkdir -p "$OFFLINE"
  python3 - "$SHELL_URL" "$ZIP" <<'PY' || rm -f "$ZIP"
import os, sys, threading, time, urllib.request, zipfile
url, out = sys.argv[1], sys.argv[2]
size = int(urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60).headers["Content-Length"])
parts, done, lock = 8, [0] * 8, threading.Lock()
bounds = [(i * size // parts, (i + 1) * size // parts - 1) for i in range(parts)]
def get(i):
    start, end = bounds[i]
    path = f"{out}.part{i}"
    for attempt in range(30):  # resume each part after a dropped connection
        have = os.path.getsize(path) if os.path.exists(path) else 0
        done[i] = have
        if start + have > end:
            return
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={start + have}-{end}"})
            with urllib.request.urlopen(req, timeout=60) as r, open(path, "ab") as f:
                while chunk := r.read(65536):
                    f.write(chunk)
                    done[i] += len(chunk)
        except Exception:
            time.sleep(3)
    raise SystemExit(f"part {i} failed")
threads = [threading.Thread(target=get, args=(i,), daemon=True) for i in range(parts)]
for t in threads: t.start()
t0 = time.time()
while any(t.is_alive() for t in threads):
    time.sleep(2)
    got = sum(done); speed = got / max(time.time() - t0, 1)
    print(f"\r      {got/2**20:6.1f}/{size/2**20:.1f} MB  {speed/1024:7.0f} KB/s", end="", flush=True)
print()
if sum(os.path.getsize(f"{out}.part{i}") for i in range(parts)) != size:
    raise SystemExit("incomplete")
with open(out, "wb") as f:
    for i in range(parts):
        with open(f"{out}.part{i}", "rb") as p:
            f.write(p.read())
        os.remove(f"{out}.part{i}")
zipfile.ZipFile(out).testzip()
PY
fi
if [ -f "$ZIP" ] && [ -n "$SHELL_DIR" ] && [ ! -f "$SHELL_DIR/INSTALLATION_COMPLETE" ]; then
  echo "    از فایل آفلاین"
  rm -rf "$SHELL_DIR" && mkdir -p "$SHELL_DIR" && \
    python3 -c "
import os, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for i in z.infolist():  # extractall() drops the executable bits
    z.extract(i, sys.argv[2])
    mode = i.external_attr >> 16
    if mode and not i.is_dir():
        os.chmod(os.path.join(sys.argv[2], i.filename), mode & 0o777)
" "$ZIP" "$SHELL_DIR" && \
    chmod +x "$SHELL_DIR"/chrome-linux/headless_shell && \
    touch "$SHELL_DIR/INSTALLATION_COMPLETE" "$SHELL_DIR/DEPENDENCIES_VALIDATED" && got=1 && rm -f "$ZIP"
fi
[ -f "$SHELL_DIR/INSTALLATION_COMPLETE" ] && got=1
if [ $got -eq 0 ]; then
  # same file on the npmmirror CDN: <host>/builds/chromium/<rev>/<zip>
  ALT_URL="https://cdn.npmmirror.com/binaries/playwright/${SHELL_URL#*/playwright/}"
  echo "    سنجش سرعت منبع‌ها…"
  RANKED="$(rank "$SHELL_URL" "$ALT_URL")"
  echo "$RANKED" | awk '{printf "      %6d KB/s  %s\n", $1/1024, $2}'
  HOSTS=""
  for u in $(echo "$RANKED" | awk '{print $2}'); do
    [ "$u" = "$ALT_URL" ] && HOSTS="$HOSTS https://cdn.npmmirror.com/binaries/playwright" || HOSTS="$HOSTS default"
  done
  for host in $HOSTS; do
    [ "$host" = "default" ] && host=""
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
${PLATFORM_OVERRIDE:+Environment=PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=$PLATFORM_OVERRIDE}
CONF
systemctl daemon-reload
systemctl restart har-scraper

echo "==> آزمایش اجرای مرورگر"
if sudo -u harscraper env PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="$PLATFORM_OVERRIDE" HOME=/var/lib/har-scraper "$APP_DIR/venv/bin/python" -c \
  "from playwright.sync_api import sync_playwright as s
with s() as p: b=p.chromium.launch(); b.close()"; then
  echo "✅ مرورگر ضبط نصب شد. در پنل، داخل هر بخش «🎥 ضبط با مرورگر» را بزنید."
else
  echo "❌ مرورگر نصب شد ولی اجرا نشد؛ خروجی بالا را بفرستید."
  exit 1
fi
