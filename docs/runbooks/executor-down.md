---
id: executor-down
order: 2
severity: SEV1
owner: on-call (money path)
alarms: [executor-down]
last_drilled: 2026-09-23
---

# Executor down / crash loop

**One line:** the only process that signs orders stopped beating. Nothing is lost — the queue is durable rows in
Postgres — but nothing new is being placed either, and *how* it stopped (clean stop, crash loop, OOM kill) decides
what you do next.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `executor-down`: `executor.lastBeatAgeMs` above 90 s (three missed beats at a 30-second cadence).
* `orderPath.intentsByState.queued` climbing while `submitted` does not.
* `docker compose -f /srv/polygm/docker-compose.prod.yml ps executor` showing `Restarting` or `Exited`.

## Diagnosis

1. **Is it down, or is it blind?** The heartbeat is written by the executor, the read comes from the API. A stale
   heartbeat with a live container means the *database write path* is broken, not the process.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["executor"])'
docker compose -f /srv/polygm/docker-compose.prod.yml ps
docker compose -f /srv/polygm/docker-compose.prod.yml logs --tail=200 executor
```

2. **Why it stopped.** In order of likelihood: crash loop after a deploy, OOM kill, database connection failure, a
   signing-provider error it treated as fatal.

```bash
docker inspect --format '{{.State.OOMKilled}} {{.RestartCount}} {{.State.ExitCode}}' "$(docker ps -aqf name=executor)"
journalctl -u polygm-executor --since '-30 min' --no-pager | tail -50
```

3. **What is queued, and is any of it in flight?** This decides between the two remediation paths, and it is the
   P06 D3 property that outlives this phase.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/drain-status" | python3 -m json.tool
psql "$PGM_DB_URL" -c "SELECT state, count(*) FROM order_intents
   WHERE state IN ('pending','queued','submitting','uncertain','submitted') GROUP BY state;"
psql "$PGM_DB_URL" -c "SELECT client_order_hash, signed_ms, submitted_ms, ack_ms FROM order_attempts
   WHERE ack_ms IS NULL ORDER BY signed_ms DESC LIMIT 20;"
```

   An attempt with `signed_ms` but no `submitted_ms`, or with `submitted_ms` and no `ack_ms`, is an **ambiguous
   order**: it may exist at the venue. Never bring up a replacement executor with a clean queue while one exists.
   Reconcile first.

## Remediation

* **Clean stop, no ambiguous rows:** restart. The executor resumes from `order_intents` and cannot double-submit,
   because `client_order_hash` is the primary key of `order_attempts`.

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml up -d executor
docker compose -f /srv/polygm/docker-compose.prod.yml logs -f --tail=50 executor
```

* **Crash loop after a deploy:** roll back the executor image only. The API stays up, and the drain protocol is
  what makes that safe.

```bash
deploy/rollback.sh --env prod --component executor        # prints the digest it is restoring, and why
python3 tools/p15-drain-guard.py --api "$API" --token "$PGM_ADMIN_TOKEN" --clear-draining --reason "rollback complete"
```

* **Ambiguous rows exist:** do **not** restart blind. Resolve them first, then start the executor. If the
  reconciler cannot resolve them, engage the kill switch and escalate to the money-path owner.

```bash
python3 -m services.executor.main --db "$PGM_DB_PATH" --reconcile-only --once      # resolve, do not place
```

* **It died because the database was unreachable:** that is a database incident wearing an executor costume — go
  to `docs/runbooks/database-failover.md`. The executor beats again on its own once the DB answers.

## Verification

* `executor.lastBeatAgeMs < 40_000` on two consecutive reads.
* `drain-status` reports `draining: false` and `safeToReplace: true`.
* `python3 tools/p15-synthetic.py --api "$API" --user u-synth --once --await 25` exits 0 — a real order end to
  end, not a health check that only proves the process started.

## Escalation

* First fifteen minutes: the money-path on-call. After that, also the incident channel, with `intentsByState`.
* If you rolled back, say so, with the digest. The deploy audit is the record: a rollback that is not in it did
  not happen.
