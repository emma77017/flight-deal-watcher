#!/bin/bash
# Pulls the latest watcher code/config from GitHub and rebuilds the container
# only when something actually changed. Run by the flightwatcher-update.timer.
set -e
cd "$(dirname "$0")"
git fetch -q origin master
LOCAL=$(git rev-parse @)
REMOTE=$(git rev-parse @{u})
if [ "$LOCAL" != "$REMOTE" ]; then
    mkdir -p logs
    echo "$(date '+%F %T') updating ${LOCAL:0:8} -> ${REMOTE:0:8}" >> logs/selfupdate.log
    git pull -q
    docker compose up -d --build >> logs/selfupdate.log 2>&1
    echo "$(date '+%F %T') update complete" >> logs/selfupdate.log
fi
