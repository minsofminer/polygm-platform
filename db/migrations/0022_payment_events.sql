-- 0022_payment_events.sql — the replay ledger for the two payment webhooks, written before either integration
-- exists.
--
-- P14 recorded payment-webhook forgery as an OPEN item with the requirements attached: verify over the raw bytes,
-- constant-time, refuse outside a five-minute tolerance, **store every event id**, and never read the amount, the
-- user or the plan from the body. The other four are code (`packages/polygm_core/payments/webhooks.py`); this
-- table is the one that needs storage, and it is built now because the moment Stripe or Stars ships, a delivery
-- that arrives twice must be refused by a uniqueness constraint rather than by a SELECT that races with the
-- second delivery.
--
-- The primary key is the *provider's* identifier — Stripe's `event.id`, Telegram Stars' charge id — because that
-- is the value that is stable across retries and that an attacker cannot choose. Two providers share one table
-- rather than getting one each: the question the table answers is "has this exact delivery been seen", and the
-- `provider` column keeps the two answerable separately without a second uniqueness rule to get wrong.
--
-- Append-only, and registered as such in `tools/build-sqlite-migrations.py`'s APPEND_ONLY list — a ledger a row
-- can be deleted from is not a ledger. The P11 gate's c27 checks both halves of that promise (the trigger and the
-- REVOKE), which is exactly the check that caught `telegram_kill_state` missing its trigger.
CREATE TABLE IF NOT EXISTS payment_events (
    event_id    TEXT PRIMARY KEY,
    provider    TEXT NOT NULL CHECK (provider IN ('stripe', 'telegram_stars')),
    event_type  TEXT NOT NULL DEFAULT '',
    received_ms BIGINT NOT NULL
);

-- The operator's question is "what did this provider send us in the last hour", which is a scan of one provider
-- by time; the primary key answers the processor's question (has this delivery been seen) and this answers the
-- other one.
CREATE INDEX IF NOT EXISTS payment_events_provider_ix ON payment_events (provider, received_ms DESC);

CREATE TRIGGER append_only_payment_events BEFORE UPDATE OR DELETE ON payment_events
    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();

DO $$
DECLARE t TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'polygm_app') THEN
        RAISE NOTICE 'role polygm_app is absent (dev/test database): skipping the append-only grants';
        RETURN;
    END IF;
    FOREACH t IN ARRAY ARRAY['payment_events'] LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM PUBLIC', t);
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM polygm_app', t);
            EXECUTE format('GRANT INSERT, SELECT ON %I TO polygm_app', t);
        END IF;
    END LOOP;
END $$;
