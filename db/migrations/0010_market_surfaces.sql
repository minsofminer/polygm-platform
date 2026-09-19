-- P09 · the market surfaces. Three things the discovery screen, the event page and the info rail must render,
-- and which no earlier table carries:
--
--   * category, resolution source and resolution criteria. `events.category` exists, but most markets have
--     no event (a single binary market is not an event), so a category filter that reads only `events` hides
--     every binary market from every category tab. That mistake is invisible in a fixture where the one
--     multi-market event is the only row anyone clicks.
--
--   * open interest and the 7d/30d volume windows. `market_stats` (0007) holds Gamma's 24h number for the
--     whole market and is written by ingest; 7d/30d are NOT there and must not be invented: they are our own
--     tape's sums, so they live in their own table with their own provenance, and the API labels which is
--     which. A screen that renders "30d volume" from a 7-day-old ingest is worse than one that omits it.
--
-- Why new tables rather than ALTER TABLE markets: the SQLite subset the test suite runs on is generated
-- from these files, and the transpiler DROPS `ALTER TABLE` (db/migrations-sqlite/DROPPED.json records it).
-- An ALTER here would therefore produce a column that exists in production and NOT in the suite - the exact
-- "tests pass, production differs" shape the transpiler exists to prevent. A 1:1 side table costs one join
-- and keeps both engines honest. It also keeps the market row itself small: it is read on every price tick.
--
-- Both tables converge in place (the venues update these numbers), so they are deliberately NOT in the
-- transpiler's APPEND_ONLY list: a table you may only insert into cannot hold "the current open interest".

CREATE TABLE IF NOT EXISTS market_meta (
    market_id           TEXT PRIMARY KEY REFERENCES markets(id) ON DELETE CASCADE,
    category            TEXT,                      -- the P09 filter vocabulary; NULL means "not classified yet",
                                                   -- which renders as "Other" and is NOT the same as 'other'
    resolution_source   TEXT,                      -- a URL. Rendered as a link, never embedded (see below)
    resolution_criteria TEXT,                      -- ATTACKER-INFLUENCED prose. Stored verbatim, rendered as
                                                   -- escaped plain text by the client, never as HTML
    image_url           TEXT,
    updated_ms          BIGINT NOT NULL
);

-- partial: the category tab is the only reader, so unclassified markets are simply not in the index
CREATE INDEX IF NOT EXISTS market_meta_category_ix ON market_meta (category) WHERE category IS NOT NULL;

CREATE TABLE IF NOT EXISTS market_activity (
    market_id           TEXT PRIMARY KEY REFERENCES markets(id) ON DELETE CASCADE,
    -- venue-reported, from the same ingest pass that writes market_stats. 0 is a real answer.
    open_interest_micro BIGINT NOT NULL DEFAULT 0,
    -- our tape's sums. Sample, not census: the tape drops fills in an outage, and the API says so.
    volume_7d_micro     BIGINT NOT NULL DEFAULT 0,
    volume_30d_micro    BIGINT NOT NULL DEFAULT 0,
    -- the newest fill we held 24h ago, i.e. the base of the "24h move" sort. NULL (not 0) when our tape does
    -- not reach back that far: a change rendered against a price of zero is a 100% move that never happened.
    price_24h_ago_micro BIGINT,
    -- newest fill price we have seen, cached so the discovery list does not aggregate the tape per row.
    last_price_micro    BIGINT,
    updated_ms          BIGINT NOT NULL
);

-- the dead-tail policy: "markets that are actually tradable" by default, long tail on request. Which column
-- an implementation indexes here is the difference between a filter and a full scan.
CREATE INDEX IF NOT EXISTS market_activity_volume_ix ON market_activity (volume_7d_micro DESC);
