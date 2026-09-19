-- P10 D8/D9 — the automation and alert surfaces' own storage.
--
-- Why new tables rather than `ALTER TABLE automation_rules ADD COLUMN name`: the SQLite subset the test suite
-- runs on is generated from these files and the transpiler DROPS `ALTER TABLE` (db/migrations-sqlite/DROPPED.json
-- records it). The house rule since 0007 has been to add a table instead of widening an applied one, because an
-- invariant that exists only in Postgres is an invariant CI never exercises. Two small tables cost one join and
-- buy portability by construction.
--
-- Nothing here is another rule engine. `automation_rules` + `automation_rule_policy` (P06) hold what a rule
-- does, `alert_rules` + `signal_state` + `alert_deliveries` (P04/P05) hold what fires and what was delivered.
-- These two tables hold the two things those phases had no place for: a rule's NAME (a list of `rule-9f3a2c`
-- is not a list a human reads) and the user's notification preferences (quiet hours, digest, default channel).

CREATE TABLE automation_rule_labels (
    -- The user's own words for a rule. Kept apart from `automation_rule_policy` because policy is the engine's
    -- input and this is the UI's: a rename must never touch a document the engine reads, and a rule with no
    -- label here is labelled from its trigger by the API rather than shown as a hex id.
    rule_id             TEXT PRIMARY KEY REFERENCES automation_rules(id) ON DELETE CASCADE,
    name                TEXT NOT NULL DEFAULT '',
    updated_ms          BIGINT NOT NULL,
    CHECK (length(name) <= 80)
);

CREATE TABLE notification_settings (
    -- One row per user, created on first read with the defaults below: Telegram (the prompt's default channel),
    -- quiet hours OFF (-1 means "no quiet window"; 0 would mean midnight-to-midnight, which is a mute button
    -- wearing a time window's clothes), digest OFF, and UTC until the client tells us where the user actually is.
    --
    -- Quiet hours are stored as minute-of-day integers plus an explicit UTC offset, not as a string like
    -- "22:00-07:00": the evaluation is then integer arithmetic the alert planner can be tested against, and a
    -- window that wraps midnight is a case the tests can name rather than a parsing edge nobody tried.
    user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    quiet_start_min     INT NOT NULL DEFAULT -1,     -- minutes past midnight in the user's own offset, -1 = off
    quiet_end_min       INT NOT NULL DEFAULT -1,
    tz_offset_min       INT NOT NULL DEFAULT 0,      -- minutes EAST of UTC; the client sends it, we never guess
    digest_mode         TEXT NOT NULL DEFAULT 'off' CHECK (digest_mode IN ('off','hourly','daily')),
    digest_at_min       INT NOT NULL DEFAULT 480,    -- 08:00 local, used by 'daily'; ignored by 'off'
    default_channel     TEXT NOT NULL DEFAULT 'telegram'
                        CHECK (default_channel IN ('telegram','email','webhook')),
    updated_ms          BIGINT NOT NULL,
    -- A quiet window is either entirely off or a real pair with two ends. `start only` would be a window the
    -- planner has to invent an end for, and the invention would be the bug.
    CHECK ((quiet_start_min = -1 AND quiet_end_min = -1)
           OR (quiet_start_min BETWEEN 0 AND 1439 AND quiet_end_min BETWEEN 0 AND 1439)),
    CHECK (tz_offset_min BETWEEN -840 AND 840),      -- UTC-14:00 .. UTC+14:00, the real-world span
    CHECK (digest_at_min BETWEEN 0 AND 1439)
);
