-- P04 D3 · 0005 enforcement, not convention.
-- The rule this file exists for (P04 constraint 4): "Ledger-style tables (cash_ledger, fills, tape_trades,
-- builder_attribution) are append-only; UPDATE/DELETE is rejected at the DB level, not by convention."
-- Convention-based invariants in a trading system fail on the first 3am `UPDATE orders SET ...` that
-- someone runs to "just fix" one row.

CREATE OR REPLACE FUNCTION polygm_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only table: % is not updatable or deletable (see db/migrations/0005_triggers.sql)',
        TG_TABLENAME
        USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE OR REPLACE FUNCTION polygm_immutable_columns() RETURNS trigger LANGUAGE plpgsql AS $$
-- For tables that MAY be updated (a state machine) but whose identity columns must never change.
-- Moving an order between users or markets by UPDATE would be indistinguishable from a data-integrity bug.
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'id is immutable on %', TG_TABLENAME USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;

-- >>> append-only tables (the gate check reads exactly this comment form; do not reformat)
CREATE TRIGGER append_only_cash_ledger          BEFORE UPDATE OR DELETE ON cash_ledger          FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_fills                BEFORE UPDATE OR DELETE ON fills                FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_tape_trades          BEFORE UPDATE OR DELETE ON tape_trades          FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_builder_attribution  BEFORE UPDATE OR DELETE ON builder_attribution  FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_position_snapshots   BEFORE UPDATE OR DELETE ON position_snapshots   FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_audit_log            BEFORE UPDATE OR DELETE ON audit_log            FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_flag_audit           BEFORE UPDATE OR DELETE ON flag_audit           FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_alert_fires          BEFORE UPDATE OR DELETE ON alert_fires          FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_kill_switch_state    BEFORE UPDATE OR DELETE ON kill_switch_state    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
CREATE TRIGGER append_only_referral_events      BEFORE UPDATE OR DELETE ON referral_events       FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();
-- <<< append-only tables

CREATE TRIGGER immutable_id_order_intents BEFORE UPDATE ON order_intents FOR EACH ROW EXECUTE FUNCTION polygm_immutable_columns();
CREATE TRIGGER immutable_id_orders        BEFORE UPDATE ON orders        FOR EACH ROW EXECUTE FUNCTION polygm_immutable_columns();

-- The application role gets NO update/delete grant on the append-only set. A trigger can be dropped by a
-- superuser; a missing GRANT cannot be bypassed by the role that does not own the table. Both are here
-- because they catch different mistakes: the trigger catches our own code, the grant catches a migration
-- that forgot to be careful.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'cash_ledger','fills','tape_trades','builder_attribution','position_snapshots',
        'audit_log','flag_audit','alert_fires','kill_switch_state','referral_events']
    LOOP
        EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM PUBLIC', t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'polygm_app') THEN
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM polygm_app', t);
            EXECUTE format('GRANT INSERT, SELECT ON %I TO polygm_app', t);
        END IF;
    END LOOP;
END $$;

-- Sequence hygiene for the two tables whose ids are minted from our namespace rather than the venue's:
-- an id that can collide with a venue id is a reconciliation nightmare, so they are UUIDs minted in the
-- service. This trigger exists to make that visible in the schema, since a bare TEXT id column invites
-- someone to start writing ids by hand.
CREATE OR REPLACE FUNCTION polygm_reject_short_id() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF length(NEW.id) < 16 THEN
        RAISE EXCEPTION 'intent/order ids must be UUIDs from the service (got % chars)', length(NEW.id)
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER id_length_intents BEFORE INSERT ON order_intents FOR EACH ROW EXECUTE FUNCTION polygm_reject_short_id();

-- CHECK for the money invariant that no amount of code review catches reliably: a notional recomputed from
-- a FLOAT price and a FLOAT size drifts by one micro at ~0.1% of realistic orders (measured during P04
-- SDK inspection). The DB cannot recompute the arithmetic, but it CAN refuse a notional whose trailing
-- digits are impossible for an integer product of tick-aligned values.
ALTER TABLE order_intents ADD CONSTRAINT notional_tick_aligned CHECK (
    -- notional_micro = size_micro * price_micro / 1e6, floored (never rounded up: a floored notional
    -- under-spends; a rounded one can fail for insufficient balance after we already locked it).
    -- The check below therefore allows the exact product or the floor of it, and nothing else.
    notional_micro = (size_micro * price_micro) / 1000000
    OR notional_micro = (size_micro * price_micro - (size_micro * price_micro % 1000000)) / 1000000
);
