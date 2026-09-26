---
id: unreconciled-orders
order: 1
severity: SEV1
owner: on-call (money path)
alarms: [unreconciled-orders, orders-unknown-state]
last_drilled: 2026-09-23
---

# Unreconciled orders

**One line:** a case the reconciler could not close, open for more than 60 seconds. This is the only alarm in the
system whose meaning is *a user's money may be inconsistent*, so it pages before anything else on the list, and
the notification already carries the case name and the age — the first question is answered before you open your
eyes.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'   # read-only for diagnosis
API=https://api.polygm.trade                                          # staging: https://api.staging.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")                           # a 503 SIGNER_UNAVAILABLE reply means the token is unset here
```

## Symptoms

* a SEV1 page titled `unreconciled-orders`; the notification names the case and the oldest age.
* the on-call dashboard's first block is red: `money.unreconciled.count > 0`, `page: true`.
* `orders-unknown-state` may fire alongside it; the same rows produce both, which is why they share this page.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["data"]["money"], indent=2))'
```

## Diagnosis

1. **Which case, how old, how many.** One query, and its output is what you paste into the incident channel.

```bash
psql "$PGM_DB_URL" -c "
  SELECT case_name, count(*) AS open, max(extract(epoch from now())*1000 - since_ms)::bigint AS oldest_ms,
         max(attempts) AS attempts
    FROM reconcile_open GROUP BY case_name ORDER BY oldest_ms DESC;"
```

   The eight cases, one line each (`db/migrations/0008_trading_plane.sql` is the authority):

   | case | meaning |
   | --- | --- |
   | `submitted_unacked` | we signed and submitted; no venue ack inside the window |
   | `no_ack` | the submit call never returned a usable answer |
   | `orphan` | a venue order id we cannot attach to one of our intents |
   | `lagging_fill` | fills arrived for a market whose tape is behind |
   | `closing_market` | the market resolved while an order was live |
   | `ghost_order` | the venue has an order we thought was cancelled |
   | `ambiguous_settlement` | the settlement event does not match our fill arithmetic |
   | `cancelled_race` | a cancel and a fill crossed |

2. **Is the order path alive at all?** One stuck executor looks exactly like a venue problem.

```bash
psql "$PGM_DB_URL" -c "
  SELECT id, state, acknowledged, placed_ms, updated_ms FROM orders
   WHERE state IN ('live','partial') AND acknowledged = 0 ORDER BY placed_ms LIMIT 20;"
curl -sS "${ADM[@]}" "$API/v1/admin/drain-status" | python3 -m json.tool
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(d["orderPath"]["intentsByState"], d["executor"])'
```

   An `order_intents.state` of `submitting` or `uncertain` is a **blocking** state: the drain guard refuses to
   replace the executor while one exists, and so should you.

3. **Our timestamps or the venue's?** A `submitted_ms` with no `ack_ms` is our gap; an `ack_ms` with no fill is
   theirs. This decides who you call, so it is worth typing out.

```bash
psql "$PGM_DB_URL" -c "
  SELECT client_order_hash, signed_ms, submitted_ms, ack_ms FROM order_attempts
   ORDER BY coalesce(ack_ms, submitted_ms, signed_ms) DESC LIMIT 20;"
```

4. **What changed in the window?** A deploy is the single most likely cause, and the ledger knows.

```bash
python3 tools/p15-deploy-audit.py verify | tail -5
git -C /srv/polygm log --oneline -5
```

## Remediation

* **One case, one order, younger than ten minutes:** do not "fix" the row by hand. Closing cases is the
  reconciler's job, and a hand-edited row destroys the evidence that would explain what happened.
* **`orphan` or `ghost_order`:** stop trading. We have an order at the venue we cannot map to a user, and the next
  one will be a money error. Engage the kill switch (`docs/runbooks/kill-switch.md`).
* **`ambiguous_settlement`:** the arithmetic does not reconcile, so every number shown for that market is suspect.
  Kill switch, then `docs/runbooks/upstream-schema-change.md`.
* **`submitted_unacked` with a healthy drain status:** keep trading and keep watching. This is usually the venue's
  ack stream lagging; escalate if the count grows over two consecutive reads.
* **Any read where `money.positionDrift.mismatchedOrders > 0`:** kill switch. Holdings and our ledger disagree.

```bash
# the only write this runbook allows, and only with a reason a human can read at 2am
curl -sS -X POST "${ADM[@]}" -H 'content-type: application/json' \
  -d '{"engaged": true, "reason": "unreconciled orphan order, stopping new risk"}' \
  "$API/v1/admin/kill-switch" | python3 -m json.tool
```

## Verification

* The query in step 1 returns **no rows**, and `money.unreconciled.page` is `false` on two reads a minute apart —
  the flag is thresholded at 60 s, so one read is not evidence.

```bash
for i in 1 2; do sleep 35; curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; u=json.load(sys.stdin)["data"]["money"]["unreconciled"]; print(u["count"], u["page"])'; done
```

* If you engaged the kill switch: leave it engaged until the case is closed *and* the venue-side order is
  accounted for. Disengaging is its own runbook, with its own verification.

## Escalation

* Fifteen minutes without a diagnosis: the engineer who owns the executor and the wallet path (the P06/P13
  on-call secondary). Page them; do not wait for the hour.
* Any suspicion of a breach rather than a bug: `docs/runbooks/key-compromise.md`, immediately. It outranks this
  page.
* Everything you touch goes in the incident channel with the time you touched it. P14's rule outlives the phase:
  **no finding closed without a re-test**, and no remediation without a recorded time.
