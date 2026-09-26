---
id: cache-queue-age
order: 16
severity: SEV2
owner: on-call (platform)
alarms: [cache-age-high, queue-depth-high]
last_drilled: 2026-09-23
---

# Cache age high / queue depth growing

**One line:** a cache serving stale data or a queue that stops draining. Neither is a money bug by itself; both
turn into one if they are left alone long enough to become the *only* source of truth for something.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV2 `cache-age-high`: cache age above five minutes while the hit rate stays high — the dangerous combination,
  because a stale cache that is still hitting looks healthy.
* SEV2 `queue-depth-high`: more than 200 intents queued for the executor.
* In the product: prices or positions that do not move, or a "last updated" that is older than the clock.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(d["freshness"], d["orderPath"]["intentsByState"])'
redis-cli -u "$PGM_REDIS_URL" INFO stats | grep -E "keyspace_hits|keyspace_misses|evicted_keys"
redis-cli -u "$PGM_REDIS_URL" --scan --pattern 'pgm:*' | head -20
```

## Diagnosis

1. **Hit rate *and* age together.** A high hit rate with a high age means the entries are not expiring. Redis in
   this stack runs with `--save "" --appendonly no --maxmemory 256mb`, so eviction is the capacity limit and the
   TTLs are the correctness limit.

```bash
redis-cli -u "$PGM_REDIS_URL" --scan --pattern 'pgm:book:*' | head -5 | while read k; do echo "$k"; redis-cli -u "$PGM_REDIS_URL" TTL "$k"; done
docker compose -f /srv/polygm/docker-compose.prod.yml ps redis
```

2. **Is the writer dead or just slow?** The cache is written by ingest and by the API's read-through path; a dead
   writer is an incident, a slow one is a capacity question.

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 15m ingest | grep -i "cache\|redis" | tail -20
```

3. **What reads the stale value?** This is the question that decides severity: a stale *price* is refused by the
   gate (`STALE_QUOTE`), but a stale *position* would be a money bug if anything traded on it. Check the readers
   before you decide this is cosmetic.

## Remediation

* **Invalidate rather than delete the cache process.** Killing Redis empties everything and hands the database a
  cold-start stampede — the failure that turns a two-minute problem into a twenty-minute one.

```bash
redis-cli -u "$PGM_REDIS_URL" --scan --pattern 'pgm:book:*' | head -1000 | xargs -r -n 50 redis-cli -u "$PGM_REDIS_URL" DEL
```

* **A queue that is not draining:** is the consumer alive, and is it stuck on one row? A stuck consumer usually
  means a poison message, which is a diagnosis, not a retry loop.

```bash
psql "$PGM_DB_URL" -c "SELECT id, state, user_id, updated_ms FROM order_intents
   WHERE state = 'queued' ORDER BY updated_ms LIMIT 5;"
```

* **If Redis is the bottleneck:** the cache is a cache — the API must survive its absence. If it does not, that is
  a finding to record, not a knob to turn at 2am.

## Verification

* Cache age is under its threshold, the hit rate is still healthy, and one product page shows a moving number.
* Queue depth returns to its baseline and the consumer's own log shows forward progress.
* Nothing money-shaped changed: `money.unreconciled.count` and `ordersUnknownState.count` are still 0.

## Escalation

* Platform on-call, SEV2, hours.
* Escalate if the queue is on the money path (order intents, fills, reconciliation): that is a money-path page,
  and the on-call there decides whether to drain and stop trading.
