---
id: cost-spike
order: 13
severity: SEV3
owner: platform owner
alarms: [cost-spike, disk-filling]
last_drilled: 2026-09-23
---

# Cost spike

**One line:** the bill is climbing faster than the traffic justifies. It is SEV3 because nothing breaks today —
and it is on this list because the first version of every system that dies at scale dies of an unbounded cost
curve while its dashboards are green.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV3 `cost-spike`: the projection crosses a budget gate (50% / 80% / 100% of the $300 envelope).
* SEV3 `disk-filling`: a host's disk passes 85% full.
* One service's share moving out of proportion with its traffic.

```bash
python3 tools/p15-cost.py --project
df -h | sort -k5 -h | tail -5
docker system df
```

## Diagnosis

1. **Where the money is going**, line by line, from the cost attribution in `config/costs.json`: compute, database,
   cache, storage, and the analytics tape. A spike in one of those has four different remedies.
2. **Traffic-shaped or not?** Compare the cost curve with `business.fills24h` and `business.traders24h`. A spike
   with flat traffic is a bug (a retry loop, a poller that lost its interval, a retention job that stopped).

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["business"])'
psql "$PGM_DB_URL" -c "SELECT pg_size_pretty(pg_database_size(current_database()));"
psql "$PGM_DB_URL" -c "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_catalog.pg_statio_user_tables
   ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"
```

3. **The tape is usually the answer.** `fills` and `book_levels` grow with the market, not with us, and a retention
   window that stopped pruning is a real cause. Check row counts against the retention policy before you change a
   plan.

## Remediation

The cut order is decided in advance, and it is in `config/costs.json`:

1. **Analytics retention first** — shorten the rollup window. Losing analysis is not losing anything.
2. **Long-tail market tracking second** — stop polling the markets nobody trades.
3. **Never** the executor, the money path, the ledger, or their backups. A cheap money path is an expensive
   incident.

```bash
# the first cut, as a flag rather than a code change — and with the reason recorded
curl -sS -X POST "${ADM[@]}" -H 'content-type: application/json' \
  -d '{"name":"tape_backfill_enabled","value":false,"reason":"cost: long-tail backfill paused at 80% of the monthly budget"}' \
  "$API/v1/admin/flags" | python3 -m json.tool
```

## Verification

* The projection drops below the gate that was crossed, and the projection is re-run with the real per-line usage:
  `python3 tools/p15-cost.py --project --usage neon-postgres=110.50`.
* Traffic metrics are unchanged: if `fills24h` fell, you cut product, not cost — put it back.
* The flag change is in `flag_audit` with a reason and a date, and there is a ticket to restore it.

## Escalation

* Platform owner, next business day for SEV3.
* Immediately if the projection crosses 100% of the envelope: that is a decision about the product's runway, and it
  belongs to the owner, not the on-call.
