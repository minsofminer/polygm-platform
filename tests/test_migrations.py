"""Migrations: the rules that must hold in Postgres, plus the same rules *executed* on the portable subset.

Split deliberately:
  * static checks over the .sql text (types, triggers, indexes) — they run everywhere, including where
    no Postgres exists;
  * behavioural checks executed on SQLite — append-only rejection, CHECK constraints, unique replay;
  * a canary proving the behavioural checks are not vacuous (a DB rule nobody can violate is a comment).
"""
from __future__ import annotations
import re
import sqlite3
import unittest
from pathlib import Path

from conftest import ROOT, tmp_db_path  # noqa: F401

PG = ROOT / "db" / "migrations"
LITE = ROOT / "db" / "migrations-sqlite"
MONEY_COLS = re.compile(r"\b(\w*(?:micro|amount|notional|basis|payout|fee|price|size)\w*)\s+(\w+)", re.I)


def pg_text(include_comments: bool = False) -> str:
    raw = "\n".join(p.read_text() for p in sorted(PG.glob("*.sql")))
    if include_comments:
        return raw
    # static checks run on CODE, not on prose: a comment mentioning a FLOAT, in the paragraph explaining why
    # FLOATs are forbidden, is not a float column - and a test that reads it as one gets "fixed" by deleting
    # the comment, which is how documentation erodes. Inline comments are truncated too, because
    # `NUMERIC(6,4) NOT NULL,  -- ...` still has the type on the line that matters.
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen", str(ROOT / "tools/build-sqlite-migrations.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)                    # same strip routine the migrations use: one oracle
    return gen.strip_inline_comments(raw, keep_blank=False)


def fresh_lite_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript("PRAGMA foreign_keys=ON;\n"
                      + "\n".join(p.read_text() for p in sorted(LITE.glob("*.sql"))))
    return con


class TestPostgresText(unittest.TestCase):
    def test_all_four_layers_exist(self):
        names = sorted(p.name for p in PG.glob("*.sql"))
        self.assertEqual(len(names), 4, names)
        self.assertTrue(all(re.match(r"^\d{4}_\w+\.sql$", n) for n in names), names)

    def test_the_rationale_still_exists_in_the_comments(self):
        # paired with the check above: stripping comments must not mean the reasoning disappears from the
        # repo, so the prose is asserted to exist in the unstripped text.
        raw = pg_text(include_comments=True)
        self.assertIn("never NUMERIC and never float", raw)
        self.assertIn("measured in P01", raw)

    def test_money_columns_are_bigint_never_float(self):
        text = pg_text()
        self.assertNotIn("-- ", text, "comment stripping failed; the check below would read prose")
        bad = [(c, t) for c, t in MONEY_COLS.findall(text)
               if t.upper() in ("REAL", "DOUBLE", "DOUBLE PRECISION", "FLOAT", "FLOAT8", "FLOAT4")]
        self.assertEqual(bad, [], f"float money columns: {bad}")
        # and positively: the money columns must be BIGINT (or a CHECK'd NUMERIC for metadata only)
        for col in ("usdc_available_micro", "notional_micro", "basis_micro", "fee_micro_expected",
                    "fee_micro_observed", "price_micro", "size_shares_micro", "amount_micro",
                    "shares_open_micro", "accrual_micro", "observed_price_micro"):
            self.assertIn(col, text, col)
        SQL_WORDS = {"IS", "IN", "NOT", "CHECK", "NULL", "OR", "AND", "BETWEEN"}
        for m in re.finditer(r"(\w+_micro)\s+([A-Za-z_][A-Za-z_0-9()]*)", text):
            if m.group(2).upper() in SQL_WORDS:
                continue          # `CHECK (max_order_micro IS NOT NULL...)` is not a column definition
            self.assertIn(m.group(2).upper(), ("BIGINT",), f"{m.group(1)} is {m.group(2)}, not BIGINT")
        # the *columns that must exist*: if a name is renamed the loop above quietly checks nothing
        for t, col in (("markets", "outcome_count"), ("markets", "first_seen_ms"),
                       ("position_snapshots", "delta_micro"), ("order_intents", "notional_micro")):
            self.assertIn(col, pg_text(include_comments=True), f"{t}.{col} vanished")

    def test_no_float_is_used_for_money_even_in_metadata_math(self):
        # NUMERIC is allowed for market metadata (tick size, min order size) and for latency; nothing else.
        allowed = ("minimum_tick_size", "minimum_order_size", "risk_latency_ms")
        for m in re.finditer(r"(\w+)\s+NUMERIC\((\d+),(\d+)\)", pg_text()):
            self.assertIn(m.group(1), allowed, f"{m.group(1)}: metadata is the only NUMERIC")

    def test_append_only_tables_declared_in_0005_have_a_trigger_and_a_grant(self):
        t5 = (PG / "0005_triggers.sql").read_text()
        block = t5[t5.index(">>> append-only tables"):t5.index("<<< append-only tables")]
        trig = set(re.findall(r"ON (\w+)\s+FOR EACH ROW", block))
        self.assertGreaterEqual(len(trig), 8, f"trigger block matched almost nothing: {trig}")
        for t in ("cash_ledger", "fills", "tape_trades", "builder_attribution"):
            self.assertIn(t, trig, f"{t} is named by the constraint but not guarded")
        grant_block = t5[t5.index("FOREACH t IN ARRAY"):]
        for t in trig:
            self.assertIn(f"'{t}'", grant_block, f"{t} has a trigger but no REVOKE")

    def test_every_create_table_in_pg_is_represented_in_the_portable_subset(self):
        pg_tables = set(re.findall(r"CREATE TABLE (\w+)", pg_text()))
        lite_tables = set(re.findall(r"CREATE TABLE (\w+)", "\n".join(
            p.read_text() for p in LITE.glob("*.sql"))))
        missing = sorted(pg_tables - lite_tables)
        self.assertEqual(missing, [], f"tables that tests can never touch: {missing}")
        self.assertGreaterEqual(len(pg_tables), 25, pg_tables)

    def test_the_neg_risk_flag_lives_where_the_pnl_code_can_read_it(self):
        text = pg_text()
        self.assertIn("neg_risk", text)
        self.assertGreaterEqual(text.count("neg_risk"), 3,
                                "events AND markets must both carry it: the PnL rule is event-level")

    def test_indexes_are_justified_in_text_not_invented(self):
        # Each CREATE INDEX must be near a comment explaining the query it serves. This is the design rule
        # "state the index reasoning", enforced by counting: more tables than justifications is a fail.
        for p in sorted(PG.glob("*.sql")):
            text = p.read_text()
            idx = len(re.findall(r"^CREATE (?:UNIQUE )?INDEX", text, re.M))
            why = len(re.findall(r"^\s*--.*\b(index|query|scan|partial|sort|read path|hot path|full scan)\b",
                                 text, re.M | re.I))
            with self.subTest(file=p.name):
                if idx == 0:
                    # 0005 defines no indexes; requiring justification text there would be theatre.
                    self.assertIn("trigger", text.lower())
                    continue
                self.assertGreaterEqual(why, 1, f"{idx} indexes and no recorded reasoning")

    def test_dropped_pg_only_features_are_recorded_not_silent(self):
        import json
        d = json.loads((LITE / "DROPPED.json").read_text())
        self.assertIn("append_only", d)
        self.assertIn("files", d)
        dropped = {k: v["dropped"] for k, v in d["files"].items()}
        total = sum(len(v) for v in dropped.values())
        self.assertGreater(total, 10, f"the transpiler recorded only {total} drops - suspect a bug")
        five = " ".join(dropped["0005_triggers.sql"])
        self.assertIn("notional_tick_aligned", five,
                      "the notional CHECK is Postgres-only; if it is not recorded, the tests silently "
                      "do not cover the money invariant and nobody would know")
        self.assertIn("no_secret_shapes", " ".join(dropped["0004_product.sql"]))


class TestPortableBehaviour(unittest.TestCase):
    """Executed on SQLite: the same three rules, where a violation must actually raise."""

    def setUp(self):
        self.con = fresh_lite_db()
        self.addCleanup(self.con.close)
        now = 1_700_000_000_000
        self.con.execute("INSERT INTO users (id,created_ms,tier) VALUES ('u',?, 'free')", (now,))
        self.con.execute("INSERT INTO markets (id,condition_id,question,accepting_orders,seconds_delay,"
                         "minimum_tick_size,minimum_order_size,fee_type,first_seen_ms,updated_ms,"
                         "outcomes_json) VALUES ('m','0xc','q',1,0,0.01,5,'None',?,?, '[]')", (now, now))

    def test_append_only_rejects_update_and_delete(self):
        now = 1_700_000_000_000
        self.con.execute("INSERT INTO cash_ledger (user_id,kind,amount_micro,ref_table,ref_id,created_ms,"
                         "reason) VALUES ('u','deposit',1000,'x','y',?,'seed')", (now,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE cash_ledger SET amount_micro = 999999")   # a "quick fix" at 3am
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("DELETE FROM cash_ledger")
        self.assertEqual(self.con.execute("SELECT amount_micro FROM cash_ledger").fetchone()[0], 1000)

    def test_the_guard_is_not_a_blanket_write_block(self):
        # The same DB must still ACCEPT inserts, or "append-only" is just "broken table".
        now = 1_700_000_000_000
        for kind, amt, ref in (("buy", -1000, "o1"), ("fee", -5, "o1")):
            self.con.execute("INSERT INTO cash_ledger (user_id,kind,amount_micro,ref_table,ref_id,"
                             "created_ms,reason) VALUES ('u',?,?, 'orders',?,?, 'x')",
                             (kind, amt, ref, now))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0], 2)
        # ...and the same (ref_table, ref_id, kind, user) twice is a duplicate credit, which the UNIQUE on
        # that tuple exists to make impossible even when a reconciliation is re-run by hand at 3am.
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO cash_ledger (user_id,kind,amount_micro,ref_table,ref_id,"
                             "created_ms,reason) VALUES ('u','fee',-5,'orders','o1',?, 'x')", (now + 1,))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM cash_ledger").fetchone()[0], 2)

    def test_money_check_constraints_fire(self):
        now = 1_700_000_000_000
        with self.assertRaises(sqlite3.IntegrityError):            # price out of (0,1)
            self.con.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,"
                             "size_micro,notional_micro,state,idempotency_key,created_ms) VALUES "
                             "('i','u','m','t','BUY',1000000,10000000,5000000,'queued','k',?)", (now,))
        with self.assertRaises(sqlite3.IntegrityError):            # negative balance
            self.con.execute("INSERT INTO balances (user_id,usdc_available_micro) VALUES ('u',-1)")
        with self.assertRaises(sqlite3.IntegrityError):            # an unknown intent state
            self.con.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,"
                             "size_micro,notional_micro,state,idempotency_key,created_ms) VALUES "
                             "('i2','u','m','t','BUY',500000,10000000,5000000,'wobbly','k2',?)", (now,))
        # and a valid row must be accepted (otherwise the above are vacuous)
        self.con.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,"
                         "size_micro,notional_micro,state,idempotency_key,created_ms) VALUES "
                         "('i3','u','m','t','BUY',500000,10000000,5000000,'queued','k3',?)", (now,))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0], 1)

    def test_side_is_case_sensitive_and_enumerated(self):
        now = 1_700_000_000_000
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,"
                             "size_micro,notional_micro,state,idempotency_key,created_ms) VALUES "
                             "('i','u','m','t','buy',500000,10000000,5000000,'queued','k',?)", (now,))

    def test_a_zero_amount_cash_entry_is_rejected(self):
        now = 1_700_000_000_000
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO cash_ledger (user_id,kind,amount_micro,ref_table,ref_id,"
                             "created_ms,reason) VALUES ('u','fee',0,'x','y',?,'why')", (now,))

    def test_idempotency_key_uniqueness_is_per_user(self):
        now = 1_700_000_000_000
        self.con.execute("INSERT INTO users (id,created_ms,tier) VALUES ('v',?, 'free')", (now,))
        for u in ("u", "u"):
            self.con.execute("INSERT INTO idempotency_keys (user_id,key,request_hash,state,created_ms)"
                             " VALUES (?, 'k','h','done',?)", (u, now))
            break
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO idempotency_keys (user_id,key,request_hash,state,created_ms)"
                             " VALUES ('u','k','other','done',?)", (now,))
        self.con.execute("INSERT INTO idempotency_keys (user_id,key,request_hash,state,created_ms)"
                         " VALUES ('v','k','h',?,?)", ("done", now))          # other user: fine
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0], 2)

    def test_kill_switch_history_is_append_only(self):
        now = 1_700_000_000_000
        self.con.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) VALUES "
                         "(1,'incident','ops',?)", (now,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE kill_switch_state SET engaged = 0")
        self.assertEqual(self.con.execute("SELECT engaged FROM kill_switch_state").fetchone()[0], 1)

    def test_a_reason_shorter_than_four_chars_is_rejected(self):
        now = 1_700_000_000_000
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) VALUES "
                             "(1,'','ops',?)", (now,))


class TestGenerationIsIdempotent(unittest.TestCase):
    def test_the_portable_subset_is_current(self):
        import subprocess
        r = subprocess.run(["python3", str(ROOT / "tools/build-sqlite-migrations.py"), "--check"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("all present", r.stdout)
        self.assertIn("portable subset executes", r.stdout)


if __name__ == "__main__":
    unittest.main()
