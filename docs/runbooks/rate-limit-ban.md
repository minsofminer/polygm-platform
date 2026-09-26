---
id: rate-limit-ban
order: 5
severity: SEV1/SEV2
owner: on-call (money path)
alarms: [venue-throttling, rate-budget-low]
last_drilled: 2026-09-23
---

# Upstream rate-limit ban (our egress is throttled)

**One line:** the venue's limiter has started refusing us. The interesting question is not "how do we get more
through" but whether we are being throttled for *our* traffic or banned for someone else's (a shared IP, a
hijacked key).

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `venue-throttling`: the venue refuses orders with `THROTTLED` (its own `rate_limited` code translated at the
  boundary) more than three times in an hour.
* SEV2 `rate-budget-low`: our own remaining budget in a bucket has fallen under a fifth — `budgets` compares the
  declared buckets in `services/ingest/net.py` against what `rate_counters` shows we have spent.
* Order placement starts failing with a venue-side refusal code instead of one of ours.

## Diagnosis

1. **Which bucket, how much left, which caller.** Our counters are the first read: they tell you whether we are
   the cause before you ask the venue anything.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["orderPath"]["rateBuckets"])'
psql "$PGM_DB_URL" -c "SELECT key, bucket_ms, count, updated_ms FROM rate_counters ORDER BY count DESC LIMIT 15;"
```

2. **Is it us (a retry storm) or them (a shared ban)?** A storm shows as one bucket at its limit with a single key
   dominating. A ban shows refusals while our counters are *low* — and that is the more dangerous case, because
   the naive reaction (retry harder) makes it permanent.

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 15m executor | grep -cE "429|THROTTLED"
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 15m ingest  | grep -cE "429|THROTTLED"
```

3. **Which egress address the venue is seeing** (a NAT gateway is a shared identity, and one noisy neighbour is
   enough):

```bash
curl -sS https://api.ipify.org; echo
sudo iptables -t nat -S | grep MASQUERADE | head
```

4. **Is anything else on this host talking to the venue?** The isolation design puts the executor on its own box
   precisely so that this question has one answer. If the answer is not "nothing else", fix that first.

## Remediation

* **Back off and honour the limiter.** Retries are exponential with jitter and a cap; if a custom retry loop is in
  play, delete it. A ban is served by patience, not by volume.
* **Degrade deliberately.** Turn off the expensive pollers first (long-tail tracking, then metadata refresh), keep
  the money-path buckets for the executor's own calls, and say so in the incident channel.

```bash
curl -sS -X POST "${ADM[@]}" -H 'content-type: application/json' \
  -d '{"name":"tape_backfill_enabled","value":false,"reason":"venue throttling: protecting the money-path buckets"}' \
  "$API/v1/admin/flags" | python3 -m json.tool
```

* **If our counters are low and refusals continue:** we are banned on a shared identity. Change the egress address
  (a fresh one, not a per-request rotation — that is how a soft ban becomes a hard one) and contact the venue with
  the request ids from the refusals.
* **Kill switch** if orders are being refused *after* being signed: an unacked signed order is an ambiguous order,
  and this is exactly the moment not to restart anything.

## Verification

* `orderPath.rateBuckets` shows every bucket under its limit, and the count of `THROTTLED` reasons in the last five
  minutes is zero.
* A synthetic order completes end to end — the probe is deliberately cheap, which makes it the right canary here.
* Flags you flipped are flipped back, with the reason in `flag_audit`. A degradation left on is an incident
  waiting for a user to discover it.

## Escalation

* Money-path on-call within fifteen minutes for any ban that touches the executor.
* Data-plane owner for ingest-side throttling (SEV2, hours).
* Venue channel: our egress IP, the bucket key, and the first refusal's timestamp. Open a case that says "we were
  throttled at T, here is our traffic", never "your API is broken".
