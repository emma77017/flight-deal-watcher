#!/bin/bash
# Installs a systemd timer on the Pi: check GitHub for watcher updates every 3h
# (and 5 min after every boot), rebuilding the container when anything changed.
# Run ON THE PI as: sudo bash install_selfupdate.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="$(stat -c '%U' "$DIR")"

cat > /etc/systemd/system/flightwatcher-update.service <<EOF
[Unit]
Description=Flight watcher self-update
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
WorkingDirectory=${DIR}
ExecStart=/bin/bash ${DIR}/pi_selfupdate.sh
EOF

cat > /etc/systemd/system/flightwatcher-update.timer <<EOF
[Unit]
Description=Flight watcher self-update timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=3h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now flightwatcher-update.timer
systemctl start flightwatcher-update.service
echo "Self-update installed: checks GitHub every 3h and 5min after each boot."
