-- P05: the ingest layer's tables. Money-shaped rules from P04 still apply: integers in micro-units, no
-- NUMERIC/REAL for anything that can be owed, append-only for anything a user could be accused of.
--
-- Two rulings the rest of the system depends on, both forced by a collision this file caused:
--
-- 1. **Rules.** `alert_rules` already exists (0004: a user's target + spam cap + channels). This file used to
--    `CREATE TABLE IF NOT EXISTS alert_rules` with the *engine's* shape on top of it, which on a real database
--    means the second definition is silently skipped and every ingest read of `params_json`/`cooldown_s`
--    fails. `IF NOT EXISTS` makes a shadowed table invisible, which is why it is spelled out here: the
--    product rule and the engine rule are **two tables, joined by a shared id** (P09's builder writes both in
--    one transaction). They are not merged because 0004 constrains `kind` to a vocabulary that predates this
--    engine, and rewriting a signed-off migration to widen a CHECK is worse than a documented join.
-- 2. **The tape.** `tape_trades` (0001) and `tape_fills` (this file) are the same venue fills in two shapes.
--    This phase is the authority: **ingest writes `tape_fills` and nothing writes `tape_trades` any more** —
--    a dual write is exactly the "two sources of truth for the same fills" bug the note below warns about.
--    `tape_trades` is frozen as the fixture table the P04 `/v1/tape` route reads (that route's JSON keeps
--    `maker`, which `tape_fills` cannot supply because the venue's public tape has no maker/taker flag, so
--    repointing it is a contract change, not a rename). `docs/P05-data-ingestion.md` carries the task for the
--    phase that serves live tape data.
--
-- Deliberately absent: ClickHouse. docs/P04-backend-architecture.md D2 chose Postgres for the tape at the
-- measured 15-35 fills/s (~2.6M rows/day), and `db/clickhouse/tape.sql` documents the DDL we would move to.
-- Two sources of truth for the same fills would be the bug, not the optimisation.

-- The durable cursor set. Restart behaviour is "resume, not now" (D8): a consumer that restarts from now
-- opens a hole in the tape exactly when the process was least healthy, and the hole is invisible.
CREATE TABLE IF NOT EXISTS ingest_cursors (
    source        TEXT PRIMARY KEY,               -- 'gamma.markets', 'ws.tape', 'clob.history'
    cursor_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_event_ms BIGINT NOT NULL DEFAULT 0,      -- newest VENUE timestamp seen (not wall clock: a replay of
                                                  -- yesterday's feed must not look like fresh data)
    last_frame_ms BIGINT NOT NULL DEFAULT 0,      -- newest anything-arrived, heartbeat included
    state         TEXT NOT NULL DEFAULT 'ok'
                  CHECK (state IN ('ok','lagging','stale','silent','down')),
    updated_ms    BIGINT NOT NULL
);

-- Metadata versioning. Keyed on the GAMMA market id, unlike the four tables below it: this one records changes
-- to a Gamma row, so the id of that row is its identity, and nothing here joins to the tape.
-- `field`+`old`/`new` is the whole table's purpose: an alert must be able to say "the end
-- date moved", and a user who is told they are wrong about a date they never saw is a user who leaves.
CREATE TABLE IF NOT EXISTS market_meta_versions (
    id            BIGSERIAL PRIMARY KEY,
    market_id     TEXT NOT NULL,
    field         TEXT NOT NULL,
    old_value     TEXT NOT NULL,
    new_value     TEXT NOT NULL,
    seen_ms       BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS market_meta_versions_market_time_idx ON market_meta_versions (market_id, seen_ms DESC);

-- The tape, normalised per D4. `ts_ms` is the venue's clock; `ingest_ms` is ours; the difference is lag, and
-- lag is the number the alarm reads, so both are stored rather than derived at query time.
CREATE TABLE IF NOT EXISTS tape_fills (
    trade_id           BIGSERIAL,
    dedupe_key         TEXT NOT NULL,             -- sha256 of (tx,token,side,price,size); the UNIQUE below
    condition_id       TEXT NOT NULL,              -- the VENUE id, not markets.id: see the bridge note
    token_id           TEXT NOT NULL,
    outcome            TEXT,
    outcome_index      INT,                       -- NULL when the venue sent its 999 sentinel
    wallet             TEXT NOT NULL,
    side               TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price_micro        BIGINT NOT NULL CHECK (price_micro > 0 AND price_micro < 1000000),
    size_micro         BIGINT NOT NULL CHECK (size_micro > 0),
    usd_notional_micro BIGINT NOT NULL CHECK (usd_notional_micro >= 0),
    ts_ms              BIGINT NOT NULL,
    ingest_ms          BIGINT NOT NULL,
    source             TEXT NOT NULL CHECK (source IN ('ws','rest')),
    fee_rate_bps       INT NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_id),
    UNIQUE (dedupe_key)
);
CREATE INDEX IF NOT EXISTS tape_fills_condition_time_idx ON tape_fills (condition_id, ts_ms DESC);
CREATE INDEX IF NOT EXISTS tape_fills_wallet_time_idx ON tape_fills (wallet, ts_ms DESC);
CREATE INDEX IF NOT EXISTS tape_fills_big_idx ON tape_fills (usd_notional_micro DESC, ts_ms DESC)
    WHERE usd_notional_micro >= 10000000000;      -- the whale query, partial because big fills are rare: an
                                                  -- index over the whole tape for a 0.2% subset is a tax on write
                                                  -- throughput, which is the resource this table actually needs

-- Rollups: 1m/5m/1h/1d volume and VWAP per market, and per-wallet daily aggregates. Every derived number in the
-- UI is reproducible from stored rows (P05 constraint), which is what these are for; they are refreshed by
-- `worker`, not computed in the request path.
CREATE TABLE IF NOT EXISTS market_rollups (
    condition_id       TEXT NOT NULL,
    bucket_ms        BIGINT NOT NULL,              -- start of the bucket, aligned to the interval
    interval         TEXT NOT NULL CHECK (interval IN ('1m','5m','1h','1d')),
    fills            INT NOT NULL DEFAULT 0 CHECK (fills >= 0),
    volume_micro     BIGINT NOT NULL DEFAULT 0,
    vwap_micro       BIGINT NOT NULL DEFAULT 0,    -- sum(p*q)/sum(q), integer math on the way in
    max_fill_micro   BIGINT NOT NULL DEFAULT 0,
    median_fill_micro BIGINT NOT NULL DEFAULT 0,   -- approximate, from the sampled window; feeds `whale`
    PRIMARY KEY (condition_id, interval, bucket_ms)
);

CREATE TABLE IF NOT EXISTS wallet_rollups (
    wallet           TEXT NOT NULL,
    day_ms           BIGINT NOT NULL,
    fills            INT NOT NULL DEFAULT 0,
    volume_micro     BIGINT NOT NULL DEFAULT 0,
    markets          INT NOT NULL DEFAULT 0,
    buy_micro        BIGINT NOT NULL DEFAULT 0,
    sell_micro       BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, day_ms)
);

-- Labels. History is kept because a label that changed must be explainable ("why did this wallet stop being
-- smart money?" is a question users ask the moment the number moves).
CREATE TABLE IF NOT EXISTS wallet_labels (
    wallet           TEXT NOT NULL,
    label            TEXT NOT NULL,
    confidence       INT NOT NULL,                  -- 0-1000, integer so a float never enters the row
    evidence_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    publishable      BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_ms    BIGINT NOT NULL,
    last_seen_ms     BIGINT NOT NULL,
    PRIMARY KEY (wallet, label)
);

CREATE TABLE IF NOT EXISTS wallet_label_history (
    id               BIGSERIAL PRIMARY KEY,
    wallet           TEXT NOT NULL,
    label            TEXT NOT NULL,
    confidence       INT NOT NULL,
    publishable      BOOLEAN NOT NULL,
    seen_ms          BIGINT NOT NULL
);

-- Alerts and their suppression memory, plus the delivery attempt rows that make the SLO measurable.
-- `signals.rule_id` is a `signal_rules.id`, NOT an `alert_rules.id`.
CREATE TABLE IF NOT EXISTS signals (
    id               BIGSERIAL PRIMARY KEY,
    rule_id          TEXT NOT NULL,
    kind             TEXT NOT NULL,
    condition_id     TEXT NOT NULL,
    token_id         TEXT NOT NULL DEFAULT '',
    severity         TEXT NOT NULL CHECK (severity IN ('info','notice','urgent')),
    title            TEXT NOT NULL,
    body_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key       TEXT NOT NULL,
    fired_bucket     BIGINT NOT NULL DEFAULT 0,     -- cooldown window the fire was counted in: the UNIQUE
    fired_ms         BIGINT NOT NULL,               -- below is what makes "one alert per window" a database
    UNIQUE (rule_id, dedupe_key, fired_bucket)       -- property instead of a code path that can be forgotten
);

CREATE TABLE IF NOT EXISTS signal_state (
    rule_id          TEXT NOT NULL,
    dedupe_key       TEXT NOT NULL,
    last_fired_ms    BIGINT NOT NULL,
    PRIMARY KEY (rule_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id               BIGSERIAL PRIMARY KEY,
    signal_id        BIGINT NOT NULL,
    user_id          TEXT NOT NULL,
    channel          TEXT NOT NULL,
    priority         INT NOT NULL DEFAULT 2,        -- 0 paid, 1 trial, 2 free: the queue's only fairness input
    queued_ms        BIGINT NOT NULL,
    sent_ms          BIGINT,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','sent','dropped_rate_limited','digest_scheduled','failed')),
    reason           TEXT
);
CREATE INDEX IF NOT EXISTS alert_deliveries_pending_idx ON alert_deliveries (priority, queued_ms)
    WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS alert_deliveries_latency_idx ON alert_deliveries (sent_ms - queued_ms DESC, queued_ms DESC)
    WHERE status = 'sent';                          -- the SLO query, without a full scan at 10k deliveries/min

-- The bridge. Every ingest table above is keyed on the venue's condition id because that is what the venue
-- speaks (`/trades?market=`, `/book`, the WS frames), while the product is keyed on Gamma's market id because
-- that is what the API routes on. This unique index is the ONE allowed hop between the two, and it is unique so
-- a condition id that ever mapped to two market rows fails loudly instead of fanning out the tape.
CREATE UNIQUE INDEX IF NOT EXISTS markets_condition_uq ON markets (condition_id);

CREATE TABLE IF NOT EXISTS signal_rules (
    id               TEXT PRIMARY KEY,
    owner            TEXT NOT NULL,
    kind             TEXT NOT NULL,
    params_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    market_filter_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    cooldown_s       INT NOT NULL DEFAULT 300,
    severity         TEXT,
    channels_json    JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_ms       BIGINT NOT NULL,
    updated_ms       BIGINT NOT NULL
);
