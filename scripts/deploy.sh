#!/usr/bin/env bash
# Deploy MovieNight to the home server (ssh alias: $HOST, default "homeserver").
#
#   scripts/deploy.sh                 main: wait for the Release workflow of origin/main, then ask
#                                     Watchtower to pull :latest. If a preview is running, recreate
#                                     the container on :latest instead (back to main).
#   scripts/deploy.sh <PR|branch>     preview: build (Preview image workflow) and run that branch's
#                                     head as image tag sha-<7>, to test it before merging.
#
# Previews recreate the container through its docker compose project, so the server compose file
# must use  image: ghcr.io/andreub/movienight:${MOVIENIGHT_TAG:-latest}
#
# Optional settings (also readable from a git-ignored .deploy.env.local at the repo root, see
# .deploy.env.example; variables set in the environment take precedence over that file):
#   HOST        ssh alias of the server                 (default: homeserver)
#   CONTAINER   container to verify                     (default: movienight; e.g. movienight-jf)
#   IMAGE       image to deploy                         (default: ghcr.io/andreub/movienight)
#   TAG_VAR     compose variable holding the image tag  (default: MOVIENIGHT_TAG)
#   SKIP_WAIT=1 do not wait for the build (main: use the latest v* tag on HEAD, if any;
#               branch: assume the sha-<7> image already exists)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Defaults from .deploy.env.local (git-ignored; in the checkout, or the main checkout when run from
# a worktree). Variables already set in the environment win over the file.
for env_file in "$PROJECT_DIR/.deploy.env.local" "$(command git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir)/../.deploy.env.local"; do
  [ -f "$env_file" ] || continue
  for name in HOST CONTAINER IMAGE TAG_VAR SKIP_WAIT; do
    [ -z "${!name+x}" ] || continue
    # shellcheck disable=SC1090  # user-provided file
    value=$(. "$env_file" >/dev/null 2>&1; printf '%s' "${!name-}")
    [ -z "$value" ] || printf -v "$name" '%s' "$value"
  done
  break
done
HOST=${HOST:-homeserver}
CONTAINER=${CONTAINER:-movienight}
IMAGE=${IMAGE:-ghcr.io/andreub/movienight}
TAG_VAR=${TAG_VAR:-MOVIENIGHT_TAG}
SKIP_WAIT=${SKIP_WAIT:-}
TARGET=${1:-}
# shellcheck disable=SC2016  # expanded on the server, not here
REMOTE_PATH='export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH'
git() { command git -C "$PROJECT_DIR" "$@"; }
die() { echo "==> $*" >&2; exit 1; }

# Wait for the newest run of workflow $1 on commit $2 whose id is greater than $3, then watch it
wait_for_run() {
  local workflow=$1 sha=$2 after=${3:-0} run_id=""
  for _ in $(seq 1 24); do
    run_id=$(gh run list --workflow "$workflow" --commit "$sha" --json databaseId \
      --jq "[.[] | select(.databaseId > $after)] | max_by(.databaseId) | .databaseId // empty")
    [ -n "$run_id" ] && break
    sleep 5
  done
  [ -n "$run_id" ] || die "no $workflow run found for ${sha:0:7} after 2 minutes"
  gh run watch "$run_id" --exit-status >/dev/null || die "the $workflow run failed: gh run view $run_id --log-failed"
}

# Recreate $CONTAINER on image tag $1 through its docker compose project (labels read on the server)
compose_recreate() {
  local tag=$1
  echo "==> Recreating $CONTAINER on $IMAGE:$tag via docker compose on $HOST"
  # shellcheck disable=SC2029  # the settings are meant to expand here and are passed as env vars
  ssh "$HOST" "$REMOTE_PATH; CONTAINER='$CONTAINER' IMAGE='$IMAGE' TAG_VAR='$TAG_VAR' TAG='$tag' bash -s" <<'REMOTE'
set -euo pipefail
label() { docker inspect -f "{{index .Config.Labels \"$1\"}}" "$CONTAINER"; }
dir=$(label com.docker.compose.project.working_dir)
files=$(label com.docker.compose.project.config_files)
service=$(label com.docker.compose.service)
[ -n "$dir" ] && [ -n "$files" ] && [ -n "$service" ] || { echo "==> $CONTAINER is not managed by docker compose" >&2; exit 1; }
cd "$dir"
if command -v direnv >/dev/null 2>&1; then eval "$(DIRENV_LOG_FORMAT= direnv export bash)"; fi
args=()
IFS=, read -ra list <<<"$files"
for f in "${list[@]}"; do args+=(-f "$f"); done
export "$TAG_VAR=$TAG"
resolved=$(docker compose "${args[@]}" config "$service" | sed -n 's/^[[:space:]]*image:[[:space:]]*//p')
if [ "$resolved" != "$IMAGE:$TAG" ]; then
  echo "==> The compose file does not honour \$$TAG_VAR: service '$service' resolves to '$resolved', expected '$IMAGE:$TAG'." >&2
  echo "==> In the server compose file set:  image: $IMAGE:\${$TAG_VAR:-latest}" >&2
  exit 1
fi
docker compose "${args[@]}" pull "$service"
docker compose "${args[@]}" up -d --no-deps "$service"
REMOTE
}

# Wait until $CONTAINER is healthy and its image label $1 equals $2 ($3 = description)
verify() {
  local label=$1 want=$2 what=$3 out state="" got=""
  echo "==> Waiting for $CONTAINER to be healthy on $what (up to 5 minutes)"
  for _ in $(seq 1 60); do
    out=$(ssh "$HOST" "$REMOTE_PATH"'; docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} {{index .Config.Labels \"'"$label"'\"}}" '"$CONTAINER" 2>/dev/null || true)
    state=${out%% *}
    got=${out#* }
    if { [ "$state" = "healthy" ] || [ "$state" = "running" ]; } && [ "$got" = "$want" ]; then
      ssh "$HOST" "$REMOTE_PATH"'; docker inspect -f "image {{.Config.Image}}{{println}}revision {{index .Config.Labels \"org.opencontainers.image.revision\"}}{{println}}version {{index .Config.Labels \"org.opencontainers.image.version\"}}{{println}}started {{.State.StartedAt}}" '"$CONTAINER"
      echo "==> $CONTAINER is $state on $what"
      exit 0
    fi
    sleep 5
  done
  echo "==> Timed out: $CONTAINER is '${state:-missing}' on '${got:-?}', expected $want. Last 30 log lines:" >&2
  ssh "$HOST" "$REMOTE_PATH"'; docker logs --tail 30 '"$CONTAINER" || true
  exit 1
}

if [ -n "$TARGET" ] && [ "$TARGET" != main ]; then
  # ---- Branch / PR mode: deploy the head of a branch as image tag sha-<7> ----
  BRANCH=$TARGET
  if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
    BRANCH=$(gh pr view "$TARGET" --json headRefName --jq .headRefName) || die "cannot resolve PR #$TARGET"
    echo "==> PR #$TARGET is branch $BRANCH"
  fi
  git fetch -q origin "$BRANCH" || die "cannot fetch branch $BRANCH from origin"
  SHA=$(git rev-parse "origin/$BRANCH")
  if [ "$(git rev-parse HEAD)" != "$SHA" ]; then
    echo "==> Warning: local HEAD ($(git rev-parse --short HEAD)) is not origin/$BRANCH (${SHA:0:7}); deploying origin/$BRANCH" >&2
  fi
  if [ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] && [ -n "$(git rev-list "origin/$BRANCH..HEAD" | head -n1)" ]; then
    echo "==> Warning: $BRANCH has unpushed local commits; they are NOT deployed" >&2
  fi
  TAG="sha-${SHA:0:7}"
  echo "==> Deploying $BRANCH @ ${SHA:0:7} as $IMAGE:$TAG"

  if [ -z "$SKIP_WAIT" ]; then
    built=""
    for wf in preview.yml release.yml; do
      built=$(gh run list --workflow "$wf" --commit "$SHA" --status success --json databaseId --jq '.[0].databaseId // empty')
      [ -n "$built" ] && break
    done
    if [ -n "$built" ]; then
      echo "==> Image $TAG already built (run $built)"
    else
      last=$(gh run list --workflow preview.yml --limit 1 --json databaseId --jq '.[0].databaseId // 0')
      echo "==> Building $TAG with the Preview image workflow"
      gh workflow run preview.yml --ref "$BRANCH" || die "cannot dispatch preview.yml on $BRANCH (it must exist on the default branch)"
      wait_for_run preview.yml "$SHA" "$last"
    fi
  fi

  compose_recreate "$TAG"
  verify org.opencontainers.image.revision "$SHA" "revision ${SHA:0:7} ($BRANCH)"
fi

# ---- Main mode ----
# 1. HEAD must be what origin/main has: only released commits are deployed
git fetch -q --tags origin main
SHA=$(git rev-parse HEAD)
[ "$SHA" = "$(git rev-parse origin/main)" ] ||
  die "HEAD ($(git rev-parse --short HEAD)) is not origin/main ($(git rev-parse --short origin/main)). Merge your PR and 'git pull' first."

# 2. wait for the Release workflow of that commit
if [ -z "$SKIP_WAIT" ]; then
  echo "==> Waiting for the Release workflow of ${SHA:0:7}"
  wait_for_run release.yml "$SHA"
  git fetch -q --tags origin
fi
TAG=$(git tag --points-at "$SHA" --list 'v*' --sort=-v:refname | head -n1)
[ -n "$TAG" ] || die "no v* tag points at ${SHA:0:7} (was it released?)"
VERSION=${TAG#v}
echo "==> Released version: $VERSION"

# 3. a preview is running: recreate on :latest; otherwise ask Watchtower to pull now
#    (POST /v1/update?image=..., Bearer token read on the server, never printed)
current=$(ssh "$HOST" "$REMOTE_PATH"'; docker inspect -f "{{.Config.Image}}" '"$CONTAINER" 2>/dev/null || true)
if [ -n "$current" ] && [ "$current" != "$IMAGE:latest" ]; then
  echo "==> $CONTAINER runs $current (a preview): going back to $IMAGE:latest"
  compose_recreate latest
else
  echo "==> Asking Watchtower on $HOST to update $IMAGE"
  ssh "$HOST" "$REMOTE_PATH"'; cd ~/etc && eval "$(DIRENV_LOG_FORMAT= direnv export bash)" && test -n "$ACME_DOMAIN" && test -n "$WATCHTOWER_HTTP_API_TOKEN" && curl -fsS --max-time 600 -X POST -H "Authorization: Bearer $WATCHTOWER_HTTP_API_TOKEN" "https://watchtower.$ACME_DOMAIN/v1/update?image='"$IMAGE"'"'
  echo
fi

# 4. wait until the container is healthy and runs the released version
verify org.opencontainers.image.version "$VERSION" "version $VERSION"
