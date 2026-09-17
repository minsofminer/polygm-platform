# P05 — data ingestion, signals, alerts

The measured contract of the venue, the modules that implement it, and the live outage evidence. Every number here
came from a run in this repo; where a claim was inherited from P01 and turned out wrong, the wrong version is
kept below with a strikethrough-style heading rather than deleted, because "we believed that for a week" is the
useful part.

## Measured, from the live venue (tools/p05-capture-fixtures.py, fixtures under tests/fixtures/p05/)
- **Gamma `volume24hr` carries float noise** (`2101490.4147200002`). Money/price input stays exact-and-strict;
  venue statistics round at 6 dp into micro-units (`normalise.stat_micro`) and are described as estimates.
- **Timestamp units differ per source by design**: `/trades` and `/activity` are integer **seconds**, the
  WebSocket is a **millisecond string**. `ts_s_from_rest` / `ts_ms_from_ws` reject the other unit instead of
  silently reading 1970 (P04's seed bug, one level up).
- **`outcomeIndex: 999`** is the venue's "not applicable" sentinel (up/down markets, aggregate rows). It is
  normalised to NULL, never used as an index.
- **`POST /books` does not work**: HTTP 400 `{"error":"Invalid payload"}` for `{"token_ids":[…]}`,
  `{"asset_ids":[…]}`, a bare array, the query-string form, and with `bids`/`asks` added — including a single
  valid token id. `GET /book?token_id=` returns 200. The batch endpoint therefore cannot be relied on, and the
  per-token budget + memory arithmetic in `books.py` is sized for polling, not for batching.
- **A `price_change` frame carries `best_bid`/`best_ask` next to each delta.** There is no sequence number on
  this channel, so that pair is the gap detector: our derived top of book must equal the venue's declared top,
  and a disagreement means a delta was lost. The TTL (>3 s) and the `hash` field are backstops; the hash is
  recorded and diffed as a diagnostic only, because one capture cannot establish what it hashes.
- **A fill price from `data-api/trades` can arrive as IEEE float noise** — `0.1699999983` for 0.17, in **49 of
  the 200 rows** of the recorded fixture. The strict 6-decimal parser refused those rows, so a quarter of the
  tape was silently missing: volume rollups, whale percentiles and every `large_fill` alert are computed from
  what we keep, so the failure looked like a quiet market, not like a bug. `normalise.fill_micro` rounds a
  fill's price/size to the micro grid (at most 5e-7 of movement by construction) and refuses only non-numbers,
  negatives and absurd magnitudes; `/book` levels and the ORDER path keep the strict parser, because a resting
  order at an off-grid price is a venue bug worth seeing. `usd_notional_micro` is derived from the two stored
  integers so a row cannot contradict itself.
- **`last_trade_price` carries no transaction hash.** So cross-source dedupe by the durable key is impossible,
  and the design says so instead of faking it: **REST is the record, the WS is the latency layer.** The WS path
  feeds the alert engine (a whale fill should page in ~0.1 s, not ~2 s) and the UI's "already booked" marker;
  it never writes the durable tape. Duplicate *alerts* are prevented by the rule's dedupe key + cooldown, which
  is assertion B of the chaos test.
- **Up/down crypto markets are not only 5-minute**: `-updown-5m-`, `-updown-15m-` (and 1h/4h/daily) exist, and
  the bucket's start timestamp is **in the slug**, so the lifecycle needs no lookup.
- The Postgres migration chain is **0001, 0002, 0004, 0005, 0006, 0007**: there is no `0003`. Cosmetic (the
  migrator applies in filename order and ledgers per name), and recorded because the P04 test that "checked" it
  asserted `len == 4` — a snapshot of one day's work that goes red for the wrong reason when a phase adds a
  file. The replacement assertion is "strictly increasing and unique" plus the P04 layers by name.
- `feeType` now includes **`sports_fees_v3`** (P01 recorded `sports_fees_v2`): the fee enum is not closed, so
  fee handling must default to "unknown fee type → treat as charged, flag it", never "not in the list → free".
- `version: "v1"` on a Gamma market row is Gamma's row-version marker, **not** the CLOB version. Nothing in
  this layer reads it; a test asserts that, so nobody "fixes" CLOB V1 back into existence.

- **`markets` has no `closed` column, and inventing one is how 19 tests died.** P04 models resolution as
  `tokens.is_winner IS NOT NULL` (and tradeability as `accepting_orders = 0`). Writing `is_winner = 0` for an
  unresolved market is worse than a schema error — it makes every open market a *loss* in `smart_money`'s
  settled sample, which is the label that decides who looks smart. NULL-until-resolved is the only legal value,
  and `RESOLVED_SQL` in `main.py` is the single expression every query uses.
- **`db/migrations-sqlite/` drops every `ALTER TABLE`, because the generator treats them as Postgres-only**
  (triggers, `ADD COLUMN` with `IF NOT EXISTS`, `DEFERRABLE`). A phase that adds columns by `ALTER` therefore
  gets a dev/CI schema that is missing them while every migration test passes. `0007_ingest_market_stats.sql`
  is a `CREATE TABLE` for exactly that reason, and the generator now `SystemExit`s on an `ADD COLUMN` drop
  instead of recording it in `DROPPED.json` and moving on.

## What exists
`services/ingest/`: `net.py` (named token buckets per source, timeouts, jittered backoff, circuit breaker),
`wsclient.py` (stdlib WS: TLS upgrade, frames, fragmentation, ping/pong, `age_s`/`data_age_s`), `books.py`
(snapshot+delta, one-sided books have no mid, depth at ±1¢/±5¢, imbalance, gap detection, 200-sub sharding,
memory arithmetic: 2,000 books × 40 levels ≈ 16.5 MB), `tape.py` (durable key, late/out-of-order counters,
bounded dedupe window, up/down lifecycle), `freshness.py` (down > silent > stale > lagging > ok, heartbeats do
not advance the event clock, only down/silent page a human), `universe.py` (keyset backfill with a resumable
cursor, discovery poll with an overlap counter, resolution-vs-move classification, metadata versioning,
dead-market prune/wake with 10× hysteresis). `packages/polygm_core/signals/engine.py` (8 built-ins, user-
composable validated rules, cooldown + dedupe + suppression counts), `classify/labels.py` (whale by market
percentile with a floor and a minimum sample, smart money behind 20 settled positions, new wallet,
insider-suspect as a conjunction with `publishable=False` always, cluster by window-bucket, wash/copy-farm).
`signals/fanout.py` (the delivery plan: age
before priority so a class can be late but never starved, a per-user-per-cycle fairness cap so one user's burst
cannot occupy every worker, a visibility timeout that costs an attempt, dead-letter after `max_attempts` and dead
forever, deterministic jitter because two workers must agree on a retry time and `hash()` would not survive a
restart). `services/ingest/main.py` (the loop: universe pass, tape poll, WS service, book resync, label pass,
prune, status) with `db/migrations/0007_ingest_market_stats.sql` (`market_stats`: the venue's activity numbers
per market, the fill/book-change clocks the prune policy reads, and the verbatim `meta_json` the metadata diff
compares against). `db/clickhouse/tape.sql` is the alternative that was **not** adopted, priced against measured
fill rates, with the four conditions that would change the answer.
`db/migrations/0006_ingest.sql` (cursors, meta versions, `tape_fills` with the UNIQUE dedupe key and a partial
index for big fills, rollups, labels + history, signals/state/deliveries, user rules) and the regenerated SQLite
subset. 45 ingest tests + 22 signal tests + 44 `main.py` tests + 16 fanout tests; **267 tests OK**
(all offline; the only network-touching checks are `tools/p05-chaos-test.py` and `tools/datasource-probe.py`).

## The chaos run (the prompt's gate, executed live)

    make gate-p05            # 300 s outage, then the gate that reads the artifact it wrote

Recorded run: `docs/verification/P05-chaos-output.txt`, 2026-09-17T22:49Z, 8 subjects (16 tokens, one $6.95 M/24 h
event), 45 s warm-up, **300 s outage**, 45 s recovery, 375 WS messages, 130 REST polls:

```
  reference tape lag: worst -0.0 s behind wall clock
  [PASS] A. the stale indicator flipped within 12s of the kill and held for 261/262 samples
  [PASS] B. no duplicate alerts (17 alerts, 17 rules fired)
  [PASS] B2. the two clocks actually collided (live fills seen=12, already-booked=0, durable merges=8129, alerts fired=17)
  [PASS] C. no missed large fills (>= $1000: 2 of 2 venue rows in the window)
  [PASS] D. book resynced to the venue within one tick after reconnect
  [PASS] D2. the resync machinery engaged (resyncs=603, gap detections=0)
  [PASS] E. the tape kept filling from REST while the socket was dead (+4348 rows)
```

What each line is allowed to mean, and what it is not:

- **A** is time-based on purpose. The indicator may read `ok` for one staleness window after the kill — that is the
  product's own `stale_ms` — so the assertion is "flips within `stale_ms` + 2 s, then never again", not "never
  again". An earlier version of the check demanded the impossible and failed a working system.
- **B** is only worth reading because **B2** says the two clocks actually collided: 12 live WS fills arrived while
  REST booked 8,129 overlapping rows through `UNIQUE (dedupe_key)`, and 17 alerts fired. A run where nothing fires
  is a run where dedupe was not exercised, so `p05-gate-check.py` refuses an artifact with `alerts = 0`,
  `live fills seen = 0`, or `covered != total`.
- **C**'s reference is the bounded `data-api/trades` range, and the `reference tape lag` line is printed with the
  verdict because "nothing was missed" rests entirely on how current the thing we compare against was. The first
  version of C compared an offset-paged view that had not reached the end of the window and reported a pass; that
  is recorded in §15 of the shared context as the phase's most expensive lesson.
- **D2** counts resyncs, and `gap detections` is reported even when it is 0: `price_change`'s declared
  `best_bid`/`best_ask` is the only gap detector this channel has, and a counter that only speaks on success is
  not a measurement.
- **E** is the reason the tape survives an outage rather than the alerts: the socket is the latency layer, REST is
  the record.

The kill is a real close (`WsClient.close()` from the harness), not a flag flip. That distinction mattered: with
"stop reading" the socket object stayed alive-looking for two samples and check A failed; closing makes `poll()`
raise, and the product's own error path produces the state. `WsClient.poll()` was fixed in the same pass — a
socket closed from another thread used to raise `AttributeError` on a `None` handle from inside the read loop,
which escapes as a dead thread instead of a `WsError` with `closed_reason = "closed locally"`.

## Shortfalls honestly listed

- **`gap detections = 0` in the recorded run.** The gap detector has fired in a synthetic test and never yet on
  live traffic; either the channel does not actually drop deltas at this subscription breadth, or our detection is
  too narrow. Not a passing claim, a measurement.
- **The alert *delivery* half (Telegram/push transport, the queue worker) is P10's.** P05 owns `alert_deliveries`,
  the priority classes, `fanout.plan()` and the fan-out rules; no code here talks to a provider, and the gate says
  so rather than implying a page was sent.
- **`/books` batching is unavailable**, so the per-token poll budget is the design, not a fallback (see §15).
- **`fee_type` is not observable per market from the public read**, so fee handling stays "unknown → charged and
  flagged" and no gate claims otherwise.

## Upstream, right now (2026-09-17T12:5xZ)

- `make probe-fresh` reported `clob-book-fields` and `clob-spread` newly failing, and I wrote here that the CLOB
  payloads had changed shape. **That was wrong and the note is replaced, not deleted:** a re-run has both
  passing with every documented field present, so they were transients. The real defect was in the checker —
  it printed failing ids with no status and no payload — and `--check-cache` now prints both and labels each
  line `TRANSIENT?` or `SHAPE`. `GET /book` keys, re-measured: `asks, asset_id, bids, hash, last_trade_price,
  market, min_order_size, neg_risk, tick_size, timestamp`.
- **The fee enum is open-ended.** Seven values across two runs of the same top-100 sample: `crypto_fees_v2`,
  `culture_fees`, `economics_fees`, `finance_prices_fees`, `politics_fees`, `sports_fees_v2`, `sports_fees_v3`
  — and the sets differed run to run, which is why `fee_type_observable_per_market` is no longer treated as a
  structural claim. Design consequence, and it is a money consequence: an unrecognised `feeType` means
  *charged and flagged*, never *free*, and no code may branch on "is this in the list I hard-coded".
- The edge cache on `/trades` is inconsistent under busting (once newer, once identical, minutes apart), so the
  WebSocket is the tape and REST is the catch-up; nothing may depend on cache busting.

## Phase state

The items this file used to carry under "still open" are closed, and how they closed is in `BUILD-LOG.md`:
check C is bounded and conclusive; `services/ingest/main.py` is a tested loop (44 tests, all offline);
`signals/fanout.py` exists with its own 16 tests; `db/clickhouse/tape.sql` is the priced alternative;
`tools/p05-gate-check.py` (14 executed checks) and `tools/p05-mutation-test.py` are wired into `make p05`,
`make gate-p05` and `make check`.
