---
id: upstream-schema-change
order: 4
severity: SEV1/SEV2
owner: data plane + money path
alarms: []
last_drilled: 2026-09-23
---

# Upstream Polymarket schema change

**One line:** the venue changed the shape of its data. A V1→V2 migration broke every bot on the platform in
April, including ones that had been stable for a year. The difference between an incident and a disaster is
whether our parser fails **closed and loudly** or silently mis-reads a field.

No alarm points at this page: it is reached *from* an alarm (`source-lagging`, `elevated-rejections`) or from an
operator who noticed. That is deliberate — a schema change announces itself differently every time.

```bash
export PGM_DB_URL='postgresql://polygm:...@pgbouncer:5432/polygm'
API=https://api.polygm.trade
ADM=(-H "x-admin-token: $PGM_ADMIN_TOKEN")
```

## Symptoms

Any of these; the order matters, because the earliest one is the cheapest to diagnose:

* a spike in `orderPath.rejectionsByReason` for a parse or validation code;
* `freshness.feeds[*].state = down` with a parser error in the ingest log rather than a socket error;
* `tape_fills` stops growing while the socket is demonstrably alive;
* schema-shaped errors in the log: `KeyError`, `TypeError`, `unexpected field`, `missing field`.

```bash
curl -sS "${ADM[@]}" "$API/v1/admin/metrics" \
  | python3 -c 'import json,sys; o=json.load(sys.stdin)["data"]["orderPath"]; print(o["rejectionsByReason"], o["rejectionsLastHour"])'
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 30m ingest | grep -iE "parse|schema|keyerror|unexpected" | tail -30
```

## Diagnosis

1. **Capture the offending payload before it rolls out of the log window.** A schema change is diagnosed from
   bytes; a stack trace only tells you where we fell over.

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml logs --since 30m ingest \
  | grep -A3 -iE "unexpected|missing field|keyerror" | head -60 > /tmp/schema-evidence.txt
```

2. **Which parser, which field.** Normalisation (`services/ingest/normalise.py`) is the boundary and the V2 shapes
   are declared under `packages/polygm_core/venue/`. The captured payload is the diff.

```bash
python3 - <<'EOF'
import pathlib
for p in sorted(pathlib.Path("packages/polygm_core/venue").glob("*.py")):
    hits = [l.strip() for l in p.read_text().splitlines() if "V2" in l][:5]
    print(p, hits)
EOF
```

3. **Is the old shape still accepted?** If the venue kept the previous version alive, the fix is configuration and
   a redeploy rather than a parser change — the fastest safe path.

4. **What is mis-read, exactly?** The dangerous case is not a crash: it is a field that still parses with a new
   unit or scale (a float where micro-units were expected, a string where a number was). Check one fill by hand
   against the venue's own record:

```bash
psql "$PGM_DB_URL" -c "SELECT id, source, ingest_ms, notional_micro, raw_json FROM fills ORDER BY ingest_ms DESC LIMIT 3;"
```

   `raw_json` is stored for exactly this reason: the evidence outlives the event.

## Remediation

* **Fail closed first.** If any field is mis-parsed, turn the affected source off so the book goes stale and the
  gate refuses with `STALE_QUOTE`. A stale book is a visible problem; a wrong book is an invisible one.

```bash
curl -sS -X POST "${ADM[@]}" -H 'content-type: application/json' \
  -d '{"name":"venue_orders_enabled","value":false,"reason":"upstream schema change: failing closed pending the parser fix"}' \
  "$API/v1/admin/flags" | python3 -m json.tool
```

* **Fix forward in one commit:** the parser change, a fixture captured from the wire, and a contract test. The P13
  rule applies unchanged — a recorded payload is a test, a hand-written dict is a wish.
* **Never hand-edit `raw_json` or the tape.** The tape is append-only for this reason: a correction is a new row.

## Verification

* The source's `state` is `ok`, `eventLagMs` is falling, and `resyncs` is not climbing.
* The rejection rate for the parse code is back at baseline for a full fifteen minutes.
* `python3 tools/p15-synthetic.py --api "$API" --user u-synth --once --await 25` completes.
* The new fixture is committed beside the parser change and `make check` is green: a fix without a re-test is not
  a fix.

## Escalation

* Data-plane owner immediately for a hard parse failure.
* Money-path owner if any *numeric* field changed (`price`, `size`, `fee`): a silently mis-scaled number is a money
  bug, and the kill switch is the conservative answer until the arithmetic reconciles.
* Venue channel with the captured payload in the same message. It is their schema; the fix is theirs too.
