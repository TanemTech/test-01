#!/usr/bin/env bash
# One-time, isolated TANEM test-01 provisioning on the existing Selectel VPS.
set -Eeuo pipefail

REPO=TanemTech/test-01
UPDATER_SHA=73072145c79d427e5d47a8930b5b15e8a7783993
WEB_ROOT=/var/www/tanem-test-01
BIN_DIR=/home/tanem-deploy/.local/bin
BIN="$BIN_DIR/tanem-test-01-updater.py"
NGINX_CONF=/etc/nginx/sites-available/tanem-test-01
NGINX_LINK=/etc/nginx/sites-enabled/tanem-test-01
SERVICE=/etc/systemd/system/tanem-test-01-sync.service
TIMER=/etc/systemd/system/tanem-test-01-sync.timer

if [[ $(id -u) != 0 ]]; then
  echo 'ERROR: run in root Selectel web console'
  exit 1
fi
if ! id tanem-deploy >/dev/null 2>&1; then
  echo 'ERROR: tanem-deploy user is missing'; exit 1
fi
if [[ -e "$NGINX_CONF" || -L "$NGINX_LINK" || -e "$SERVICE" || -e "$TIMER" ]]; then
  echo 'STOP: test-01 appears already installed. No existing configuration was overwritten.'
  exit 1
fi
for command_name in curl python3 nginx runuser install systemctl; do
  command -v "$command_name" >/dev/null 2>&1 || { echo "ERROR: missing $command_name"; exit 1; }
done
nginx -t

TEMP="$(mktemp)"
trap 'rm -f "$TEMP"' EXIT
curl --fail --silent --show-error --location --connect-timeout 10 --max-time 60 \
  "https://raw.githubusercontent.com/$REPO/$UPDATER_SHA/ops/selectel-test-updater.py" -o "$TEMP"
python3 -m py_compile "$TEMP"

install -d -m 0755 -o tanem-deploy -g tanem-deploy "$WEB_ROOT"
install -d -m 0750 -o tanem-deploy -g tanem-deploy "$BIN_DIR"
install -d -m 0700 -o tanem-deploy -g tanem-deploy /home/tanem-deploy/.local/state
install -m 0750 -o tanem-deploy -g tanem-deploy "$TEMP" "$BIN"

# No public route is created until GitHub checks have succeeded and the
# page has been deployed into the separate document root.
runuser -u tanem-deploy -- /usr/bin/python3 "$BIN"
if [[ ! -f "$WEB_ROOT/index.html" ]]; then
  echo 'NOT READY: GitHub workflow has not finished successfully yet.'
  echo 'Existing websites are unchanged. Rerun the installer after GitHub Actions is green.'
  exit 1
fi

cat > "$NGINX_CONF" <<'NGINX'
server {
    listen 80;
    server_name test-01.tanem.ru;
    root /var/www/tanem-test-01;
    index index.html;
    add_header X-Robots-Tag "noindex, nofollow" always;
    location / { try_files $uri $uri/ =404; }
}
NGINX

ln -s "$NGINX_CONF" "$NGINX_LINK"
if ! nginx -t; then
  rm -f "$NGINX_LINK" "$NGINX_CONF"
  echo 'ERROR: Nginx validation failed. Test config removed.'
  exit 1
fi
systemctl reload nginx

cat > "$SERVICE" <<'UNIT'
[Unit]
Description=TANEM isolated test-01 sync from green GitHub main
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
User=tanem-deploy
Group=tanem-deploy
UMask=0027
NoNewPrivileges=true
ExecStart=/usr/bin/python3 /home/tanem-deploy/.local/bin/tanem-test-01-updater.py
UNIT

cat > "$TIMER" <<'UNIT'
[Unit]
Description=TANEM isolated test-01 auto-sync every 15 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
AccuracySec=1min
Unit=tanem-test-01-sync.service
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now tanem-test-01-sync.timer
echo 'OK: Test site deployed to Selectel. Automatic timer active.'
echo 'CHECK: curl -H "Host: test-01.tanem.ru" http://127.0.0.1/'
echo 'DNS was not modified. No changes to other websites.'
