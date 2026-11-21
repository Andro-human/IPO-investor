#!/usr/bin/env bash
# Self-update: runs on the box every few minutes (ipo-update.timer).
# Pulls origin/main; if it moved, resets the working tree to it, refreshes deps and systemd units.
# Local edits to tracked files (e.g. config.toml) are overwritten: change config via git, not on the box.
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch -q --force origin +main:refs/remotes/origin/main   # --force: follow rewritten history too
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

git reset -q --hard origin/main
.venv/bin/pip install -q -r requirements.txt

ROOT="$(pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
for job in run remind sell iplog update; do
  sed "s#__ROOT__#$ROOT#g" "deploy/ipo-$job.service" > "$UNIT_DIR/ipo-$job.service"
  cp "deploy/ipo-$job.timer" "$UNIT_DIR/ipo-$job.timer"
done
systemctl --user daemon-reload
systemctl --user enable -q ipo-run.timer ipo-remind.timer ipo-sell.timer ipo-iplog.timer ipo-update.timer

echo "updated ${LOCAL:0:7} -> ${REMOTE:0:7}"
.venv/bin/python -m ipo_bidder notify "Box updated to $(git log -1 --format='%h: %s') · mode $(grep -E '^mode' config.toml | cut -d'"' -f2)" || true
