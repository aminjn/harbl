#!/usr/bin/env bash
# Watchdog: every minute, ask the panel /healthz (it needs free request threads and a free database).
# Two failed checks in a row (20s timeout each) -> restart har-scraper. Running collections continue by
# themselves after the restart (progress files), so a restart costs a few seconds, not the work.
# Install:   sudo bash deploy/install-watchdog.sh
# Its log:   journalctl -t har-scraper-watchdog
set -euo pipefail

cat > /usr/local/bin/har-scraper-watchdog <<'SH'
#!/usr/bin/env bash
check() { curl -fsS -m 20 -o /dev/null http://127.0.0.1:8000/healthz; }
systemctl is-active --quiet har-scraper || exit 0   # stopped on purpose / restarting: leave it
check && exit 0
sleep 10
check && exit 0
logger -t har-scraper-watchdog "panel did not answer /healthz twice: restarting har-scraper"
systemctl restart har-scraper
SH
chmod 755 /usr/local/bin/har-scraper-watchdog

cat > /etc/systemd/system/har-scraper-watchdog.service <<'UNIT'
[Unit]
Description=Restart har-scraper when the panel stops answering
[Service]
Type=oneshot
ExecStart=/usr/local/bin/har-scraper-watchdog
UNIT

cat > /etc/systemd/system/har-scraper-watchdog.timer <<'UNIT'
[Unit]
Description=Check the har-scraper panel every minute
[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now har-scraper-watchdog.timer
echo "watchdog installed: checks http://127.0.0.1:8000/healthz every minute"
systemctl list-timers har-scraper-watchdog.timer --no-pager | head -3
