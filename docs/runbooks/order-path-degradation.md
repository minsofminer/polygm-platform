---
id: order-path-degradation
order: 15
severity: SEV2
owner: on-call (money path)
alarms: [order-path-degraded, elevated-rejections, synthetic-order-failed, p99-degraded]
last_drilled: 2026-09-23
---

# Order path degraded (latency, rejections, one hop)

**One line:** orders still work, but one hop of the path has become slow or has started refusing. This is the page
that happens *before* the money-correctness pages, so it is where you have time to act instead of react.

It is also the page for `synthetic-order-failed`: when the external probe misses three times, the end-to-end path
is broken in a way no component-level check can see, and this is the runbook that starts from the whole path.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV2 `order-path-degraded`: `orderPath.hops.<hop>.p99 > 8000` ms (the intent → risk → sign → submit → ack ladder),
  or the p99 moving while the p50 does not — a queue, not a load problem.
* SEV2 `elevated-rejections`: more than 25 rejections in an hour, or one reason code dominating.
* SEV1 `synthetic-order-failed`: three consecutive probe misses.
* SEV3 `p99-degraded`: the same tail over 3 s, as a ticket.
* Users see it first as "the button spins".

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; o=json.load(sys.stdin)["data"]["orderPath"]; print(json.dumps({"hops":o["hops"],"rejections":o["rejectionsByReason"],"lastHour":o["rejectionsLastHour"],"states":o["intentsByState"]}, indent=2))'
```

## Diagnosis

1. **Which hop, and is it ours?** A p99-only move is a queue; a p50 move is capacity or a dependency.

```bash
psql "$PGM_DB_URL" -c "
  SELECT state, count(*) FROM order_intents
   WHERE state IN ('pending','queued','submitting','uncertain','submitted') GROUP BY state;"
psql "$PGM_DB_URL" -c "
  SELECT quantile_disc(0.5) WITHIN GROUP (ORDER BY submitted_ms - signed_ms) AS p50_ms,
         quantile_disc(0.99) WITHIN GROUP (ORDER BY ack_ms - submitted_ms) AS p99_ack_ms
    FROM order_attempts WHERE signed_ms > (extract(epoch from now())*1000 - 900000)::bigint;"
```

2. **The rejection *reasons*, not the count.** A spike concentrated in one code is a specific upstream change; a
   spread across codes is our own bug or an outage.

```bash
psql "$PGM_DB_URL" -c "SELECT risk_code, count(*) FROM order_intents
   WHERE created_ms > (extract(epoch from now())*1000 - 3600000)::bigint GROUP BY risk_code ORDER BY 2 DESC;"
```

3. **The end-to-end probe, which is the only check that spans every hop:**

```bash
python3 tools/p15-synthetic.py --api "$API" --user u-synth --once --await 25
cat /var/lib/polygm/synth.json | python3 -m json.tool
```

   The state file distinguishes a **miss** (no answer: the path) from a **rejection** (an answer we chose: the
   market). A run of rejections is not this incident; a run of misses is.

4. **Rate limits** (a refusal the venue chose, and the only one we should treat as backpressure):

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["orderPath"]["rateBuckets"])'
```

5. **Is the executor keeping up?** A p99-only move with a growing `queued` count is the executor, not the venue:

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["executor"])'
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 15m executor | tail -50
```

## Remediation

* **Queue growth:** give the executor more room, and keep the drain protocol in mind — a restart while an ambiguous
  order exists is the failure the protocol prevents.
* **A dependency is slow:** say so, and degrade deliberately. Do not add retries to a slow hop — retries turn a
  slow hop into a queue, which is how a p99 problem becomes an outage.
* **The synthetic probe missing with every component green:** suspect the edge (TLS, DNS, the load balancer, an
  ingress rule that forgot one path). This is exactly why the probe runs outside the fleet.
* **One rejection code spiking:** it is usually a validation change on our side or a market state on theirs. If the
  code means "we refused for safety" (stale book, size cap, wallet policy), the product is working: check whether
  the market is simply moving.
* **Elevated rejections from the venue:** `docs/runbooks/rate-limit-ban.md`.

## Verification

* `hops.<hop>.p99` back inside budget for fifteen minutes (not one sample — the metric is a rolling window and one
  good read is noise).
* `intentsByState.queued` flat or falling.
* The synthetic probe is green again and its own `endToEnd` number appears in the next window.

## Escalation

* SEV2, hours. Money-path on-call owns it while it is a latency problem.
* Escalate to SEV1 the moment a money-correctness metric moves: `unreconciled` non-zero or `ordersUnknownState`
  non-zero means stop trading, and that decision does not wait for the latency investigation.
