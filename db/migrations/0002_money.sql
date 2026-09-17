-- P04 D3 · 0002 the money path. Append-only by construction (P04 rule 4): the UPDATE/DELETE guards live
-- in 0005_triggers.sql, so this is enforced by the database and not by developer discipline.
--
-- order_intents (requested) → orders (submitted) → fills (matched).
-- Three tables, not one, because their failure modes differ: an intent that never reached the venue can be
-- cancelled silently; an order that did must be tracked until the venue says otherwise; and a fill is a
-- fact about the past that nothing may revise.

CREATE TABLE balances (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    -- our VIEW of the balance. Not the truth: the truth is GET /data/balance-allowance, which is limited
    -- to 200 req/10s across ALL users (shared context §2), so it cannot be read per order. See
    -- reconcile_ms for how stale "current" is allowed to be, and docs/P04 §D3.2 for the argument.
    usdc_available_micro   BIGINT NOT NULL DEFAULT 0 CHECK (usdc_available_micro >= 0),
    usdc_locked_micro      BIGINT NOT NULL DEFAULT 0 CHECK (usdc_locked_micro >= 0),
    version         BIGINT NOT NULL DEFAULT 0,   -- optimistic lock: two concurrent fills on one row must
                                                 -- not interleave into a balance that never existed
    reconcile_ms    BIGINT NOT NULL DEFAULT 0,
    CHECK (usdc_available_micro + usdc_locked_micro >= 0)
);

-- (created in 0001 alongside the users table: a governance table cannot depend on one that does not exist yet)

CREATE TABLE order_intents (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id),
    market_id           TEXT NOT NULL,
    token_id            TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price_micro         BIGINT NOT NULL CHECK (price_micro > 0 AND price_micro < 1000000),
    size_micro          BIGINT NOT NULL CHECK (size_micro > 0),
    notional_micro      BIGINT NOT NULL CHECK (notional_micro >= 0),
    -- state is a CHECK'd text column rather than a PG ENUM: an ENUM makes a rollback painful (ALTER TYPE
    -- inside a migration that a live replica is reading), and this set changes during P05-P06.
    state               TEXT NOT NULL CHECK (state IN ('pending','rejected','queued','submitting',
                                                        'uncertain','submitted','cancelled')),
    risk_code           TEXT,
    risk_latency_ms     NUMERIC(10,3),
    idempotency_key     TEXT NOT NULL,
    venue_order_id      TEXT,
    client_order_hash   TEXT,
    created_ms          BIGINT NOT NULL,
    updated_ms          BIGINT NOT NULL DEFAULT 0,
    UNIQUE (user_id, idempotency_key)            -- the DB, not a cache, decides a replay
);
CREATE INDEX intents_open_ix  ON order_intents (user_id, state, created_ms DESC)
    WHERE state IN ('pending','queued','submitting','uncertain','submitted');
CREATE INDEX intents_stuck_ix ON order_intents (updated_ms) WHERE state = 'uncertain';
-- intents_stuck_ix exists for the executor's recovery loop and for the pager: a row that has been
-- 'uncertain' for minutes is the single most expensive thing in this system.

CREATE TABLE orders (
    id                  TEXT PRIMARY KEY,        -- the venue's orderID
    intent_id           TEXT NOT NULL REFERENCES order_intents(id),
    user_id             TEXT NOT NULL REFERENCES users(id),
    token_id            TEXT NOT NULL,
    market_id           TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price_micro         BIGINT NOT NULL,
    size_micro          BIGINT NOT NULL,
    size_matched_micro  BIGINT NOT NULL DEFAULT 0 CHECK (size_matched_micro >= 0),
    state               TEXT NOT NULL CHECK (state IN ('live','partial','filled','cancelled','expired','rejected')),
    -- Signed-but-unacknowledged is a distinct state from live, because the executor's restart logic treats
    -- them completely differently: live may be cancelled, unacknowledged must be reconciled first.
    acknowledged        BOOLEAN NOT NULL DEFAULT FALSE,
    builder_code        TEXT NOT NULL,
    metadata            TEXT,
    expiration_ts       BIGINT NOT NULL DEFAULT 0,
    placed_ms           BIGINT NOT NULL,
    updated_ms          BIGINT NOT NULL,
    CHECK (size_matched_micro <= size_micro)
);
CREATE INDEX orders_user_state_ix ON orders (user_id, state, placed_ms DESC)
    WHERE state IN ('live','partial');            -- the open-orders query, which runs on every submit
CREATE INDEX orders_intent_ix     ON orders (intent_id);

CREATE TABLE fills (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            TEXT NOT NULL REFERENCES orders(id),
    trade_id            TEXT,                     -- venue trade id when the WS gives one
    taker_order_id      TEXT,
    side                TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price_micro         BIGINT NOT NULL CHECK (price_micro > 0 AND price_micro < 1000000),
    size_micro          BIGINT NOT NULL CHECK (size_micro > 0),
    notional_micro      BIGINT NOT NULL,
    fee_micro           BIGINT NOT NULL DEFAULT 0,
    maker               BOOLEAN NOT NULL,
    exchange_ts         BIGINT NOT NULL,
    ingest_ms           BIGINT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('ws','rest_poll','reconcile','on_chain')),
    -- the venue can report the same fill twice (WS plus the reconciliation poll). Deduplicating on a
    -- partial unique index is cheaper and more honest than a merge-on-write, and it keeps BOTH rows'
    -- provenance in `source` via the conflict path rather than losing the second sighting entirely.
    raw_json            JSONB NOT NULL
);
CREATE UNIQUE INDEX fills_dedupe_ix ON fills (order_id, exchange_ts, price_micro, size_micro, maker);
CREATE INDEX fills_order_ix ON fills (order_id);

CREATE TABLE cash_ledger (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    kind            TEXT NOT NULL CHECK (kind IN ('deposit','withdraw','buy','sell_fill','merge_receipt',
                                                  'resolution_payout','fee','refund','adjust')),
    amount_micro    BIGINT NOT NULL CHECK (amount_micro <> 0),   -- a zero row is a bug wearing evidence
    ref_table       TEXT NOT NULL,
    ref_id          TEXT NOT NULL,
    -- A (ref_table, ref_id, kind) uniqueness constraint is what stops a reconciliation re-run from
    -- crediting the same payout twice. It is the difference between "idempotent code" and "idempotent
    -- system", because the re-run that matters is the one a human triggers with psql at 3am.
    created_ms      BIGINT NOT NULL,
    reason          TEXT NOT NULL,
    UNIQUE (ref_table, ref_id, kind, user_id)
);
CREATE INDEX cash_user_time_ix ON cash_ledger (user_id, created_ms DESC);

CREATE TABLE position_lots (
    -- Cost basis per acquisition, so a merge or partial close releases basis deterministically (floor of
    -- the pro-rata share, in integers — see polygm_core/ledger/ledger.py::Position.sell).
    id                  BIGSERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id),
    token_id            TEXT NOT NULL,
    market_id           TEXT NOT NULL,
    shares_open_micro   BIGINT NOT NULL CHECK (shares_open_micro >= 0),
    basis_micro         BIGINT NOT NULL CHECK (basis_micro >= 0),
    opened_ms           BIGINT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('fill','merge','transfer','adjust')),
    CHECK (shares_open_micro = 0 OR basis_micro > 0)
);
CREATE INDEX lots_user_token_ix ON position_lots (user_id, token_id) WHERE shares_open_micro > 0;

CREATE TABLE position_snapshots (
    -- Reconciliation against on-chain truth. A snapshot is a fact we observed, so it is never updated:
    -- a corrected snapshot is a new row with a later observed_ms.
    id                  BIGSERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id),
    token_id            TEXT NOT NULL,
    shares_micro        BIGINT NOT NULL CHECK (shares_micro >= 0),
    our_computed_micro  BIGINT NOT NULL,
    delta_micro         BIGINT GENERATED ALWAYS AS (shares_micro - our_computed_micro) STORED,
    source              TEXT NOT NULL CHECK (source IN ('on_chain','data_api','clob')),
    observed_ms         BIGINT NOT NULL
);
CREATE INDEX snap_delta_ix ON position_snapshots (observed_ms DESC) WHERE shares_micro <> our_computed_micro;
-- The partial index above is the "everything is fine" query the dashboard runs every load, and it is the
-- non-obvious one: filtering on a generated column's *result* in the predicate lets a healthy system keep
-- an index the size of its incident history instead of the size of its whole history.

CREATE TABLE builder_attribution (
    -- Every order we submit, so our revenue can be reconciled against on-chain events INDEPENDENTLY of
    -- Polymarket's own reporting (P04 D3). Written even for rejected orders: a rejected order proves we
    -- tried, which matters when the dispute is "we never got that fee".
    id                  BIGSERIAL PRIMARY KEY,
    order_id            TEXT,
    intent_id           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    builder_code        TEXT NOT NULL,
    fee_bps_expected    INT NOT NULL,
    fee_micro_expected  BIGINT NOT NULL,
    fee_micro_observed  BIGINT,                  -- from on-chain / /v1/builder trades, filled by `worker`
    market_id           TEXT NOT NULL,
    token_id            TEXT NOT NULL,
    placed_ms           BIGINT NOT NULL,
    CHECK (fee_bps_expected BETWEEN 0 AND 1000)
);
CREATE INDEX attribution_unreconciled_ix ON builder_attribution (placed_ms)
    WHERE fee_micro_observed IS NULL;
CREATE INDEX attribution_day_ix ON builder_attribution (date_trunc('day', to_timestamp(placed_ms / 1000.0)),
                                                        fee_micro_expected);
-- attribution_day_ix exists because the founder's actual question is monthly ("are we being paid"), and an
-- index that cannot answer it turns the answer into a full scan of the only table that grows with order
-- count. date_trunc in an index is allowed; the expression must be IMMUTABLE, hence to_timestamp on a
-- BIGINT rather than on now().
