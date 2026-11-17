#!/usr/bin/env bash
# Deploy MovieNight: wait for the Release workflow of the commit on origin/main, then ask
# Watchtower on the home server (ssh alias: $HOST, default "homeserver") to pull the released image.
#
# Optional settings:
#   HOST        ssh alias of the server                 (default: homeserver)
#   CONTAINER   container to verify                     (default: movienight; e.g. movienight-jf)
#   IMAGE       image Watchtower should update          (default: ghcr.io/andreub/movienight)
#   SKIP_WAIT=1 do not wait for the Release workflow (use the latest v* tag on HEAD, if any)
set -euo pipefail

HOST=${HOST:-homeserver}
CONTAINER=${CONTAINER:-movienight}
IMAGE=${IMAGE:-ghcr.io/andreub/movienight}
SKIP_WAIT=${SKIP_WAIT:-}
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC2016  # expanded on the server, not here
REMOTE_PATH='export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH'
git() { command git -C "$PROJECT_DIR" "$@"; }
die() { echo "==> $*" >&2; exit 1; }

# 1. HEAD must be what origin/main has: only released commits are deployed
git fetch -q --tags origin main
SHA=$(git rev-parse HEAD)
[ "$SHA" = "$(git rev-parse origin/main)" ] ||
  die "HEAD ($(git rev-parse --short HEAD)) is not origin/main ($(git rev-parse --short origin/main)). Merge your PR and 'git pull' first."

# 2. wait for the Release workflow of that commit
if [ -z "$SKIP_WAIT" ]; then
  echo "==> Waiting for the Release workflow of ${SHA:0:7}"
  run_id=""
  for _ in $(seq 1 24); do
    run_id=$(gh run list --workflow release.yml --commit "$SHA" --json databaseId --jq '.[0].databaseId // empty')
    [ -n "$run_id" ] && break
    sleep 5
  done
  [ -n "$run_id" ] || die "no Release workflow run found for ${SHA:0:7} after 2 minutes"
  gh run watch "$run_id" --exit-status >/dev/null || die "the Release workflow failed: gh run view $run_id --log-failed"
  git fetch -q --tags origin
fi
TAG=$(git tag --points-at "$SHA" --list 'v*' --sort=-v:refname | head -n1)
[ -n "$TAG" ] || die "no v* tag points at ${SHA:0:7} (was it released?)"
VERSION=${TAG#v}
echo "==> Released version: $VERSION"

# 3. ask Watchtower to pull now (POST /v1/update?image=..., Bearer token read on the server, never printed)
echo "==> Asking Watchtower on $HOST to update $IMAGE"
ssh "$HOST" "$REMOTE_PATH"'; cd ~/etc && eval "$(DIRENV_LOG_FORMAT= direnv export bash)" && test -n "$ACME_DOMAIN" && test -n "$WATCHTOWER_HTTP_API_TOKEN" && curl -fsS --max-time 600 -X POST -H "Authorization: Bearer $WATCHTOWER_HTTP_API_TOKEN" "https://watchtower.$ACME_DOMAIN/v1/update?image='"$IMAGE"'"'
echo

# 4. wait until the container is healthy and runs the released version
echo "==> Waiting for $CONTAINER to be healthy on version $VERSION (up to 5 minutes)"
state="" got=""
for _ in $(seq 1 60); do
  out=$(ssh "$HOST" "$REMOTE_PATH"'; docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} {{index .Config.Labels \"org.opencontainers.image.version\"}}" '"$CONTAINER" 2>/dev/null || true)
  state=${out%% *}
  got=${out#* }
  if { [ "$state" = "healthy" ] || [ "$state" = "running" ]; } && [ "$got" = "$VERSION" ]; then
    ssh "$HOST" "$REMOTE_PATH"'; docker inspect -f "image {{.Image}}{{println}}version {{index .Config.Labels \"org.opencontainers.image.version\"}}{{println}}started {{.State.StartedAt}}" '"$CONTAINER"
    echo "==> $CONTAINER is $state on $VERSION"
    exit 0
  fi
  sleep 5
done

echo "==> Timed out: $CONTAINER is '${state:-missing}' on version '${got:-?}', expected $VERSION. Last 30 log lines:" >&2
ssh "$HOST" "$REMOTE_PATH"'; docker logs --tail 30 '"$CONTAINER" || true
exit 1
