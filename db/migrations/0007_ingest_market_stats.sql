-- P05: the venue's own activity numbers, kept where the universe policy can read them.
--
-- Why a table and not `ALTER TABLE markets ADD COLUMN`: the sqlite generator treats `ALTER TABLE` as
-- Postgres-only and drops it, which is right for triggers and constraints and wrong for a column the code
-- reads — dev/CI would run on a schema missing the column production has, and the failure would surface as an
-- "unknown column" in whichever test touches it first. Either the generator refuses such a drop (it now does)
-- or the columns arrive in a table of their own. This is the second option, and it is also the better one:
-- `markets` is P04's table, its column list is pinned by the API's own contract, and the ingest's idea of a
-- market's activity is the ingest's to own.
--
-- `closed` is deliberately absent. P04 already answers it: a market is resolved when some outcome has
-- `tokens.is_winner IS NOT NULL`, and untradeable when `accepting_orders = 0`. A second boolean for one fact is
-- a second source of truth, and the first disagreement between them would be a bug report about a market that
-- is "closed but has a winner at 0.40".
CREATE TABLE IF NOT EXISTS market_stats (
    condition_id        TEXT PRIMARY KEY,          -- the venue id, like every other ingest table
    volume_24h_micro    BIGINT NOT NULL DEFAULT 0, -- Gamma's number for the whole market, not our sample of it
    liquidity_micro     BIGINT NOT NULL DEFAULT 0,
    fill_count          INT NOT NULL DEFAULT 0 CHECK (fill_count >= 0),
    last_fill_ms        BIGINT NOT NULL DEFAULT 0, -- newest fill WE recorded (0 = never), for the idle test
    last_book_change_ms BIGINT NOT NULL DEFAULT 0,
    resolved_seen_ms    BIGINT NOT NULL DEFAULT 0, -- when WE saw the resolution, which is not the venue's time
    -- The tracked Gamma fields exactly as the venue sent them, and the ONLY thing a metadata diff is compared
    -- against. Storing the translation instead would mean every field we re-render (a REAL 5.0 against the
    -- venue's "5", an ISO string against a rounded one) looks like a change the venue never made — which the
    -- first version of this did, three rows per market per pass, and which buries the one row that matters.
    meta_json           TEXT NOT NULL DEFAULT '{}',
    updated_ms          BIGINT NOT NULL
);
-- This is the prune read path: "quiet and small, drop it" is a scan over volume and recency, and it runs every
-- pass over every tracked market. Without the index it is a full scan of the table the universe poll rewrites
-- every 5 s, so the ingest pays for its own housekeeping on the hot path.
CREATE INDEX IF NOT EXISTS market_stats_activity_ix ON market_stats (volume_24h_micro DESC, updated_ms DESC);
