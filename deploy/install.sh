#!/usr/bin/env bash
# Install the IPO bidder as systemd user timers on a Linux box.
# Usage: ./deploy/install.sh   (run from the project root, as the user who will run it)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

command -v python3 >/dev/null || { echo "python3 missing"; exit 1; }
python3 - <<'EOF'
import sys; assert sys.version_info >= (3, 10), f"need Python 3.10+, have {sys.version}"
EOF

if [ ! -x .venv/bin/python ]; then
  if python3 -m venv .venv 2>/dev/null && [ -x .venv/bin/pip ]; then
    .venv/bin/pip install -q --upgrade pip
  else
    # no python3-venv/pip on this box: use uv (installs under ~/.local, no sudo needed)
    rm -rf .venv
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    export PATH="$HOME/.local/bin:$PATH"
    uv venv -q .venv --python python3
  fi
fi
if [ -x .venv/bin/pip ]; then
  .venv/bin/pip install -q -r requirements.txt
else
  export PATH="$HOME/.local/bin:$PATH"
  uv pip install -q --python .venv/bin/python -r requirements.txt
fi
[ -f .env ] || { cp .env.example .env; echo ">> created .env from template: fill it in, then rerun"; exit 1; }
mkdir -p state

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
chmod +x deploy/*.sh
for job in run remind sell iplog update; do
  sed "s#__ROOT__#$ROOT#g" "deploy/ipo-$job.service" > "$UNIT_DIR/ipo-$job.service"
  cp "deploy/ipo-$job.timer" "$UNIT_DIR/ipo-$job.timer"
done
systemctl --user daemon-reload
for job in run remind sell iplog update; do
  systemctl --user enable --now "ipo-$job.timer"
done
# keep user services running when not logged in
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" || true

echo
echo "Installed. Timers:"
systemctl --user list-timers 'ipo-*' --no-pager
echo
echo "Next: .venv/bin/python -m ipo_bidder test-auth   and   .venv/bin/python -m ipo_bidder notify-test"
echo "Logs: journalctl --user -u ipo-run.service -n 50   or   tail state/ipo_bidder.log"
