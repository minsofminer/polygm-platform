-- P11 D4 · The identity a ranked wallet appears under.
--
-- One table, one row per account, and **no row means private**. That direction matters: the default is the
-- absence of a decision, so a user who never opens the setting has never been listed, and no backfill, migration
-- or seed can list them by forgetting a column default. `state` exists so that opting OUT is also recorded —
-- "you turned this off on the 3rd" is answerable — but `private` and "no row" answer the same questions.
--
-- What this table does NOT control, deliberately: whether the wallet is ranked. Every eligible wallet is on the
-- board (D1's integrity rules, and the kit's "no ranking that hides a blown-up account"), so the setting cannot
-- be a way out of a ranking. It governs the LINK between a board row and the human behind it:
--
--   * `listed`  — the row carries the account's handle, and `/trader/<handle>` resolves to the same wallet as
--                 `/trader/<pseudonym>`. This is what "appear on public leaderboards" means here.
--   * `private` — the row is published exactly as every row was before this table existed: pseudonym, stats,
--                 classifications, and no field anywhere that connects it to an account.
--
-- `handle` is a copy of the claimed `user_identities(kind='handle')` value at the moment of the opt-in, and it
-- is NOT the source of truth for the name — the identity table is. It is kept here because the read path that
-- decorates a board row must not join identity rows per row, and because a listing is a fact about the past: if
-- a user renames, the row still says which handle it was published under until the next opt-in refreshes it.
--
-- `listed_ms` is when consent was given, and it is NULL whenever the state is `private`, enforced in the table
-- rather than in the writer. The append-only history of both directions lives in `audit_log`
-- (`action='leaderboard.identity'`), which is where "when did they agree to this" belongs.
CREATE TABLE IF NOT EXISTS leaderboard_identity (
    user_id    TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    state      TEXT NOT NULL CHECK (state IN ('private', 'listed')),
    handle     TEXT NOT NULL DEFAULT '',
    listed_ms  BIGINT,
    updated_ms BIGINT NOT NULL,
    CHECK (state = 'listed' OR listed_ms IS NULL)
);
-- The read path this exists for: "which handles are published right now", asked on every board render so that a
-- listed trader's row carries their handle. Without it that is a scan of the table on every board read.
CREATE INDEX leaderboard_identity_listed_ix ON leaderboard_identity (state, user_id);
