-- 0018_append_only_grants.sql — the trigger Postgres never got, and the grant half of the append-only promise
-- for every table that had only one of the two.
--
-- Two halves, in different places on purpose: `polygm_reject_mutation()` stops our own code at runtime, and a
-- missing GRANT stops a role that does not own the table. A trigger can be dropped by a superuser; a REVOKE cannot
-- be bypassed by the role it was written for. 0005 does both for the ten tables that existed when it ran, and that
-- is the whole story it can tell — `REVOKE ... ON <a table that does not exist yet>` is an error, so every
-- append-only table added after 0005 had to write its own grant and none of them did.
--
-- P11's D6 sweep found this by reading 0005 while adding 0017 and asking which tables the list no longer covers:
-- **seventeen** were append-only by trigger and writable by the application role — `auth_events`, `wash_findings`,
-- the copy engine's would-be actions, the drill records, the referral accruals among them. The same reading found
-- the mirror-image gap: `leaderboard_exclusions` has been in the portable list (`tools/build-sqlite-migrations.py`
-- `APPEND_ONLY`, which writes the SQLite triggers) since D1, and the Postgres side never got a trigger at all, so
-- an exclusion row could be edited by anyone with a connection. Both are fixed here, and the P11 gate's c27 now
-- reads the declared list and fails if a declared table has no trigger or no REVOKE.
--
-- The declared list is the authority rather than a scan of `CREATE TRIGGER` statements, because a scan cannot tell
-- a live trigger from the one 0012 left behind on `referral_events` before 0016 dropped that table. `to_regclass`
-- keeps the block safe on a database that has not applied every earlier migration.
CREATE TRIGGER append_only_leaderboard_exclusions
    BEFORE UPDATE OR DELETE ON leaderboard_exclusions
    FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();

DO $$
DECLARE t TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'polygm_app') THEN
        FOREACH t IN ARRAY ARRAY[
        'cash_ledger', 'fills', 'tape_trades',
        'builder_attribution', 'position_snapshots', 'auth_events',
        'wash_findings', 'broadcast_gates', 'backup_restore_tests',
        'drill_records', 'audit_log', 'flag_audit',
        'alert_fires', 'kill_switch_state', 'wallet_events',
        'order_lifecycle', 'reconcile_actions', 'copy_events',
        'automation_runs', 'chain_events', 'flag_audit_p06',
        'kill_switch_drills', 'copy_dry_runs', 'trader_metric_snapshots',
        'wallet_pseudonyms', 'leaderboard_exclusions', 'referral_accruals'
        ]
        LOOP
            CONTINUE WHEN to_regclass(t) IS NULL;
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM PUBLIC', t);
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM polygm_app', t);
            EXECUTE format('GRANT INSERT, SELECT ON %I TO polygm_app', t);
        END LOOP;
    END IF;
END $$;
