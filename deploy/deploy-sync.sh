#!/usr/bin/env bash
# Pull-based config sync, run on a systemd timer (~every 2 min). The droplet pulls
# main and reconciles its *config* to the new state. (Image rollouts are pushed
# separately by CI over SSH — see deploy-service.sh — so they need no timer.)
#
#   site/ change                -> nothing: a directory mount, Caddy serves it live
#   Caddyfile change            -> recreate caddy (single-file mount, inode changes)
#   compose change              -> re-apply the whole stack
#   edge/adapter image changes  -> deployed separately by CI over SSH (deploy-service.sh)
#
# Install (once, on the droplet):
#   cp /opt/emer-ai-tools/deploy/systemd/emer-deploy-sync.* /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now emer-deploy-sync.timer
set -euo pipefail

# Serialise the deploy paths. CI's deploy-service.sh and the emer-deploy-sync
# timer both git-pull this same checkout, and the timer fires every two minutes,
# so they overlap sooner or later — git then fails to lock
# refs/remotes/origin/main and the deploy dies halfway. Observed twice on
# 2026-09-22, each time looking like an unrelated git problem.
exec 9>/var/lock/emer-deploy.lock
flock -w 300 9 || { echo "another deploy holds /var/lock/emer-deploy.lock" >&2; exit 1; }

REPO=/opt/emer-ai-tools
cd "$REPO"

# steledger.com lives in its own repository and is served from its own checkout.
# Pull it here, before the early exit below — that exit fires whenever the gateway
# repo has nothing new, which is most ticks, and the site would then never update.
# dist/ is a directory bind mount, so a pull is the whole deployment.
if [ -d /opt/steledger.com/.git ]; then
  git -C /opt/steledger.com pull --ff-only -q || \
    logger -t emer-deploy-sync "steledger.com pull failed"
fi

git fetch -q origin main
before=$(git rev-parse HEAD)
after=$(git rev-parse origin/main)
[ "$before" = "$after" ] && exit 0          # nothing new

if ! git merge --ff-only -q origin/main; then
  logger -t emer-deploy-sync "non-fast-forward; manual intervention needed"
  exit 1
fi

changed=$(git diff --name-only "$before" "$after")
cd "$REPO/deploy"
COMPOSE="docker compose -f docker-compose.droplet.yaml --env-file .env"

if echo "$changed" | grep -qE '^deploy/docker-compose'; then
  $COMPOSE up -d --remove-orphans
fi
# Caddyfile is a single-file bind mount: its inode changes on pull, so the running
# container keeps the old config -> force-recreate. (site/ is a directory mount and
# is served live, so site-only changes need no action.)
if echo "$changed" | grep -qE '^deploy/Caddyfile'; then
  $COMPOSE up -d --force-recreate caddy
fi

logger -t emer-deploy-sync "synced ${before:0:7} -> ${after:0:7}"
echo "synced ${before:0:7} -> ${after:0:7} (changed: $(echo "$changed" | tr '\n' ' '))"
