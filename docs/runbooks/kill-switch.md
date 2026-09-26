---
id: kill-switch
order: 8
severity: SEV1
owner: on-call (money path)
alarms: [kill-switch-engaged]
last_drilled: 2026-09-23
---

# Kill switch: activation and deactivation

**One line:** the switch that stops new risk. Engaging it must be easy from a phone and require a reason;
releasing it must be harder, and must be someone's deliberate decision rather than a timeout that expired.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `kill-switch-engaged` fired **unexpectedly** — meaning nobody claims the engagement.
* Or: you are the one engaging it, because another runbook told you to (unreconciled orders, drift, silent feeds,
  a withdrawal spike, a suspected compromise).
* In the product: order placement returns a refusal envelope naming the kill switch, and the terminal shows the
  banner. This is the intended, visible behaviour.

## Diagnosis

1. **The current state, who changed it, and why** — the reason column is a `CHECK (length(reason) BETWEEN 4 AND 400)`,
   so an engagement without a reason cannot exist:

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["killSwitch"])'
psql "$PGM_DB_URL" -c "SELECT engaged, reason, changed_by, at_ms FROM kill_switch_state ORDER BY at_ms DESC LIMIT 5;"
```

2. **Is trading actually stopped?** Prove it rather than trusting the flag: an order attempt must be refused by
   the risk gate, not by the client.

```bash
curl -sS -X POST "$API/v1/orders" -H 'content-type: application/json' -H "x-user-id: u-synth" \
  -d '{"marketId":"...","tokenId":"...","side":"BUY","priceMicro":500000,"sizeMicro":100000,"idempotencyKey":"kill-probe-1"}' \
  | python3 -m json.tool     # expect a refusal envelope naming the switch; a 202 here is an incident
```

3. **What is still open?** The switch stops *new* risk; it does not cancel what is live. Decide deliberately what
   to do with open orders — the drain status tells you whether the executor is mid-order:

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/drain-status" | python3 -m json.tool
psql "$PGM_DB_URL" -c "SELECT state, count(*) FROM order_intents WHERE state IN ('pending','queued','submitting','uncertain','submitted') GROUP BY state;"
```

## Remediation

* **Engaging (from a phone):** one POST with a reason that names the incident. The reason is what the next person
  reads at 3am, so write it for them.

```bash
curl -sS -X POST "${ADM[@]}" -H 'content-type: application/json' \
  -d '{"engaged": true, "reason": "position drift on 3 orders, stopping new risk"}' \
  "$API/v1/admin/kill-switch" | python3 -m json.tool
```

* **Letting open orders finish.** Do not cancel everything reflexively: a cancel racing a fill is a worse mess than
  a fill. Cancel only what the venue will let you cancel cleanly, and only when the incident is about exposure
  rather than correctness.
* **Releasing.** Four conditions, all of them, and no exceptions for a "quick test":
  1. the case that caused the engagement is closed and verified;
  2. `money.unreconciled.count == 0` and `ordersUnknownState.count == 0` on two reads a minute apart;
  3. a synthetic paper order is attempted and the refusal is the *expected* one (the probe proves the path is
     reachable and auth works while the gate refuses);
  4. a second person has read the incident channel and agrees. At 3am that person is the owner on the phone.

```bash
curl -sS -X POST "${ADM[@]}" -H 'content-type: application/json' \
  -d '{"engaged": false, "reason": "reconcile clean for 10 min; owner agreed on the call"}' \
  "$API/v1/admin/kill-switch" | python3 -m json.tool
```

## Verification

* A refused order becomes a placed order: `tools/p15-synthetic.py --api "$API" --user u-synth --once --await 25`
  exits 0.
* `killSwitch.engaged` is `false` and the **history** retains both rows with reasons — the table is append-only, so
  the release is as visible as the engagement.
* A drill note exists: this runbook's `last_drilled` date moves, and `kill_switch_drills` has a row with a time.

## Escalation

* Engaging: no approval needed, ever. The switch exists so that the person with the most context can stop the
  bleeding; a switch that needs a meeting is decorative.
* Releasing: money-path owner. If they are unreachable for thirty minutes, the release waits — the cost of a
  delayed release is missed volume, and the cost of a wrong one is a user's money.
