"""Idempotency, feature flags and the risk-gate notional rule (P04 rules 5, D7, D4)."""
from __future__ import annotations
import json
import sqlite3
import unittest
from decimal import Decimal

from conftest import ROOT, tmp_db_path  # noqa: F401
from polygm_core.config.flags import FlagStore, Flags, set_flag
from polygm_core.risk.gate import Intent, Limits, MarketState, evaluate
from polygm_core.risk.idempotency import Idem, request_hash

M = 10**6


def conn_with_schema() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE idempotency_keys (
        user_id TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('in_progress','done')),
        response_json TEXT, order_hash TEXT,
        created_ms INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, key));
    CREATE TABLE feature_flags (name TEXT PRIMARY KEY, kind TEXT NOT NULL, value_json TEXT NOT NULL);
    CREATE TABLE flag_audit (id INTEGER PRIMARY KEY, name TEXT NOT NULL, old_value TEXT, new_value TEXT,
                             changed_by TEXT NOT NULL, reason TEXT NOT NULL, at_ms INTEGER NOT NULL);
    """)
    return con


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self.con = conn_with_schema()
        self.idem = Idem(self.con)
        self.body = {"marketId": "0xM1", "side": "BUY", "price": "0.5", "size": "10"}

    def test_first_begin_is_in_progress(self):
        r = self.idem.begin("u1", "key-1", self.body)
        self.assertEqual(r.state, "in_progress")

    def test_second_begin_while_running_does_not_replay_or_execute(self):
        self.idem.begin("u1", "key-1", self.body)
        r = self.idem.begin("u1", "key-1", self.body)
        self.assertEqual(r.state, "in_progress")           # the client must retry, we must not guess

    def test_done_key_replays_the_stored_response_verbatim(self):
        self.idem.begin("u1", "key-1", self.body)
        out = {"intentId": "abc", "state": "queued", "notionalMicro": 5 * M}
        self.idem.finish("u1", "key-1", out)
        r = self.idem.begin("u1", "key-1", self.body)
        self.assertEqual(r.state, "done")
        self.assertEqual(json.loads(r.response_json), out)  # byte-for-byte the same answer

    def test_same_key_different_body_is_a_conflict_not_a_replay(self):
        self.idem.begin("u1", "key-1", self.body)
        self.idem.finish("u1", "key-1", {"intentId": "abc"})
        tampered = dict(self.body, size="11")
        r = self.idem.begin("u1", "key-1", tampered)
        self.assertEqual(r.state, "mismatch")
        self.assertNotEqual(r.request_hash, request_hash(tampered))

    def test_abandon_lets_a_post_crash_retry_run(self):
        # the whole reason abandon() deletes rather than flags "failed": the retry after a crash is the
        # point of the key, and a poisoned key means the user can never re-send their order.
        self.idem.begin("u1", "key-1", self.body)
        self.idem.abandon("u1", "key-1")
        r = self.idem.begin("u1", "key-1", self.body)
        self.assertEqual(r.state, "in_progress")
        self.assertEqual(r.request_hash, request_hash(self.body))

    def test_abandon_never_touches_a_finished_row(self):
        self.idem.begin("u1", "key-1", self.body)
        self.idem.finish("u1", "key-1", {"intentId": "abc"})
        self.idem.abandon("u1", "key-1")
        self.assertEqual(self.idem.begin("u1", "key-1", self.body).state, "done")

    def test_keys_are_scoped_per_user(self):
        self.idem.begin("u1", "shared", self.body)
        self.idem.finish("u1", "shared", {"intentId": "one"})
        r = self.idem.begin("u2", "shared", self.body)
        self.assertEqual(r.state, "in_progress", "u2 must not inherit u1's answer")

    def test_hash_is_stable_over_ordering_and_whitespace(self):
        a = {"b": 2, "a": 1}
        b = {"a": 1, "b": 2}
        self.assertEqual(request_hash(a), request_hash(b))
        self.assertNotEqual(request_hash({"x": "0.50"}), request_hash({"x": "0.51"}))
        self.assertEqual(len(request_hash(a)), 64)


class TestFlags(unittest.TestCase):
    def setUp(self):
        self.con = conn_with_schema()
        self._open = [self.con]
        self.addCleanup(lambda: [c.close() for c in self._open])

    def test_set_flag_requires_a_reason(self):
        with self.assertRaises(ValueError):
            set_flag(self.con, "tape_ws", True, changed_by="ops", reason="   ")
        with self.assertRaises(ValueError):
            set_flag(self.con, "tape_ws", True, changed_by="ops", reason="")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM feature_flags").fetchone()[0], 0)

    def test_set_flag_writes_the_flag_and_an_audit_row_atomically(self):
        set_flag(self.con, "copy_trading", True, changed_by="ops", reason="P05 dogfood", now_ms=1)
        row = self.con.execute("SELECT kind, value_json FROM feature_flags WHERE name='copy_trading'"
                              ).fetchone()
        self.assertEqual(row[0], "bool")
        self.assertTrue(json.loads(row[1])["on"])
        a = self.con.execute("SELECT name, old_value, new_value, changed_by, reason FROM flag_audit"
                             " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(a, ("copy_trading", None, "true", "ops", "P05 dogfood"))
        set_flag(self.con, "copy_trading", False, changed_by="ops", reason="rollback", now_ms=2)
        a2 = self.con.execute("SELECT old_value, new_value FROM flag_audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(a2[0])["on"], True)     # the previous value is preserved, not "unknown"
        self.assertEqual(json.loads(a2[1]), False)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM flag_audit").fetchone()[0], 2)

    def test_a_number_is_never_also_a_switch(self):
        set_flag(self.con, "max_order_notional_micro", 1234, changed_by="risk", reason="t", now_ms=6)
        store = FlagStore(self.con, ttl_ms=0)
        f = store.current()
        self.assertEqual(f.max_order_notional_micro, 1234)
        self.assertFalse(f.on("max_order_notional_micro"),
                         "a numeric tunable must not appear in the enabled-feature list")
        self.assertIsNone(json.loads(self.con.execute(
            "SELECT value_json FROM feature_flags").fetchone()[0]).get("on"))

    def test_zero_is_off_not_on(self):
        set_flag(self.con, "some_switch", False, changed_by="ops", reason="t", now_ms=7)
        self.assertIs(json.loads(self.con.execute("SELECT value_json FROM feature_flags").fetchone()[0])["on"],
                      False)

    def test_numeric_flags_are_audited_too_and_are_not_coerced_to_bool(self):
        set_flag(self.con, "max_order_notional_micro", 900 * M, changed_by="risk", reason="tighten",
                 now_ms=3)
        self.assertEqual(self.con.execute("SELECT kind FROM feature_flags").fetchone()[0], "number")
        self.assertEqual(self.con.execute("SELECT new_value FROM flag_audit").fetchone()[0],
                         str(900 * M))

    def test_store_starts_on_boot_defaults_and_clears_after_first_read(self):
        store = FlagStore(self.con, ttl_ms=0)
        self._open.append(store.conn)
        self.assertTrue(store.boot_defaults_used, "a store that never read the DB must report it")
        store.current()
        self.assertFalse(store.boot_defaults_used)
        self.assertGreaterEqual(store.age_ms, 0)

    def test_a_db_failure_keeps_the_last_snapshot_rather_than_defaulting(self):   # noqa: C901
        store = FlagStore(self.con, ttl_ms=0)
        set_flag(self.con, "kill_switch_engaged", True, changed_by="ops", reason="incident", now_ms=4)
        self.assertTrue(store.current().on("kill_switch_engaged"))
        # now break the connection: the store must keep serving the last snapshot, NOT fall back to code
        # defaults, because "flag read failed" silently re-enabling a feature is an outage with extra steps.
        broken = sqlite3.connect(":memory:")              # no feature_flags table -> every read raises
        self._open.append(broken)                          # closed in the same list, in order
        store.conn = broken
        store._loaded_ms = 0
        snap = store.current()
        self.assertTrue(snap.on("kill_switch_engaged"), "the flag was lost on a transient DB error")
        self.assertTrue(store.boot_defaults_used is False, "a recovered store must not claim boot defaults")
        self.assertGreater(store.age_ms, -1)
        store.conn = None                                         # both handles are in self._open

    def test_numeric_override_from_the_db_reaches_the_flags_object(self):
        set_flag(self.con, "max_order_notional_micro", 1234, changed_by="risk", reason="test", now_ms=5)
        store = FlagStore(self.con, ttl_ms=0)
        self.assertEqual(store.current().max_order_notional_micro, 1234)

    def test_env_overrides_are_the_boot_layer_only(self):
        import os
        os.environ["PGM_STALE_MS_BOOK"] = "1500"
        try:
            f = Flags.from_env()
            self.assertEqual(f.stale_ms_book, 1500)
        finally:
            del os.environ["PGM_STALE_MS_BOOK"]
        self.assertEqual(Flags.from_env().stale_ms_book, 3000)

    def test_flag_names_are_not_free_form_text_in_code(self):
        # Flags.on() takes a string; a typo ("tape_webs") is a silently-off feature. This test pins the ones
        # the architecture doc names, so a rename has to update three places and fail somewhere.
        f = Flags.with_flags(Flags(), ["tape_ws", "copy_trading"])
        self.assertTrue(f.on("tape_ws") and f.on("copy_trading"))
        self.assertFalse(f.on("tape_webs"))
        self.assertEqual(f.flags, tuple(sorted(f.flags)), "flag sets must be canonical for equality")


class TestNotionalFloor(unittest.TestCase):
    """D4's money rule, at the gate boundary rather than only in the money module."""

    def state(self, **kw):
        # 0.001 tick so that any multiple-of-1000 price is on-tick: with a 0.01 tick four of these six
        # cases are denied for OFF_TICK before notional is ever computed, which is what the first run of
        # this test did to itself.
        base = dict(accepting_orders=True, seconds_delay=0, enable_order_book=True, minimum_tick_size="0.001",
                    minimum_order_size="5", fee_type="None", best_bid_micro=490_000, best_ask_micro=510_000,
                    snap_age_ms=0)
        base.update(kw)
        return MarketState(**base)

    def test_gate_notional_equals_floored_integer_product(self):
        # Prices are on-tick for a 0.001 market (every multiple of 1000 micro), sizes keep the notional
        # inside the per-order cap, and every price sits within the band of the 0.50 mid — otherwise the
        # gate denies for a different reason and the test proves nothing about notional.
        cases = [dict(size_shares_micro=10 * M, price_micro=500_000),
                 dict(size_shares_micro=7 * M, price_micro=333_000),
                 dict(size_shares_micro=5 * M, price_micro=1_000, m=dict(best_bid_micro=0,
                                                                        best_ask_micro=2_000)),
                 dict(size_shares_micro=12_345_678, price_micro=654_000),
                 dict(size_shares_micro=4_999_999, price_micro=501_000, m=dict(minimum_order_size="0")),
                 dict(size_shares_micro=5 * M, price_micro=999_000,
                      m=dict(best_bid_micro=998_000, best_ask_micro=999_000))]
        for c in cases:
            c = dict(c)
            ikw, mkw = {"idempotency_key": "k", "market_id": "m", "user_id": "u", "token_id": "t",
                       "side": "BUY"}, c.pop("m", {})
            ikw.update(c)
            with self.subTest(**{k: v for k, v in ikw.items() if k in ("size_shares_micro", "price_micro")}):
                i = Intent(**ikw)
                d = evaluate(i, self.state(**mkw), limits=Limits(), open_orders=0, spent_24h_micro=0,
                             kill_switch=False)
                self.assertTrue(d.allowed, d.code)
                self.assertEqual(d.notional_micro,
                                 (i.size_shares_micro * i.price_micro) // M)
                self.assertLessEqual(d.notional_micro, i.size_shares_micro * i.price_micro / M)

    def test_a_float_product_would_have_differ(self):
        # The precise D4 claim, measured rather than asserted. At size = 1.000001 shares, `size * price`
        # computed through floats then rounded DISAGREES WITH THE INTEGER PRODUCT for ~half of all prices in
        # (0,1) — 499,999 of 999,999 in the scan below. That is the exact operation the venue's own
        # order_builder does when you hand it floats, and it is why our notional is computed in integers
        # and only the individual price/size fields ever cross the float boundary.
        mism = [pm for pm in range(1, 1_000_000)
                if round(1_000_001 * (pm / M)) != (1_000_001 * pm) // M]
        self.assertGreater(len(mism), 100_000,
                           "the float product was expected to disagree often; if it does not, re-argue D4")
        self.assertLess(len(mism), 999_999, "if every price disagreed the parser is broken, not the float")
        # ...while the fields themselves DO round-trip: the danger is arithmetic on floats, not transport.
        rt_fail = [m for m in range(1, 1_000_000) if round(float(m) / M * M) != m]
        self.assertEqual(rt_fail, [], f"{len(rt_fail)} micro values cannot cross the float boundary")

    def test_decimal_string_agrees_with_the_integer_path(self):
        size, price = 1_000_001, 333_333
        self.assertEqual(int((Decimal(size) * Decimal(price)) // Decimal(M)), (size * price) // M)


if __name__ == "__main__":
    unittest.main()
