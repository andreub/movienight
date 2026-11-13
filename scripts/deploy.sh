#!/usr/bin/env bash
# Deploy MovieNight to the home server (ssh alias: $HOST, default "macmini").
set -euo pipefail

HOST=${HOST:-macmini}
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_PATH='export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH'

echo "==> Syncing project to $HOST:movienight/"
rsync -az --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'data' \
  --exclude 'letterboxd' \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.DS_Store' \
  "$PROJECT_DIR"/ "$HOST:movienight/"

if [ -d "$PROJECT_DIR/letterboxd" ]; then
  echo "==> Syncing Letterboxd export to $HOST:etc/movienight/letterboxd/"
  ssh "$HOST" 'mkdir -p ~/etc/movienight/data ~/etc/movienight/letterboxd'
  rsync -az "$PROJECT_DIR/letterboxd/" "$HOST:etc/movienight/letterboxd/"
else
  ssh "$HOST" 'mkdir -p ~/etc/movienight/data ~/etc/movienight/letterboxd'
fi

echo "==> Building and starting container on $HOST"
ssh "$HOST" "$REMOTE_PATH"'; cd ~/etc && eval "$(direnv export bash)" && test -n "$ACME_DOMAIN" && docker compose up -d --build movienight'

echo "==> Waiting for healthy status (up to 2 minutes)"
for _ in $(seq 1 24); do
  if ssh "$HOST" "$REMOTE_PATH"'; docker ps --format "{{.Names}} {{.Status}}" | grep "^movienight "' 2>/dev/null | grep -q '(healthy)'; then
    echo "==> movienight is healthy"
    exit 0
  fi
  sleep 5
done

echo "==> Timed out waiting for healthy status. Last 30 log lines:" >&2
ssh "$HOST" "$REMOTE_PATH"'; docker logs --tail 30 movienight' || true
exit 1
