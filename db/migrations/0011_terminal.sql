-- P10 · the terminal. Seven tables, and every one of them exists because a rule in this phase needs somewhere
-- to live that a component cannot fake:
--
--   * `copy_config_guards` — the guard rails D7 lists (skip-if-moved, do-not-enter-within, price bounds,
--     TP/SL). A config with NO guard row is DRY-RUN, which is why `dry_run` defaults TRUE: the safe state has
--     to be the state you get by not writing anything. The alternative default (a config that starts trading
--     when somebody adds the row) is a default nobody would defend out loud.
--   * `copy_dry_runs` — what the engine WOULD have done. Separate from `copy_events` (P06, what it DID), because
--     a single table with a `dry_run` flag is a table where a monitor can merge the two by forgetting a WHERE,
--     and the one thing a copy screen must never do is show a simulation as a fill.
--   * `trader_metrics` — a materialised window of the D3 metric set, with `win_rate_bps` NULLABLE and NULL
--     unless `resolved_markets >= sample_gate`. The constraint is in the schema as well as the code, so a
--     backfill cannot write a win rate the API would refuse to compute.
--   * `trader_metric_snapshots` — append-only history of those numbers. "Why did this wallet stop being smart
--     money" is a question about a change over time, and a mutable row cannot answer it.
--   * `trader_metric_series` — the cumulative curve, one row per point, with a CHECK that
--     `drawdown_micro = peak_micro - cum_micro`. D3's "drawdown wherever PnL appears" is enforced on write.
--   * `whale_views` — a saved filter, optionally bound to an `alert_rules` row (P04) so D4's inline alert
--     creation has somewhere to point. A global view may not notify: the CHECK says a notifying view names a
--     market, mirroring `alert_rules`' own `rule_has_target`.
--   * `wallet_pseudonyms` — the address↔pseudonym pairs we have resolved, so `/v1/tape/fills?wallet=w_…` is a
--     lookup rather than a hash of every address in the tape. Append-only: a pseudonym that can be reassigned
--     is a pseudonym that can be made to point at a different trader than the one a user was watching.
--
-- Why new tables rather than ALTER TABLE on P06's `copy_configs`: the SQLite subset the suite runs on is
-- generated from these files and the transpiler drops `ALTER TABLE` (db/migrations-sqlite/DROPPED.json records
-- it), so a column added here would exist in production and not in the tests — the "tests pass, production
-- differs" shape that migrator exists to prevent.

CREATE TABLE IF NOT EXISTS copy_config_guards (
    config_id                   TEXT PRIMARY KEY REFERENCES copy_configs(id) ON DELETE CASCADE,
    -- TRUE means "record what you would have done and send nothing". The default is the safe state, and a
    -- config with no row here at all is dry-run in the same way.
    dry_run                     BOOLEAN NOT NULL DEFAULT TRUE,
    -- D7's default is "skip instead of chase": 2 cents. A copier who chases turns a good source's record into
    -- a bad copier's result, and the fee arithmetic only works when our fill is near the source's.
    skip_if_moved_cents         INT NOT NULL DEFAULT 2,
    -- Entering a market that resolves in minutes is a coin flip with fees. 24h is the default, and it is
    -- a default rather than a rule because a user may know something about the market we do not.
    do_not_enter_within_hours   INT NOT NULL DEFAULT 24,
    category_filter             TEXT NOT NULL DEFAULT '',
    min_price_micro             BIGINT,
    max_price_micro             BIGINT,
    take_profit_micro           BIGINT,
    stop_loss_micro             BIGINT,
    -- set when dry-run was turned off, so the confirmation has a time and not just a boolean
    live_since_ms               BIGINT,
    updated_ms                  BIGINT NOT NULL,
    CHECK (skip_if_moved_cents BETWEEN 0 AND 50),
    CHECK (do_not_enter_within_hours BETWEEN 0 AND 168),
    CHECK (min_price_micro  IS NULL OR (min_price_micro BETWEEN 1 AND 999999)),
    CHECK (max_price_micro  IS NULL OR (max_price_micro BETWEEN 1 AND 999999))
);

CREATE TABLE IF NOT EXISTS copy_dry_runs (
    id                  BIGSERIAL PRIMARY KEY,
    config_id           TEXT NOT NULL REFERENCES copy_configs(id) ON DELETE CASCADE,
    copier_id           TEXT NOT NULL DEFAULT '',
    source_user_id      TEXT NOT NULL DEFAULT '',
    source_intent_id    TEXT NOT NULL DEFAULT '',
    market_id           TEXT NOT NULL DEFAULT '',
    would_action        TEXT NOT NULL,
    would_size_micro    BIGINT NOT NULL DEFAULT 0,
    would_price_micro   BIGINT NOT NULL DEFAULT 0,
    source_price_micro  BIGINT NOT NULL DEFAULT 0,
    deviation_bps       INT NOT NULL DEFAULT 0,
    reason              TEXT NOT NULL DEFAULT '',
    at_ms               BIGINT NOT NULL,
    CHECK (would_action IN ('enter', 'exit', 'skip'))
);
-- Index reasoning: each of the five below serves exactly one read path, and nothing is indexed
-- speculatively. `copy_dry_runs` is read per config newest-first (the monitor, and the "has this
-- config ever run?" check that gates going live). `trader_metric_snapshots` is read per wallet
-- newest-first (the dossier's stored figures). `trader_metric_series` is read per wallet+window in
-- time order (the PnL curve with its drawdown overlay - a sort by ts_ms, which is why the index
-- carries it). `whale_views` is read per user newest-first (the saved-views list). `wallet_pseudonyms`
-- is looked up by `anon_id`, and UNIQUE because two wallets sharing a pseudonym would merge two
-- traders into one row on a public page. A scan of the tape for a wallet's fills is deliberately NOT
-- indexed by this migration: the source of truth for it is P06's `copy_source_stats`, and an index
-- added for a query the product does not run is a write tax with no reader.
CREATE INDEX IF NOT EXISTS copy_dry_runs_config_idx ON copy_dry_runs(config_id, at_ms DESC);

CREATE TABLE IF NOT EXISTS trader_metrics (
    wallet_id           TEXT NOT NULL,
    window_key          TEXT NOT NULL,
    fills               INT NOT NULL DEFAULT 0,
    resolved_markets    INT NOT NULL DEFAULT 0,
    wins                INT NOT NULL DEFAULT 0,
    -- NULL below the gate, and the CHECK is the schema's half of the promise: a win rate without the sample
    -- behind it cannot be stored, so no reader has to decide whether to trust one.
    win_rate_bps        INT,
    sample_gate         INT NOT NULL DEFAULT 20,
    volume_micro        BIGINT NOT NULL DEFAULT 0,
    realised_micro      BIGINT NOT NULL DEFAULT 0,
    unrealised_micro    BIGINT NOT NULL DEFAULT 0,
    best_micro          BIGINT NOT NULL DEFAULT 0,
    worst_micro         BIGINT NOT NULL DEFAULT 0,
    max_drawdown_micro  BIGINT NOT NULL DEFAULT 0,
    avg_hold_ms         BIGINT NOT NULL DEFAULT 0,
    median_hold_ms      BIGINT NOT NULL DEFAULT 0,
    open_fills          INT NOT NULL DEFAULT 0,
    matched_positions   INT NOT NULL DEFAULT 0,
    distinct_markets    INT NOT NULL DEFAULT 0,
    computed_ms         BIGINT NOT NULL,
    PRIMARY KEY (wallet_id, window_key),
    CHECK (win_rate_bps IS NULL OR resolved_markets >= sample_gate),
    CHECK (max_drawdown_micro >= 0)
);

CREATE TABLE IF NOT EXISTS trader_metric_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    wallet_id           TEXT NOT NULL,
    window_key          TEXT NOT NULL,
    resolved_markets    INT NOT NULL,
    wins                INT NOT NULL,
    win_rate_bps        INT,
    realised_micro      BIGINT NOT NULL,
    max_drawdown_micro  BIGINT NOT NULL,
    at_ms               BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS trader_metric_snapshots_wallet_idx ON trader_metric_snapshots(wallet_id, at_ms DESC);

CREATE TABLE IF NOT EXISTS trader_metric_series (
    id                  BIGSERIAL PRIMARY KEY,
    wallet_id           TEXT NOT NULL,
    window_key          TEXT NOT NULL,
    ts_ms               BIGINT NOT NULL,
    cum_micro           BIGINT NOT NULL,
    peak_micro          BIGINT NOT NULL,
    drawdown_micro      BIGINT NOT NULL,
    open_positions      INT NOT NULL DEFAULT 0,
    -- The overlay is not a rendering decision. A curve point that is not the high-water mark minus the
    -- cumulative PnL is a curve point that was computed wrong, and this rejects it at write time.
    CHECK (drawdown_micro = peak_micro - cum_micro),
    CHECK (drawdown_micro >= 0)
);
CREATE INDEX IF NOT EXISTS trader_metric_series_key_idx ON trader_metric_series(wallet_id, window_key, ts_ms);

CREATE TABLE IF NOT EXISTS whale_views (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    filters_json        TEXT NOT NULL DEFAULT '{}',
    channel             TEXT NOT NULL DEFAULT 'telegram',
    severity            TEXT NOT NULL DEFAULT 'notice' CHECK (severity IN ('info','notice','urgent')),
    scope               TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global','market')),
    market_id           TEXT,
    rule_id             TEXT,
    created_ms          BIGINT NOT NULL,
    -- A notifying view must name a market; a view that only filters must not pretend to. This mirrors
    -- `alert_rules`' own `rule_has_target` (P04) rather than inventing a second vocabulary for the same rule.
    CHECK ((scope = 'market') = (market_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS whale_views_user_idx ON whale_views(user_id, created_ms DESC);

CREATE TABLE IF NOT EXISTS wallet_pseudonyms (
    wallet_id           TEXT PRIMARY KEY,
    anon_id             TEXT NOT NULL,
    first_seen_ms       BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS wallet_pseudonyms_anon_idx ON wallet_pseudonyms(anon_id);

CREATE TRIGGER append_only_copy_dry_runs BEFORE UPDATE OR DELETE ON copy_dry_runs
    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_trader_metric_snapshots BEFORE UPDATE OR DELETE ON trader_metric_snapshots
    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_wallet_pseudonyms BEFORE UPDATE OR DELETE ON wallet_pseudonyms
    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
