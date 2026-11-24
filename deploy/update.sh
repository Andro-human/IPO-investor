#!/usr/bin/env bash
# Self-update: runs on the box every 5 minutes (ipo-update.timer).
#
# Security model: this box holds Upstox credentials, so it only runs code that YOU signed.
#   - origin/main's tip commit must carry a valid SSH signature from a key listed in
#     ~/.config/ipo-bidder/allowed_signers (kept on the box, outside the repo, so a pushed
#     commit cannot change who is trusted). Unsigned/unknown => refuse + one Telegram alert.
#   - dependencies come only from requirements.lock with --require-hashes.
# Local edits to tracked files are overwritten: change config via git, not on the box.
#
# Everything is inside main() so bash has read the whole script before `git reset` replaces it.
set -euo pipefail

main() {
  cd "$(dirname "$0")/.."
  local signers="$HOME/.config/ipo-bidder/allowed_signers"
  local state_file="state/update_rejected"

  # one-time bootstrap from the code already running here (never from the fetched commit, never overwritten)
  if [ ! -s "$signers" ] && [ -s deploy/allowed_signers ]; then
    mkdir -p "$(dirname "$signers")" && cp deploy/allowed_signers "$signers" && chmod 444 "$signers"
  fi

  git fetch -q --force origin +main:refs/remotes/origin/main   # --force: follow rewritten history too
  local LOCAL REMOTE
  LOCAL=$(git rev-parse HEAD)
  REMOTE=$(git rev-parse origin/main)
  [ "$LOCAL" = "$REMOTE" ] && return 0

  if [ ! -s "$signers" ] || ! git -c gpg.ssh.allowedSignersFile="$signers" verify-commit "$REMOTE" >/dev/null 2>&1; then
    if [ "$(cat "$state_file" 2>/dev/null)" != "$REMOTE" ]; then   # alert once per rejected commit
      echo "$REMOTE" > "$state_file"
      .venv/bin/python -m ipo_bidder notify "⚠️ Update REFUSED: commit ${REMOTE:0:7} on GitHub is not signed by your key. The box keeps running ${LOCAL:0:7}. If you didn't push this, secure your GitHub account." || true
    fi
    echo "refused unsigned/unknown commit ${REMOTE:0:7}" >&2
    return 1
  fi

  git reset -q --hard "$REMOTE"
  rm -f "$state_file"
  .venv/bin/pip install -q --require-hashes --no-deps -r requirements.lock

  local ROOT UNIT_DIR job
  ROOT="$(pwd)"
  UNIT_DIR="$HOME/.config/systemd/user"
  for job in run remind sell iplog update; do
    sed "s#__ROOT__#$ROOT#g" "deploy/ipo-$job.service" > "$UNIT_DIR/ipo-$job.service"
    cp "deploy/ipo-$job.timer" "$UNIT_DIR/ipo-$job.timer"
  done
  systemctl --user daemon-reload
  systemctl --user enable -q ipo-run.timer ipo-remind.timer ipo-sell.timer ipo-iplog.timer ipo-update.timer

  echo "updated ${LOCAL:0:7} -> ${REMOTE:0:7}"
  .venv/bin/python -m ipo_bidder notify "Box updated to $(git log -1 --format='%h: %s') (signature verified) · mode $(grep -E '^mode' config.toml | cut -d'"' -f2)" || true
}

main "$@"
exit $?
