#!/bin/bash
# Removes all scheduled watcher jobs.
set -euo pipefail
for suffix in "" ".pulse" ".watchdog"; do
    label="com.emma.flight-deal-watcher${suffix}"
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/${label}.plist"
done
echo "All scheduled watcher jobs removed."
