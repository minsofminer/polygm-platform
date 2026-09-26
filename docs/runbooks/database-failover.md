---
id: database-failover
order: 9
severity: SEV1
owner: on-call (platform)
alarms: []
last_drilled: 2026-09-23
---

# Database failover

**One line:** the database is the product's memory: intents, attempts, fills, the ledger. Losing it stops trading;
coming back from a stale copy silently loses orders, which is worse than stopping.

No alarm points here directly: a database incident reaches this page through `executor-down` (a dead executor is
usually a dead pool), `/readyz` failing, or an owner-side replication-lag probe. That last probe is listed in the
owner steps — it needs the provider's connection string, not just the API host.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* the API's `/readyz` is not ready, or 5xxs with a database-shaped envelope;
* `executor.lastBeatAgeMs` climbing because the executor cannot reach its store;
* the pooler's logs showing saturated pools, or queries queued behind a lock.

```bash
curl -sS "$API/readyz" | python3 -m json.tool
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(d["executor"], d["orderPath"]["intentsByState"])'
```

## Diagnosis

1. **Managed (Neon) or self-hosted (CNPG)?** The procedure differs, and the decision is P15 D2's: Neon at launch,
   with the failure modes written down. Read the provider's status page before touching anything.

```bash
psql "$PGM_DB_URL" -c "SELECT pg_is_in_recovery(), now();"
psql "$PGM_DB_URL" -c "SELECT count(*), max(now() - pg_stat_activity.query_start) FROM pg_stat_activity WHERE state <> 'idle';"
```

2. **Is it the database, the pooler, or the pool?** `pgbouncer` in transaction mode sits in front of everything;
   a saturated pool looks exactly like a dead database from the application's side.

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml ps postgres pgbouncer
docker compose -f /srv/polygm/docker-compose.prod.yml logs --tail=100 pgbouncer | grep -iE "pool|error"
psql "$PGM_DB_URL" -c "SHOW max_connections;" -c "SELECT count(*) FROM pg_stat_activity;"
```

3. **What could be lost by a failover?** Answer this *before* promoting anything. Our RPO claim is that no order
   intent may be lost, which is only true if the intent rows reached the primary.

```bash
psql "$PGM_DB_URL" -c "SELECT pid, state, xact_start, query_start, left(query,80) FROM pg_stat_activity
   WHERE state='active' ORDER BY xact_start LIMIT 10;"
```

## Remediation

* **Managed failover:** let the provider promote. Announcing it matters more than performing it: the executor must
  **not** be restarted during the failover window if any ambiguous order exists — a restart would be the second
  event in an incident that already has one.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/drain-status" | python3 -m json.tool
python3 tools/p15-drain-guard.py --api "$API" --token "$PGM_ADMIN_TOKEN" --set-draining --reason "database failover in progress"
```

* **Self-hosted (CNPG):** the cluster promotes on its own; your job is to confirm the new primary and to keep
  probes honest (`/readyz` must be red while the pool is not usable).

```bash
kubectl -n polygm get cluster polygm -o jsonpath='{.status.currentPrimary}'; echo
kubectl -n polygm get pods -l cnpg.io/cluster=polygm
```

* **If the pooler is the failure:** restart it, not the database. A pooler restart is invisible to correctness.

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml restart pgbouncer
```

* **After any failover, reconcile before trading.** The reconciler is the only component allowed to decide that
  the world is consistent again.

```bash
python3 -m services.executor.main --db "$PGM_DB_PATH" --reconcile-only --once
python3 tools/p15-drain-guard.py --api "$API" --token "$PGM_ADMIN_TOKEN" --clear-draining --reason "failover complete, reconcile clean"
```

## Verification

* `/readyz` is 200, and a write-read round trip has the expected latency from the API host.
* `reconcile_open` is empty, and `money.ordersUnknownState.count` is 0.
* The restore point is recorded: run `python3 tools/p15-restore-drill.py --json /tmp/restore.json` and keep the
  output — the P15 D7 rule is that a restore is only real once it has a date and a result.
* If the failover promoted a replica: note the LSN delta in the incident channel. It is the size of the window we
  cannot see, and pretending it is zero is how a lost order becomes a mystery.

## Escalation

* Platform on-call immediately; money-path on-call in parallel, because a database incident is a trading incident.
* Provider support for a managed failover that does not complete in its stated window.
* If any committed intent is missing after the failover, this is a data-loss incident, and the owner decides what
  users are told — before the support queue tells them instead.
