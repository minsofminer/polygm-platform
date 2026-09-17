-- P05 CAPACITY DECISION: THE TAPE STAYS IN POSTGRES. This file is the alternative, specified and priced, not
-- adopted. The prompt asked for an answer to "ClickHouse or Postgres for the tape", and an answer is a number
-- plus a trigger, not a preference.
--
-- ── what was measured (2026-09-17, tools/p05-chaos-test.py + bounded data-api probes) ────────────────────────
--   Venue-wide taker tape: a 500-row page (the endpoint's own cap) spanned 36 s of venue timestamps
--     => >= 13.9 fills/s venue-wide, i.e. >= ~1.2M fills/day across ALL markets, and that is a floor because the
--        page was saturated, not a rate we read off an unconstrained query.
--   Our retained subset: 8 subscribed markets produced ~10 rows/s while a REST catch-up window was being
--     replayed, and ~1 row/s at steady state on the same subjects (smoke run: 288 rows in a 30 s warm-up that
--     included the initial backfill, +1309 rows across a 120 s outage where the poller double-covered).
--   Stored width per fill in `tape_fills`: 13 columns, ~64 B of values + 32 B dedupe_key + two indexes ~= 200 B
--     per row including indexes. `book_levels` for the whole top-N set costs 2,000 books ~= 16.5 MB measured,
--     which is the other half of the write load and is a DELETE+INSERT per refresh, not an append.
--   Postgres 17 on the dev box (docker-compose volume, fsync on, synchronous_commit off) inserted the
--     fixture's 200 rows in ~4 ms inside one transaction, and the P04 gate's ledger tests run the same engine.
--
-- ── why Postgres wins here, in the product's own terms ──────────────────────────────────────────────────────
--   1. The joins the labels need are transactional and cross-domain: `tape_fills` x `position_lots` x
--      `orders` x `users` (smart_money needs settled PnL; insider_suspect needs the user's own orders). The
--      whole point of the classifier is that it reads OUR ledger and OUR tape in one query. In ClickHouse that
--      becomes a batch job against a copy, and "our copy is 90 s behind" is the exact staleness class P05
--      exists to eliminate.
--   2. Dedupe is a constraint, not a query. `UNIQUE (dedupe_key)` makes a replayed poll page structurally
--      harmless. ClickHouse's equivalent is ReplacingMergeTree, which collapses on merge — so for an unbounded
--      window between merges, `SELECT COUNT(*)` over a hot partition double-counts volume, and every derived
--      number (whale percentile, volume_spike baseline, the leaderboard) is wrong in the direction of loud.
--   3. Row count is not the constraint at this stage. 1.2M rows/day venue-wide is ~440M/year, which a
--      partitioned Postgres table with BRIN on ts and B-tree on (condition_id, ts) serves fine for the 30-day
--      product window; the up/down markets that dominate the count are also the ones we prune by policy
--      (0006: quiet >6 h AND <$50 AND no open alert), so retention is decided by the same rule that decides
--      subscription, not by storage economics.
--   4. One engine, one migration runner, one backup. The kit's phase order puts a ledger with real money at
--      P13; "two systems that must agree about what a user did" is the kind of split that shows up in a
--      settlement dispute at 3 a.m.
--
-- ── the trigger that would change this answer (write it down or it is a vibe) ───────────────────────────────
--   Move the tape (and only the tape) to ClickHouse when ANY of these is true and measured, not predicted:
--     a) sustained inserts > 250 rows/s into `tape_fills` (i.e. ~20x today's steady state, from widening the
--        watch universe rather than from the venue speeding up), or
--     b) `INSERT` p95 > 25 ms measured at the daemon for 15 consecutive minutes with autovacuum not thrashing,
--     c) a product requirement to keep > 180 days of raw fills (rollups stay in Postgres regardless: they are
--        small and are recomputed from stored rows, so they are a cache, not an archive), or
--     d) analytics queries (leaderboards, per-wallet daily aggregates) start blocking the ingest path: the
--        symptom is `tape_pages` latency rising during dashboard reads.
--   Conditions (a) and (d) are what a 10x-subject launch produces; (b) is the one that shows up first in
--   practice, and note that it says "measured at the daemon", not "in a benchmark".
--
-- ── if it is adopted, this is the shape, and these are the losses to accept out loud ────────────────────────
--   Accepted losses: no FK to `markets`/`users` (so a market rename desynchronises a name the API serves — the
--   ingest must denormalise question/slug into every row, which `tape_fills` already does); no transactional
--   dedupe (see 2 above); no `SELECT ... FOR UPDATE` (so the cursor for the ClickHouse writer moves to a
--   Postgres row, which is what `ingest_cursors` already is, so that cost is zero); no point update, so
--   `book_levels` stays in Postgres forever and ONLY the append-only fill log moves.
--
-- Run with: clickhouse-client --multiquery < db/clickhouse/tape.sql
-- Never run automatically: no phase in the kit provisions this, and a table nobody wrote to is a migration
-- that has already been lost.

CREATE DATABASE IF NOT EXISTS polygm;

-- MergeTree, not ReplacingMergeTree, for the primary copy: see the reasoning above about double-counting. If
-- this is ever adopted, the ingest writes with an explicit batch id and the dedupe window is enforced by the
-- `ORDER BY` key plus a `FINAL`-free read path that sums over one batch per fill — a design the Postgres
-- UNIQUE constraint gives us for free, which is the honest way to see the trade.
CREATE TABLE IF NOT EXISTS polygm.tape_fills_ch
(
    dedupe_key          String,
    batch_id            UInt64,
    condition_id        LowCardinality(String),
    token_id            String,
    market_slug         LowCardinality(String),
    outcome             LowCardinality(String),
    outcome_index       Nullable(UInt8),
    wallet              String,
    side                LowCardinality(String),
    price_micro         UInt64,
    size_micro          UInt64,
    usd_notional_micro  UInt64,
    fee_rate_bps        UInt16,
    ts_ms               UInt64,
    ingest_ms           UInt64,
    source              LowCardinality(String),
    -- Partitioning by day, not hour: at 1.2M rows/day venue-wide a day partition is ~1 GB, which is the range
    -- ClickHouse handles well; 24x more parts buys nothing we need and makes merges the bottleneck instead.
    -- `ts_ms` inside the ORDER BY is what makes a market's tape read a single range scan, which is the query
    -- this table exists for. A `wallet` ordering would serve the label job and starve the market job; the
    -- label job reads Postgres.
)
ENGINE = MergeTree
PARTITION BY toStartOfDay(toDateTime(intDiv(ts_ms, 1000)))
ORDER BY (condition_id, ts_ms, dedupe_key)
TTL toDateTime(intDiv(ts_ms, 1000)) + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;

-- The two read paths that would actually land here, both denormalised so neither joins:
CREATE TABLE IF NOT EXISTS polygm.tape_wallet_day_ch
(
    day                 Date,
    wallet              String,
    fills               AggregateFunction(count),
    volume_usd_micro    AggregateFunction(sum, UInt64),
    markets             AggregateFunction(uniqExact, String),
    big_fills           AggregateFunction(countIf, UInt8)
)
-- AggregatingMergeTree and not SummingMergeTree: `markets` is a cardinality, which cannot be summed, and the
-- honest alternative (a plain count over a group) is a second source of truth about how many markets a wallet
-- traded. Reads on this table MUST use the -Merge combinators (`countMerge(fills)`), or they read the raw state.
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (wallet, day);

-- Rollups, if they move at all. They are recomputed from stored rows in Postgres today (`market_rollups`),
-- which is only affordable because they are small; the day this stops being small is trigger (c).
CREATE TABLE IF NOT EXISTS polygm.market_rollups_ch
(
    bucket_start_ms     UInt64,
    condition_id        LowCardinality(String),
    interval            LowCardinality(String),
    volume_micro        SimpleAggregateFunction(sum, UInt64),
    buy_micro           SimpleAggregateFunction(sum, UInt64),
    sell_micro          SimpleAggregateFunction(sum, UInt64),
    notional_micro      SimpleAggregateFunction(sum, UInt64),
    fills               SimpleAggregateFunction(sum, UInt64),
    max_fill_micro      SimpleAggregateFunction(max, UInt64)
    -- No `vwap_micro` column, deliberately: VWAP is notional / volume, and averaging the VWAPs of two
    -- unequal buckets is wrong in a way nobody catches in review. Postgres keeps a vwap column because it
    -- RECOMPUTES each bucket from stored rows (0006's `market_rollups` are a cache, not an archive); a
    -- rollup-of-rollups here has to carry the numerator and denominator instead.
)
ENGINE = AggregatingMergeTree
PARTITION BY intDiv(bucket_start_ms, 86400000)
ORDER BY (condition_id, interval, bucket_start_ms);

CREATE MATERIALIZED VIEW IF NOT EXISTS polygm.tape_wallet_day_mv_ch TO polygm.tape_wallet_day_ch AS
SELECT
    toDate(toDateTime(intDiv(ts_ms, 1000)))     AS day,
    wallet                                      AS wallet,
    countState()                                AS fills,
    sumState(usd_notional_micro)                AS volume_usd_micro,
    uniqExactState(condition_id)                AS markets,
    countIfState(usd_notional_micro >= 10000000000) AS big_fills
FROM polygm.tape_fills_ch
GROUP BY day, wallet;

-- The $1,000,000,0000 in the line above is `usd_notional_micro` for $1,000, the same constant as
-- `tape_fills_big_idx` in 0006 and for the same reason: "large" has one definition in this product, and it is
-- the number the whale label and the P05 gate both read. If it is ever made configurable, both places change
-- together or the two views of the same fills stop agreeing.
