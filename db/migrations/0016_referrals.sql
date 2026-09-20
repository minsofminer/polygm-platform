-- P11 D5 · Referrals: the funnel somebody is paid for, and the evidence that stops it being farmed.
--
-- --------------------------------------------------------------------------- what this file replaces, and why
-- P04 sketched a referral model before there was a reward decision to model: `referrals (code, owner_user,
-- share_bps, created_ms)` and `referral_events (code, referred_user, fee_micro, accrual_micro, at_ms)`. Nothing
-- has ever written or read either table — `grep -rn referral_events services packages tests tools` returns the
-- schema, the append-only trigger list and the portable-subset builder's table list, and no caller. They are
-- dropped here rather than migrated, for two reasons that would each be a bug if they survived:
--
--   1. **`referrals.share_bps` is a per-code negotiated rate.** D5's model is one uniform share (`SHARE_BPS`)
--      because a rate that can differ per code is a rate an account manager can raise, and the anti-Sybil
--      arithmetic in the package docstring (an attacker must pay more in fees than the share returns) stops
--      holding the moment somebody can negotiate the share up.
--   2. **`referral_events` cannot answer the question the dashboard asks.** It has no referrer column, no state
--      and no term, so "who is owed what, and is this referral still inside its year" is not expressible against
--      it. A second referral schema living beside the real one is exactly the two-authorities-over-one-number
--      failure this project keeps writing gates about.
--
-- The name a user meets is `referral_links`, which is also the name P01's required-table list already uses.
--
-- ---------------------------------------------------------------------------------------------- the tables
--   referral_links        one row per referrer per kind: a generated link token, plus an optional short code.
--   referral_clicks       the denominator. Every hit on a link, hashed signals and a timestamp: no user, no IP.
--   referral_signals      the device/IP/funding digests a dedupe decision reads, hashed with the service salt.
--   referral_attributions ONE row per referee, for ever: who referred them, the order that qualified it, its term.
--   referral_accruals     one row per referee per day, from fees the venue actually paid us. Append-only.
--   referral_reviews      the manual queue: velocity, collisions, self-referrals, payout thresholds, clawbacks.
--   referral_payouts      what was sent, when, by what method, and whether a tax form is on file.
--
-- Three choices are load-bearing rather than incidental:
--
-- 1. **`referral_attributions` is keyed by the referee.** One person can be referred once, ever. That is what
--    makes the published clawback rule enforceable ("claw back the referral" needs a referral to point at) and
--    what stops a referee being re-attributed to a friendlier referrer after a review goes badly.
-- 2. **`referral_accruals` is append-only** and keyed by (referrer, referee, day): the sum of the table IS what
--    a referrer was owed, and a re-run of the accrual job for a day cannot pay twice. `referral_payouts` is
--    deliberately NOT append-only — it is a state machine (pending → approved → sent) — and its guard is the
--    CHECK that nothing reaches `approved` without a tax form on file.
-- 3. **Signals are stored hashed and nothing else is stored at all.** We keep no IP, no user agent and no
--    funding address: only `d_…`/`i_…`/`f_…` digests (`polygm_core.referrals.sybil.hash_`), so a dump of this
--    schema contains nothing about a stranger who clicked a link and never signed up.

DROP TABLE IF EXISTS referral_events;
DROP TABLE IF EXISTS referrals;

CREATE TABLE IF NOT EXISTS referral_links (
    user_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    code        TEXT PRIMARY KEY,                -- normalised: lower-case, a-z0-9 only
    token       TEXT NOT NULL,                   -- 'ref_' + 22 base32 chars; the thing an attribution records
    kind        TEXT NOT NULL DEFAULT 'link',
    state       TEXT NOT NULL DEFAULT 'active',
    created_ms  BIGINT NOT NULL,
    retired_ms  BIGINT,
    CHECK (kind IN ('link','short')),
    CHECK (state IN ('active','retired')),
    CHECK (retired_ms IS NULL OR retired_ms >= created_ms),
    -- A short code is optional on purpose: a referrer who never says a code on a stream never claims one, and a
    -- placeholder row per user would make "the referrer's code" ambiguous.
    CHECK (kind <> 'short' OR length(code) BETWEEN 4 AND 16)
);
-- One ACTIVE link and one ACTIVE short code per account. A partial unique index rather than a CHECK, because the
-- constraint is about state and not about the row: retiring a code and claiming another must be possible, and
-- two live short codes for one referrer is how two links end up on one dashboard with two different click counts.
-- The token is unique per LINK and not per row: a referrer's short code carries the same token as their link,
-- so a click on either resolves to one referrer, and an attribution records one string. Two unique tokens per
-- referrer would be two things to reconcile, and the second one would exist only because a UNIQUE constraint
-- was declared on a column instead of on the fact.
CREATE UNIQUE INDEX referral_links_token_ix ON referral_links (token)
    WHERE kind = 'link';
CREATE UNIQUE INDEX referral_links_one_short_ix ON referral_links (user_id)
    WHERE kind = 'short' AND state = 'active';
CREATE UNIQUE INDEX referral_links_one_link_ix ON referral_links (user_id)
    WHERE kind = 'link' AND state = 'active';

CREATE TABLE IF NOT EXISTS referral_clicks (
    -- No user id: a click is by definition from somebody who is not (yet) an account, and the row that ties a
    -- click to a person is the attribution. What is kept is what the velocity and dedupe rules read, hashed.
    id          BIGSERIAL PRIMARY KEY,
    referrer    TEXT NOT NULL,                   -- users.id of whoever's link was clicked
    code        TEXT NOT NULL DEFAULT '',        -- the code or token as it arrived; never used as identity
    token       TEXT NOT NULL DEFAULT '',
    device_hash TEXT NOT NULL DEFAULT '',
    ip_hash     TEXT NOT NULL DEFAULT '',
    landing     TEXT NOT NULL DEFAULT '',
    at_ms       BIGINT NOT NULL
);
-- Read path 1: a referrer's own funnel counts are scoped to them and ordered by recency ("clicks this week").
CREATE INDEX referral_clicks_referrer_ix ON referral_clicks (referrer, at_ms DESC);
-- Read path 2: attributing a signup walks back from the landing token to the click it came from, which is a
-- lookup on the token — without this index that is a scan of a table that grows with every share.
CREATE INDEX referral_clicks_token_ix ON referral_clicks (token, at_ms DESC);

CREATE TABLE IF NOT EXISTS referral_signals (
    -- The digests a dedupe decision reads. `first_ms` is what a collision is reported against; `last_ms` is what
    -- makes a stale device hash expire rather than live for ever.
    user_id     TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    hash        TEXT NOT NULL,
    first_ms    BIGINT NOT NULL,
    last_ms     BIGINT NOT NULL,
    PRIMARY KEY (user_id, kind, hash),
    CHECK (kind IN ('device','ip','funding')),
    CHECK (hash LIKE '_\_%' ESCAPE '\'),          -- 'd_…', 'i_…', 'f_…': a raw value must not be storable here
    CHECK (last_ms >= first_ms)
);
-- The dedupe question is "who ELSE has this digest", so the index is on the digest and not the user: every
-- attribution runs that lookup twice (the referee's signals, the referrer's), inside the write that creates the
-- attribution. An index on user_id would be the wrong way round for the only question this table answers.
CREATE INDEX referral_signals_hash_ix ON referral_signals (kind, hash);

CREATE TABLE IF NOT EXISTS referral_attributions (
    -- ONE ROW PER REFEREE, FOR EVER. That primary key is a fraud mechanism that has nothing to do with signals:
    -- a wallet that has been referred cannot be referred again by somebody else, so a farm cannot launder the
    -- same referee through a fresh referrer when a review goes against it.
    referee        TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    referrer       TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    code           TEXT NOT NULL DEFAULT '',
    token          TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT 'pending',
    reason         TEXT NOT NULL DEFAULT '',
    signed_up_ms   BIGINT NOT NULL,
    qualify_order  TEXT NOT NULL DEFAULT '',     -- the order id that opened the term ('' until it does)
    qualify_ms     BIGINT NOT NULL DEFAULT 0,
    notional_micro BIGINT NOT NULL DEFAULT 0,
    term_ends_ms   BIGINT NOT NULL DEFAULT 0,
    builder_code   TEXT NOT NULL DEFAULT '',     -- the affiliate code this referral runs under (P08 registry)
    decided_ms     BIGINT NOT NULL DEFAULT 0,
    CHECK (state IN ('pending','qualified','review','refused','expired','clawed_back')),
    CHECK (state <> 'qualified' OR qualify_ms > 0),
    CHECK (referee <> referrer),                 -- the hard self-referral block, in the schema
    CHECK (qualify_ms = 0 OR qualify_ms >= signed_up_ms)
);
-- Read path 1: the dashboard lists one referrer's referrals, filtered by state, newest first.
CREATE INDEX referral_attributions_referrer_ix ON referral_attributions (referrer, state, signed_up_ms DESC);
-- Read path 2: the expiry sweep walks live referrals by term end; without this it is a full scan per sweep.
CREATE INDEX referral_attributions_term_ix ON referral_attributions (state, term_ends_ms);

CREATE TABLE IF NOT EXISTS referral_accruals (
    id                  BIGSERIAL PRIMARY KEY,
    referrer            TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    referee             TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    day                 TEXT NOT NULL,           -- 'YYYY-MM-DD' UTC, the day the fee was collected
    fee_observed_micro  BIGINT NOT NULL CHECK (fee_observed_micro >= 0),
    share_bps           INT NOT NULL CHECK (share_bps BETWEEN 0 AND 10000),
    share_micro         BIGINT NOT NULL CHECK (share_micro > 0),
    created_ms          BIGINT NOT NULL,
    UNIQUE (referrer, referee, day),
    -- A share cannot exceed the fee it is a share of. The CHECK is the arithmetic identity, enforced where it
    -- cannot be argued with, because this table is a claim on revenue we were paid.
    CHECK (share_micro <= fee_observed_micro)
);
-- Read path: the dashboard sums one referrer's accruals and buckets them by the settlement hold, so the index is
-- (referrer, created_ms) — the same order `terms.payable()` walks them in.
CREATE INDEX referral_accruals_referrer_ix ON referral_accruals (referrer, created_ms DESC);

CREATE TABLE IF NOT EXISTS referral_reviews (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT NOT NULL,
    subject       TEXT NOT NULL,                 -- the referrer whose account is in question
    referee       TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'open',
    findings_json TEXT NOT NULL DEFAULT '[]',    -- sentences built by the engine; never raw signal values
    decision      TEXT NOT NULL DEFAULT '',
    actor         TEXT NOT NULL DEFAULT '',
    opened_ms     BIGINT NOT NULL,
    decided_ms    BIGINT NOT NULL DEFAULT 0,
    CHECK (kind IN ('velocity','shared_device_or_ip','duplicate_funding','self_referral',
                    'payout_threshold','clawback','manual')),
    CHECK (state IN ('open','cleared','actioned')),
    CHECK (decided_ms = 0 OR decided_ms >= opened_ms)
);
-- Read path 1: the queue itself — open rows, newest first, which is how the admin screen reads it.
CREATE INDEX referral_reviews_open_ix ON referral_reviews (state, opened_ms DESC);
-- Read path 2: a referrer's own dashboard says "one of your referrals is in review" without scanning the queue.
CREATE INDEX referral_reviews_subject_ix ON referral_reviews (subject, opened_ms DESC);

CREATE TABLE IF NOT EXISTS referral_payouts (
    id            BIGSERIAL PRIMARY KEY,
    referrer      TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    period        TEXT NOT NULL,                 -- 'YYYY-MM', the month the accruals fell in
    amount_micro  BIGINT NOT NULL CHECK (amount_micro > 0),
    method        TEXT NOT NULL DEFAULT 'usdc',
    status        TEXT NOT NULL DEFAULT 'pending',
    tax_form      TEXT NOT NULL DEFAULT '',      -- 'w9' | 'w8ben' | …: empty means we may not pay yet
    tax_reported  BOOLEAN NOT NULL DEFAULT FALSE,
    tx_ref        TEXT NOT NULL DEFAULT '',
    approved_ms   BIGINT NOT NULL DEFAULT 0,
    sent_ms       BIGINT NOT NULL DEFAULT 0,
    created_ms    BIGINT NOT NULL,
    UNIQUE (referrer, period),
    CHECK (method IN ('usdc','bank','credit')),
    CHECK (status IN ('pending','approved','sent','failed','clawed_back')),
    -- The rule the payout page states, in the schema: nothing moves without a tax form on file.
    CHECK (status NOT IN ('approved','sent') OR tax_form <> '')
);
-- Read path: a referrer's payout history is one month per row, newest first; the status index is what the
-- payout job and the "how much is outstanding" question read.
CREATE INDEX referral_payouts_referrer_ix ON referral_payouts (referrer, period DESC);
CREATE INDEX referral_payouts_status_ix ON referral_payouts (status, created_ms);

-- Append-only, at the same level as the money tables: an accrual IS a claim on our revenue, and a row that can
-- be edited is a row that can be argued with after the fact by somebody who was not in the room.
CREATE TRIGGER append_only_referral_accruals
    BEFORE UPDATE OR DELETE ON referral_accruals
    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
