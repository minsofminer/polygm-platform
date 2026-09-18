-- P07: the security plane. Sessions and their rotation, TOTP, the withdrawal address allowlist, the key
-- envelope and its versions, the revocation job the drill measures, and the rows that make abuse detection and
-- incident readiness *auditable* rather than promised.
--
-- Three rules this schema is built on, and every table below is one of them:
--   1. anything that answers "who did what, from where, when" is append-only. An edit to it is not a fix, it
--      is the cover-up the post-mortem is about,
--   2. a secret is stored as a hash or a wrapped blob, never in plaintext, and never in a column that a
--      `SELECT *` in some future endpoint would happily return,
--   3. a control without a row is not a control. `backup_restore_tests` and `drill_records` exist so that
--      "we have a runbook" is a query with a date in it, not a paragraph in a document.

BEGIN;

-- ---------------------------------------------------------------------- D3: sessions and credentials ------ D3: how a human is *named*, and what it takes to claim one. Login looks an identity up here, the
-- password/Telegram/wallet proof lives behind it. A `claimed` row authorises nothing on its own, which is what
-- stops "link my existing Polymarket wallet" from becoming "take over whoever made that account first".
CREATE TABLE user_identities (
    kind            TEXT NOT NULL,
    value           TEXT NOT NULL,               -- normalised: lower-case, trimmed, addresses in lower hex
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state           TEXT NOT NULL DEFAULT 'claimed',
    claimed_ms      INTEGER NOT NULL,
    verified_ms     INTEGER,
    proof_kind      TEXT NOT NULL DEFAULT '',
    revoked_ms      INTEGER,
    CHECK (kind IN ('email','telegram','wallet','handle')),
    CHECK (state IN ('claimed','verified','revoked')),
    CHECK (revoked_ms IS NULL OR revoked_ms >= 0),
    -- Plain UNIQUE, not the partial index this table first shipped with. `WHERE state <> 'revoked'` cannot
    -- cross into the portable subset (the generator records it as PG-only), and a constraint that exists only
    -- in production shows up as a dev-only failure at 2am. Revocation therefore updates this row in place -
    -- the append-only `auth_events` table holds the history - which also keeps a lost email address
    -- re-claimable instead of hostage to a dead row.
    UNIQUE (kind, value)
);
CREATE INDEX user_identities_user ON user_identities (user_id, kind);

-- ---------------------------------------------------------------------- D3: sessions and credentials ----
CREATE TABLE password_credentials (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    phc           TEXT NOT NULL,
    kdf           TEXT NOT NULL DEFAULT 'argon2id',
    memory_kib    INTEGER NOT NULL,
    time_cost     INTEGER NOT NULL,
    parallelism   INTEGER NOT NULL,
    set_ms        INTEGER NOT NULL,
    updated_ms    INTEGER NOT NULL,
    gen           INTEGER NOT NULL DEFAULT 1,
    CHECK (kdf IN ('argon2id','argon2i')),
    CHECK (memory_kib >= 19456),
    CHECK (time_cost >= 2),
    CHECK (parallelism >= 1),
    CHECK (gen >= 1)
);

CREATE TABLE auth_sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    device_label    TEXT NOT NULL DEFAULT '',
    token_hash      TEXT NOT NULL UNIQUE,
    family_id       TEXT NOT NULL,                    -- refresh family: reuse detection is per family
    issued_ms       INTEGER NOT NULL,
    last_seen_ms    INTEGER NOT NULL,
    expires_ms      INTEGER NOT NULL,                 -- short: ACCESS_TOKEN_TTL
    revoked_ms      INTEGER,
    revoked_reason  TEXT NOT NULL DEFAULT '',
    cred_gen        INTEGER NOT NULL DEFAULT 1,       -- bumped by a password change, stale => refused
    ip_hash         TEXT NOT NULL DEFAULT '',         -- truncated HMAC, never the address itself
    ua_hash         TEXT NOT NULL DEFAULT '',
    CHECK (kind IN ('web','telegram','mobile','service','break_glass')),
    CHECK (ua_hash IS NULL OR length(ua_hash) <= 1500)
);
CREATE INDEX auth_sessions_user ON auth_sessions (user_id, revoked_ms);
CREATE INDEX auth_sessions_family ON auth_sessions (family_id);

CREATE TABLE refresh_secrets (
    token_hash      TEXT PRIMARY KEY,                 -- again: hash of the secret, never the secret
    family_id       TEXT NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    issued_ms       INTEGER NOT NULL,
    used_ms         INTEGER,                          -- rotation marks the old one used, and a *reuse* of it
    reused_ms       INTEGER,                          -- is the alarm: it means somebody else has the token
    revoked_ms      INTEGER
);
CREATE INDEX refresh_secrets_family ON refresh_secrets (family_id);

-- Every login, failure, rotation, revocation, TOTP use and cooldown decision. This is the table an
-- investigator reads first, so it is append-only and it does not contain payloads (see `redact.py`).
CREATE TABLE auth_events (
    id              INTEGER PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,                    -- login_ok, login_bad_password, telegram_replay, ...
    at_ms           INTEGER NOT NULL,
    session_id      TEXT NOT NULL DEFAULT '',
    ip_hash         TEXT NOT NULL DEFAULT '',
    detail_json     JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX auth_events_user_time ON auth_events (user_id, at_ms);

CREATE TABLE totp_enrollments (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    secret_wrapped  TEXT NOT NULL,
    nonce           TEXT NOT NULL,
    alg             TEXT NOT NULL DEFAULT 'AES-256-GCM',
    digits          INTEGER NOT NULL DEFAULT 6,
    period_s        INTEGER NOT NULL DEFAULT 30,
    enrolled_ms     INTEGER NOT NULL,
    verified_ms     INTEGER,
    kek_version     INTEGER NOT NULL DEFAULT 1,
    tag             TEXT NOT NULL DEFAULT '',
    last_step       INTEGER NOT NULL DEFAULT -1,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    locked_until_ms INTEGER NOT NULL DEFAULT 0,
    CHECK (alg = 'AES-256-GCM'),
    CHECK (digits IN (6,8)),
    CHECK (period_s IN (30,60)),
    CHECK (locked_until_ms IS NULL OR locked_until_ms >= 0)
);

CREATE TABLE withdrawal_addresses (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address         TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    added_ms        INTEGER NOT NULL,
    usable_ms       INTEGER NOT NULL,
    confirmed_ms    INTEGER,
    removed_ms      INTEGER,
    added_via       TEXT NOT NULL DEFAULT 'user',
    skip_cooldown   INTEGER NOT NULL DEFAULT FALSE,
    CHECK (added_via IN ('user','support_verified','import')),
    CHECK (skip_cooldown = FALSE)
);
CREATE INDEX withdrawal_addresses_user ON withdrawal_addresses (user_id, removed_ms);

-- The replay defence for Telegram `initData`. A valid signature is valid forever unless something remembers it,
-- and a captured query string from a phishing page is a login the moment we forget that. `used_ms` is set on
-- the first *login* that consumes it, a later presentation of the same hash is refused even though the
-- signature still checks, which is the only difference between this table and a comment in a doc.
CREATE TABLE telegram_nonces (
    auth_hash       TEXT PRIMARY KEY,                  -- sha256 of the whole query string
    user_id         TEXT NOT NULL,
    seen_ms         INTEGER NOT NULL,
    used_ms         INTEGER,                           -- consumed by a login (fresh data may be re-read)
    bound_session   TEXT NOT NULL DEFAULT ''
);

-- ----------------------------------------------------------------------- D2: the key envelope (self-host) ----
CREATE TABLE kek_versions (
    version         INTEGER PRIMARY KEY,
    created_ms      INTEGER NOT NULL,
    retired_ms      INTEGER,
    provider        TEXT NOT NULL DEFAULT 'env',
    key_id          TEXT NOT NULL DEFAULT '',
    ceremony_by     TEXT NOT NULL DEFAULT '',
    witnesses       TEXT NOT NULL DEFAULT '',          -- comma-separated approver ids, >=2 for break-glass
    audit_note      TEXT NOT NULL DEFAULT '',
    CHECK (provider IN ('env','kms','hsm','mock')),
    CHECK (audit_note IS NULL OR length(audit_note) <= 1500)
);

CREATE TABLE key_wraps (
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dek_version     INTEGER NOT NULL,
    wrapped_dek     TEXT NOT NULL,
    nonce           TEXT NOT NULL,
    tag             TEXT NOT NULL,
    kek_version     INTEGER NOT NULL REFERENCES kek_versions(version),
    policy_hash     TEXT NOT NULL,
    created_ms      INTEGER NOT NULL,
    rotated_ms      INTEGER,
    revoked_ms      INTEGER,
    messages_wrapped INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, dek_version),
    CHECK (messages_wrapped >= 0)
);

-- The drill's evidence: how long it actually takes to revoke N keys, batched, with the provider's answers.
CREATE TABLE revoke_jobs (
    id              TEXT PRIMARY KEY,
    scope           TEXT NOT NULL,
    requested_by    TEXT NOT NULL,
    started_ms      INTEGER NOT NULL,
    finished_ms     INTEGER,
    total           INTEGER NOT NULL,
    revoked         INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    batch_size      INTEGER NOT NULL,
    per_call_ms     INTEGER NOT NULL DEFAULT 0,
    detail_json     JSONB NOT NULL DEFAULT '{}',
    CHECK (scope IN ('user','all','node','code')),
    CHECK (total >= 0),
    CHECK (revoked >= 0),
    CHECK (failed >= 0),
    CHECK (batch_size BETWEEN 1 AND 2000),
    CHECK (detail_json IS NOT NULL)
);

-- ------------------------------------------------------------------ D4/D8: routing, abuse, platform risk ----
-- The auth level of every operation, as data. `authz.coverage()` reconciles this table against the served
-- OpenAPI document, so "an endpoint shipped with no authorisation decision" is a red gate, not a review miss.
CREATE TABLE route_auth_levels (
    operation       TEXT PRIMARY KEY,                  -- 'POST /v1/orders'
    level           TEXT NOT NULL,
    object_check    TEXT NOT NULL DEFAULT '',          -- the id the ownership test is applied to, if any
    note            TEXT NOT NULL DEFAULT '',
    CHECK (level IN ('public','user','user-owns-resource','admin','service')),
    CHECK (note IS NULL OR length(note) <= 1500)
);

CREATE TABLE wash_findings (
    id              INTEGER PRIMARY KEY,
    user_id         TEXT NOT NULL,
    builder_code    TEXT NOT NULL DEFAULT '',
    score_bps       INTEGER NOT NULL,
    factors_json    JSONB NOT NULL,                    -- every factor that contributed, with its weight
    window_start_ms INTEGER NOT NULL,
    window_end_ms   INTEGER NOT NULL,
    volume_micro    BIGINT NOT NULL DEFAULT 0,
    action          TEXT NOT NULL,
    at_ms           INTEGER NOT NULL,
    decided_by      TEXT NOT NULL DEFAULT 'rule',
    CHECK (score_bps BETWEEN 0 AND 10000),
    CHECK (action IN ('watch','hold_payout','freeze','clear')),
    CHECK (decided_by IS NULL OR length(decided_by) <= 1500)
);

-- The alert channel is the product's megaphone and it is attacker-fed: anyone can create a market. This is
-- the gate that a broadcast passed or failed, with the reasons, kept.
CREATE TABLE broadcast_gates (
    id              INTEGER PRIMARY KEY,
    market_id       TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    reasons_json    JSONB NOT NULL,
    audience        INTEGER NOT NULL DEFAULT 0,
    at_ms           INTEGER NOT NULL,
    gate_version    TEXT NOT NULL DEFAULT 'p07.1',
    CHECK (verdict IN ('broadcast','hold','refused')),
    CHECK (audience >= 0),
    CHECK (gate_version IS NULL OR length(gate_version) <= 1500)
);

CREATE TABLE builder_code_status (
    code            TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    last_seen_ms    INTEGER NOT NULL,
    changed_ms      INTEGER NOT NULL,
    reject_count    INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'venue_rejection',
    note            TEXT NOT NULL DEFAULT '',
    CHECK (state IN ('active','throttled','disabled','unknown')),
    CHECK (source IN ('venue_rejection','manual','api')),
    CHECK (note IS NULL OR length(note) <= 1500)
);

-- ------------------------------------------------------------------------ D6/D9: readiness as a query ----
CREATE TABLE secret_inventory (
    name            TEXT PRIMARY KEY,
    environment     TEXT NOT NULL,
    stored_in       TEXT NOT NULL,
    owner           TEXT NOT NULL,                      -- a human, not a team alias
    rotate_by_ms    INTEGER NOT NULL,                   -- a date, or "rotation procedure" is a slogan
    last_rotated_ms INTEGER NOT NULL DEFAULT 0,
    can_rotate_live INTEGER NOT NULL DEFAULT TRUE,     -- FALSE means a maintenance window: said out loud
    note            TEXT NOT NULL DEFAULT '',
    CHECK (environment IN ('dev','staging','prod')),
    CHECK (stored_in IN ('kms','secret_manager','env','provider_console')),
    CHECK (note IS NULL OR length(note) <= 1500)
);

CREATE TABLE backup_restore_tests (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    encrypted       INTEGER NOT NULL,
    taken_ms        INTEGER NOT NULL,
    restore_started_ms INTEGER NOT NULL,
    restore_done_ms INTEGER,
    verified_rows   INTEGER NOT NULL DEFAULT 0,
    money_checks_ok INTEGER NOT NULL DEFAULT FALSE,
    tested_by       TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    CHECK (kind IN ('pg_snapshot','sqlite_file','ledger_csv','keystore')),
    CHECK (encrypted IN (0,1)),
    CHECK (note IS NULL OR length(note) <= 1500)
);

CREATE TABLE drill_records (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    started_ms      INTEGER NOT NULL,
    finished_ms     INTEGER,
    verdict         TEXT NOT NULL DEFAULT 'incomplete',
    measured_ms     INTEGER NOT NULL DEFAULT 0,
    failed_step     TEXT NOT NULL DEFAULT '',
    participants    TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    CHECK (kind IN ('key_compromise','phishing_support','pg_failover','channel_poison')),
    CHECK (verdict IN ('pass','fail','incomplete')),
    CHECK (notes IS NULL OR length(notes) <= 1500)
);

CREATE TABLE incident_findings (
    id              INTEGER PRIMARY KEY,
    component       TEXT NOT NULL,
    threat          TEXT NOT NULL,
    actor           TEXT NOT NULL,
    control         TEXT NOT NULL,
    owner           TEXT NOT NULL,                      -- "no control without an owner"
    test_ref        TEXT NOT NULL,                      -- and a test: a file::name or a gate check id
    severity        TEXT NOT NULL,
    likelihood      INTEGER NOT NULL,
    impact_micro    BIGINT NOT NULL DEFAULT 0,          -- expected loss, micros, as our own estimate
    rank            INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    accepted_reason TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    CHECK (severity IN ('critical','high','medium','low')),
    CHECK (likelihood BETWEEN 1 AND 5),
    CHECK (rank >= 1),
    CHECK (status IN ('open','mitigated','accepted')),
    CHECK (note IS NULL OR length(note) <= 1500)
);
CREATE UNIQUE INDEX incident_findings_rank ON incident_findings (rank);

CREATE OR REPLACE FUNCTION polygm_withdrawal_hold_holds() RETURNS trigger LANGUAGE plpgsql AS $$
-- The 24 h hold on a new destination is a control only if the row carrying it cannot be edited out of the hold.
-- An address may be removed, its label may change, and `usable_ms` may legitimately move *later* (a compliance
-- freeze). What no UPDATE may do is move it earlier or clear it, because that single write is what turns "we
-- noticed the takeover in time" into "we did not": the attacker with a SQL write does not bother with the API.
--
-- This lives in Postgres only, on purpose: the twin generator special-cases the append-only block and nothing
-- else (tools/build-sqlite-migrations.py), so a conditional trigger here has no SQLite equivalent. `gate:c8`
-- therefore asserts the declaration exists, and asserts the *behaviour* where the twin can carry it (the
-- `skip_cooldown` CHECK refuses at insert). A dev database without this trigger is a weaker box, not a lie
-- about the shipped one.
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.usable_ms IS NULL
                             OR (OLD.usable_ms IS NOT NULL AND NEW.usable_ms < OLD.usable_ms)) THEN
        RAISE EXCEPTION 'the 24h hold on a withdrawal destination cannot be shortened (usable_ms % -> %)',
            OLD.usable_ms, NEW.usable_ms USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER withdrawal_hold_immutable BEFORE UPDATE ON withdrawal_addresses FOR EACH ROW
    EXECUTE FUNCTION polygm_withdrawal_hold_holds();

-- >>> append-only tables (the gate check reads exactly this comment form; do not reformat)
-- The five tables that *are* the evidence: auth events (who logged in, who was refused, who break-glassed),
-- wash findings (the payout gate's decisions), broadcast gate refusals, backup restore tests, and drill records.
-- Without these triggers every one of them is an UPDATE away from a different story, and the people writing the
-- story are the people the story is about. `incident_findings` is deliberately absent: its `status` column moves
-- from open to mitigated, so making it append-only would make it wrong.
--
-- This block is in 0009 rather than 0005 because the phase that creates a table owns its triggers - a deployed
-- database never re-runs an applied migration. It was missing on the Postgres side while the SQLite twin had it,
-- which is the shape of gap a test suite that runs on SQLite cannot see: `gate:c11` in tools/p07-gate-check.py
-- now asserts the declaration in this file, not only the behaviour of the twin.
CREATE TRIGGER append_only_auth_events          BEFORE UPDATE OR DELETE ON auth_events          FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_wash_findings        BEFORE UPDATE OR DELETE ON wash_findings        FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_broadcast_gates      BEFORE UPDATE OR DELETE ON broadcast_gates      FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_backup_restore_tests BEFORE UPDATE OR DELETE ON backup_restore_tests FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_drill_records        BEFORE UPDATE OR DELETE ON drill_records        FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
-- <<< append-only tables

COMMIT;

-- Notes moved out of the column lists below: the portable-subset generator splices column
-- clauses by line position, so a comment line *inside* a column list makes it eat the
-- neighbouring column definition. The generator's own execute-and-report loop caught this
-- (0009, 2026-09-18) - `make migrate` on a fresh database is the check that notices.

-- note 1: No CHECK here, and that is a measured decision rather than an oversight: this file's first draft had `CHECK (proof_kind IN (...))` on this column and `tools/build-sqlite-migrations.py` produced a twin whose table definition lost its closing paren, because the rewriter that moves column CHECK clauses to the end of the list does not expect one on the second-to-last line. The vocabulary is enforced in `store.link_identity` (PROOF_KINDS) with a test, which is where a wrong proof kind is also *named*.
-- note 2: The PHC string only. `argon2id$v=19$m=65536,t=3,p=4$salt$hash` is self-describing, which is what lets `passwords.needs_update` notice that a 2024-era parameter set is now cheap to crack and re-hash the password on the next successful login instead of waiting for a breach.
-- note 3: A password change must invalidate every session immediately. The counter is what a session row compares against, so "invalidate" is one UPDATE instead of a scan of every token ever issued.
-- note 4: Tokens are never stored: `token_hash` is SHA-256 of the random 256-bit secret we handed out. A read of this table (a backup, an analyst, a SQL injection into a reporting view) must not mint a session.
-- note 5: The shared secret is wrapped by the same KEK hierarchy as the signing keys: a TOTP secret is a second-factor *key*, and an attacker with the DB gets a 30-day head start on every account if it is stored in plaintext "because it is only a second factor".
-- note 6: which KEK sealed this secret, and the GCM tag. Both are needed to *read it back*: a rotation that cannot tell which version wrapped a row is a rotation that loses the row.
-- note 7: The step last accepted. A code is single-use even inside its window: without this, a 6-digit code captured by a shoulder-surf or a phishing page is valid for 30-90 seconds and replayable at will.
-- note 8: 24 h before a new destination may be used, measured from the moment it was *confirmed* (not from when it was typed): the cooldown exists to make account takeover slow enough to notice.
-- note 9: An admin or support path that could add an address and use it immediately is the classic "support impersonation" payout. `support_verified` still gets the cooldown, nothing skips it.
-- note 10: The ceremony is recorded here, not in a wiki: an operator can see who rotated what and when, and a retired KEK with any live DEK under it is a schema-visible contradiction (the gate checks it).
-- note 11: `wrapped_dek` is the DEK encrypted under the KEK, the wallet key itself never exists in our process except as bytes inside a signing call. `policy_hash` is the D2 policy that the DEK is *only* allowed to satisfy (CLOB contract + pUSD token, nothing else).
-- note 12: Polymarket can revoke a builder code in its sole discretion, and every order carrying a disabled code is rejected. The state machine here is what turns that from a support ticket into an alarm plus a fallback: orders continue unattributed, because the user's fill is not where we settle our revenue.
-- note 13: The step that failed, when one did. A drill that reports only "pass" has not been run hard enough to produce a failure, so the field is required for a `fail` and the gate checks that.