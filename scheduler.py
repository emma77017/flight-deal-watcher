#!/usr/bin/env python3
"""Long-running scheduler for Docker (Raspberry Pi): replaces launchd/GitHub cron.

Runs forever inside the container:
  - full scan at 08:00 and 20:00 local time (TZ env var)
  - pulse scan every 2 hours in between (and once at startup)
  - healthcheck at 12:00
The watcher's own lock file prevents overlaps; if a scan crashes the loop just
keeps going, and `restart: unless-stopped` in docker-compose revives the whole
container if this process ever dies.
"""

import subprocess
import sys
import time
from datetime import datetime, timedelta

FULL_HOURS = (8, 20)
HEALTH_HOUR = 12
PULSE_EVERY_MIN = 120


def run(*args: str):
    print(f"[scheduler] {datetime.now():%Y-%m-%d %H:%M:%S} -> watcher.py {' '.join(args)}", flush=True)
    try:
        subprocess.run([sys.executable, "watcher.py", *args], timeout=3600)
    except Exception as e:
        print(f"[scheduler] watcher run failed: {e}", flush=True)


def main():
    done: set = set()  # (date, marker) so each daily slot fires exactly once
    run("scan", "--pulse")  # catch up immediately on (re)start
    last_pulse = time.time()

    while True:
        now = datetime.now()
        done = {m for m in done if m[0] >= now.date() - timedelta(days=1)}

        if now.hour in FULL_HOURS and (now.date(), f"full{now.hour}") not in done:
            done.add((now.date(), f"full{now.hour}"))
            run("scan")
            last_pulse = time.time()
        elif now.hour == HEALTH_HOUR and (now.date(), "health") not in done:
            done.add((now.date(), "health"))
            run("healthcheck")
        elif time.time() - last_pulse >= PULSE_EVERY_MIN * 60:
            run("scan", "--pulse")
            last_pulse = time.time()

        time.sleep(60)


if __name__ == "__main__":
    main()
