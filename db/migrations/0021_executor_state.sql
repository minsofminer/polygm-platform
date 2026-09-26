-- P15 D5 — the executor's heartbeat.
--
-- Why this table exists, and why it is a mutable one-row state table rather than an append-only ledger: the kit
-- asks for "executor down" to be a page, and a page needs a *staleness* answer — "no beat for 90 seconds" — which
-- only exists if the process itself writes a timestamp it keeps up to date. Deriving liveness from `order_intents`
-- would have been cheaper and wrong: an idle executor that is perfectly healthy writes nothing, so every quiet
-- night would page somebody, and a pager that fires when nothing is wrong is a pager that gets muted.
--
-- One row, id = 1, UPSERTed on every tick. It is not a ledger: its history is worth nothing (the interesting
-- questions are all "now"), and a heartbeat table that grows forever is a cost with no evidence attached.
--
-- It cannot live in `ingest_cursors`: that table belongs to the data plane, and the executor has no cursor.
--
-- No index, and no trigger: the table has exactly one row, so every read is a primary-key lookup and any index
-- would be dead weight; and it is deliberately *not* append-only, because the interesting question is "how long
-- since the last beat" — the opposite of a history. The write path is an UPSERT that overwrites, and that is the
-- one place in this schema where overwriting is the correct semantics rather than the bug: a heartbeat that kept
-- its old values would report a healthy executor forever.
CREATE TABLE IF NOT EXISTS executor_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    at_ms           BIGINT NOT NULL,
    pid             TEXT NOT NULL DEFAULT '',
    version         TEXT NOT NULL DEFAULT '',
    ticks           BIGINT NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    CHECK (note IS NULL OR length(note) <= 400)
);
