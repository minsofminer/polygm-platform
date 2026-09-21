-- P13 D2 — an intent's directive is part of the intent.
--
-- `order_directives` was invisible for eleven phases: the table existed, the executor read it with `COALESCE`
-- defaults, and the API was the one producer that never wrote a row. The all-in spending cap changed that — the
-- directive is now where the executor reads the cap from, so every intent the API queues has one — and with the
-- row present, a plain `REFERENCES order_intents(id)` became reachable: anything that removes intents (a user
-- teardown, a retention job, a test's own cleanup) now hits an orphan and fails the foreign key.
--
-- Cascade is the honest model rather than a wider workaround. A directive has no meaning without its intent:
-- it is the intent's instructions, not a record of them. It is also NOT the ledger — the trail a user reads
-- lives in `order_lifecycle` and `cash_ledger`, both of which are append-only and refuse deletion, so nothing
-- auditable is lost when a queued intent that was never signed is removed.
--
-- Recorded as a Postgres-only invariant: the SQLite transpiler drops `ALTER TABLE` statements, so dev/CI keeps a
-- non-cascading foreign key and the harness deletes directives explicitly (see db/migrations-sqlite/DROPPED.json,
-- where that drop is listed rather than hidden). Production is the engine with the cascade.
ALTER TABLE order_directives
    DROP CONSTRAINT IF EXISTS order_directives_intent_id_fkey;
ALTER TABLE order_directives
    ADD CONSTRAINT order_directives_intent_id_fkey
    FOREIGN KEY (intent_id) REFERENCES order_intents(id) ON DELETE CASCADE;

-- No index is created or justified here, which the migration linter asks about explicitly: the column this
-- constraint sits on (`order_directives.intent_id`) is the table's PRIMARY KEY, so the cascade lookup rides an
-- index that already exists, and there is no trigger to add — the append-only triggers this schema relies on
-- are about INSERT-only evidence tables, and `order_directives` is deliberately not one of them (a rule's
-- cap can be revised before it fires, and the audit trail for that lives in `order_lifecycle`).
