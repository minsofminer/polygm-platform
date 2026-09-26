---
id: key-compromise
order: 10
severity: SEV1
owner: security lead + money path
alarms: [key-compromise-indicator]
last_drilled: 2026-09-23
---

# Suspected key compromise

**One line:** assume the keys are gone and act in that order: stop the bleeding, preserve the evidence, rotate,
then investigate. P07's `security/incident.py` holds the first-sixty-minutes list; this page is the operator's view
of it.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `key-compromise-indicator`: a revocation job was opened, or a wallet was suspended with reason
  `export_under_investigation` / `insider_flagged`;
* an unexplained `export_requested` entry in `wallet_events`;
* provider-side notification of anomalous signing, or a session token seen somewhere it should not be.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["security"])'
psql "$PGM_DB_URL" -c "SELECT event, count(*) FROM wallet_events WHERE at_ms > (extract(epoch from now())*1000 - 86400000)::bigint GROUP BY event;"
psql "$PGM_DB_URL" -c "SELECT id, scope, requested_by, total, revoked, failed, started_ms, finished_ms FROM revoke_jobs ORDER BY started_ms DESC LIMIT 5;"
```

## Diagnosis

The first sixty minutes, in this order, because the order is the control:

1. **Stop new signing.** Kill switch first — it is one call and it needs no analysis.
2. **Preserve evidence before rotating.** Sessions, tokens, logs: a rotation that runs first destroys the answer to
   "what did they do while they were here".

```bash
python3 -c "import sys; sys.path.insert(0,'packages'); import polygm_core.security.incident as i; [print(s.actor, '|', s.action, '|', s.verify) for s in i.first_60_minutes()]"
```

3. **Scope it.** Which key kinds are implicated: session keys (bounded, per-wallet), the KEK (envelope keys, the
   P07 rotation path), or provider-held policy keys (their console, their rotation).

```bash
psql "$PGM_DB_URL" -c "SELECT id, kind, state, created_ms FROM key_wraps ORDER BY created_ms DESC LIMIT 10;"
psql "$PGM_DB_URL" -c "SELECT code, state, changed_ms, note FROM builder_code_status;"
```

4. **Blast radius from the money path's side:** which orders were signed in the window? Every one is suspect until
   matched to a user's intent.

```bash
psql "$PGM_DB_URL" -c "SELECT client_order_hash, signed_ms, submitted_ms FROM order_attempts
   WHERE signed_ms > (extract(epoch from now())*1000 - 3600000)::bigint ORDER BY signed_ms;"
```

## Remediation

* **Revoke every session, then plan the KEK rotation, then notify the provider** — in that order, and with times
  written down for each step (P14's rule: an untested runbook is a hypothesis, and an unstamped step is an
  untested one).

```bash
# revocation is the break-glass route: two named approvers, a reason of at least 20 characters, and every
# session in scope dies in one statement (the API's own audit row is the evidence)
curl -sS -X POST "${ADM[@]}" -H 'content-type: application/json' \
  -d '{"reason":"suspected compromise: incident INC-2418, revoking every session","approvers":["ops-ani","sec-lead"],"scope":"all"}' \
  "$API/v1/admin/revoke-sessions" | python3 -m json.tool
psql "$PGM_DB_URL" -c "SELECT id, scope, total, revoked, failed, finished_ms FROM revoke_jobs ORDER BY started_ms DESC LIMIT 3;"

# the KEK rotation is planned from the live wraps before anything is written: rotation_plan refuses an order
# that retires the old KEK early, and it is idempotent, so a re-run resumes rather than re-wraps
psql "$PGM_DB_URL" -tA -c "SELECT coalesce(json_agg(t), '[]'::json) FROM (SELECT * FROM key_wraps WHERE revoked_ms IS NULL) t" \
  > /tmp/keywraps.json
python3 -c "import json,sys; sys.path.insert(0,'packages'); from polygm_core.security import keys; print(json.dumps(keys.rotation_plan(json.load(open('/tmp/keywraps.json')), new_kek_version=2), indent=2))"
```

* **Suspend the wallets involved**, with the reason that will be read in the audit:
  `export_under_investigation` or `insider_flagged` (never `operator`, which says nothing).

```bash
psql "$PGM_DB_URL" -c "UPDATE wallets SET state='suspended' WHERE state='trading'
   AND user_id IN (SELECT user_id FROM wallet_events WHERE event='export_requested'
   AND at_ms > (extract(epoch from now())*1000 - 3600000)::bigint);"
```

* **Tell users when the evidence says funds moved**, not before: a false alarm that scares people out of the
  product is its own kind of breach. The owner writes the message; the on-call routes it.
* **Break-glass has an hour budget.** If the response needs longer than an hour of administrative access, that is
  itself a finding (P14's rule) — record it as one rather than extending quietly.

## Verification

* `security.pendingRevocations` is 0 and `revoke_jobs` shows `finished_ms` for every job, with `failed = 0`.
* Every affected wallet is suspended with a stated reason, and every user-facing session is invalidated: an old
  token gets 401.
* The drill row exists with a measured time, and any KEK rotation is visible in `kek_versions`.
* Restore-test the backup taken before the incident (`python3 tools/p15-restore-drill.py`): a compromise response
  that cannot restore a clean database is half a response.

## Escalation

* Security lead immediately, money-path owner in parallel, provider support within the hour.
* If funds moved: legal/insurance per `docs/P14-audit-bounty-legal.md`, and the owner decides on disclosure.
* Every action gets a timestamp in the incident channel. This page is the one place where "we think we did that"
  is not good enough.
