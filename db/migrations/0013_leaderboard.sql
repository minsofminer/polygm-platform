-- P11 · The leaderboard: rank history, per-wallet integrity state, and the recompute record.
--
-- Three tables, each answering a question the ranking engine cannot answer about itself:
--
--   * `leaderboard_snapshots` — where a wallet stood, per board per window, at a moment. The engine ranks from
--     the current ledger; "were they falling?" is a question about the past, and a past recomputed from today's
--     rows is a different past. The 30-day rank sparkline (D3) reads this table and nothing else.
--   * `wallet_integrity` — the newest integrity verdict per wallet: state, the reasons behind it, and the
--     numbers a row states (washed volume, verified volume, best-trade share, derived-from). One row per wallet,
--     overwritten by each recompute, with the history living in `leaderboard_snapshots` where it belongs.
--   * `leaderboard_runs` — one row per board per window per hour: how many wallets ranked, how long it took.
--     This is the cadence, and a leaderboard whose cadence cannot be read from the database is a leaderboard
--     whose staleness nobody notices until a user does.
--
-- Exclusions are NOT a column anyone edits: they are rows in `leaderboard_exclusions`, append-only like every
-- other decision table in this schema, because "who removed this wallet and when" is a question an incident asks
-- and an UPDATE cannot answer.

CREATE TABLE leaderboard_snapshots (
    board           TEXT NOT NULL,
    window_key      TEXT NOT NULL,
    wallet          TEXT NOT NULL,
    rank            INTEGER NOT NULL CHECK (rank >= 1),
    score_bps       BIGINT NOT NULL,
    settled         INTEGER NOT NULL DEFAULT 0 CHECK (settled >= 0),
    drawdown_micro  BIGINT NOT NULL DEFAULT 0 CHECK (drawdown_micro >= 0),
    computed_ms     BIGINT NOT NULL,
    PRIMARY KEY (board, window_key, wallet, computed_ms)
);
-- Read path 1: the 30-day sparkline on a profile asks "this wallet, this board, the newest 30 days" — a range
-- scan on computed_ms over a fixed (wallet, board) prefix, never a full table scan.
CREATE INDEX leaderboard_snapshots_wallet_ix ON leaderboard_snapshots (wallet, board, computed_ms);
-- Read path 2: serving a board asks "this board, this window, newest computed_ms first" — the hot path of
-- GET /v1/leaderboard, and the sort is satisfied by the index rather than by a sort node.
CREATE INDEX leaderboard_snapshots_board_ix ON leaderboard_snapshots (board, window_key, computed_ms);

CREATE TABLE wallet_integrity (
    wallet                  TEXT PRIMARY KEY,
    state                   TEXT NOT NULL CHECK (state IN ('ranked', 'provisional', 'blew_up', 'excluded', 'review')),
    reasons_json            JSONB NOT NULL DEFAULT '{}',
    washed_micro            BIGINT NOT NULL DEFAULT 0 CHECK (washed_micro >= 0),
    verified_micro          BIGINT NOT NULL DEFAULT 0 CHECK (verified_micro >= 0),
    best_trade_share_bps    INTEGER NOT NULL DEFAULT 0 CHECK (best_trade_share_bps BETWEEN 0 AND 10000),
    derived_from            TEXT NOT NULL DEFAULT '',
    provisional_until_ms    BIGINT,
    computed_ms             BIGINT NOT NULL
);

CREATE TABLE leaderboard_exclusions (
    id          BIGSERIAL PRIMARY KEY,
    wallet      TEXT NOT NULL,
    board       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL CHECK (action IN ('exclude', 'include', 'flag', 'clear')),
    reason      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    at_ms       BIGINT NOT NULL
);
-- The anti-gaming dashboard asks "what happened to this wallet" (partial lookups by wallet), while the recompute
-- asks "what has been excluded lately" (range scan on at_ms). One composite index serves both without a sort.
CREATE INDEX leaderboard_exclusions_wallet_ix ON leaderboard_exclusions (wallet, at_ms);

CREATE TABLE leaderboard_runs (
    board           TEXT NOT NULL,
    window_key      TEXT NOT NULL,
    bucket_hour     BIGINT NOT NULL,
    ranked          INTEGER NOT NULL DEFAULT 0 CHECK (ranked >= 0),
    unranked        INTEGER NOT NULL DEFAULT 0 CHECK (unranked >= 0),
    blew_up         INTEGER NOT NULL DEFAULT 0 CHECK (blew_up >= 0),
    duration_ms     INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    computed_ms     BIGINT NOT NULL,
    PRIMARY KEY (board, window_key, bucket_hour)
);
