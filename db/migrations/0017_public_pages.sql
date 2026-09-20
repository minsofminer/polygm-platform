-- P11 D6 · Public pages: the crawl surface, what it is allowed to show, and what stops it being scraped.
--
-- D6 publishes three pages that exist to be shared and indexed — `/trader/<handle>`, `/market/<slug>`,
-- `/leaderboard/<board>` — plus the images that go in a link preview. That turns three things that were
-- internal facts into public contracts: a handle is now a URL, a market slug is now a URL, and an anonymous
-- caller is now a load source with no session to throttle.
--
-- ------------------------------------------------------------------------------------------ the two indexes
-- Both are **the database enforcing what the API already promises**, which is the only reason to prefer this to
-- a comment saying "remember to check":
--
--   1. `leaderboard_identity.handle` had no uniqueness constraint at all. D4's `POST /v1/leaderboard/identity`
--      answers 409 HANDLE_TAKEN by *looking the handle up and comparing*, which is a promise with a race in it:
--      two accounts claiming `surat_whale` in the same millisecond both read "not taken". Nothing in D4 exposed
--      that, because a handle was decoration on a row. D6 makes it a URL — the address of a public page — and a
--      URL that resolves to one of two people is a phishing primitive, not a cosmetic bug.
--   2. `markets.slug` was a nullable column with **no index of any kind**, because nothing ever read it; the
--      market routes resolve by `id`. A page addressed by slug needs the slug to be unique and the lookup to be
--      an index hit, or the first duplicate slug silently rewrites somebody's canonical URL.
--
-- Both are partial: `WHERE state = 'listed' AND handle <> ''` and `WHERE slug IS NOT NULL AND slug <> ''`.
-- A partial index is the correct shape here because the constraint is about *published* values only — an
-- account that has never listed, and a market the venue never gave a slug, must not collide with each other on
-- the empty string. (The empty-string half of each predicate is not decoration either: SQLite and Postgres
-- agree that NULLs are distinct in a unique index, and disagree about nothing else here, but `''` is a value
-- both engines will happily duplicate.)
--
-- -------------------------------------------------------------------------------------- the anonymous budget
-- `public_page_hits` is a fixed-window counter per (kind, subject digest, window), and it exists because the
-- public pages are the only part of this system a caller can reach without a credential. Two notes on the
-- design, both of which are the difference between a limit and a denial of service:
--
--   * **The subject is a digest, never an address.** It is salted with the deployment's secret exactly as the
--     referral signals are (`polygm_core.referrals.sybil.hash_`), so a dump of this table cannot be joined
--     against anything, and two deployments' tables cannot be joined to each other. We cannot rate-limit by IP
--     without hashing it; we can hash it in a way that stops the hash being the IP.
--   * **The window is fixed from the first hit, not sliding**, for the reason P07's login lock is: a sliding
--     window makes an honest burst pay for the window's shape, and it makes "am I blocked?" depend on the order
--     of events. Fixed windows are explainable, and the lock is a hard lock with a stated retry-after.
--
-- The budget is deliberately *generous* per subject and *strict* per kind, because of what a shared address
-- means: a school, a carrier NAT exit and a corporate proxy put thousands of readers behind one digest, and a
-- budget tight enough to stop a scraper at that subject is a budget that denies service to everybody sharing an
-- exit. The limit that actually protects the pages is the one that counts a subject's hits *across* kinds — a
-- crawler does not politely stay inside one page type.
CREATE TABLE IF NOT EXISTS public_page_hits (
    kind            TEXT NOT NULL,              -- trader | market | leaderboard | sitemap | og
    subject_hash    TEXT NOT NULL,              -- i_… digest of the caller's address; never an address
    window_start_ms BIGINT NOT NULL,            -- fixed from the first hit in the window
    hits            INT NOT NULL DEFAULT 0,
    locked_until_ms BIGINT NOT NULL DEFAULT 0,  -- non-zero means the window ended in a lock
    last_ms         BIGINT NOT NULL
);
CREATE UNIQUE INDEX public_page_hits_uq ON public_page_hits (kind, subject_hash, window_start_ms);
-- Read path: "is this subject locked right now, across every kind" — asked before every public read.
CREATE INDEX public_page_hits_subject_ix ON public_page_hits (subject_hash, last_ms DESC);

-- Abuse protection that is not a rate limit: a scraper that stays politely under the budget is still a
-- scraper, and the only remaining lever is to refuse the subject outright for a while. A block is a row with
-- an expiry and a reason, never a row without one — an unexpiring block nobody can explain is how a public
-- page becomes "that page that is broken for me".
CREATE TABLE IF NOT EXISTS public_page_blocks (
    subject_hash TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'all',   -- all | the page kind being abused
    reason       TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'manual',-- manual | auto
    until_ms     BIGINT NOT NULL,
    created_ms   BIGINT NOT NULL,
    created_by   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (subject_hash, scope)
);
-- Read path: which blocks are live at this instant. Expired rows are kept (they are the record of a decision
-- that was taken) and swept by the same job that writes the hits' window rollovers.
CREATE INDEX public_page_blocks_until_ix ON public_page_blocks (until_ms);

-- The crawl surface has to be bounded, and the bound is a number somebody chose, so it lives in a row rather
-- than in a constant in a route: "every listed handle" is a sitemap that grows without limit, and the day it
-- exceeds the protocol's file limit the failure is a truncated sitemap nobody notices.
CREATE TABLE IF NOT EXISTS public_page_budget (
    key         TEXT PRIMARY KEY,               -- sitemap_handles | sitemap_markets | card_ttl_ms | ...
    value       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    updated_ms  BIGINT NOT NULL
);

-- No append-only table here, and that is a decision rather than an omission: a hit counter is incremented and a
-- budget row is rewritten on every request (that is what a fixed window IS), and a block is lifted early when a
-- false positive is reported. Making any of the three append-only would either break the counter or push an
-- operator's correction into a second table nobody reads. The append-only half of this feature is the audit
-- trail those tables feed (`audit_log`), which 0005 already enumerates in both halves — trigger and grant — and
-- the grant list for the leaderboard/referral tables 0005 could not see lives in 0016. The P11 gate's c27 reads
-- every `append_only_*` trigger in this directory and every table in a REVOKE, and fails when they disagree.
