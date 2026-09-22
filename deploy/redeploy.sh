#!/usr/bin/env bash
# Manual full redeploy on the droplet. Use after a node-v* release or any compose/
# Caddyfile/.env change. edge & adapter deploy themselves on release (CI -> SSH ->
# deploy-service.sh), so for those you don't need this — it's the all-services path.
#
#   ssh root@<droplet> 'bash /opt/emer-ai-tools/deploy/redeploy.sh'
set -euo pipefail

# Serialise the deploy paths. CI's deploy-service.sh and the emer-deploy-sync
# timer both git-pull this same checkout, and the timer fires every two minutes,
# so they overlap sooner or later — git then fails to lock
# refs/remotes/origin/main and the deploy dies halfway. Observed twice on
# 2026-09-22, each time looking like an unrelated git problem.
exec 9>/var/lock/emer-deploy.lock
flock -w 300 9 || { echo "another deploy holds /var/lock/emer-deploy.lock" >&2; exit 1; }

REPO=/opt/emer-ai-tools
COMPOSE="docker compose -f docker-compose.droplet.yaml --env-file .env"

git -C "$REPO" pull --ff-only
cd "$REPO/deploy"
$COMPOSE pull
$COMPOSE up -d --remove-orphans
# The Caddyfile is a single-file bind mount: when git pull replaces it the inode
# changes and the running container keeps the old one (a `caddy reload` would just
# re-read the stale inode). Force-recreate caddy so it re-binds the current file.
# (Static files in ../site are a directory mount and update live — no recreate.)
$COMPOSE up -d --force-recreate caddy
docker image prune -f
echo "--- status ---"
$COMPOSE ps
