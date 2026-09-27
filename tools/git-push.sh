#!/usr/bin/env bash
# Push to origin, with credentials that survive this sandbox.
#
# Two things in this workspace are outside the snapshot and therefore disappear on a reset: `.git/config`
# (the `origin` remote itself) and any stored credential. Both have already cost a push during a build loop.
# This script re-establishes both from `/home/user/.secrets/tokens.env` — which IS in the snapshot — and then
# pushes. It never writes the token into the repository, into `.git/config`, or to stdout.
#
# Usage: bash tools/git-push.sh [remote-url] [branch]
set -euo pipefail

REMOTE="${1:-https://github.com/minsofminer/polygm-platform.git}"
BRANCH="${2:-main}"
SECRETS="${PGM_SECRETS:-/home/user/.secrets/tokens.env}"

if [ ! -f "$SECRETS" ]; then
  echo "git-push: no secrets file at $SECRETS — cannot authenticate" >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "$SECRETS"; set +a
: "${GH_TOKEN:?git-push: GH_TOKEN is not set in $SECRETS}"

git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REMOTE"
git remote set-url origin "$REMOTE"
git push "https://x-access-token:${GH_TOKEN}@${REMOTE#https://}" "$BRANCH"
