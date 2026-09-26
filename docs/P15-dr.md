# P15 D7 — disaster recovery

The kit's framing decides the document: **RPO and RTO per component, and for the executor RPO zero — no order
intent may be lost, specified rather than asserted.** So the interesting content here is not the backup schedule;
it is the argument for why zero is achievable, and the list of things that are honestly not zero.

## RPO and RTO, per component

| component | RPO | RTO | how, and what it costs |
| --- | --- | --- | --- |
| **executor** (order intents, attempts, signed orders) | **zero** | < 60 s | the queue is durable rows in Postgres, written *before* anything is signed. The executor holds no state that is not in the database: a restart re-reads `order_intents` and resumes. There is no in-memory queue to lose. |
| **API** | zero | < 60 s | stateless; sessions live in Postgres, flags are re-read on boot, and the drain protocol means a replacement never races an in-flight order. |
| **Postgres (managed, Neon)** | ≤ 1 transaction (synchronous commit) | 30–60 s provider failover; 4 h for a region loss | PITR to any second in the last 7 days. A region loss is the nightly dump restored in another region — that path is the 4-hour number, and it is `[UNVERIFIED]` until an owner runs it. |
| **tape / analytics** | minutes | hours | reconstructed from our own `raw_json` and the venue's history endpoint; losing the rollups loses speed, not truth. |
| **ClickHouse / rollups** (if enabled) | 1 h | 24 h | derived data. Its loss is a re-ingest, which is why it is the first cost cut (D8) rather than a protected asset. |
| **object storage (R2)** | zero for a written object | minutes | Cloudflare's, not ours; the bucket is versioned by policy and the restore path is `rclone`. |
| **secrets / KEK** | zero | 1 h | KMS-held KEK, wrapped DEKs in Postgres, and a rotation path that refuses to retire a KEK until every wrap has been re-read (`keys.rotation_plan`). |
| **Telegram bot** | n/a | 5 min for webhook | not a data store; a re-registered webhook with the current secret token is the whole procedure. |

**Why the executor's RPO is genuinely zero.** The claim is only true if the intent row is committed *before* the
signature is requested, and if a crash between signing and submitting is recoverable. Both hold, and both are
tested rather than asserted:

* `order_intents` is written in a transaction with the idempotency key (`UNIQUE(user_id, idempotency_key)`) and the
  state machine starts at `pending` — so a crash before submission leaves a durable intent that the next executor
  picks up.
* `order_attempts` is keyed by `client_order_hash`, and the row is written **after** signing. That ordering is what
  makes a crash between signing and submitting visible: the row exists with `signed_ms` and no `submitted_ms`,
  which is an ambiguous order, and P06 D3's reconciliation resolves it instead of guessing. It is also why the
  drain protocol refuses to replace the executor while a `submitting`/`uncertain` intent exists.
* P13's recovery loop kills the executor 100 times mid-flight and asserts no intent is lost and no order is
  submitted twice (`docs/verification/P13-*`).
* The drain guard is the operational half: `tools/p15-drain-guard.py` is fail-closed, and the deploy pipeline
  cannot reach the executor without passing it.

## Backups

* **Nightly logical dump** from the API host: `pg_dump | age -r $PGM_BACKUP_RECIPIENT | rclone copyto r2:polygm-backups/<env>/<date>.age`,
  written by `services/api/backup.py` and driven by a systemd timer in `infra/terraform/cloud-init/`.
* **Encryption by age**, with the private recipient key held off the hosts: a replica of the bucket is not a
  replica of the data, and an operator with host access cannot read the backups.
* **Retention:** 35 daily, 12 monthly. The monthly tier exists because the failure this protects against is "we
  discovered the corruption three weeks later".
* **Staging has its own prefix**, so a restore can never cross planes: restoring production into staging and
  *then* pointing staging at production's data is a scenario worth designing against explicitly.
* **The bucket is R2** because egress is free, and a restore on a bad day downloads everything.

## The tested restore — with a date and a result

`tools/p15-restore-drill.py` runs the whole procedure against a database this environment can build: snapshot,
rebuild the schema through the product's own migration runner, copy the rows back, then verify that row counts
match, that **every money column's integer micro-sum matches**, that there are no foreign-key violations, and that
the append-only triggers still *fire* after the restore (a restore that drops its guards turns a tamper-evident
ledger into an editable table).

Result of the first run, on 2026-09-23: **123 tables copied, 3,261 rows, money checksums matched on all 7
money columns, 0 foreign-key violations, 58 triggers present after the restore, and the append-only guard
confirmed live** (the probe's `UPDATE` against a restored ledger row was refused). Total 128 ms — snapshot 8.5 ms,
restore 105.7 ms, verify 14.0 ms. Recorded in `docs/verification/P15-restore-drill.txt` and as a row in
`backup_restore_tests` (the P07 table, written through the product's own store).

**What it does not prove, stated plainly:** the production transport is `pg_dump → age → R2 → restore`, and that
path needs the hosts and the provider credentials this build environment does not have. It is marked
`[UNVERIFIED]` here and in the owner-steps list, and the drill prints the same sentence in its own transcript, so
the gap travels with the evidence rather than living only in a document.

## Region failure

**What survives:** the backups (R2 replicates across locations independently of our region), the DNS and TLS
configuration (Cloudflare), the code and images (git + registry), and the secrets (KMS, held outside the region).

**What does not:** every running process, the primary database (until the provider restores service or we restore
a dump elsewhere), and every user session.

**The manual procedure** (in `docs/runbooks/database-failover.md` and, for the full loss, below):

1. Assess: provider status, expected window. Under an hour, waiting is usually right; over an hour, execute.
2. Provision the hosts in a second region from the same Terraform (`-var region=…`), which is why the
   infrastructure is code rather than a console history.
3. Create an empty Neon project in the second region, then restore the newest dump into it. Record the LSN/dump
   date: the gap between it and the loss is the data we cannot see, and it belongs in the incident channel.
4. Point DNS at the new API host, redeploy the images **by digest** from `deploy/image-digests.txt`, and run the
   drain guard's clear step only after reconciliation is clean.
5. Tell users what happened, with the gap stated. A silent recovery that lost two hours of fills is the thing
   that turns an outage into a trust event.

**RTO for this path: 4 hours**, dominated by database provisioning and the restore, not by our own steps. It is
exercised annually as a table-top at minimum; a live regional failover is an owner decision with a cost attached.

## The scenario the kit singles out: the wallet provider is gone

The kit's rule is the design constraint: **if users cannot get their funds out themselves, that is a design defect,
not a DR gap.** So the position, stated plainly:

* **Custody is delegated, not custodial-of-last-resort.** Every wallet is a smart account whose policy engine lives
  at the provider; we never hold a raw key. The user's own key (or passkey, or export) is the authority — see the
  key-export path and P13's drill on it. With the provider gone, a user's route out is the wallet itself, on-chain,
  which no service of ours can block.
* **What actually breaks if the provider disappears:** trading (we cannot sign) and *withdrawals we initiate on the
  user's behalf*. That is a real degradation and it is the reason the export path is a first-class, tested feature
  rather than an emergency menu item.
* **The honest gap:** "can users get their funds out *comfortably*" is not the same claim as "is it possible". The
  acceptance test for this scenario is an owner step on a real phone with a real wallet, and it is listed as such
  in `docs/P15-readiness.md`. Until it is run, this document claims the design and not the experience.
* **What we would never do:** introduce a fallback signer to keep trading through a provider outage. That trades a
  contained outage for a custody defect, and the P15 constraints forbid it by name.

## The annual DR test

A table-top in Q1 and the live restore drill quarterly. Both write rows: `backup_restore_tests` for the restore,
`drill_records` for the exercise, each with a verdict, a measured time and a named operator. `tools/p14-key-drills.py`
already writes the key-compromise half of this; the restore drill writes the data half. A DR test with no row is a
meeting, not a test.
