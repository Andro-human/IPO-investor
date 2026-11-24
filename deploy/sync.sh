#!/usr/bin/env bash
# Push the project to the box (code + config + .env), leaving its venv and state alone.
# Usage: ./deploy/sync.sh [host]   (default host: homelab)
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${1:-homelab}"
rsync -az --exclude .venv --exclude state --exclude __pycache__ --exclude '*.pyc' \
      --exclude .git --exclude .gstack ./ "$HOST:~/ipo-bidder/"
ssh "$HOST" 'cd ~/ipo-bidder && chmod 600 .env && .venv/bin/pip install -q --require-hashes --no-deps -r requirements.lock && grep -E "^mode" config.toml'
