---
id: source-silent
order: 3
severity: SEV1
owner: on-call (data plane)
alarms: [all-websocket-sources-dead, source-lagging]
last_drilled: 2026-09-23
---

# A WebSocket source has gone silent

**One line:** a socket that stays *open* and stops sending is the failure this part of the system exists for. The
connection state is green, the prices stop moving, and the product will happily trade on a stale book unless
something measures *frames* rather than sockets.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

* SEV1 `all-websocket-sources-dead`: every feed whose `transport` is `ws` is silent or has never been seen.
* SEV2 `source-lagging`: one source's `eventLagMs` is above its threshold (book and tape 3 s, metadata 120 s).
* In the product: the freshness chip on prices has gone from live to stale. A user can see this too.

## Diagnosis

1. **What the API believes, and why** — every number carries the reason it is stale:

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]["freshness"]; [print(f) for f in d["feeds"]]; print("silent:", d["silent"])'
```

   `eventLagMs` is the venue's event clock (a quiet market); `frameAgeMs` is our clock (a dead pipe). A **silent**
   source has fresh frames and an ancient event — that is a quiet market. A **neverSeen** source has no frame at
   all: that is a source that never started, which is a deployment bug, not a market one.

2. **The socket, from the ingest process** (`--status` prints one JSON line with `--once`):

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml exec ingest python3 -m services.ingest.main --once --status | python3 -m json.tool
docker compose -f /srv/polygm/docker-compose.prod.yml logs --tail=100 ingest | grep -iE "ws|resync|pong"
```

3. **Is the venue sending at all?** If frames arrive in a hand you open yourself and not in the service, the
   problem is on our side (a stuck reader, a full queue, a thread that died holding a lock).

```bash
curl -sS -o /dev/null -w 'book REST: %{http_code} in %{time_total}s\n' \
  "https://clob.polymarket.com/book?token_id=$(psql -tA "$PGM_DB_URL" -c 'SELECT token_id FROM tokens LIMIT 1')"
```

4. **Our egress is not blocked.** The executor host has a tight allowlist and the ingest host is looser, but a
   change in the egress script shows up here first.

```bash
sudo systemctl status polygm-egress --no-pager | head -20
sudo iptables -S OUTPUT | head -30
```

5. **Resync count.** A socket that re-handshakes every few minutes is not silent, it is *unstable*, and the fix is
   different. `freshness.feeds[*].resyncs` is that count, and it should be flat during a healthy day.

## Remediation

* **Quiet market, healthy pipe:** nothing. This is what the thresholds are for. No order is placed on a stale book
  because the gate refuses with `STALE_QUOTE` — that refusal is the product working, and it is counted, not paged.
* **Dead pipe, live process:** restart the ingest consumer for that source. It resumes from its cursor, and a
  replay is deduped by the tape's own key, so duplicates are not a risk.

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml restart ingest
docker compose -f /srv/polygm/docker-compose.prod.yml logs -f --tail=50 ingest
```

* **Every ws source dead at once:** assume the venue changed something (see
  `docs/runbooks/upstream-schema-change.md`) and treat the book as untrusted until proven otherwise — every quote
  on the screen is now older than its label claims, so the kill switch is the honest response.
* **Egress blocked:** re-apply the documented firewall state. `infra/terraform/cloud-init/` is the source of
  truth; a hand-edited rule is invisible to the next rebuild and will be lost by it.

## Verification

* `freshness.silent` is empty on three consecutive reads, and each ws feed has `state: ok` with a `frameAgeMs`
  under `SILENT_FEED_MS` (120 s).
* A fresh frame is a *new* one: watch `eventLagMs` fall rather than the socket state flip.

```bash
for i in 1 2 3; do curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]["freshness"]; print(d["silent"], [(f["source"], f["frameAgeMs"], f["state"]) for f in d["feeds"]])'; sleep 5; done
```

## Escalation

* One source for an hour: SEV2, ticket on the data-plane owner.
* All sources, any duration: SEV1, and the kill-switch decision belongs to the money-path on-call within fifteen
  minutes.
* If the venue's status page is green while ours says dead, escalate to the venue channel with the raw frames you
  captured — that evidence is what turns "your API is broken" into a fix.
