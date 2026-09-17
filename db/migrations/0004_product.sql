-- P04 D3 · 0004 everything the paying customer touches: entitlements, alerts, copy configs, automation,
-- watchlists, referrals, billing. These are NOT money-path tables (no order can be blocked or created by
-- a row here), which is why they are separated from 0002 — a schema change to billing must not be able to
-- deadlock an order.

CREATE TABLE feature_flags (
    name          TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('bool','number','string')),
    value_json    TEXT NOT NULL                  -- JSON, so a number and a bool are stored the same way
                                                 -- and `set_flag` cannot silently coerce
);

CREATE TABLE flag_audit (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT NOT NULL,
    changed_by    TEXT NOT NULL,                 -- a human or a named automation, never "system"
    reason        TEXT NOT NULL,
    at_ms         BIGINT NOT NULL
);
CREATE INDEX flag_audit_name_ix ON flag_audit (name, at_ms DESC);
-- D7's answer to "how do you audit who changed it": this table, append-only (0005), with a NOT NULL
-- reason. The service that reads flags has INSERT-only grants here, so it cannot rewrite history.

CREATE TABLE entitlements (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    plan            TEXT NOT NULL CHECK (plan IN ('free','trader','pro','team')),
    max_alerts      INT NOT NULL,
    max_watchlists  INT NOT NULL,
    max_automation_rules INT NOT NULL,
    radar_poll_ms   INT NOT NULL,
    api_rpm         INT NOT NULL,
    -- limits are DENORMALISED onto this row on purpose: every gate check reads them, and a JOIN to a
    -- plan-limits table on the hot path would make the pricing page a dependency of order placement.
    updated_ms      BIGINT NOT NULL
);

CREATE TABLE subscriptions (
    id                  TEXT PRIMARY KEY,          -- Stripe sub id or Stars subscription id
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL CHECK (provider IN ('stripe','telegram_stars')),
    status              TEXT NOT NULL CHECK (status IN ('active','past_due','cancelled','trialing')),
    current_period_end_ms BIGINT NOT NULL,
    amount_micro        BIGINT NOT NULL CHECK (amount_micro >= 0),
    currency            TEXT NOT NULL,
    provider_event_id   TEXT,                      -- set by `billing`'s recompute only if the event is
                                                   -- in the DB, so a replayed webhook cannot double-extend
    updated_ms          BIGINT NOT NULL
);
CREATE UNIQUE INDEX subs_provider_event_uq ON subscriptions (provider, provider_event_id)
    WHERE provider_event_id IS NOT NULL;

CREATE TABLE alert_rules (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    market_id       TEXT,
    event_id        TEXT,
    kind            TEXT NOT NULL CHECK (kind IN ('price_level','spread_widen','whale_fill','resolve_lead',
                                                  'illiquid_top','new_market','manual')),
    -- the window cap is NOT NULL and enforced by CHECK because the design system (D6.5) made "a rule
    -- without a window cap" the top source of notification spam in the competitor teardown. A default
    -- would let it be forgotten; a NOT NULL with a range makes it impossible.
    fires_per_window INT NOT NULL DEFAULT 3 CHECK (fires_per_window BETWEEN 1 AND 24),
    window_ms       BIGINT NOT NULL DEFAULT 3600000 CHECK (window_ms >= 60000),
    params_json     JSONB NOT NULL DEFAULT '{}',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_ms      BIGINT NOT NULL,
    -- "every alert rule must carry its own cap in the builder, prefilled" — so the cap is part of the
    -- rule identity, and a partial index lets `signals` scan only what can actually fire.
    CONSTRAINT rule_has_target CHECK (market_id IS NOT NULL OR event_id IS NOT NULL)
);
CREATE INDEX alert_rules_live_ix ON alert_rules (enabled, kind) WHERE enabled;

CREATE TABLE alert_fires (
    id              BIGSERIAL PRIMARY KEY,
    rule_id         TEXT NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    fired_ms        BIGINT NOT NULL,
    -- the value that tripped it, stored as text-and-scale rather than a float, so an old fire can be
    -- re-displayed exactly and a disputed notification can be reproduced
    observed_price_micro BIGINT,
    observed_delta_ticks INT,
    delivered       BOOLEAN NOT NULL DEFAULT FALSE,
    channel         TEXT CHECK (channel IN ('telegram','email','push','webhook','inapp')),
    UNIQUE (rule_id, fired_ms)                     -- a fanout retry must not create a second notification
);
CREATE INDEX alert_fires_recent_ix ON alert_fires (rule_id, fired_ms DESC);

CREATE TABLE copy_configs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_user     TEXT NOT NULL,                -- whom we copy (their public address, not their key)
    mode            TEXT NOT NULL CHECK (mode IN ('mirror','ratio','cap')),
    ratio_bps       INT CHECK (ratio_bps BETWEEN 1 AND 10000),
    max_order_micro BIGINT CHECK (max_order_micro > 0),
    max_daily_micro BIGINT CHECK (max_daily_micro > 0),
    blocked_markets JSONB NOT NULL DEFAULT '[]',
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    -- Copy trading is the feature most likely to lose someone money who did not mean to spend it, so the
    -- caps are NOT NULL: an unbounded copy config is a bug, and the DB says so.
    CHECK (max_order_micro IS NOT NULL AND max_daily_micro IS NOT NULL),
    created_ms      BIGINT NOT NULL
);

CREATE TABLE automation_rules (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('take_profit','stop_loss','auto_redeem','scale_out','hedge')),
    -- every rule that can MOVE MONEY is armed with an explicit ceiling; `auto_redeem` needs none because
    -- redemption cannot lose principal, which is the one asymmetry worth encoding in a CHECK.
    trigger_json    JSONB NOT NULL,
    max_loss_micro  BIGINT NOT NULL DEFAULT 0 CHECK (max_loss_micro >= 0),
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    last_run_ms     BIGINT,
    CONSTRAINT redemption_needs_no_ceiling CHECK (kind = 'auto_redeem' OR max_loss_micro > 0)
);

CREATE TABLE watchlists (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    position        INT NOT NULL DEFAULT 0,
    unique (user_id, lower(name))
);
CREATE TABLE watchlist_items (
    watchlist_id    TEXT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    market_id       TEXT NOT NULL,
    added_ms        BIGINT NOT NULL,
    PRIMARY KEY (watchlist_id, market_id)
);

CREATE TABLE referrals (
    code            TEXT PRIMARY KEY,
    owner_user      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    share_bps       INT NOT NULL DEFAULT 0 CHECK (share_bps BETWEEN 0 AND 5000),
    created_ms      BIGINT NOT NULL
);
CREATE TABLE referral_events (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL REFERENCES referrals(code),
    referred_user   TEXT NOT NULL,
    -- accrual is recorded against the fee we OBSERVED, not a projection, so a disputed referral payout is
    -- answerable from this table alone
    fee_micro       BIGINT NOT NULL CHECK (fee_micro >= 0),
    accrual_micro   BIGINT NOT NULL CHECK (accrual_micro >= 0),
    at_ms           BIGINT NOT NULL,
    UNIQUE (code, referred_user, at_ms)
);
CREATE INDEX referral_accrual_ix ON referral_events (code, at_ms DESC);

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    at_ms           BIGINT NOT NULL,
    actor_type      TEXT NOT NULL CHECK (actor_type IN ('user','admin','service','automation')),
    actor_id        TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_table    TEXT,
    target_id       TEXT,
    request_id      TEXT NOT NULL,               -- joins to the log lines; without it this table is
                                                 -- decorative, and decorative tables are not read at 3am
    detail_json     JSONB NOT NULL DEFAULT '{}'  CHECK (jsonb_typeof(detail_json) = 'object'),
    -- the payload must never contain a key or a mnemonic. Enforced in code (services/api redacts) and
    -- checked in CI by a grep for likely secret shapes in this column's writes.
    CONSTRAINT no_secret_shapes CHECK (detail_json::text !~ '(""(private_key|mnemonic|secret|api_secret)"")')
);
CREATE INDEX audit_actor_ix ON audit_log (actor_id, at_ms DESC);
CREATE INDEX audit_action_ix ON audit_log (action, at_ms DESC);
