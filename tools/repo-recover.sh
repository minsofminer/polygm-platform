#!/usr/bin/env bash
# Put this checkout back on the truth, without losing a byte of work in progress.
#
# Why this exists: this workspace has been reset six times during the build. Each time, the same three things vanish —
# pip's site-packages, `web/node_modules`, and `.git/config` (so the `origin` remote disappears) — and the local
# branch can be left on an older commit than the remote. The visible symptom is a `git status` full of files that look
# like they were never committed, which reads as "the last three hours of work are gone" when in fact the work is on
# the remote and the *branch pointer* is behind.
#
# What it does, in order, and why each step is not optional:
#
#   1. `git ls-remote` FIRST. The remote is the truth; the local reflog is not, because the reset that moved HEAD also
#      removed the reflog entries that would explain it.
#   2. A `backup-pre-restore-N` branch before anything moves. It is never pushed — it exists so that a working tree
#      carrying uncommitted work cannot lose it to a bad reset.
#   3. `reset --mixed`, never `--hard`. `--mixed` moves the branch and the index and leaves the working tree exactly
#      as it is, which is what makes "the files are all still here" true. `--hard` would discard the very work this
#      script exists to protect, and `git restore --source=HEAD` after a rewind restores the *rewound* version.
#   4. `git status` last, so the operator sees what is genuinely uncommitted rather than what the rewind implied.
#
# Usage: tools/repo-recover.sh            (dry run: shows what it would do)
#        tools/repo-recover.sh --apply
set -euo pipefail

BRANCH="${PGM_RECOVER_BRANCH:-main}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

cd "$(dirname "$0")/.."

if ! git remote get-url origin >/dev/null 2>&1; then
  # Two places a token legitimately lives in this workspace, tried in order: gh's own config, and the operator's
  # `~/.secrets/tokens.env` (which is where this build keeps them and which is gitignored and outside the repo).
  # The `gh auth git-credential` helper is a third option and was the only one for a while, but it fails with
  # "Permission denied" whenever the gh binary's own state is missing — and a recovery script that cannot add a
  # remote is a recovery script that cannot recover.
  TOKEN_FILE="$HOME/.config/gh/hosts.yml"
  TOKEN=""
  if [ -f "$TOKEN_FILE" ]; then
    TOKEN=$(sed -n 's/.*oauth_token:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$TOKEN_FILE" | head -1)
  fi
  if [ -z "$TOKEN" ] && [ -f "$HOME/.secrets/tokens.env" ]; then
    TOKEN=$(sed -n 's/^export GH_TOKEN=["'"'"']\?\([^"'"'"'[:space:]]*\).*/\1/p' "$HOME/.secrets/tokens.env" | head -1)
  fi
  [ -n "$TOKEN" ] || { echo "no origin remote and no token in $TOKEN_FILE or ~/.secrets/tokens.env" >&2; exit 1; }
  echo "+ git remote add origin github.com/minsofminer/polygm-platform"
  [ "$APPLY" = "1" ] && git remote add origin "https://x-access-token:$TOKEN@github.com/minsofminer/polygm-platform.git"
fi

echo "+ git fetch origin $BRANCH"
[ "$APPLY" = "1" ] && git fetch origin "$BRANCH" >/dev/null 2>&1

REMOTE=$(git ls-remote origin -h "refs/heads/$BRANCH" | awk '{print $1}')
LOCAL=$(git rev-parse HEAD)
echo "  remote $BRANCH = ${REMOTE:-<none>}"
echo "  local  HEAD    = $LOCAL"

if [ -z "${REMOTE:-}" ]; then
  echo "the remote has no $BRANCH yet; nothing to recover onto" >&2
  exit 0
fi
if [ "$REMOTE" = "$LOCAL" ]; then
  echo "already level with the remote; nothing to do"
  git status --short | head -30
  exit 0
fi
if git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
  echo "local is BEHIND the remote: a plain reset is safe (working tree is untouched by --mixed)"
else
  echo "local and remote have DIVERGED — inspect before resetting:" >&2
  git log --oneline "$REMOTE..$LOCAL" | head -10 >&2
fi

N=1
while git rev-parse --verify --quiet "refs/heads/backup-pre-restore-$N" >/dev/null; do N=$((N + 1)); done
echo "+ git branch backup-pre-restore-$N   (local only; never pushed)"
[ "$APPLY" = "1" ] && git branch "backup-pre-restore-$N" HEAD

echo "+ git reset --mixed origin/$BRANCH"
[ "$APPLY" = "1" ] && git reset --mixed "origin/$BRANCH" >/dev/null

echo
echo "recovered. what is actually uncommitted:"
git status --short | head -40
echo
echo "the rest is environment, not history: reinstall with"
echo "  pip install -r requirements.txt -r requirements-dev.txt   # if the API tests cannot import"
echo "  (cd web && npm install)                                   # if web tests/build cannot find tools"
