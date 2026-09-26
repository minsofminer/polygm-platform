---
id: withdrawal-spike
order: 18
severity: SEV1
owner: on-call (money path)
alarms: [withdrawal-spike]
last_drilled: 2026-09-23
---

# Withdrawal spike

**One line:** users are leaving faster than usual. A spike in withdrawals is the earliest signal of a trust event —
something is wrong and the people who know it best are the first to act on it — and it is the one alarm whose
correct response is often *nothing technical at all* until you know why.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `withdrawal-spike`: `business.withdrawalSpikeRatio` at or above 3 — this hour's withdrawals against the
  mean hour of the last day.
* Support messages about withdrawals before any metric moves (the metric exists so that this order is reversed).
* A single large withdrawal, or many small ones, from accounts that were dormant.

## Diagnosis

1. **Scale, shape, and who.** The ratio is the alarm; the shape is the diagnosis: one whale leaving is different
   from two hundred users leaving, and a run of *newly funded* accounts withdrawing is different from both.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["business"])'
psql "$PGM_DB_URL" -c "
  SELECT count(*) AS n, count(DISTINCT user_id) AS users, sum(amount_micro) AS total_micro,
         min(requested_ms) AS first_ms, max(requested_ms) AS last_ms
    FROM withdrawals WHERE requested_ms > (extract(epoch from now())*1000 - 3600000)::bigint;"
psql "$PGM_DB_URL" -c "
  SELECT u.id, u.created_ms, count(w.id) AS withdrawals, sum(w.amount_micro) AS micro, max(w.requested_ms) AS last_ms
    FROM withdrawals w JOIN users u ON u.id = w.user_id
   WHERE w.requested_ms > (extract(epoch from now())*1000 - 3600000)::bigint
   GROUP BY u.id, u.created_ms ORDER BY micro DESC LIMIT 20;"
```

2. **Is the money actually there?** A withdrawal that cannot be funded is the one variant that is unambiguously
   our bug; check the ledger before interpreting anything else.

```bash
psql "$PGM_DB_URL" -c "
  SELECT w.id, w.user_id, w.amount_micro, w.state, b.available_micro
    FROM withdrawals w LEFT JOIN balances b ON b.user_id = w.user_id
   WHERE w.requested_ms > (extract(epoch from now())*1000 - 3600000)::bigint
     AND (b.available_micro IS NULL OR b.available_micro < w.amount_micro);"
```

3. **Is it a breach?** Cross-check the same indicators the security runbook uses; a withdrawal spike that is *not*
   driven by a public event usually is one.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["security"])'
psql "$PGM_DB_URL" -c "SELECT event, count(*) FROM wallet_events
   WHERE at_ms > (extract(epoch from now())*1000 - 21600000)::bigint GROUP BY event;"
```

4. **Is it a public event?** A market resolution, a venue incident, or news about the venue explains a spike; so
   does a withdrawal that has been *slow* for a few hours. Check the venue's status and the largest markets that
   resolved today before assuming the worst.

## Remediation

* **A trust event with an unknown cause is a stop-trading condition.** Not because trading caused it, but because
  you cannot know what is broken while the money path keeps moving. Engage the kill switch
  (`docs/runbooks/kill-switch.md`) and say so in the incident channel.
* **Do not freeze withdrawals to "buy time".** Our funds are the users' funds; the ability to leave is the product
  promise that makes everything else credible. Slow a withdrawal only by *processing* it, never by pretending it is
  pending.
* **If the spike is legitimate** (a resolution, a whale rebalancing, a fee change announced yesterday): the alarm
  has done its job — mark the incident as a false alarm with the evidence, and consider whether the threshold needs
  a known-event exemption. Do not tune the threshold first and ask later.
* **If the money is not there:** that is a money-correctness incident. Stop trading, follow
  `docs/runbooks/unreconciled-orders.md`, and escalate to the owner immediately.

## Verification

* `business.withdrawalSpikeRatio` is back under 2 for two consecutive hours, and the withdrawals that were in
  flight have `state = 'sent'` with matching `wallet_events`.
* Every account in the spike has a ledger that balances: the query above returns no rows.
* If the kill switch was engaged, the release follows `docs/runbooks/kill-switch.md` — four conditions, a second
  person, and a record.

## Escalation

* Money-path on-call immediately, always: this alarm is SEV1 because it is the first thing that happens when trust
  breaks, and the cost of a false alarm is one phone call.
* The owner, within fifteen minutes, for any decision about what users are told.
* Security lead in parallel whenever the security counters are not clean — do not wait to be sure.
