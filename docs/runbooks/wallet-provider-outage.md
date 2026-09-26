---
id: wallet-provider-outage
order: 6
severity: SEV1
owner: on-call (money path)
alarms: [wallet-provider-degraded]
last_drilled: 2026-09-23
---

# Wallet provider outage

**One line:** the provider that holds the signing policy is not answering. We cannot sign, so we cannot trade — and
the one thing that must not happen is a workaround that signs without the policy engine.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV2 `wallet-provider-degraded` first (the provider refusing orders), which becomes SEV1 the moment it stops
  trading entirely.
* orders fail with a provider-shaped error (`WALLET_PROVIDER_ERROR`, timeouts, 5xx from the provider's API);
* new wallets cannot be provisioned; reads keep working (reads do not need the provider).

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["orderPath"]["rejectionsByReason"])'
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 15m executor | grep -iE "provider|sign|timeout" | tail -40
```

## Diagnosis

1. **Their status, our view, and the difference.** Check the provider's status page — then check our own call
   latency: a provider answering in thirty seconds is an outage for an order path with a latency budget.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["orderPath"]["hops"])'
```

2. **Which class of call is failing** — signing, key provisioning, address derivation, or policy re-read. Each has
   a different blast radius: signing stops trading; provisioning stops only *new* users.

3. **Are any orders ambiguous?** The decisive question, exactly as in `docs/runbooks/executor-down.md`:

```bash
psql "$PGM_DB_URL" -c "SELECT client_order_hash, signed_ms, submitted_ms, ack_ms FROM order_attempts
   WHERE ack_ms IS NULL ORDER BY signed_ms DESC LIMIT 20;"
```

4. **Have we burned the provider's rate limit rather than lost the service?** Count our calls over the last
   fifteen minutes: a retry storm against a provider is self-inflicted and looks identical from the outside.

## Remediation

* **Stop placing, keep reconciling.** The executor's `--reconcile-only` mode exists for this: it closes cases
  without signing anything.

```bash
python3 -m services.executor.main --db "$PGM_DB_PATH" --reconcile-only --once
```

* **Do not "fail over" to a second signer.** Custody is one policy engine per wallet; a fallback that signs outside
  it is a design defect this phase would have to report, not a remedy. If the provider is gone for hours, the
  answer is to stop trading and say so.
* **Users must be able to get their funds out.** Withdrawals are signed by the same provider, so during a provider
  outage say plainly — on the status page and in the bot — that withdrawals are queued. Never silently accept a
  request you cannot execute; queue it, timestamp it, and show the queue.

## Verification

* A synthetic order completes end to end.
* `orderPath.rejectionsByReason` no longer contains the provider code, and `hops.signedToSubmitted` p95 is back
  inside budget.
* Queued withdrawals drain in order, with matching `wallet_events` rows.

## Escalation

* Money-path on-call immediately: this is SEV1 because it stops trading.
* Provider support with our account id plus the first failing call's timestamp and request id.
* If the provider cannot give an ETA within an hour this becomes a *product* incident — the P15 D7
  wallet-provider-gone scenario — and the owner makes the call on what users are told.
