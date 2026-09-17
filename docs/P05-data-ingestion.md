# P05 — data ingestion, signals, alerts (IN PROGRESS)

Built and tested in this session; the phase's remaining items are named at the bottom so nothing here reads as
finished that is not.

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
- **`last_trade_price` carries no transaction hash.** So cross-source dedupe by the durable key is impossible,
  and the design says so instead of faking it: **REST is the record, the WS is the latency layer.** The WS path
  feeds the alert engine (a whale fill should page in ~0.1 s, not ~2 s) and the UI's "already booked" marker;
  it never writes the durable tape. Duplicate *alerts* are prevented by the rule's dedupe key + cooldown, which
  is assertion B of the chaos test.
- **Up/down crypto markets are not only 5-minute**: `-updown-5m-`, `-updown-15m-` (and 1h/4h/daily) exist, and
  the bucket's start timestamp is **in the slug**, so the lifecycle needs no lookup.
- The Postgres migration chain is **0001, 0002, 0004, 0005, 0006**: there is no `0003`. Cosmetic (the migrator
  applies in filename order and ledgers per name), and recorded because the P04 test that "checked" it asserted
  `len == 4` — a snapshot of one day's work that went red for the wrong reason when P05 added a file. The
  replacement assertion is "strictly increasing and unique" plus the P04 layers by name.
- `feeType` now includes **`sports_fees_v3`** (P01 recorded `sports_fees_v2`): the fee enum is not closed, so
  fee handling must default to "unknown fee type → treat as charged, flag it", never "not in the list → free".
- `version: "v1"` on a Gamma market row is Gamma's row-version marker, **not** the CLOB version. Nothing in
  this layer reads it; a test asserts that, so nobody "fixes" CLOB V1 back into existence.

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
`db/migrations/0006_ingest.sql` (cursors, meta versions, `tape_fills` with the UNIQUE dedupe key and a partial
index for big fills, rollups, labels + history, signals/state/deliveries, user rules) and the regenerated SQLite
subset. 43 ingest tests + 22 signal tests; **221 tests OK**.

## The chaos run (300 s WS outage, 2026-09-17T12:14Z, docs/verification/P05-chaos-output.txt)

A stale indicator held for all 300 samples; zero duplicate alerts; the two clocks genuinely collided (live WS
trades seen while REST booked 10,412 overlapping rows through the UNIQUE dedupe key); the book matched the
venue within one tick after reconnect and the resync machinery engaged 79 times; the tape kept filling from REST
while the socket was dead (+127 rows). **Check C is inconclusive and the run is red because of it**: the
reference query pages /trades by offset and reaches only ~50-100 s, so the first two thirds of the outage were
never compared. That is recorded rather than smoothed over, and it is the first item below.

Re-verified after P05 landed: 221 tests OK, P04 gate **55/55**, `tools/lint-rules.py` had to learn that
`statistics` is stdlib (the core rule is about third-party dependencies, not the standard library).

## Upstream, right now (2026-09-17T12:2xZ, from `make probe-fresh`)

- `clob-book-fields` and `clob-spread` (P01's checks) are FAILING: the CLOB payloads changed shape. First thing
  to re-verify in P06, before any order builder is written against them.
- The edge cache on `/trades` is inconsistent under busting (once newer, once identical, minutes apart). The
  design already treats the WebSocket as the tape and REST as catch-up; nothing may depend on cache busting.

## Still open for this phase
1. Make check C conclusive: time-window the reference query in `venue_fills_in_window`, re-run the 300 s outage, and keep the run red until it passes.
2. Alert fanout + Telegram budget/SLO (D7) — `alert_deliveries` exists, the dispatcher does not yet.
3. `ingest/main.py` — the process wiring those modules into a loop that writes rows (the pieces are tested,
   the loop is not).
4. `tools/p05-gate-check.py` + `tools/p05-mutation-test.py` + `make p05`, and the ClickHouse alternative DDL.
