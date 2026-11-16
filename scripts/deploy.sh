#!/usr/bin/env bash
# Deploy MovieNight to the home server (ssh alias: $HOST, default "macmini").
#
# Optional settings (defaults = the existing service, unchanged):
#   SERVICE     compose service and container name   (default: movienight)
#   REMOTE_DIR  build folder on the server            (default: ~/movienight)
#   CONTAINER   container name, if compose names it otherwise (default: $SERVICE)
# e.g. a second instance from the current branch:
#   SERVICE=movienight-jf REMOTE_DIR='~/movienight-jf' ./scripts/deploy.sh
set -euo pipefail

HOST=${HOST:-macmini}
SERVICE=${SERVICE:-movienight}
CONTAINER=${CONTAINER:-$SERVICE}
REMOTE_DIR=${REMOTE_DIR:-~/movienight}
REMOTE_DIR=${REMOTE_DIR%/}
REMOTE_DIR=${REMOTE_DIR/#\~\//}            # rsync/ssh paths are relative to the remote home
REMOTE_DIR=${REMOTE_DIR/#$HOME\//}
case "$REMOTE_DIR" in ""|"~"|"$HOME"|/) echo "REMOTE_DIR must be a folder below the remote home" >&2; exit 1 ;; esac
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_PATH='export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH'

# only what is committed: work in progress in the working tree never reaches the server
REV=$(git -C "$PROJECT_DIR" rev-parse --short HEAD)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
git -C "$PROJECT_DIR" archive HEAD | tar -x -C "$STAGE"

echo "==> Syncing commit $REV to $HOST:$REMOTE_DIR/ (service $SERVICE)"
rsync -az --delete "$STAGE"/ "$HOST:$REMOTE_DIR/"

# The Letterboxd export is shared by every instance (mounted read-only); each service has its own data.
if [ -d "$PROJECT_DIR/letterboxd" ]; then
  echo "==> Syncing Letterboxd export to $HOST:etc/movienight/letterboxd/"
  ssh "$HOST" "mkdir -p ~/etc/$SERVICE/data ~/etc/movienight/letterboxd"
  rsync -az "$PROJECT_DIR/letterboxd/" "$HOST:etc/movienight/letterboxd/"
else
  ssh "$HOST" "mkdir -p ~/etc/$SERVICE/data ~/etc/movienight/letterboxd"
fi

echo "==> Building and starting $SERVICE on $HOST"
ssh "$HOST" "$REMOTE_PATH"'; cd ~/etc && eval "$(direnv export bash)" && test -n "$ACME_DOMAIN" && docker compose up -d --build '"$SERVICE"

# The image has a HEALTHCHECK; a container without one counts once it is running.
echo "==> Waiting for healthy status (up to 2 minutes)"
for _ in $(seq 1 24); do
  state=$(ssh "$HOST" "$REMOTE_PATH"'; docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" '"$CONTAINER" 2>/dev/null || true)
  if [ "$state" = "healthy" ] || [ "$state" = "running" ]; then   # "running" = no healthcheck at all
    echo "==> $SERVICE is $state"
    exit 0
  fi
  sleep 5
done

echo "==> Timed out waiting for healthy status. Last 30 log lines:" >&2
ssh "$HOST" "$REMOTE_PATH"'; docker logs --tail 30 '"$CONTAINER" || true
exit 1
