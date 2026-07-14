#!/bin/bash
# Installs three launchd agents:
#   - full scan of the whole date grid, daily at 08:00 and 20:00
#   - fast "pulse" scan (rotating slice of the grid) every 2 hours, and at login
#   - daily noon healthcheck that complains if scanning has silently stopped
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
UID_N="$(id -u)"
mkdir -p "$DIR/logs"
chmod +x "$DIR/run_watcher.sh"

write_plist() {  # $1=label-suffix $2=args-xml $3=schedule-xml
cat > "$HOME/Library/LaunchAgents/com.emma.flight-deal-watcher$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.emma.flight-deal-watcher$1</string>
    <key>ProgramArguments</key>
    <array>
        <string>${DIR}/run_watcher.sh</string>
$2
    </array>
    <key>WorkingDirectory</key><string>${DIR}</string>
$3
    <key>StandardOutPath</key><string>${DIR}/logs/launchd.out.log</string>
    <key>StandardErrorPath</key><string>${DIR}/logs/launchd.err.log</string>
</dict>
</plist>
EOF
}

write_plist "" \
"        <string>scan</string>" \
"    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>
    </array>"

write_plist ".pulse" \
"        <string>scan</string>
        <string>--pulse</string>" \
"    <key>StartInterval</key><integer>7200</integer>
    <key>RunAtLoad</key><true/>"

write_plist ".watchdog" \
"        <string>healthcheck</string>" \
"    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>"

for suffix in "" ".pulse" ".watchdog"; do
    label="com.emma.flight-deal-watcher${suffix}"
    launchctl bootout "gui/${UID_N}/${label}" 2>/dev/null || true
    launchctl bootstrap "gui/${UID_N}" "$HOME/Library/LaunchAgents/${label}.plist"
done

echo "Installed:"
echo "  full scan   daily 08:00 + 20:00"
echo "  pulse scan  every 2h (rotating slice, also runs at login)"
echo "  watchdog    daily 12:00 (warns if scans stopped or email unconfigured)"
echo "Uninstall with: ./uninstall_schedule.sh"
