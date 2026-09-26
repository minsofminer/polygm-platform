#!/usr/bin/env bash
# P15 D3 — rollback, one command, under a minute, and specified for orders that are in flight.
#
#   deploy/rollback.sh --env prod --host https://api.example --to previous
#
# What "rollback" means here, precisely, because a rollback that is not specified is a coin flip:

#   * **The API** rolls back freely. It is stateless apart from Postgres (sessions are rows, not files), so the
#     previous image starts and serves immediately. Rollback is a `docker compose up -d --no-deps api` with the
#     previous digest.
#
#   * **The executor does NOT roll back freely, and this script will refuse to pretend otherwise.** If the new
#     executor has an order in `submitting`/`uncertain`, the order may exist at the venue and not in our database
#     — the P6 D3 ambiguity. Replacing the process that knows about it, with a version whose reconciliation logic
#     may differ, is how one ambiguous order becomes two. So the executor half is gated on the drain guard, and
#     when the guard cannot establish safety the script rolls back the API and STOPS, with the reason printed.
#
#   * **Money is never rolled back by a file.** No migration in this repo drops or rewrites a money column
#     without a `-- CONTRACT:` marker (tools/p15-migration-check.py enforces the window), so the previous image
#     runs against the current schema. If a contract step is what broke production, the rollback is a forward
#     deploy, not a restore, and the runbook says so.
#
#   * **A rollback is a deploy.** It is recorded in the same hash-chained ledger, with the measured duration, so
#     "we rolled back in 40 seconds" is a row and not a memory.
set -eu

ENV_NAME=""
HOST=""
TO="previous"
ACTOR="${PGM_DEPLOY_ACTOR:-$(whoami 2>/dev/null || echo ci)}"
ADMIN_TOKEN="${PGM_ADMIN_TOKEN:-}"
COMPOSE_FILE="docker-compose.prod.yml"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

die() { printf '%s\n' "rollback: $*" >&2; exit 1; }
say() { printf '%s\n' "rollback: $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV_NAME="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$ENV_NAME" ] || die "--env is required"
[ -n "$HOST" ] || die "--host is required"

cd "$REPO_DIR"
STARTED="$(date +%s)"
[ -f deploy/image-digests.previous.txt ] || die "no deploy/image-digests.previous.txt: there is nothing recorded to roll back TO"
say "rolling $ENV_NAME back to the digests recorded in deploy/image-digests.previous.txt"

API_IMAGE="$(awk '$1=="api"{print $2}' deploy/image-digests.previous.txt | tail -n 1)"
EXECUTOR_IMAGE="$(awk '$1=="executor"{print $2}' deploy/image-digests.previous.txt | tail -n 1)"
[ -n "$API_IMAGE" ] || die "the previous digest file has no api line"

DEPLOY_ID=""
if command -v python3 >/dev/null 2>&1; then
  DEPLOY_ID="$(python3 tools/p15-deploy-audit.py begin --env "$ENV_NAME" --actor "$ACTOR" --reason "ROLLBACK to ${API_IMAGE##*@}" | tail -n 1)"
  say "ledger: $DEPLOY_ID"
fi
finish() {
  if [ -n "$DEPLOY_ID" ] && command -v python3 >/dev/null 2>&1; then
    python3 tools/p15-deploy-audit.py finish --id "$DEPLOY_ID" --result "$1" --note "$2" --actor "$ACTOR" >/dev/null || true
  fi
}

export POLYGM_API_IMAGE="$API_IMAGE"
[ -n "$EXECUTOR_IMAGE" ] && export POLYGM_EXECUTOR_IMAGE="$EXECUTOR_IMAGE"

# 1. API first: it is the thing users are hitting, and it is the safe half.
say "api: replacing with $API_IMAGE"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate api || { finish failed "rollback: api replace failed"; die "api rollback failed"; }

i=0
until docker compose -f "$COMPOSE_FILE" exec -T api python3 -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz',timeout=2).status==200 else 1)" 2>/dev/null; do
  i=$((i + 1)); [ "$i" -lt 30 ] || { finish failed "rollback: api never became ready"; die "/readyz never went green during rollback"; }
  sleep 2
done
say "api: ready on the previous image"

# 2. The executor half, gated on the ambiguity question.
if [ -n "$EXECUTOR_IMAGE" ]; then
  say "executor: asking the drain guard whether an order may be in flight"
  if python3 tools/p15-drain-guard.py --api "$HOST" --token "$ADMIN_TOKEN" --timeout 120; then
    say "executor: replacing with $EXECUTOR_IMAGE"
    docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate executor \
      || { finish rolled-back "api rolled back; executor replace failed"; die "executor replace failed during rollback"; }
  else
    code=$?
    ELAPSED=$(( $(date +%s) - STARTED ))
    say "API IS ROLLED BACK; THE EXECUTOR IS NOT (guard exit $code, ${ELAPSED}s)."
    say "next: run P6 D3 reconciliation against the CURRENT executor, then re-run this script."
    finish rolled-back "api rolled back in ${ELAPSED}s; executor left alone: the drain guard refused (exit $code)"
    exit "$code"
  fi
fi

ELAPSED=$(( $(date +%s) - STARTED ))
say "rolled back in ${ELAPSED}s"
finish rolled-back "api+executor rolled back in ${ELAPSED}s to ${API_IMAGE##*@}"
[ "$ELAPSED" -lt 60 ] || say "NOTE: this rollback took ${ELAPSED}s, over the 60s budget — the monthly drill records the number"
exit 0
