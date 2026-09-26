---
id: stuck-bridge
order: 12
severity: SEV1
owner: on-call (money path)
alarms: [deposit-stuck, withdrawal-delayed]
last_drilled: 2026-09-23
---

# Stuck bridge / deposit not credited

**One line:** a user's money is in transit and the product does not show it. From the user's side this is
indistinguishable from theft, which is why it is SEV1 even when the amount is small.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `deposit-stuck`: `business.stuckDeposits.page` true for 15 minutes — a deposit seen and not credited.
* SEV2 `withdrawal-delayed`: a withdrawal in flight for more than 15 minutes.
* a `wallet_events` row with `event = 'deposit_stuck'` (the ingest saw it and could not credit it);
* users reporting it in support before any metric fires — the metric exists so that this happens to us and not to
  them.

```bash
psql "$PGM_DB_URL" -c "SELECT id, user_id, amount_micro, first_seen_ms, credited_ms FROM deposits
   WHERE credited_ms IS NULL ORDER BY first_seen_ms LIMIT 20;"
psql "$PGM_DB_URL" -c "SELECT id, user_id, amount_micro, requested_ms, state FROM withdrawals
   WHERE state IN ('requested','sent','denied') ORDER BY requested_ms LIMIT 20;"
psql "$PGM_DB_URL" -c "SELECT id, user_id, event, detail_json, at_ms FROM wallet_events
   WHERE event IN ('deposit_stuck','deposit_detected','withdraw_denied') ORDER BY at_ms DESC LIMIT 20;"
```

## Diagnosis

1. **Is the chain event there and it is our accounting, or is the chain event missing?** `chain_events` is the
   evidence; the ledger is our interpretation of it.

```bash
psql "$PGM_DB_URL" -c "SELECT source, kind, tx_hash, at_ms FROM chain_events ORDER BY at_ms DESC LIMIT 20;"
```

2. **Which settlement path:** direct transfer, or a bridge (a wrapped asset arriving from another chain). A bridge
   that is slow is a different incident from a credit our code dropped.

3. **Our ingestion's view of the chain:** is the cursor moving? A stuck cursor explains every stuck deposit at once
   and is a data-plane incident, not a per-user one.

```bash
psql "$PGM_DB_URL" -c "SELECT source, state, last_event_ms, last_frame_ms FROM ingest_cursors ORDER BY source;"
```

4. **Could it be credited twice?** Before any manual credit, check the dedupe key: `cash_ledger` is unique on
   `(ref_table, ref_id, kind, user_id)`, which is the mechanism that makes a manual credit safe — but only if you
   use the same key the automatic path would have.

## Remediation

* **Never edit `balances` or `cash_ledger` in place.** A balance change without its ledger entry breaks the
  invariant the whole money plane rests on, and the next reconciliation surfaces it as drift with no explanation.
* **Credit through the ledger, with the chain event as the reference**, then let the reconcile loop agree:

```bash
psql "$PGM_DB_URL" -c "SELECT source, kind, tx_hash, at_ms FROM chain_events ORDER BY at_ms DESC LIMIT 5;"
psql "$PGM_DB_URL" -c "SELECT ref_table, ref_id, kind, user_id, amount_micro FROM cash_ledger
   WHERE ref_id IN (SELECT tx_hash FROM chain_events ORDER BY at_ms DESC LIMIT 5);"
```

* **A withdrawal that cannot be executed:** tell the user, with a timestamp, and do not leave the row pretending to
  be in flight. `withdraw_denied` with a machine-readable reason is a better answer than a silent `requested`.

## Verification

* `deposits` rows have `credited_ms`, `wallet_events` shows the matching `deposit_credited`, and the user's balance
  changed by exactly the ledger's amount.
* `money.unreconciled` is empty and `positionDrift` is 0 for the affected user.
* The user can withdraw the amount (real-phone test if the amount matters).

## Escalation

* Money-path on-call immediately: a stuck deposit is a trust event with a clock on it.
* Owner for anything over a small threshold, and for anything that needs a user-facing credit as a gesture — that
  is a business decision, not an engineering one.
