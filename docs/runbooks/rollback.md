---
id: rollback
order: 14
severity: SEV1
owner: on-call (money path)
alarms: [deploy-verification-failed]
last_drilled: 2026-09-23
---

# Full rollback

**One line:** put the previous artifact back, in under sixty seconds, without creating an ambiguous order — and
know which of the two things you are rolling back, because the code rolls back and a migration does not.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `deploy-verification-failed`: the smoke watch failed right after a deploy.
* a canary metric moved the wrong way in the first ten minutes (unreconciled non-zero, unknown state non-zero, a
  silent feed, or the builder-fee delta opening up);
* the manual gate was approved, prod went out, and the error rate or the p99 of `submittedToAck` tripled.

## Diagnosis

1. **Which component, and is it the money path?** `tools/p15-money-path.py` answers this mechanically — it reads
   the diff between the running commit and the incoming one and decides whether the change may auto-promote or
   needs the gate. Use it rather than arguing about it:

```bash
python3 tools/p15-money-path.py --since origin/main --explain
```

2. **What is running, and what would we go back to** — the deploy audit's last entries are exactly this:

```bash
python3 tools/p15-deploy-audit.py verify | tail -5
cat deploy/image-digests.txt
```

3. **Were any orders ambiguous at the moment of the deploy?** This is the question the drain guard exists to
   answer *before* the deploy; after the fact, ask it again, because a rollback is itself a replacement:

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/drain-status" | python3 -m json.tool
psql "$PGM_DB_URL" -c "SELECT client_order_hash, signed_ms, submitted_ms, ack_ms FROM order_attempts
   WHERE ack_ms IS NULL ORDER BY signed_ms DESC LIMIT 20;"
```

4. **Did a migration go out with it?** If yes, a rollback of the *image* runs the old code against the new schema:
   fine when the migration was expand-only (the P15 D3 rule, enforced by `tools/p15-migration-check.py`), and not
   fine otherwise. Verify rather than assume:

```bash
python3 tools/p15-migration-check.py --since "$(git -C /srv/polygm rev-parse HEAD~5)"
```

## Remediation

* **One command, and it prints what it is doing** (the rollback records itself in the audit ledger, so a rollback
  that is not in the ledger did not happen):

```bash
deploy/rollback.sh --env prod --component api          # or: --component executor
```

* **Executor rollbacks are different, and only because of the drain protocol.** Never replace the executor while an
  ambiguous order may exist. Drain first, wait for `safeToReplace`, then roll back, then clear:

```bash
python3 tools/p15-drain-guard.py --api "$API" --token "$PGM_ADMIN_TOKEN" --set-draining --reason "rollback of the current sha"
deploy/rollback.sh --env prod --component executor
python3 tools/p15-drain-guard.py --api "$API" --token "$PGM_ADMIN_TOKEN" --clear-draining --reason "rollback complete"
```

* **Never roll a migration back by hand.** If a contract step is what broke, the fix is a forward deploy, not a
  `DROP COLUMN` at 3am. The schema is the record of what happened; deleting part of it destroys the evidence.
* **Feature flags are the fast path and they are not a rollback.** If the change is behind a flag, turn the flag
  off (`/v1/admin/flags`) — it is instant, needs no restart, and leaves the artifact in place so the next fix is a
  deploy rather than a second rollback.

## Verification

* The rollback job's own output names the digest it restored; confirm it is the digest in the ledger's
  `previous_commit`/`digests` for the failed deploy.
* `python3 tools/p15-smoke.py --api "$API" --token "$PGM_ADMIN_TOKEN" --watch 300` exits 0.
* `/v1/admin/metrics` shows `money.unreconciled.page: false` and `ordersUnknownState.count: 0`.
* **Time it.** The budget is under 60 seconds, and the monthly drill is what keeps that a fact rather than a hope —
  write the measured seconds into `docs/verification/` and move `last_drilled` at the top of this file.

## Escalation

* Money-path on-call owns a rollback, and does not need permission to start one.
* Tell the incident channel **within five minutes** of rolling back, with the digest and the reason. A rollback
  nobody knows about is how two people fix the same thing in opposite directions.
* If the rollback does not fix it: stop rolling, start diagnosing
  (`docs/runbooks/order-path-degradation.md`), and remember that the second rollback is usually a symptom of a
  wrong diagnosis rather than of a bad artifact.
