-- P06: the trading plane. Wallet lifecycle, order lifecycle beyond the three columns P04 could get away
-- with, the single reconciler, the risk limits the gate did not have, copy + automation state, and the
-- builder-revenue rows D8 demands.
--
-- Two rules this migration exists to enforce in the schema rather than in code, because code gets rewritten
-- and schemas get migrated with care:
--   1. nothing money-shaped here is a float, and nothing that represents a *decision* is editable in place;
--   2. every state a user can see has a row behind it that says WHY, so "why is my order stuck?" is a
--      SELECT, not a grep of journald.

BEGIN;

-- ---------------------------------------------------------------- D1: wallets and their lifecycle ----
CREATE TABLE wallets (
    user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL CHECK (provider IN ('turnkey','privy','dynamic','self_hosted')),
    -- The whole custody answer in one column: WE never hold a raw key. `delegated` = a scoped session key
    -- signs, the policy engine + our gate decide what it may sign.
    custody             TEXT NOT NULL CHECK (custody IN ('delegated','read_only')),
    address             TEXT,                        -- public, so storable; a deposit address is not a secret
    proxy_address       TEXT,
    signature_type      INTEGER NOT NULL DEFAULT 3 CHECK (signature_type IN (0,1,2,3)),
    -- The policy that was in force when this wallet was provisioned. If the live policy hashes differently
    -- the wallet is refused for trading, because "the limits silently changed" is the one failure mode a
    -- custodial setup must not have. Drift is detected at submit time, not at audit time.
    policy_hash         TEXT NOT NULL,
    -- Set when we need a policy the provider cannot express locally. The gate refuses that action class
    -- instead of trading without the guardrail (docs/P06 D1.4).
    policy_gap          TEXT,
    state               TEXT NOT NULL CHECK (state IN ('provisioned','funded','trading','suspended','closing')),
    created_ms          BIGINT NOT NULL,
    updated_ms          BIGINT NOT NULL,
    CHECK ((custody = 'delegated') = (signature_type IN (1,2,3)))
);

CREATE TABLE wallet_events (
    -- Every key-lifecycle event, appended. Export in particular: the phase demands it be "obvious and
    -- logged", and a log a user cannot read is not obvious.
    id                  BIGSERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event               TEXT NOT NULL,
    detail_json         TEXT NOT NULL DEFAULT '{}',  -- never contains key material; digests only
    actor               TEXT NOT NULL DEFAULT 'user', -- user | operator | system
    at_ms               BIGINT NOT NULL,
    CHECK (event IN
        ('provisioned','policy_drift','policy_gap','deposit_detected','deposit_credited','deposit_stuck',
         'allowance_granted','allowance_revoked','withdraw_requested','withdraw_sent','withdraw_denied',
         'export_requested','export_completed','suspended','reinstated','closed'))
);
CREATE INDEX wallet_events_user_idx ON wallet_events(user_id, at_ms DESC);

CREATE TABLE allowances (
    -- The ERC-20 approval state per wallet per token. Not a cache: a *claim*, so it carries when it was
    -- last checked and what it was checked against (docs/P06 D1.5 refuses trading if the spender moved).
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token               TEXT NOT NULL,               -- 'pUSD' today; 'USDC.e' on Polygon is not approved
    spender             TEXT NOT NULL,               -- must equal the venue's CTF exchange address
    -- 0 = no approval. The upper bound is not decoration: uint256 max is what the chain holds, and it does
    -- not fit an 8-byte integer on either engine, so `9223372036854775807` is our sentinel for "unlimited"
    -- (see polygm_core.venue.clob_v2.UNLIMITED_ALLOWANCE).
    amount_micro        BIGINT NOT NULL CHECK (amount_micro BETWEEN 0 AND 9223372036854775807),
    checked_ms          BIGINT NOT NULL,
    PRIMARY KEY (user_id, token, spender)
);

CREATE TABLE deposits (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    asset               TEXT NOT NULL,
    chain               TEXT NOT NULL,
    amount_micro        BIGINT NOT NULL CHECK (amount_micro > 0),
    tx_hash             TEXT,
    bridge_tx_hash      TEXT,
    -- Our own credit transaction: the idempotency key of the ledger entry, so a re-run of a stuck bridge
    -- credits once (rule 6), never twice.
    credit_key          TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,
    confirmations       INT NOT NULL DEFAULT 0,
    attempts            INT NOT NULL DEFAULT 0,
    last_error          TEXT,
    first_seen_ms       BIGINT NOT NULL,
    resolved_ms         BIGINT,
    CHECK (status = 'credited' OR resolved_ms IS NULL),
    CHECK (status IN
        ('detecting','confirming','bridging','crediting','credited','stuck','failed'))
);
CREATE INDEX deposits_user_idx ON deposits(user_id, first_seen_ms DESC);

CREATE TABLE withdrawals (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    asset               TEXT NOT NULL,
    chain               TEXT NOT NULL,
    amount_micro        BIGINT NOT NULL CHECK (amount_micro > 0),
    dest_address        TEXT NOT NULL,
    -- The typed confirmation, stored as TEXT because "the user typed 12.50" vs "the amount is 12500000"
    -- is a string comparison the UI can be tested against; a float round-trip would make it unfalsifiable.
    typed_amount        TEXT NOT NULL,
    typed_address       TEXT NOT NULL,
    allowlist_hit       BOOLEAN NOT NULL DEFAULT FALSE,
    cooldown_ok         BOOLEAN NOT NULL DEFAULT FALSE,
    -- The two notifications D1 demands the user cannot opt out of. FALSE at submit time is a bug we want
    -- the CHECK below to make unrepresentable.
    notified_email      BOOLEAN NOT NULL DEFAULT FALSE,
    notified_telegram   BOOLEAN NOT NULL DEFAULT FALSE,
    password_verified   BOOLEAN NOT NULL DEFAULT FALSE,
    status              TEXT NOT NULL,
    -- "The user cannot turn these off" is a schema property, not a UI promise: once a withdrawal has left,
    -- both delivery flags MUST be set. A caller that forgot is refused by the database, which is the only
    -- reviewer that never skips a line.
    CHECK (status NOT IN ('submitted','confirmed') OR (notified_email AND notified_telegram)),
    deny_code           TEXT,
    tx_hash             TEXT,
    idempotency_key     TEXT NOT NULL UNIQUE,
    requested_ms        BIGINT NOT NULL,
    completed_ms        BIGINT,
    CHECK (status IN
        ('awaiting_confirmation','queued','submitted','confirmed','failed','denied'))
);
CREATE INDEX withdrawals_user_idx ON withdrawals(user_id, requested_ms DESC);

CREATE TABLE address_allowlist (
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address             TEXT NOT NULL,               -- checksummed; compared case-exactly, never normalised
    added_ms            BIGINT NOT NULL,
    usable_ms           BIGINT NOT NULL,              -- added_ms + cooldown; a new address is unusable until
    added_by            TEXT NOT NULL DEFAULT 'user',
    note                TEXT,
    PRIMARY KEY (user_id, address)
);

-- ------------------------------------------------- D2/D7: order lifecycle, one row per transition ----
CREATE TABLE order_lifecycle (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            TEXT NOT NULL,               -- venue id, or '' while unknown
    intent_id           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    -- D7's machine: draft -> preflight -> signing -> submitted -> live -> partial -> filled |
    -- cancelled | rejected | expired -> unknown -> reconciled. `unknown` is entered by ANY unexpected
    -- response (not just a timeout) and left only by reconciliation.
    state               TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',     -- what the user sees in Activity, verbatim
    source              TEXT NOT NULL CHECK (source IN ('venue_ws','venue_rest','reconciler','user','system')),
    -- The user-visible-inconsistency check: while an order is in `unknown`/`submitted`, the API must render
    -- "working", never "failed". `show_as_working` is written with the state, so the read path cannot guess.
    show_as_working     BOOLEAN NOT NULL DEFAULT FALSE,
    at_ms               BIGINT NOT NULL,
    CHECK (state IN
        ('draft','preflight','signing','submitted','live','partial','filled','cancelled','rejected',
         'expired','unknown','reconciled'))
);
CREATE INDEX order_lifecycle_intent_idx ON order_lifecycle(intent_id, at_ms);

CREATE TABLE order_directives (
    -- P04 left the intent as "price, size, side". P06 needs five more facts per order, and they belong to
    -- the REQUEST rather than to the venue row, so they live beside it in a table this phase owns (an ALTER
    -- on a migration a database has already applied is a migration that never runs again).
    intent_id           TEXT PRIMARY KEY REFERENCES order_intents(id) ON DELETE CASCADE,
    order_type          TEXT NOT NULL DEFAULT 'GTC' CHECK (order_type IN ('GTC','GTD','FOK','FAK','MARKET')),
    expiration_ts       BIGINT NOT NULL DEFAULT 0,     -- GTD only; 0 means "no expiry", not "missing"
    builder_bps         INT NOT NULL DEFAULT 0 CHECK (builder_bps BETWEEN 0 AND 1000),
    fee_rate_bps        INT NOT NULL DEFAULT 0 CHECK (fee_rate_bps >= 0),
    -- 'user' or 'automation': decides which order types are legal (docs/P06 D2.3) and whose rate counters
    -- and caps apply. Recorded, not inferred, so the batch/audit story always knows who asked.
    audience            TEXT NOT NULL DEFAULT 'user' CHECK (audience IN ('user','automation','copy')),
    rule_id             TEXT NOT NULL DEFAULT '',
    config_id           TEXT NOT NULL DEFAULT '',
    max_slippage_bps    INT NOT NULL DEFAULT 0,        -- mandatory whenever order_type is FOK/MARKET
    all_in_limit_micro  BIGINT NOT NULL DEFAULT 0,     -- market buys: the number we refuse to exceed
    created_ms          BIGINT NOT NULL
);

CREATE TABLE order_notifications (
    -- D7's notification set. Queued here and delivered by the same planner the P05 fanout uses, so an
    -- order event and a whale alert compete for one user's attention under one priority rule. `status`
    -- starts 'queued' because the executor must never claim a message was delivered.
    id                  BIGSERIAL PRIMARY KEY,
    intent_id           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    event               TEXT NOT NULL,
    channel             TEXT NOT NULL DEFAULT 'in_app' CHECK (channel IN ('in_app','email','telegram')),
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','failed','dead')),
    priority            INT NOT NULL DEFAULT 0,
    detail_json         TEXT NOT NULL DEFAULT '{}',
    at_ms               BIGINT NOT NULL,
    delivered_ms        BIGINT,
    attempts            INT NOT NULL DEFAULT 0,
    CHECK (event IN
        ('queued','submitted','live','partial_fill','filled','cancelled','rejected','expired','unknown',
         'reconciled','position_closed','alert'))
);
CREATE INDEX order_notifications_open_idx ON order_notifications(status, priority, at_ms);

CREATE TABLE automation_rule_targets (
    -- Which markets a rule watches. P04's `automation_rules` row is user-scoped (a rule the user owns), but
    -- a trigger about a price needs a market, and a rule that watches several markets must not fire twice for
    -- the same minute on two of them. Kept out of `automation_rules` because that table is already applied in
    -- this file's Postgres form and this phase adds the association rather than editing it.
    rule_id             TEXT NOT NULL,
    market_id           TEXT NOT NULL,
    token_id            TEXT NOT NULL DEFAULT '',
    created_ms          BIGINT NOT NULL,
    PRIMARY KEY (rule_id, market_id)
);

CREATE TABLE automation_rule_state (
    -- The runtime half of a rule: what paused it, what it armed itself with, how many times it has failed.
    -- Split from `automation_rule_policy` because policy is what the USER set and state is what the ENGINE
    -- did; conflating them means a user's edit silently clears a pause, and a pause is a safety property.
    rule_id             TEXT PRIMARY KEY REFERENCES automation_rules(id) ON DELETE CASCADE,
    paused_reason       TEXT NOT NULL DEFAULT '',
    armed_market_id     TEXT NOT NULL DEFAULT '',
    armed_token_id      TEXT NOT NULL DEFAULT '',
    take_profit_bp      INT,
    stop_loss_bp        INT,
    failure_count       INT NOT NULL DEFAULT 0,
    last_error          TEXT NOT NULL DEFAULT '',
    updated_ms          BIGINT NOT NULL,
    CHECK (take_profit_bp IS NULL OR (take_profit_bp > 0 AND take_profit_bp <= 10000)),
    CHECK (stop_loss_bp IS NULL OR (stop_loss_bp > 0 AND stop_loss_bp <= 10000))
);

CREATE TABLE order_attempts (
    -- Written AFTER signing, BEFORE submitting. This is what makes a crash between the two provable: the
    -- payload we signed exists, so the reconciler can re-derive the client order id and ask the venue
    -- whether the order exists (D3's first case) without any network state in the process.
    client_order_hash   TEXT PRIMARY KEY,
    intent_id           TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    token_id            TEXT NOT NULL,
    side                TEXT NOT NULL,
    price_micro         BIGINT NOT NULL,
    size_micro          BIGINT NOT NULL,
    order_type          TEXT NOT NULL,
    expiration_ts       BIGINT NOT NULL DEFAULT 0,
    signature_type      INTEGER NOT NULL,
    builder_code        TEXT NOT NULL,
    payload_digest      TEXT NOT NULL,               -- sha256 of the signed struct; never the signature
    signed_ms           BIGINT NOT NULL,
    submitted_ms        BIGINT,
    ack_ms              BIGINT
);

CREATE TABLE venue_fees (
    -- Estimate at submit, actual at fill, delta forever. D2 asks for an estimate-accuracy metric; this
    -- table is that metric and the invoice dispute at the same time.
    order_id            TEXT PRIMARY KEY,
    intent_id           TEXT NOT NULL,
    fee_rate_bps        INT NOT NULL DEFAULT 0,
    builder_bps         INT NOT NULL DEFAULT 0,
    est_platform_micro  BIGINT NOT NULL DEFAULT 0,
    est_builder_micro   BIGINT NOT NULL DEFAULT 0,
    actual_platform_micro BIGINT,
    actual_builder_micro  BIGINT,
    delta_micro         BIGINT,
    at_ms               BIGINT NOT NULL
);

-- ------------------------------------------------------------- D3: one reconciler, one durable cursor ----
CREATE TABLE reconcile_cursors (
    -- Exactly one row, named 'main'. The phase's answer to "who owns this case" has to be singular or the
    -- eight cases end up with three owners and a gap between them.
    name                TEXT PRIMARY KEY CHECK (name = 'main'),
    watermark_ms        BIGINT NOT NULL DEFAULT 0,
    -- Rows claimed by a pass that did not finish. Recovery reads this and re-owns them; nothing else may.
    in_flight           BIGINT NOT NULL DEFAULT 0,
    last_run_ms         BIGINT NOT NULL DEFAULT 0,
    last_error          TEXT,
    updated_ms          BIGINT NOT NULL
);

CREATE TABLE reconcile_actions (
    -- Append-only proof of what the reconciler did, keyed so a re-run is a no-op (rule 6).
    id                  BIGSERIAL PRIMARY KEY,
    case_name           TEXT NOT NULL,
    intent_id           TEXT NOT NULL DEFAULT '',
    order_id            TEXT NOT NULL DEFAULT '',
    action              TEXT NOT NULL,
    dedupe_key          TEXT NOT NULL UNIQUE,
    at_ms               BIGINT NOT NULL
);

CREATE TABLE reconcile_open (
    -- The set the `unreconciled_orders` metric counts. A row is inserted when something enters an
    -- unreconciled state and deleted when it resolves, so the metric is a SELECT, not a scan, and its age
    -- is measurable per row (the >60s alarm needs an age, not a count).
    dedupe_key          TEXT PRIMARY KEY,
    case_name           TEXT NOT NULL,
    intent_id           TEXT NOT NULL DEFAULT '',
    order_id            TEXT NOT NULL DEFAULT '',
    since_ms            BIGINT NOT NULL,
    note                TEXT NOT NULL DEFAULT '',
    -- Sightings and escalation. A queue without a counter is a queue where every pass looks equally
    -- unproductive; the number is what turns "still open" into "nobody has moved this in 40 passes".
    attempts            INT NOT NULL DEFAULT 0,
    escalated_ms        BIGINT,
    CHECK (case_name IN
        ('submitted_unacked','no_ack','orphan','lagging_fill','closing_market','ghost_order',
         'ambiguous_settlement','cancelled_race'))
);
CREATE INDEX reconcile_open_age_idx ON reconcile_open(since_ms);

-- ---------------------------------------------------------------------- D4: limits the gate lacked ----
CREATE TABLE risk_blocklists (
    market_id           TEXT PRIMARY KEY,             -- conditionId
    reason              TEXT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('uma_dispute','manual','auto_insider')),
    added_by            TEXT NOT NULL,
    added_ms            BIGINT NOT NULL,
    expires_ms          BIGINT                        -- NULL = until someone removes it
);

CREATE TABLE loss_halts (
    -- Daily loss limit tripped -> trading halts for that user until THEY acknowledge. An operator lifting it
    -- is a separate, audited event (D8), never a side effect of the next order.
    user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    threshold_micro     BIGINT NOT NULL,
    realized_micro      BIGINT NOT NULL,              -- signed, negative = loss
    tripped_ms          BIGINT NOT NULL,
    acknowledged_ms     BIGINT,
    acknowledged_by     TEXT
);

CREATE TABLE rate_counters (
    -- Per-user order rate and per-rule automation counters in one table, so both are observable in the
    -- same dashboard and both expire by the same rule.
    --
    -- The key is (counter, bucket): an earlier version keyed the table on `key` alone, which is a sliding
    -- window wearing a fixed-window costume - the second minute's insert hit the primary key and the
    -- executor crashed mid-submit. Fixed buckets need the bucket in the identity.
    key                 TEXT NOT NULL,                -- 'user:<id>|orders' | 'rule:<id>|runs' | 'global|cancel_all'
    bucket_ms           BIGINT NOT NULL,
    count               BIGINT NOT NULL DEFAULT 0,
    updated_ms          BIGINT NOT NULL,
    PRIMARY KEY (key, bucket_ms)
);

CREATE TABLE kill_switch_drills (
    -- D4: "document and DRILL it". A drill that never ran is a paragraph. This table plus the artifact the
    -- tool writes is the evidence, and the gate refuses to pass if the freshest drill is older than 30 days.
    id                  BIGSERIAL PRIMARY KEY,
    started_ms          BIGINT NOT NULL,
    t_zero_ms           BIGINT NOT NULL,              -- when the flag was written
    observed_ms         BIGINT NOT NULL,              -- when the last component refused a trade
    propagation_ms      BIGINT,                        -- per-component, as JSON
    verdict             TEXT NOT NULL CHECK (verdict IN ('pass','fail')),
    budget_ms           BIGINT NOT NULL,
    artifact            TEXT
);

-- ------------------------------------------------------------------ D5/D6: copy and automation state ----
CREATE TABLE copy_config_policy (
    -- Companion to P04's `copy_configs`, in this phase's own table rather than an ALTER on an older one:
    -- the SQLite transpiler records a dropped ADD COLUMN as an untested invariant, and a phase that edits
    -- a migration no database has ever applied is a phase that ships two schemas.
    config_id             TEXT PRIMARY KEY REFERENCES copy_configs(id) ON DELETE CASCADE,
    -- D5's guardrails. Deviation is measured against the quote at the moment the source filled; past the
    -- bound the fill is SKIPPED, never chased (docs/P06 D5.4).
    max_entry_deviation_bps INT NOT NULL DEFAULT 150 CHECK (max_entry_deviation_bps BETWEEN 0 AND 2000),
    min_interval_ms       BIGINT NOT NULL DEFAULT 0 CHECK (min_interval_ms >= 0),
    -- Take-profit / stop-loss, in basis points of the copied entry price. Both absent = the copier is
    -- mirroring a naked position, which the API must be able to say out loud.
    take_profit_bp        INT CHECK (take_profit_bp IS NULL OR take_profit_bp > 0),
    stop_loss_bp          INT CHECK (stop_loss_bp IS NULL OR stop_loss_bp > 0),
    -- No copy of an order that resolves inside this window: a 15-second-old market with 4 minutes left is
    -- a coin flip with fees, and a copier cannot evaluate it in time to have an opinion.
    min_seconds_to_resolution BIGINT NOT NULL DEFAULT 900 CHECK (min_seconds_to_resolution >= 0),
    copy_up_to_side       BOOLEAN NOT NULL DEFAULT FALSE,  -- never copy ABOVE this price (micro)
    max_price_micro       BIGINT CHECK (max_price_micro IS NULL OR (max_price_micro > 0 AND max_price_micro < 1000000)),
    -- 1 hop = copying a trader. >1 = copying a copier, which is refused (D5.6).
    hop_depth             INT NOT NULL DEFAULT 1 CHECK (hop_depth BETWEEN 1 AND 1),
    paused_reason         TEXT,                            -- why we stopped copying, shown to the user
    updated_ms            BIGINT NOT NULL
);

CREATE TABLE copy_events (
    id                  BIGSERIAL PRIMARY KEY,
    copier_id           TEXT NOT NULL,
    source_user_id      TEXT NOT NULL,
    source_intent_id    TEXT NOT NULL DEFAULT '',
    intent_id           TEXT NOT NULL DEFAULT '',
    action              TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    deviation_bps       INT NOT NULL DEFAULT 0,
    at_ms               BIGINT NOT NULL,
    CHECK (action IN
        ('copied','skipped','tp_filled','sl_filled','paused','resumed','source_stopped','insider_flagged',
         'rate_limited'))
);
CREATE INDEX copy_events_copier_idx ON copy_events(copier_id, at_ms DESC);

CREATE TABLE copy_source_stats (
    -- The track record, with the numbers that hurt: drawdown and losing streaks, not just PnL.
    source_user_id      TEXT NOT NULL,
    window_days         INT NOT NULL,
    closed_trades       INT NOT NULL DEFAULT 0,
    win_rate_bp         INT NOT NULL DEFAULT 0,       -- basis points, integer forever
    realized_pnl_micro  BIGINT NOT NULL DEFAULT 0,
    fees_micro          BIGINT NOT NULL DEFAULT 0,
    net_after_fees_micro BIGINT NOT NULL DEFAULT 0,
    max_drawdown_micro  BIGINT NOT NULL DEFAULT 0,    -- positive magnitude, measured on the equity curve
    longest_losing_streak INT NOT NULL DEFAULT 0,
    avg_latency_ms      BIGINT NOT NULL DEFAULT 0,
    updated_ms          BIGINT NOT NULL,
    PRIMARY KEY (source_user_id, window_days)
);

CREATE TABLE copy_economics (
    -- What a source costs our copiers in fees versus what they earn. If the aggregate is negative, the UI
    -- must say so on the leaderboard (P09), so it has to be computable here.
    source_user_id      TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    copier_count        INT NOT NULL DEFAULT 0,
    copier_volume_micro BIGINT NOT NULL DEFAULT 0,
    fees_micro          BIGINT NOT NULL DEFAULT 0,
    builder_fees_micro  BIGINT NOT NULL DEFAULT 0,
    copier_net_micro    BIGINT NOT NULL DEFAULT 0,
    source_payout_micro BIGINT NOT NULL DEFAULT 0,
    updated_ms          BIGINT NOT NULL
);

CREATE TABLE automation_rule_policy (
    rule_id               TEXT PRIMARY KEY REFERENCES automation_rules(id) ON DELETE CASCADE,
    -- The composable tree: {"any":[{"all":[...]}]}. Kept as JSON because the shape is user-authored; the
    -- ENGINE validates it, and a rule whose tree does not validate is refused at save time, never at fire
    -- time (a rule that fails at fire time fails silently, which is how automation dies unnoticed).
    trigger_json          TEXT NOT NULL DEFAULT '{}',
    actions_json          TEXT NOT NULL DEFAULT '[]',
    -- D6: dry-run is not a nicety. A rule cannot be enabled until it has run in dry mode at least once
    -- against live data, and the DB records that it did, with the outcome.
    dry_run_completed_ms  BIGINT,
    max_per_day           INT NOT NULL DEFAULT 24 CHECK (max_per_day BETWEEN 1 AND 288),
    min_interval_ms       BIGINT NOT NULL DEFAULT 60000 CHECK (min_interval_ms >= 1000),
    human_priority_ms     BIGINT NOT NULL DEFAULT 120000,  -- D6: never race a human on a new market
    last_fire_ms          BIGINT NOT NULL DEFAULT 0,
    updated_ms            BIGINT NOT NULL
);

CREATE TABLE automation_runs (
    -- Every evaluation, including the ones that did nothing. "Show me the run history for that rule,
    -- including skips and why" is D6's quality bar.
    id                  BIGSERIAL PRIMARY KEY,
    rule_id             TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    mode                TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    deny_code           TEXT NOT NULL DEFAULT '',
    intent_id           TEXT NOT NULL DEFAULT '',
    detail_json         TEXT NOT NULL DEFAULT '{}',
    at_ms               BIGINT NOT NULL,
    CHECK (mode IN ('dry_run','live')),
    CHECK (outcome IN ('placed','would_place','skipped','failed'))
);
CREATE INDEX automation_runs_rule_idx ON automation_runs(rule_id, at_ms DESC);

-- ------------------------------------------------------------------------ D8: builder revenue rows ----
CREATE TABLE builder_attribution_terms (
    -- The terms in force for one attributable order, frozen at the moment we placed it. `builder_attribution`
    -- (P04) records what we EXPECTED to be paid; this records the code, the rate and the exclusion decision
    -- that produced that expectation, plus the registry's content hash. When a dispute arrives three weeks
    -- later and somebody has since edited a label or revoked a code, the row here is what the order actually
    -- ran under. Notional lives here too, because the fee base is a term, not a fact about the venue.
    attribution_id        BIGINT PRIMARY KEY,           -- -> builder_attribution.id
    builder_code          TEXT NOT NULL DEFAULT '',
    fee_bps               INT NOT NULL DEFAULT 0,
    notional_micro        BIGINT NOT NULL DEFAULT 0,
    excluded_reason       TEXT NOT NULL DEFAULT '',
    code_fingerprint      TEXT NOT NULL DEFAULT '',
    recorded_ms           BIGINT NOT NULL
);

CREATE TABLE builder_attribution_measures (
    -- The second, independent measurement of the same fee (D8). Written by the reconciliation job from
    -- venue/chain events only: it is forbidden to read `fee_micro_expected`, so a bug in our expectation
    -- cannot make the delta look like zero.
    attribution_id        BIGINT PRIMARY KEY,           -- -> builder_attribution.id
    order_id              TEXT NOT NULL DEFAULT '',
    chain_measured_micro  BIGINT,
    notional_micro        BIGINT NOT NULL DEFAULT 0,
    source                TEXT NOT NULL CHECK (source IN ('chain_log','builder_trades_api','manual')),
    delta_micro           BIGINT,                       -- expected - measured, NULL until both exist
    reconciled_ms         BIGINT
);

CREATE TABLE builder_revenue_daily (
    day                 TEXT PRIMARY KEY,             -- 'YYYY-MM-DD' UTC
    orders              INT NOT NULL DEFAULT 0,
    fills               INT NOT NULL DEFAULT 0,
    volume_micro        BIGINT NOT NULL DEFAULT 0,
    expected_micro      BIGINT NOT NULL DEFAULT 0,    -- our ledger's claim
    chain_micro         BIGINT NOT NULL DEFAULT 0,    -- independent measure from OrderFilled events
    platform_fee_micro  BIGINT NOT NULL DEFAULT 0,
    delta_micro         BIGINT NOT NULL DEFAULT 0,    -- expected - chain; the number that needs explaining
    status              TEXT NOT NULL DEFAULT 'unreconciled'
                          CHECK (status IN ('unreconciled','matched','investigating','settled')),
    updated_ms          BIGINT NOT NULL
);

CREATE TABLE chain_events (
    -- The venue's own record, ingested separately from our fills so the two cannot share a bug. Only
    -- `builder_revenue` rows feed the reconciliation; the rest is for P13.
    id                  BIGSERIAL PRIMARY KEY,
    source              TEXT NOT NULL,
    kind                TEXT NOT NULL,                -- OrderFilled | OrderCancelled | Transfer
    block_number        BIGINT,
    log_index           BIGINT,
    tx_hash             TEXT NOT NULL,
    order_id            TEXT NOT NULL DEFAULT '',
    maker               TEXT NOT NULL DEFAULT '',
    taker               TEXT NOT NULL DEFAULT '',
    token_id            TEXT NOT NULL DEFAULT '',
    matched_micro       BIGINT NOT NULL DEFAULT 0,
    price_micro         BIGINT NOT NULL DEFAULT 0,
    fee_micro           BIGINT NOT NULL DEFAULT 0,
    builder             TEXT NOT NULL DEFAULT '',
    seen_ms             BIGINT NOT NULL,
    UNIQUE (tx_hash, log_index),
    CHECK (source IN ('clob_ws','clob_rest','chain_log'))
);

CREATE TABLE flag_audit_p06 (
    -- D8's "a limit change is an audited config change" + the kill switch trail, kept in its own table so
    -- P09's audit feed can read it without knowing about flags. Deliberately append-only.
    id                  BIGSERIAL PRIMARY KEY,
    key                 TEXT NOT NULL,
    old_value           TEXT,
    new_value           TEXT,
    actor               TEXT NOT NULL,
    reason              TEXT NOT NULL,                -- empty reason is refused by the service, not here
    at_ms               BIGINT NOT NULL
);

-- Append-only for the eight evidence tables, mirroring 0005_triggers.sql. Written here rather than added to
-- 0005 because a migration a deployed database has already applied is a migration that will never run
-- again; the phase that creates a table owns its triggers.
CREATE TRIGGER append_only_wallet_events    BEFORE UPDATE OR DELETE ON wallet_events    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_order_lifecycle  BEFORE UPDATE OR DELETE ON order_lifecycle  FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_recon_actions    BEFORE UPDATE OR DELETE ON reconcile_actions FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_copy_events      BEFORE UPDATE OR DELETE ON copy_events      FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_automation_runs  BEFORE UPDATE OR DELETE ON automation_runs FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_chain_events     BEFORE UPDATE OR DELETE ON chain_events     FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_flag_audit_p06   BEFORE UPDATE OR DELETE ON flag_audit_p06   FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_kill_drills      BEFORE UPDATE OR DELETE ON kill_switch_drills FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();

COMMIT;
