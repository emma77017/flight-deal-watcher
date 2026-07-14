#!/bin/bash
# launchd entry point: keeps the Mac awake during the scan (caffeinate) and pops
# a notification if the watcher itself crashes (e.g. broken venv after an OS or
# Homebrew Python upgrade) - so failures are never silent.
DIR="$(cd "$(dirname "$0")" && pwd)"
/usr/bin/caffeinate -is "$DIR/.venv/bin/python" "$DIR/watcher.py" "$@"
rc=$?
if [ $rc -ne 0 ]; then
  /usr/bin/osascript -e "display notification \"'watcher $1' crashed (exit $rc). If this repeats, the Python venv may be broken - reinstall with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\" with title \"⚠️ Flight Deal Watcher\" sound name \"Basso\"" || true
fi
exit $rc
