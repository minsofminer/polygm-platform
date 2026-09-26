---
id: builder-code-disabled
order: 7
severity: SEV1
owner: on-call (revenue path)
alarms: [builder-code-disabled]
last_drilled: 2026-09-23
---

# Builder code disabled

**One line:** the venue has stopped honouring our builder code, so orders still fill and our revenue stops. It is
SEV1 not because users are at risk but because it is invisible: volume looks healthy, the dashboard looks healthy,
and the fees quietly are not there.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `builder-code-disabled`: `builderCode.state` is not `active` for our code.
* `money.builderFees.chainMeasuredMicro` is flat while `expectedMicro` grows (`deltaMicro` widening).
* The venue's own rejections carry a builder-code reason: `builder_code_status.reject_count` climbing with
  `source = 'venue_rejection'`.

## Diagnosis

1. **What the product records** (our own table, written by the executor when a venue rejection mentions the code):

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["money"]["builderFees"])'
psql "$PGM_DB_URL" -c "SELECT code, state, changed_ms, reject_count, source, note FROM builder_code_status;"
```

2. **Our arithmetic, independently of the venue's dashboard.** This is the reason these columns exist: expected
   comes from our own ledger, measured comes from the chain's `OrderFilled` events. If they disagree with the
   venue's number, believe ours.

```bash
psql "$PGM_DB_URL" -c "
  SELECT day, orders, fills, volume_micro, expected_micro, chain_micro, delta_micro, status
    FROM builder_revenue_daily ORDER BY day DESC LIMIT 7;"
```

3. **When it started.** `changed_ms` on the status row is the moment the venue said so; the daily rows show which
   day's accrual stopped. The two should agree within one settlement window — if they do not, your first problem
   is our ingest of the chain, not the builder code.

4. **Was it us?** A `source = 'manual'` row means an operator or a self-referral check disabled the code (the
   P11 sybil path writes here). A `source = 'api'` row means we asked the venue to change it. Anything else is the
   venue's own decision.

## Remediation

* **If we disabled it** (self-referral, fraud decision): that was deliberate. Confirm with the decision record,
  re-enable only on the owner's instruction, and note the reason — the column is `note`, and it is read during the
  next audit.
* **If the venue disabled it:** there is no local fix. Contact the venue's builder support with our code, the
  first rejected order id, and the daily rows above. Until it is restored, revenue is zero **and we keep trading**:
  the money path does not depend on the builder fee.
* **If the chain ingest is what broke** (`chain_micro` flat but the venue says the code is active): that is
  `docs/runbooks/upstream-schema-change.md` — we are blind to our own revenue, which is the failure this metric
  exists to catch.

```bash
# the manual re-enable, with the reason an auditor will read
psql "$PGM_DB_URL" -c "UPDATE builder_code_status SET state='active', changed_ms=(extract(epoch from now())*1000)::bigint,
  source='manual', note='venue confirmed the code is active again' WHERE code='<OUR_CODE>';"
```

## Verification

* `builderCode.state` is `active` and `money.builderFees.daysUnmatched` stops growing.
* The next daily row shows `chain_micro > 0` and `status = 'matched'`.
* One real order's fee is reconciled by hand: `expected_micro` vs the chain event, in the same row.

## Escalation

* Revenue-path owner (the person who owns the builder agreement) within the hour — this is money, but it is not
  *users'* money, so it is not a 3am page unless it also moves a money-correctness metric.
* If the code cannot be restored within 24 h: the owner decides whether to pause trading in the markets where the
  code matters. Record the decision; do not let it be a silent default.
