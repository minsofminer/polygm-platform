-- 0019_telegram.sql — P12 D1: the bot's four tables, in the order the request path touches them.
--
-- The phase needs somewhere to be honest about four things: that an update was handled exactly once, that a
-- conversation was left mid-flow, that a message still has to go out, and what users actually asked for. Each
-- table below exists because of a specific failure it prevents, and the failures are the reason this is a
-- migration rather than a Python dictionary:
--
--   * `telegram_updates` — the retry that matters arrives after a restart. Telegram re-delivers an update whose
--     2xx we never sent, with the same `update_id`, and the update most likely to arrive twice is the one where
--     the user pressed Confirm. A dedupe in process memory cannot see it.
--   * `telegram_sessions` — a chat is a state machine with no screen: "waiting for a size on market X" has to
--     survive the process that is answering, and it has to *expire*, or a user returns to a confirm card whose
--     buttons are a day old.
--   * `telegram_outbox` — a fill notification must not be lost because the worker restarted between the fill and
--     the send, and it must not be sent twice because two workers claimed it.
--   * `telegram_commands` — metrics with a real answer: DAU, commands per user, alert→trade conversion, and the
--     deposit→first-trade clock the kit's D7 sets a target for. A metric nobody wrote down is a metric nobody has.
--
-- Everything here is append-only except `telegram_sessions` (a state row is updated in place, and its own history
-- is not evidence) and the outbox's claim columns (a sent flag is a fact about work, not a decision about a
-- person). The append-only list in `tools/build-sqlite-migrations.py` names the rest, and 0018 already grants for
-- the tables listed there — this migration's tables are deliberately NOT append-only except where noted, so no
-- grant block is needed here.

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id   BIGINT PRIMARY KEY,          -- Telegram's, not ours: the dedupe key is theirs and it is unique
    kind        TEXT NOT NULL DEFAULT 'unknown',
    chat_id     TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    command     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'claimed',   -- claimed | done | failed
    note        TEXT NOT NULL DEFAULT '',
    runs        INTEGER NOT NULL DEFAULT 0,        -- a value above 1 is an incident, and the gate looks for it
    first_ms    BIGINT NOT NULL,
    done_ms     BIGINT NOT NULL DEFAULT 0,
    CHECK (state IN ('claimed','done','failed'))
);
-- Read path 1: the dedupe itself, which is a primary-key hit (`SELECT state ... WHERE update_id=?`).
-- Read path 2: the operator's question, "what did this chat do today", answered from the same table.
CREATE INDEX IF NOT EXISTS telegram_updates_chat_ix ON telegram_updates (chat_id, first_ms DESC);
-- Read path 3: stale claims — a crash mid-handler leaves `claimed`, and the on-call needs to see them without a
-- full scan of a table that grows with every message Telegram ever sends us.
CREATE INDEX IF NOT EXISTS telegram_updates_state_ix ON telegram_updates (state, first_ms) WHERE state = 'claimed';

CREATE TABLE IF NOT EXISTS telegram_sessions (
    chat_id      TEXT PRIMARY KEY,           -- one live conversation per chat, because a chat has one "next message"
    step         TEXT NOT NULL DEFAULT 'idle',
    payload_json TEXT NOT NULL DEFAULT '{}', -- the step's inputs; never a price, never a signed order
    message_id   BIGINT NOT NULL DEFAULT 0,  -- the message the flow edits, so the flow stays in one bubble
    started_ms   BIGINT NOT NULL,
    updated_ms   BIGINT NOT NULL,
    expires_ms   BIGINT NOT NULL,            -- every session expires; the export flow expires fastest
    version      INTEGER NOT NULL DEFAULT 1,
    CHECK (step IN ('idle','choose_market','choose_side','choose_size','confirm_order','custom_size',
                    'withdraw_amount','withdraw_address','export_disclaimer','export_confirm','alert_rule',
                    'copy_config','verify_handle')),
    CHECK (expires_ms > started_ms),
    CHECK (updated_ms >= started_ms)
);
CREATE INDEX IF NOT EXISTS telegram_sessions_expiry_ix ON telegram_sessions (expires_ms);

CREATE TABLE IF NOT EXISTS telegram_outbox (
    id             BIGSERIAL PRIMARY KEY,
    chat_id        TEXT NOT NULL,
    chat_type      TEXT NOT NULL DEFAULT 'private',
    priority       INTEGER NOT NULL,
    method         TEXT NOT NULL DEFAULT 'sendMessage',
    text           TEXT NOT NULL DEFAULT '',
    keyboard_json  TEXT NOT NULL DEFAULT '',
    edit_message_id BIGINT NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'queued',     -- queued | sending | sent | failed
    attempts       INTEGER NOT NULL DEFAULT 0,
    claim_ms       BIGINT NOT NULL DEFAULT 0,          -- when a worker took it: a stuck claim is a stuck message
    created_ms     BIGINT NOT NULL,
    due_ms         BIGINT NOT NULL DEFAULT 0,
    sent_ms        BIGINT NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    CHECK (state IN ('queued','sending','sent','failed')),
    CHECK (priority BETWEEN 1 AND 999),
    CHECK (attempts >= 0)
);
-- Read path: the drain asks for "queued, due, in priority order" — and it must be an index scan, because this
-- table is the hot path during a broadcast and a full scan of it would be a full scan of a queue under load.
CREATE INDEX IF NOT EXISTS telegram_outbox_ready_ix ON telegram_outbox (state, due_ms, priority, id);
-- Read path 2: the sending-state sweep looks for claims older than a minute (a worker that died mid-send).
CREATE INDEX IF NOT EXISTS telegram_outbox_claim_ix ON telegram_outbox (state, claim_ms) WHERE state = 'sending';

CREATE TABLE IF NOT EXISTS telegram_commands (
    id          BIGSERIAL PRIMARY KEY,
    at_ms       BIGINT NOT NULL,
    chat_id     TEXT NOT NULL,
    chat_type   TEXT NOT NULL DEFAULT 'private',
    user_id     TEXT NOT NULL DEFAULT '',
    command     TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',      -- the callback action, when the "command" was a button tap
    ok          INTEGER NOT NULL DEFAULT 1,
    dur_ms      INTEGER NOT NULL DEFAULT 0,
    update_id   BIGINT NOT NULL DEFAULT 0,
    CHECK (ok IN (0, 1)),
    CHECK (dur_ms >= 0)
);
-- Read path 1: DAU and commands-per-user — a range scan on the day, then a group by chat.
CREATE INDEX IF NOT EXISTS telegram_commands_day_ix ON telegram_commands (at_ms DESC, chat_id);
-- Read path 2: the funnel the kit sets a target for (deposit → first trade), which asks about one chat's history.
CREATE INDEX IF NOT EXISTS telegram_commands_chat_ix ON telegram_commands (chat_id, at_ms DESC);

CREATE TABLE IF NOT EXISTS telegram_broadcasts (
    -- The public channel's record: what went out, when, and why it was allowed to. D5 puts a quality gate and a
    -- cadence cap in front of the broadcast, and both of those are questions about this table's history.
    id          BIGSERIAL PRIMARY KEY,
    channel     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',      -- large_fill | volume_spike | new_market | resolution
    market_id   TEXT NOT NULL DEFAULT '',
    message_id  BIGINT NOT NULL DEFAULT 0,     -- the channel message, for the one-tap trade button's later edits
    quality     INTEGER NOT NULL DEFAULT 0,    -- the gate's score for the underlying event, 0-100
    fired_ms    BIGINT NOT NULL,
    CHECK (quality BETWEEN 0 AND 100)
);
CREATE INDEX IF NOT EXISTS telegram_broadcasts_cadence_ix ON telegram_broadcasts (channel, fired_ms DESC);

CREATE TABLE IF NOT EXISTS telegram_kill_state (
    -- Separate from P06's trading kill switch on purpose. They stop different things, and conflating them is how an
    -- operator who wants to stop *messages* ends up stopping *trades*: the trading switch halts orders and leaves
    -- the outbox alone (a user must still be told what happened to their money); this one pauses delivery and
    -- leaves trading alone. Both are append-only logs of "who did what, when, and why", which is the only form of a
    -- switch anyone can reconstruct after the fact.
    id          BIGSERIAL PRIMARY KEY,
    engaged     BOOLEAN NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'all',
    reason      TEXT NOT NULL DEFAULT '',
    changed_by  TEXT NOT NULL DEFAULT '',
    at_ms       BIGINT NOT NULL,
    CHECK (scope IN ('all','channel','personal')),
    CHECK (NOT engaged OR length(reason) >= 8)      -- an unexplained kill switch reads as an outage
);
CREATE INDEX IF NOT EXISTS telegram_kill_ix ON telegram_kill_state (at_ms DESC, id DESC);

-- The one append-only table in this migration, and it is declared in `tools/build-sqlite-migrations.py`'s
-- portable list for the reason that list exists: a broadcast record is evidence of what we told thousands of
-- people. Both halves of the promise live here — the trigger, and the grant — because 0005's `DO` block cannot
-- name a table that did not exist when it ran, and the D6 sweep is the reason this repository no longer forgets
-- the second half.
CREATE TRIGGER append_only_telegram_broadcasts BEFORE UPDATE OR DELETE ON telegram_broadcasts
    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();

DO $$
DECLARE t TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'polygm_app') THEN
        RAISE NOTICE 'role polygm_app is absent (dev/test database): skipping the append-only grants';
        RETURN;
    END IF;
    FOREACH t IN ARRAY ARRAY['telegram_broadcasts', 'telegram_kill_state'] LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM PUBLIC', t);
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM polygm_app', t);
            EXECUTE format('GRANT INSERT, SELECT ON %I TO polygm_app', t);
        END IF;
    END LOOP;
END $$;
