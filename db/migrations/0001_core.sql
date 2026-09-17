-- P04 D3 · 0001 core: domains that exist before any money does.
-- Money columns are BIGINT micro-units, never NUMERIC and never float. NUMERIC would be *acceptable*
-- but it invites a silent scale change in a later ALTER, and BIGINT + a documented scale is the one
-- representation nobody can mis-read at 3am. Scale is 1e-6 for USDC and for shares (both are 6dp on
-- chain; measured in P01 from the token decimals and the CLOB book).

CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- gen_random_uuid()

CREATE TABLE events (
    id              TEXT PRIMARY KEY,          -- Gamma event id, kept as text: they are opaque and may grow
    slug            TEXT NOT NULL,
    title           TEXT NOT NULL,
    neg_risk        BOOLEAN NOT NULL DEFAULT FALSE,
    -- neg_risk is the flag that changes PnL arithmetic (see 0002 + polygm_core.ledger). Storing it here
    -- rather than deriving it per query is deliberate: the derivation needs every market in the event,
    -- and a partially-loaded event would compute the wrong thing.
    category        TEXT,
    start_ts        TIMESTAMPTZ,
    end_ts          TIMESTAMPTZ,
    created_ms      BIGINT NOT NULL,
    updated_ms      BIGINT NOT NULL
);
CREATE UNIQUE INDEX events_slug_uq ON events (slug);

CREATE TABLE markets (
    id                  TEXT PRIMARY KEY,       -- Gamma market id
    condition_id        TEXT NOT NULL,          -- CLOB condition id; what the venue speaks
    event_id            TEXT REFERENCES events(id) ON DELETE SET NULL,
    question            TEXT NOT NULL,
    slug                TEXT,
    -- the four fields the risk gate reads before it will sign anything (P04 D5.2). NOT NULL where the
    -- venue always returns them: a NULL here means "we have not read this market", which must not be
    -- confusable with "this market accepts orders".
    accepting_orders    BOOLEAN NOT NULL,
    seconds_delay       INT NOT NULL DEFAULT 0,
    enable_order_book   BOOLEAN NOT NULL DEFAULT TRUE,
    minimum_tick_size   NUMERIC(6,4) NOT NULL,  -- 0.001 | 0.01 observed (P01). NUMERIC is correct here:
                                                -- this is *metadata*, not money, and it is never summed.
    minimum_order_size  NUMERIC(18,6) NOT NULL DEFAULT 5,
    fee_type            TEXT NOT NULL,          -- read per market; the taxonomy is open, so no CHECK list
    neg_risk            BOOLEAN NOT NULL DEFAULT FALSE,
    neg_risk_group_id   TEXT,
    end_ts              TIMESTAMPTZ,
    outcomes_json       JSONB NOT NULL DEFAULT '[]',   -- 128..315 outcomes seen in P01; never a wide table
    outcome_count       INT GENERATED ALWAYS AS (jsonb_array_length(outcomes_json)) STORED,
    -- Reasoning for the two indexes above: a terminal lists events by "ends soonest, still trading", and
    -- an event page needs all its markets. outcome_count is generated because the neg-risk layout decision
    -- ("does this need the 315-row scroll handler?") is a per-request comparison we must not compute by
    -- parsing JSON in the hot path.
    first_seen_ms       BIGINT NOT NULL,
    updated_ms          BIGINT NOT NULL,
    CONSTRAINT market_tick_sane CHECK (minimum_tick_size > 0 AND minimum_tick_size <= 0.1),
    CONSTRAINT market_size_sane CHECK (minimum_order_size > 0)
);
CREATE INDEX markets_event_ix ON markets (event_id);
CREATE INDEX markets_ends_ix  ON markets (end_ts) WHERE accepting_orders;   -- partial: only live markets are listed

CREATE TABLE tokens (
    token_id     TEXT PRIMARY KEY,             -- CLOB token id (a huge decimal string; TEXT, never numeric)
    market_id    TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    outcome      TEXT NOT NULL,                -- "Yes" | "No" | candidate name in a multi-outcome market
    outcome_index INT NOT NULL DEFAULT 0,
    is_winner    BOOLEAN,                      -- NULL until resolved: NULL must not read as FALSE
    unique (market_id, outcome_index)
);
CREATE INDEX tokens_market_ix ON tokens (market_id);

CREATE TABLE book_levels (
    -- Normalised snapshot from `ingest`. One row per (market, side, price) with AGGREGATED size, because
    -- the design system specifies aggregate-by-level rendering (D4a) and the venue returns per-order rows.
    market_id           TEXT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    side                TEXT NOT NULL CHECK (side IN ('bid','ask')),
    price_micro         BIGINT NOT NULL CHECK (price_micro > 0 AND price_micro < 1000000),
    size_shares_micro   BIGINT NOT NULL CHECK (size_shares_micro >= 0),
    level_count         INT NOT NULL DEFAULT 1,
    updated_ms          BIGINT NOT NULL,
    PRIMARY KEY (market_id, side, price_micro)
);
-- Deliberately no index on updated_ms: the read path is "give me this market's ladder", which the PK
-- already serves, and a second index on a table rewritten ~15-33x/sec is write amplification we pay for
-- on every tick and use never.

CREATE TABLE tape_trades (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price_micro     BIGINT NOT NULL CHECK (price_micro > 0 AND price_micro < 1000000),
    size_shares_micro BIGINT NOT NULL CHECK (size_shares_micro > 0),
    taker_is_maker  BOOLEAN NOT NULL DEFAULT FALSE,
    exchange_ts     BIGINT NOT NULL,             -- venue clock; used for ordering, never for "how old is this"
    ingest_ms       BIGINT NOT NULL,             -- our clock; used for freshness. Mixing the two is how a
                                                 -- tape shows a 4-hour-old trade as brand new.
    raw_json        JSONB NOT NULL
);
CREATE INDEX tape_market_time_ix ON tape_trades (market_id, exchange_ts DESC);
CREATE INDEX tape_time_ix        ON tape_trades (exchange_ts DESC);
-- Retention: this table is the one place a columnar store earns its keep, but it is NOT in the money path,
-- so it stays here in Postgres with a 7-day window until P05 proves the volume needs ClickHouse
-- (D2 decision, defended in docs/P04-backend-architecture.md rather than by fashion).

CREATE TABLE users (
    id              TEXT PRIMARY KEY,           -- our id, stable across wallet changes
    stonks_address  TEXT,                       -- Polymarket proxy wallet (0x…); NULL until deposit
    created_ms      BIGINT NOT NULL,
    tier            TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free','trader','pro','team')),
    -- created here, not in 0002: idempotency_keys references it, and 0002 references both. Dangling FKs
    -- would make `make migrate` fail on a fresh database, which is the one machine that must work.
    entitlement_until_ms BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE idempotency_keys (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    request_hash    TEXT NOT NULL,               -- sha256 of the canonical body; a same-key/different-body
                                                 -- retry is a CLIENT bug and must be a 409, never a replay
    state           TEXT NOT NULL CHECK (state IN ('in_progress','done')),
    response_json   TEXT,                        -- stored verbatim so a retry replays the SAME answer
    order_hash      TEXT,
    created_ms      BIGINT NOT NULL DEFAULT (floor(extract(epoch from now()) * 1000)::BIGINT),
    PRIMARY KEY (user_id, key)
);
CREATE INDEX idem_age_ix ON idempotency_keys (created_ms) WHERE state = 'in_progress';
-- in_progress rows older than ~10 min mean a process died between begin() and finish(). They are cleaned
-- by `worker`, NOT by a TTL in Redis: an in_progress marker that silently expires is a double-spend
-- waiting for the retry to arrive.

CREATE TABLE kill_switch_state (
    id              BIGSERIAL PRIMARY KEY,
    engaged         BOOLEAN NOT NULL,
    reason          TEXT NOT NULL CHECK (length(reason) BETWEEN 4 AND 400),  -- a switch pulled without a
                                                 -- reason is unreviewable; enforced here so it cannot be
                                                 -- bypassed by writing straight to the table
    changed_by      TEXT NOT NULL,
    at_ms           BIGINT NOT NULL
);
-- Append-only, deliberately: the CURRENT state is the last row, not a mutable column. "When was trading
-- disabled and why" is evidence, and an UPDATE would destroy the only copy of that answer.

