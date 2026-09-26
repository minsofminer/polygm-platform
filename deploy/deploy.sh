#!/usr/bin/env bash
# P15 D3 — one command to deploy, and one command to undo it.
#
#   deploy/deploy.sh --env staging --api-image ghcr.io/.../polygm-api@sha256:... \
#                    --executor-image ghcr.io/.../polygm-executor@sha256:... --host api.staging.example
#
# The properties this script is written to have, in the order they matter:
#
#   1. **The executor is replaced only when the drain guard says it is safe.** Not "after the API is up", not
#      "because the deploy is running late" — the guard's answer, and the guard fails closed.
#   2. **The API is replaced without dropping a request.** Draining flag on, wait until the running API reports
#      no blocking intents, `up -d --no-deps api`, wait for /readyz, draining flag off. The API is stateless
#      apart from Postgres, so two API containers briefly overlapping is fine; the executor is not, so it never
#      overlaps.
#   3. **What runs is a digest, never a tag.** Whatever the pipeline built is what this box runs, and the digest
#      is written back to deploy/image-digests.txt *before* the containers change, so the file describes the
#      deploy even if the deploy dies halfway through.
#   4. **The ledger records the attempt.** begin before, finish after, with the note a human would have written.
#      A deploy that fails is recorded too: the interesting deploys are the ones that went wrong.
#
# Requires: docker, docker compose v2, curl, a checkout of this repo on the target host, and an admin token for
# the API it is about to drain. Reads /etc/polygm/*.env (0600, written by the platform, never in git).
set -eu

ENV_NAME=""
API_IMAGE=""
EXECUTOR_IMAGE=""
HOST=""
COMPOSE_FILE="docker-compose.prod.yml"
DRAIN_TIMEOUT="${PGM_DRAIN_TIMEOUT:-300}"
REASON=""
ACTOR="${PGM_DEPLOY_ACTOR:-$(whoami 2>/dev/null || echo ci)}"
SKIP_EXECUTOR=""
ADMIN_TOKEN="${PGM_ADMIN_TOKEN:-}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

die() { printf '%s\n' "deploy: $*" >&2; exit 1; }
say() { printf '%s\n' "deploy: $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV_NAME="$2"; shift 2 ;;
    --api-image) API_IMAGE="$2"; shift 2 ;;
    --executor-image) EXECUTOR_IMAGE="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --reason) REASON="$2"; shift 2 ;;
    --drain-timeout) DRAIN_TIMEOUT="$2"; shift 2 ;;
    --skip-executor) SKIP_EXECUTOR=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$ENV_NAME" ] || die "--env is required (staging|canary|prod)"
[ -n "$API_IMAGE" ] || die "--api-image is required (a digest reference, not a tag)"
[ -n "$HOST" ] || die "--host is required (the API base URL the drain guard should talk to)"
case "$API_IMAGE" in *"@sha256:"*) ;; *) die "the api image must be pinned by digest: $API_IMAGE" ;; esac
if [ -n "$EXECUTOR_IMAGE" ]; then
  case "$EXECUTOR_IMAGE" in *"@sha256:"*) ;; *) die "the executor image must be pinned by digest: $EXECUTOR_IMAGE" ;; esac
fi
[ -n "$ADMIN_TOKEN" ] || die "PGM_ADMIN_TOKEN is not set; the drain guard cannot ask the running system anything without it"

cd "$REPO_DIR"
say "environment=$ENV_NAME host=$HOST actor=$ACTOR"
say "api=$API_IMAGE"
[ -n "$EXECUTOR_IMAGE" ] && say "executor=$EXECUTOR_IMAGE"

# ---------------------------------------------------------------- 1. the change set and its migrations
if command -v python3 >/dev/null 2>&1; then
  say "migration check (expand-contract only):"
  python3 tools/p15-migration-check.py --since "${PGM_DEPLOY_SINCE:-origin/main}" || die "a migration step is not allowed yet"
fi

# ---------------------------------------------------------------- 2. which container this deploy may touch
# `--skip-executor` is how an API-only rollback or a hotfix avoids the money path entirely. It is an argument
# rather than a heuristic because "the diff looked like API only" is not something a shell script should guess.
if [ -n "$SKIP_EXECUTOR" ]; then say "executor: NOT part of this deploy (--skip-executor)"; fi

# ---------------------------------------------------------------- 3. write the digests before anything moves
DIGESTS_FILE="deploy/image-digests.txt"
cp -f "$DIGESTS_FILE" "${DIGESTS_FILE%.txt}.previous.txt" 2>/dev/null || : > "${DIGESTS_FILE%.txt}.previous.txt"
python3 - "$API_IMAGE" "$EXECUTOR_IMAGE" <<'PY' || die "could not record the digests"
import datetime as dt, pathlib, sys
api, executor = sys.argv[1], sys.argv[2]
p = pathlib.Path("deploy/image-digests.txt")
head = []
if p.exists():
    head = [l for l in p.read_text().splitlines() if l.startswith("#") or not l.strip()]
stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
rows = [("api", api)]
if executor:
    rows.append(("executor", executor))
body = "\n".join("%s %s %s %s" % (n, ref, stamp, "deploy.sh") for n, ref in rows)
p.write_text("\n".join(head) + ("\n" if head else "") + body + "\n")
print("recorded %d digest(s)" % len(rows))
PY

# ---------------------------------------------------------------- 4. the audit ledger
DEPLOY_ID=""
if command -v python3 >/dev/null 2>&1; then
  DEPLOY_ID="$(python3 tools/p15-deploy-audit.py begin --env "$ENV_NAME" --actor "$ACTOR" \
      --reason "${REASON:-deploy}" | tail -n 1)"
  say "ledger: $DEPLOY_ID"
fi

finish() {  # $1 = ok|failed|rolled-back, $2 = note
  if [ -n "$DEPLOY_ID" ] && command -v python3 >/dev/null 2>&1; then
    python3 tools/p15-deploy-audit.py finish --id "$DEPLOY_ID" --result "$1" --note "$2" --actor "$ACTOR" >/dev/null || true
  fi
}

export POLYGM_API_IMAGE="$API_IMAGE"
[ -n "$EXECUTOR_IMAGE" ] && export POLYGM_EXECUTOR_IMAGE="$EXECUTOR_IMAGE"

# ---------------------------------------------------------------- 5. the API: drain, wait, replace
say "api: turning the draining flag on"
python3 tools/p15-drain-guard.py --api "$HOST" --token "$ADMIN_TOKEN" --set-draining \
    --reason "deploy $ENV_NAME ${REASON:-}" --timeout "$DRAIN_TIMEOUT" || {
        finish failed "drain guard refused before the api replace"; die "not deploying: the system is not in a state where replacing it is safe"; }

say "api: replacing the container"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate api || { finish failed "compose up api failed"; die "api replace failed"; }

i=0
until docker compose -f "$COMPOSE_FILE" exec -T api python3 -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz',timeout=2).status==200 else 1)" 2>/dev/null; do
  i=$((i + 1)); [ "$i" -lt 30 ] || { finish failed "api never became ready"; die "/readyz never went green after the replace"; }
  sleep 2
done
say "api: ready"

# ---------------------------------------------------------------- 6. the executor: only with a safe answer
if [ -n "$EXECUTOR_IMAGE" ] && [ -z "$SKIP_EXECUTOR" ]; then
  say "executor: asking the drain guard (this is the money path; the guard fails closed)"
  python3 tools/p15-drain-guard.py --api "$HOST" --token "$ADMIN_TOKEN" --timeout "$DRAIN_TIMEOUT" || {
      finish failed "executor replace skipped: the drain guard never saw a safe state"
      die "the api is deployed; the EXECUTOR IS NOT. Run P6 D3 reconciliation, then re-run with --skip-executor off."; }
  say "executor: replacing the container"
  docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate executor || { finish failed "compose up executor failed"; die "executor replace failed"; }
  sleep 5
  docker compose -f "$COMPOSE_FILE" ps executor | grep -qi "up" || { finish failed "executor did not come up"; die "the executor did not come up"; }
fi

# ---------------------------------------------------------------- 7. hand the switch back
say "clearing the draining flag"
python3 tools/p15-drain-guard.py --api "$HOST" --token "$ADMIN_TOKEN" --clear-draining \
    --reason "deploy $ENV_NAME complete" --timeout 60 >/dev/null || say "warning: could not clear the draining flag; clear it by hand"

finish ok "deployed ${API_IMAGE##*@} api; executor ${EXECUTOR_IMAGE:+replaced}${EXECUTOR_IMAGE:-untouched}"
say "done. deploy id $DEPLOY_ID"
