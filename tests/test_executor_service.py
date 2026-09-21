"""P06 D2/D7/D8 integration, end to end against the mock venue and a real SQLite database.

These are not unit tests of `handle_intent`'s branches. The phase's claim is that an intent that enters the
queue comes out as exactly one venue order, exactly one set of ledger rows, a lifecycle trail a user can
read, and a fee estimate that can be compared with the actual — so the test runs the whole path and asserts
on the database, because a mock of the store would assert that the code calls itself.

The venue is `ScenarioTransport`: the same adapter shape the HTTP path uses (P04's rule: the mock and the
real client must be the same shape), and every scenario the mock can produce is exercised somewhere in this
suite or in `tools/p06-chaos-test.py`.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

from conftest import apply_schema, tmp_db_path                        # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "packages"), os.path.join(ROOT, "services", "executor"),
          os.path.join(ROOT, "services", "executor-mock"), os.path.join(ROOT, "services", "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mock_clob                                                        # noqa: E402
from store import Store, now_ms                                         # noqa: E402
from polygm_core.ledger.ledger import IntentState                       # noqa: E402
from polygm_core.money.cents import SCALE, notional_floor               # noqa: E402
from polygm_core.reconcile.reconciler import CASE_ORDER, Cfg, Reconciler  # noqa: E402
from polygm_core.venue import clob_v2 as v2                             # noqa: E402
from polygm_core.wallets import lifecycle as wl                          # noqa: E402

# Loaded under a private module name on purpose. `import main` would register the EXECUTOR as `sys.modules
# ["main"]`, and `tests/test_ingest_main.py` imports the INGEST daemon by that same bare name — so the whole
# suite would start driving an ingest daemon against an executor module and every one of its 40 tests would
# error out with `module 'main' has no attribute 'Daemon'`. Shadowing a sibling service's module name is a
# packaging bug in the test, not in the product, and it is only visible when the suite runs in one process.
import importlib.util as _ilu
_SPEC = _ilu.spec_from_file_location("polygm_p06_executor_main",
                                     os.path.join(ROOT, "services", "executor", "main.py"))
executor_main = _ilu.module_from_spec(_SPEC)
# Registered under its private name BEFORE execution: `@dataclass` resolves string annotations through
# `sys.modules[cls.__module__]`, and a module that is not in `sys.modules` yet makes dataclass processing
# raise AttributeError on None. (Also why `sys.modules["main"]` is deliberately NOT written here.)
sys.modules[_SPEC.name] = executor_main
_SPEC.loader.exec_module(executor_main)


class Harness(unittest.TestCase):
    """One queue, one wallet, one funded user, and a venue that can be told to misbehave."""

    def setUp(self) -> None:
        self.at = now_ms()
        self.db = tmp_db_path(self.id())
        apply_schema(self.db)
        import seed
        seed.seed_sqlite(self.db)
        self.store = Store.open(self.db)
        self.mock = mock_clob.MockClob()
        self.tp = mock_clob.ScenarioTransport(self.mock)
        self.policy = wl.Policy(allowed_spender="0xexchange")
        self.ex = executor_main.Executor(self.store, transport=self.tp, policy=self.policy, batch_size=5)
        row = self.store.conn.execute("SELECT id FROM markets WHERE accepting_orders=1 "
                                      "AND enable_order_book=1 ORDER BY id LIMIT 1").fetchone()
        self.market = row[0]
        tok = self.store.conn.execute("SELECT token_id FROM tokens WHERE market_id=? ORDER BY token_id "
                                      "LIMIT 1", (self.market,)).fetchone()
        self.token = tok[0]
        self.user = "u-demo"
        # A balance that is NOT exactly the order cost: the fee-inclusive check must be able to fail, and it
        # cannot fail against a wallet seeded with an arbitrary large number in every test.
        self.fund(balance=100_000_000, allowance=v2.UNLIMITED_ALLOWANCE)

    def tearDown(self) -> None:
        self.store.close()

    # --------------------------------------------------------------------- fixtures that write real rows
    def fund(self, *, balance: int, allowance: int, spender: str = "0xexchange", wallet: bool = True) -> None:
        self.store.conn.execute("INSERT OR REPLACE INTO balances (user_id,usdc_available_micro,"
                                "usdc_locked_micro,version,reconcile_ms) VALUES (?,?,?,?,?)",
                                (self.user, balance, 0, 1, self.at))
        if allowance:
            self.store.set_allowance(user_id=self.user, token="pUSD", spender=spender, amount_micro=allowance,
                                     at=self.at)
        if wallet:
            self.store.conn.execute(
                "INSERT OR REPLACE INTO wallets (user_id,provider,custody,address,proxy_address,"
                "signature_type,policy_hash,state,created_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.user, "turnkey", "delegated", "0xwallet", "0xproxy", 3, self.policy.policy_hash(),
                 "trading", self.at, self.at))

    def queue(self, *, price_micro: int = 550_000, size_micro: int = 100 * 10**6, side: str = "BUY",
              order_type: str = "GTC", audience: str = "user", builder_bps: int = 100,
              fee_rate_bps: int = 0, state: str = "queued", market: str | None = None,
              key: str | None = None, max_slippage_bps: int = 0, expiration_ts: int = 0) -> str:
        iid = "i-" + (key or os.urandom(6).hex())
        notional = notional_floor(size_micro, price_micro)
        self.store.conn.execute(
            "INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,size_micro,"
            "notional_micro,state,idempotency_key,created_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, self.user, market or self.market, self.token, side, price_micro, size_micro, notional,
             state, key or iid, self.at, self.at))
        self.store.conn.execute(
            "INSERT INTO order_directives (intent_id,order_type,expiration_ts,builder_bps,fee_rate_bps,"
            "audience,max_slippage_bps,all_in_limit_micro,created_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (iid, order_type, expiration_ts, builder_bps, fee_rate_bps, audience, max_slippage_bps,
             notional * 2, self.at))
        return iid

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.store.conn.execute(sql, params)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def trail(self, iid: str) -> list[str]:
        return [r["state"] for r in self.rows("SELECT state FROM order_lifecycle WHERE intent_id=? "
                                              "ORDER BY id", (iid,))]

    def orders(self) -> list[dict]:
        return self.rows("SELECT * FROM orders")

    def fills(self) -> list[dict]:
        return self.rows("SELECT * FROM fills")

    def cash(self) -> list[dict]:
        return self.rows("SELECT kind,amount_micro,ref_table,ref_id,reason FROM cash_ledger ORDER BY id")


class TestHappyPath(Harness):
    def test_intent_becomes_exactly_one_order_with_a_trail(self) -> None:
        iid = self.queue()
        rep = self.ex.tick(at=self.at, reconcile=False)
        handled = rep["handled"][0]
        self.assertEqual(handled["state"], "submitted", handled)
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(self.orders()[0]["state"], "live")
        self.assertEqual(self.orders()[0]["acknowledged"], 1)
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                         "submitted")
        # The trail is the user-visible story; "preflight" and "submitted" must both be in it, and it must
        # not claim anything the venue did not say (no `filled`).
        self.assertEqual(self.trail(iid), ["draft", "preflight", "signing", "submitted", "live"])
        self.assertEqual(self.tp.post_calls, 1)

    def test_signed_exactly_once_even_when_ticked_twice(self) -> None:
        self.queue()
        self.ex.tick(at=self.at, reconcile=False)
        again = self.ex.tick(at=self.at + 1000, reconcile=False)
        self.assertEqual(again["handled"], [], "a submitted order was picked up again")
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(self.tp.post_calls, 1, "the venue was asked to accept the same order twice")

    def test_fee_estimate_is_persisted_and_comparable(self) -> None:
        iid = self.queue(builder_bps=100)
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        fee = self.rows("SELECT * FROM venue_fees WHERE order_id=?", (oid,))[0]
        # 100 shares at 0.55: notional $55, builder 100bps = $0.55, platform fee 0 (this market's rate)
        self.assertEqual(fee["est_platform_micro"], 0)
        self.assertEqual(fee["est_builder_micro"], 550_000, fee)
        self.assertEqual(fee["builder_bps"], 100)
        r = self.store.record_fee_actual(order_id=oid, platform_micro=0, builder_micro=600_000, at=self.at)
        self.assertEqual(r["delta_micro"], 50_000)
        self.assertEqual(r["accuracy_bps"], 909, r)       # 0.05 / 0.55 under-estimate, in basis points
        self.assertEqual(self.store.fee_accuracy_summary(at=self.at)["under_estimates"], 1)
        self.assertFalse(iid == "")

    def test_attribution_row_written_for_every_order(self) -> None:
        self.queue(builder_bps=100)
        self.ex.tick(at=self.at, reconcile=False)
        a = self.rows("SELECT * FROM builder_attribution")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["fee_bps_expected"], 100)
        self.assertEqual(a[0]["fee_micro_expected"], 550_000)

    def test_notification_queued_then_delivered(self) -> None:
        iid = self.queue()
        self.ex.tick(at=self.at, reconcile=False)
        n = self.rows("SELECT * FROM order_notifications WHERE intent_id=?", (iid,))
        self.assertEqual([r["event"] for r in n], ["submitted"])
        self.assertEqual(n[0]["status"], "sent", "delivered by the same tick, not claimed optimistically")


class TestPreflightRefusals(Harness):
    def test_user_may_not_send_a_market_order(self) -> None:
        iid = self.queue(order_type="MARKET", audience="user")
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "ORDER_TYPE_FORBIDDEN", rep["handled"][0])
        self.assertEqual(self.orders(), [], "an order the venue never accepted was created")
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                         "rejected")
        # Rejected before signing: no attempt row means we can prove we never sent it.
        self.assertEqual(self.rows("SELECT * FROM order_attempts"), [])

    def test_allowance_shortfall_is_a_park_not_a_rejection(self) -> None:
        self.fund(balance=100_000_000, allowance=1_000)          # approval of one cent
        iid = self.queue()
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "ALLOWANCE_REQUIRED")
        self.assertEqual(self.tp.post_calls, 0, "we handed an unapproved order to the venue")
        reason = " ".join(str(x) for x in self.rows("SELECT reason FROM order_lifecycle WHERE intent_id=?",
                                                    (iid,)))
        self.assertIn("allowance", reason.lower(), reason)

    def test_balance_must_cover_notional_plus_fees(self) -> None:
        # $55 notional + $0.55 builder fee against a $55.20 balance: the order fits on notional alone and
        # fails on the all-in number, which is the only version that is true at the venue.
        # $50 notional + $0.50 builder fee = $50.50 against a $50.20 balance: the order fits on notional
        # alone and fails on the all-in number, which is the only version that is true at the venue.
        self.fund(balance=50_200_000, allowance=v2.UNLIMITED_ALLOWANCE)
        iid = self.queue(price_micro=500_000, size_micro=100 * 10**6, builder_bps=100)
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "INSUFFICIENT_BALANCE", rep["handled"][0])

    def test_stale_or_missing_book_refuses(self) -> None:
        self.queue()
        self.store.conn.execute("DELETE FROM book_levels")
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "STALE_QUOTE", rep["handled"][0])
        self.assertEqual(self.tp.post_calls, 0)

    def test_policy_drift_suspends_the_wallet_instead_of_signing(self) -> None:
        self.queue()
        moved = wl.Policy(allowed_spender="0xexchange", max_daily_outflow_micro=1_000)
        self.ex.policy = moved
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "POLICY_DRIFT", rep["handled"][0])
        self.assertEqual(self.store.wallet(self.user)["state"], "suspended")
        self.assertIn("policy_drift", [r["event"] for r in self.rows("SELECT event FROM wallet_events")])
        self.assertEqual(self.tp.post_calls, 0, "signed under a policy that no longer matches")

    def test_policy_gap_refuses_trading(self) -> None:
        self.queue()
        self.store.conn.execute("UPDATE wallets SET policy_gap='threshold approval unavailable' WHERE user_id=?",
                                (self.user,))
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "POLICY_GAP")

    def test_blocklisted_market_refused(self) -> None:
        self.queue()
        self.store.add_blocklist(market_id=self.market, reason="UMA dispute opened on the resolution",
                                source="uma_dispute", actor="ops@openout", at=self.at)
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "MARKET_BLOCKLISTED", rep["handled"][0])
        # An expiry in the past is history, not a block.
        self.store.add_blocklist(market_id=self.market, reason="temporary", source="manual",
                                 actor="ops@openout", at=self.at - 10_000, expires_ms=self.at - 1_000)
        self.assertIsNone(self.store.blocklisted(self.market, at=self.at))

    def test_daily_loss_halt_blocks_until_acknowledged(self) -> None:
        self.store.trip_loss_halt(user_id=self.user, threshold_micro=1_000_000_000,
                                  realized_micro=-1_500_000_000, at=self.at)
        iid = self.queue()
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(rep["handled"][0]["code"], "DAILY_LOSS_HALT")
        self.assertEqual(self.store.acknowledge_loss_halt(user_id=self.user, actor="u-demo",
                                                          at=self.at + 1), 1)
        # The halt is a state, so releasing it does not resurrect the rejected intent: the user has to ask
        # again. Re-queuing here is that second request, and it must now be accepted.
        self.store.conn.execute("UPDATE order_intents SET state='queued', risk_code=NULL, updated_ms=? "
                                "WHERE id=?", (self.at + 2, iid))
        rep2 = self.ex.tick(at=self.at + 2, reconcile=False)
        self.assertEqual(rep2["handled"][0]["code"], "OK", rep2["handled"][0])
        self.assertEqual(len(self.orders()), 1)
        with self.assertRaises(ValueError):
            self.store.acknowledge_loss_halt(user_id=self.user, actor="  ", at=self.at + 3)


class TestKillSwitch(Harness):
    def test_engaged_switch_claims_nothing_and_halts_the_queue(self) -> None:
        iid = self.queue()
        self.store.conn.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) "
                               "VALUES (1,'incident: venue price feed disagreement','ops',?)", (self.at,))
        self.store._kill_cache = (0, False)
        rep = self.ex.tick(at=self.at + 10, reconcile=False)
        self.assertTrue(rep["halted"])
        self.assertEqual(self.tp.post_calls, 0)
        state = self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"]
        self.assertEqual(state, "rejected", "the queued intent must not stay queued behind a halt")
        self.assertEqual(self.orders(), [])

    def test_release_is_observed_within_the_poll_cache(self) -> None:
        self.store.conn.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) "
                               "VALUES (1,'incident','ops',?)", (self.at,))
        self.assertTrue(self.store.kill_switch_engaged(at_ms=self.at, force=True))
        self.store.conn.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) "
                               "VALUES (0,'released after incident','ops',?)", (self.at + 100,))
        self.assertFalse(self.store.kill_switch_engaged(at_ms=self.at + 200, force=True))

    def test_kill_switch_read_failure_fails_closed(self) -> None:
        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError("database is gone")

        real = self.store.conn
        self.store.conn = Broken()                                        # type: ignore[assignment]
        try:
            self.assertTrue(self.store.kill_switch_engaged(at_ms=0, force=True),
                            "a kill switch whose error path is 'carry on trading' is not a control")
        finally:
            self.store.conn = real                                        # restore, or tearDown closes a fake


class TestCrashBetweenSignAndPost(Harness):
    """The phase's headline failure mode, in-process version. `tools/p06-chaos-test.py` performs the same
    sequence with a real SIGKILL of a real child process; this test is the fast version that runs in CI."""

    def test_signed_then_lost_recovers_without_a_second_post(self) -> None:
        iid = self.queue()
        self.mock.set_scenario("timeout_after_accept")
        rep = self.ex.tick(at=self.at, reconcile=False)
        h = rep["handled"][0]
        self.assertEqual(h["state"], "uncertain", h)
        self.assertEqual(h["code"], "UNCERTAIN_INTENT")
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                         "uncertain")
        # The trail must say "working" — an uncertain order rendered as "failed" is the inconsistency that
        # makes a user place the order twice.
        st = self.store.user_visible_state(iid, at=self.at)
        self.assertTrue(st["working"], st)
        self.assertEqual(st["state"], "unknown")
        self.assertEqual(self.orders(), [])
        posts = self.tp.post_calls
        # Venue had it all along: reconciliation finds it and adopts it. No second POST, ever.
        p = self.ex.reconciler.run_pass(at=self.at + 60_000)
        self.assertEqual(self.tp.post_calls, posts)
        self.assertEqual(self.orders()[0]["intent_id"], iid)
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                         "submitted")
        self.assertIn("unknown", self.trail(iid))
        self.assertIn("reconciled", self.trail(iid))
        self.assertEqual(p.per_case.get("no_ack", {}).get("closed", 0), 1, p.per_case)
        self.assertEqual(self.store.unreconciled(), [])
        self.assertFalse(self.ex.reconciler.metric(at=self.at + 61_000)["alarm"])

    def test_absent_order_is_declared_only_after_repeated_sightings(self) -> None:
        iid = self.queue()
        # Nothing at the venue, and no answer from us: the intent is uncertain but the order does not exist.
        self.store.set_intent_state(iid, IntentState.UNCERTAIN.value, at=self.at - 10 ** 6,
                                    client_order_hash="0x" + "0" * 64)
        for i in range(Cfg().max_sightings + 1):
            p = self.ex.reconciler.run_pass(at=self.at + i * 1000)
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                         "cancelled")
        self.assertEqual(self.orders(), [], "we invented an order the venue never had")
        self.assertEqual(self.tp.post_calls, 0)

    def test_unmappable_venue_status_keeps_the_intent_open(self) -> None:
        iid = self.queue()
        self.mock.set_scenario("timeout_after_accept")
        self.ex.tick(at=self.at, reconcile=False)
        # The venue now answers with a status we have never seen. Mapping it to "live" would keep the order
        # open forever; mapping it to "does not exist" would let a re-post happen. So: stay uncertain.
        oid = list(self.mock.orders)[0]
        self.mock.orders[oid]["status"] = "frobnicated"
        p = self.ex.reconciler.run_pass(at=self.at + 60_000)
        self.assertTrue(p.errors or self.store.unreconciled(), (p.errors, p.per_case))
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                         "uncertain")


class TestFillsAndLedger(Harness):
    def _placed(self) -> str:
        iid = self.queue(size_micro=100 * 10**6, price_micro=500_000, builder_bps=100)
        self.ex.tick(at=self.at, reconcile=False)
        return iid

    def test_fill_books_one_lot_one_cash_row_and_updates_matched(self) -> None:
        iid = self._placed()
        oid = self.orders()[0]["id"]
        # A venue trade that carries a fee of its own. The executor's *estimate* is a quote; this is the
        # charge, and cost basis must include it or a full exit reports a profit that was never made.
        self.mock.fill(oid, price=0.5, size=40.0)
        self.mock.trades[-1]["fee_micro"] = 40_000
        p = self.ex.reconciler.run_pass(at=self.at + 10_000)
        self.assertEqual(len(self.fills()), 1, p.per_case)
        f = self.fills()[0]
        self.assertEqual(f["notional_micro"], notional_floor(40 * 10**6, 500_000))
        self.assertEqual(self.orders()[0]["state"], "partial")
        self.assertEqual(self.orders()[0]["size_matched_micro"], 40 * 10**6)
        lots = self.rows("SELECT * FROM position_lots WHERE user_id=?", (self.user,))
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["shares_open_micro"], 40 * 10**6)
        # Cost basis includes the fee: a position that ignores its own entry cost understates the loss on a
        # full exit, and the sum of the ledger would then not close.
        self.assertEqual(lots[0]["basis_micro"], 20_000_000 + 40_000)
        cash = self.cash()
        self.assertEqual([c["kind"] for c in cash], ["buy"])
        self.assertEqual(cash[0]["amount_micro"], -(20_000_000 + 40_000))

    def test_a_booked_fill_records_the_order_event_that_becomes_the_message(self) -> None:
        """D4's data half: money in the ledger AND the event the notification is rendered from.

        `book_fill` is the only door money comes through, so it is the only place this row can honestly be born.
        The test asserts the *event name* as well as the row: `partial_fill` vs `filled` is the difference between
        "some of your order went through" and "your order is done", and getting it from the order's own size is the
        whole of that decision.
        """
        iid = self._placed()
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.5, size=40.0)
        self.mock.trades[-1]["fee_micro"] = 40_000
        self.ex.reconciler.run_pass(at=self.at + 10_000)
        # Only the rows that carry a fill's numbers: the executor writes `submitted` for the same intent, and the
        # reconciler writes its own thin `partial_fill` for the in-app trail. This asserts the one the message is
        # rendered from, which is `book_fill`'s — the money door's.
        events = [e for e in self.rows("SELECT * FROM order_notifications ORDER BY id")
                  if "tokenId" in (e["detail_json"] or "")]
        self.assertEqual([e["event"] for e in events], ["partial_fill"])
        detail = json.loads(events[0]["detail_json"])
        self.assertEqual(detail["side"], "BUY")
        self.assertEqual(detail["sizeMicro"], str(40 * 10**6))
        self.assertEqual(detail["priceMicro"], "500000")
        self.assertEqual(detail["feeMicro"], "40000")
        self.assertEqual(detail["tokenId"], self.token)
        self.assertEqual(detail["marketId"], self.market)
        self.assertEqual(events[0]["intent_id"], iid)
        # …and a fill that takes the order to its full size reads `filled`, once.
        self.mock.fill(oid, price=0.5, size=60.0)
        self.ex.reconciler.run_pass(at=self.at + 20_000)
        events = [e for e in self.rows("SELECT event, detail_json FROM order_notifications ORDER BY id")
                  if "tokenId" in (e["detail_json"] or "")]
        self.assertEqual([e["event"] for e in events], ["partial_fill", "filled"])
        self.assertEqual(json.loads(events[1]["detail_json"])["matchedMicro"], str(100 * 10**6))

    def test_replaying_the_same_venue_fill_records_no_second_notification(self) -> None:
        """The dedupe that protects the ledger has to protect the message too: nobody gets a fill twice."""
        self._placed()
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.5, size=40.0)
        self.ex.reconciler.run_pass(at=self.at + 10_000)
        rich = ("SELECT * FROM order_notifications WHERE detail_json LIKE '%tokenId%'")
        self.assertEqual(len(self.rows(rich)), 1)
        for i in range(3):
            self.ex.reconciler.run_pass(at=self.at + 20_000 + i)
        self.assertEqual(len(self.rows(rich)), 1, "a replayed venue trade must not queue a second fill message")

    def test_replaying_the_same_venue_fill_changes_nothing(self) -> None:
        self._placed()
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.5, size=40.0)
        self.ex.reconciler.run_pass(at=self.at + 10_000)
        before = (self.fills(), self.cash(), self.rows("SELECT * FROM position_lots"))
        for i in range(3):
            self.ex.reconciler.run_pass(at=self.at + 20_000 + i)
        after = (self.fills(), self.cash(), self.rows("SELECT * FROM position_lots"))
        self.assertEqual([len(x) for x in before], [len(x) for x in after])
        self.assertEqual(before[1], after[1], "the ledger changed on a replay")

    def test_lagging_fill_case_closes_when_booked(self) -> None:
        self._placed()
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.5, size=40.0)
        p = self.ex.reconciler.run_pass(at=self.at + 10_000)
        self.assertEqual(p.per_case.get("lagging_fill", {}).get("closed", 0), 1, p.per_case)
        self.assertEqual(self.store.unreconciled(), [])

    def test_unbookable_fill_keeps_a_case_open_and_the_ledger_untouched(self) -> None:
        self._placed()
        oid = self.orders()[0]["id"]
        # The venue must CLAIM a fill for the lagging case to look at; claiming one in `trades` alone is a
        # story the order row does not corroborate, and that is not the case under test.
        self.mock.orders[oid]["size_matched"] = 10_000_000
        # A trade the venue reports with a price that is not on the micro grid: on the money path we refuse
        # it (P04's rule) and open a case rather than rounding a stranger's number into a user's balance.
        self.mock.trades.append({"tradeID": "0xweird", "orderID": oid, "price": 0.4999999991234,
                                "size": 10.0, "side": "BUY", "maker": True,
                                "timestamp": self.at // 1000, "status": "matched"})
        p = self.ex.reconciler.run_pass(at=self.at + 10_000)
        self.assertEqual(self.fills(), [], "an off-grid price was rounded into the ledger")
        self.assertTrue(any(r["case_name"] == "ambiguous_settlement" for r in self.store.unreconciled()),
                        self.store.unreconciled())

    def test_sell_without_a_lot_records_a_snapshot_instead_of_forcing_zero(self) -> None:
        """We hold 40 shares; the venue says 100 were sold. Forcing the lot to zero would make the books
        agree by inventing 60 shares. The position closes at what we have, the shortfall becomes a
        `position_snapshots` delta row, and the discrepancy is a queue item — not a rounding decision."""
        self._placed()
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.5, size=40.0)
        self.ex.reconciler.run_pass(at=self.at + 10_000)
        self.assertEqual(self.rows("SELECT shares_open_micro FROM position_lots WHERE user_id=?",
                                   (self.user,))[0]["shares_open_micro"], 40 * 10**6)
        # Now a SELL order for 100 shares that the venue fills in full.
        sell = self.queue(size_micro=100 * 10**6, side="SELL", key="sell-short")
        # Refresh the book: the gate refuses to sign against a quote older than `max_snap_age_ms`, and this
        # test has advanced its own clock 20s. A test that "fixes" that by faking freshness data is a test
        # that would hide a real stale-quote bug, so it moves the book forward the way ingest would.
        self.store.conn.execute("UPDATE book_levels SET updated_ms=?", (self.at + 20_000,))
        rep = self.ex.tick(at=self.at + 20_000, reconcile=False)
        self.assertEqual([h["state"] for h in rep["handled"]], ["submitted"], rep["handled"])
        sold = self.rows("SELECT id FROM orders WHERE side='SELL'")[0]["id"]
        self.mock.fill(sold, price=0.6, size=100.0)
        self.ex.reconciler.run_pass(at=self.at + 30_000)
        short = self.store.positions_short()
        self.assertEqual(len(short), 1, short)
        self.assertEqual(short[0]["delta_micro"], 60 * 10**6, short[0])
        # And the position is closed, not negative: 40 shares of basis released by the 40 we held.
        self.assertEqual(self.rows("SELECT shares_open_micro FROM position_lots WHERE user_id=?",
                                   (self.user,))[0]["shares_open_micro"], 0)
        self.assertEqual(sell, self.rows("SELECT intent_id FROM orders WHERE side='SELL'")[0]["intent_id"])


class TestCancellation(Harness):
    def _one(self) -> str:
        iid = self.queue()
        self.ex.tick(at=self.at, reconcile=False)
        return self.orders()[0]["id"]

    def test_single_cancel_is_audited_and_updates_state(self) -> None:
        oid = self._one()
        r = self.ex.cancel(v2.CancelRequest(scope="order", reason="user cancelled from the book",
                                           order_ids=(oid,)), at=self.at + 1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.orders()[0]["state"], "cancelled")
        self.assertIn("cancelled", self.trail(self.orders()[0]["intent_id"]))

    def test_cancel_all_requires_the_phrase_and_a_reason(self) -> None:
        self._one()
        with self.assertRaises(ValueError):
            self.ex.cancel(v2.CancelRequest(scope="all", reason="oops"))
        with self.assertRaises(ValueError):
            self.ex.cancel(v2.CancelRequest(scope="all", reason="a reason that is long enough to act upon",
                                            confirm_phrase="cancel all orders"))   # case matters
        r = self.ex.cancel(v2.CancelRequest(scope="all", reason="incident: venue mispricing the tape",
                                           actor="ops@openout", confirm_phrase=v2.CONFIRM_PHRASE),
                           at=self.at + 2)
        self.assertTrue(r["ok"])
        self.assertEqual(r["audit"]["actor"], "ops@openout")
        self.assertIn("scope", r["audit"])

    def test_cancel_all_is_throttled_by_the_venue_budget(self) -> None:
        self._one()
        req = lambda: v2.CancelRequest(scope="all", reason="incident: venue mispricing the tape",
                                       confirm_phrase=v2.CONFIRM_PHRASE)
        self.ex.cancel_budget.limit = 2
        ok = [self.ex.cancel(req(), at=self.at + i)["ok"] for i in range(4)]
        self.assertEqual(ok, [True, True, False, False], "the 250/10s budget is decoration")
        # The window passes: the same call is allowed again, so the control throttles rather than latches.
        self.assertTrue(self.ex.cancel(req(), at=self.at + v2.CANCEL_ALL_BUDGET_WINDOW_MS + 10)["ok"])

    def test_batch_cancel_refuses_more_than_fifteen(self) -> None:
        with self.assertRaises(ValueError):
            v2.CancelRequest(scope="batch", reason="x", order_ids=tuple("0x%d" % i for i in range(16))).validate()

    def test_cancel_that_lost_the_race_books_the_fill(self) -> None:
        oid = self._one()
        self.mock.set_scenario("cancel_races_fill")
        self.ex.cancel(v2.CancelRequest(scope="order", reason="user cancelled", order_ids=(oid,)),
                       at=self.at + 1)
        self.assertEqual(self.orders()[0]["state"], "filled", "the venue filled it and we said cancelled")
        self.assertEqual(len(self.fills()), 1)


class TestOrphansAndGhosts(Harness):
    def test_ghost_order_stays_open_and_alarms(self) -> None:
        iid = self.queue()
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        self.mock.set_scenario("ghost_order")
        self.ex.cancel(v2.CancelRequest(scope="order", reason="user cancelled", order_ids=(oid,)),
                       at=self.at + 1)
        self.assertEqual(self.orders()[0]["state"], "cancelled")
        # 30s of grace, then the venue's disagreement is a case, not a shrug.
        for i in range(Cfg().max_sightings + 1):
            self.ex.reconciler.run_pass(at=self.at + 40_000 + i * 1_000)
        open_rows = self.store.unreconciled()
        self.assertTrue(any(r["case_name"] == "ghost_order" for r in open_rows), open_rows)
        m = self.ex.reconciler.metric(at=self.at + 200_000)
        self.assertTrue(m["alarm"], m)
        self.assertEqual(self.orders()[0]["state"], "cancelled",
                         "the reconciler must not overwrite its own row to chase the venue")

    def test_orphan_is_adopted_from_the_signed_attempt(self) -> None:
        # The exact crash shape: signed, POSTed, acknowledged, and killed before the `orders` write. The
        # attempt row survives, so "is this ours?" has a yes/no answer instead of a guess.
        iid = self.queue()
        self.mock.set_scenario("timeout_after_accept")
        self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual(self.orders(), [])
        posts = self.tp.post_calls
        p = self.ex.reconciler.run_pass(at=self.at + 200_000)
        self.assertEqual(len(self.orders()), 1, p.per_case)
        self.assertEqual(self.orders()[0]["intent_id"], iid)
        self.assertEqual(self.tp.post_calls, posts, "adoption re-posted the order")
        self.assertEqual(self.store.unreconciled(), [])

    def test_orphan_with_no_local_evidence_is_cancelled_not_adopted(self) -> None:
        payload = {"version": "v2", "price": 0.5, "size": 10.0, "side": "BUY", "token_id": self.token,
                   "maker": "someone-else", "builder": "0x" + "0" * 64, "tick_size": "0.001",
                   "client_order_hash": "0x" + "e" * 64}
        self.mock.post_order(payload)
        for i in range(Cfg().max_sightings + 2):
            self.ex.reconciler.run_pass(at=self.at + 200_000 + i * 1_000)
        self.assertEqual(self.mock.snapshot()["orders"][list(self.mock.orders)[0]]["status"], "canceled")
        self.assertEqual(self.orders(), [], "an order we cannot attribute was booked into our ledger")

    def test_unknown_status_from_the_venue_is_a_loud_error_not_a_default(self) -> None:
        with self.assertRaises(ValueError):
            from polygm_core.ledger.ledger import venue_status_to_state
            venue_status_to_state("frobnicated")


class TestBatch(Harness):
    def test_more_than_fifteen_is_chunked_not_dropped(self) -> None:
        ids = [self.queue(size_micro=10 * 10**6) for _ in range(20)]
        items = [self.store.load_intent(i) for i in ids]
        out = self.ex.submit_batch([i for i in items if i], at=self.at)
        self.assertEqual(len(out.accepted), 20, out.counts)
        self.assertEqual(len(out.rejected), 0, out.rejected)
        self.assertEqual(self.tp.batch_calls, 2, "15 + 5 needs two requests, not one truncated one")
        self.assertEqual(self.tp.mock.batch_requests, 0, "the in-process transport must not reach the HTTP layer")
        self.assertEqual(len(self.orders()), 20)

    def test_partial_failure_is_per_item(self) -> None:
        good = self.queue(size_micro=10 * 10**6, key="batch-good")
        bad = self.queue(size_micro=10 * 10**6, key="batch-bad", price_micro=555_555)   # off-tick for 0.01
        m = self.store.conn.execute("SELECT minimum_tick_size FROM markets WHERE id=?",
                                    (self.market,)).fetchone()[0]
        if str(m) not in ("0.0100", "0.01"):
            self.skipTest("seed market %s has tick %s; off-tick price is on-grid there" % (self.market, m))
        out = self.ex.submit_batch([self.store.load_intent(good), self.store.load_intent(bad)], at=self.at)
        self.assertEqual(len(out.accepted), 1, out.counts)
        self.assertEqual(len(out.rejected), 1, out.rejected)
        self.assertEqual(out.rejected[0]["code"], "TICK", out.rejected[0])
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (bad,))[0]["state"],
                         "rejected")

    def test_unanswered_item_becomes_uncertain_never_rejected(self) -> None:
        a = self.queue(key="u1", size_micro=10 * 10**6)
        b = self.queue(key="u2", size_micro=10 * 10**6)
        self.mock.set_scenario("timeout_after_accept")
        out = self.ex.submit_batch([self.store.load_intent(a), self.store.load_intent(b)], at=self.at)
        self.assertEqual(len(out.accepted), 0)
        self.assertEqual(len(out.uncertain), 2, out)
        for iid in (a, b):
            self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                             "uncertain")
        # And the reconciler adopts both, because both exist at the venue.
        posts = self.tp.post_calls
        self.ex.reconciler.run_pass(at=self.at + 60_000)
        self.assertEqual(len(self.orders()), 2)
        self.assertEqual(self.tp.post_calls, posts, "the batch retry re-posted orders the venue already had")


class TestReconcilerContract(Harness):
    def test_one_cursor_and_one_owner(self) -> None:
        self.ex.reconciler.run_pass(at=self.at)          # the pass is what creates the cursor row
        c = self.rows("SELECT * FROM reconcile_cursors")
        self.assertEqual(len(c), 1, "a second cursor means a second owner, which means a gap")
        self.assertEqual(c[0]["name"], "main")
        with self.assertRaises(Exception):
            self.store.conn.execute("INSERT INTO reconcile_cursors (name,watermark_ms,in_flight,last_run_ms,"
                                    "updated_ms) VALUES ('second',0,0,0,0)")

    def test_case_names_are_closed_against_the_module(self) -> None:
        with self.assertRaises(Exception):
            self.store.conn.execute("INSERT INTO reconcile_open (dedupe_key,case_name,intent_id,order_id,"
                                    "since_ms,note) VALUES ('k','made_up_case','',?,0,'')", ("0x1",))

    def test_alarm_fires_only_after_the_budget(self) -> None:
        key = "x:1"
        self.store.open_case(case="no_ack", intent_id="i1", since_ms=self.at, note="test", dedupe_key=key)
        self.assertFalse(self.ex.reconciler.metric(at=self.at + 30_000)["alarm"])
        self.assertTrue(self.ex.reconciler.metric(at=self.at + 61_000)["alarm"])
        m = self.ex.reconciler.metric(at=self.at + 61_000)
        self.assertEqual(m["oldest_age_ms"], 61_000, m)
        self.ex.reconciler.run_pass(at=self.at + 62_000)
        self.assertEqual(self.rows("SELECT escalated_ms FROM reconcile_open WHERE dedupe_key=?",
                                   (key,))[0]["escalated_ms"] > 0, True, "aged past the alarm but not escalated")

    def test_reconcile_open_closes_only_when_the_fact_resolves(self) -> None:
        key = "y:1"
        self.store.open_case(case="ghost_order", order_id="0xabc", since_ms=self.at, note="n", dedupe_key=key)
        self.assertEqual(len(self.store.unreconciled()), 1)
        self.assertEqual(self.store.case_attempts(key)["attempts"], 0)
        self.store.bump_case(key, at=self.at + 1, note="still")
        self.assertEqual(self.store.case_attempts(key), {"attempts": 1, "escalated_ms": 0,
                                                        "since_ms": self.at, "note": "still"})
        self.assertEqual(self.store.close_case(key), 1)
        self.assertEqual(len(self.rows("SELECT * FROM reconcile_open")), 0)

    def test_actions_are_idempotent_across_passes(self) -> None:
        self._place_and_ghost()
        # Warm the queue up until the ghost case has actually acted, THEN measure: the first action is new
        # information, not a duplicate, and a test that counts from zero proves nothing about idempotency.
        for i in range(3):
            self.ex.reconciler.run_pass(at=self.at + 100_000 + i * 1_000)
        first = self.rows("SELECT * FROM reconcile_actions")
        self.assertGreaterEqual(len(first), 1, "the reconciler never acted, so the assertion below is vacuous")
        for i in range(5):
            self.ex.reconciler.run_pass(at=self.at + 200_000 + i * 1_000)
        second = self.rows("SELECT * FROM reconcile_actions")
        self.assertEqual(len(second), len(first),
                         "the action count grew with the number of passes: a pass is not idempotent")
        self.assertEqual(len({r["dedupe_key"] for r in second}), len(second), "a dedupe key repeated")

    def _place_and_ghost(self) -> None:
        iid = self.queue()
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        self.mock.set_scenario("ghost_order")
        self.ex.cancel(v2.CancelRequest(scope="order", reason="user cancelled", order_ids=(oid,)),
                       at=self.at + 1)


class TestClaimLease(Harness):
    def test_a_crashed_claim_becomes_claimable_again(self) -> None:
        iid = self.queue()
        self.mock.set_scenario("unreachable")
        rep = self.ex.tick(at=self.at, reconcile=False)
        # unreachable raises ConnectionError inside submit; the intent must not be stuck in `submitting`
        self.assertIn(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                      ("uncertain", "rejected"))
        self.store.conn.execute("UPDATE order_intents SET state='submitting', updated_ms=? WHERE id=?",
                               (self.at - CLAIM_LEASE_MS - 1, iid))
        got = self.store.requeue_expired_claims(at=self.at)
        self.assertEqual(got, 1)
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"], "queued")

    def test_uncertain_is_never_requeued(self) -> None:
        iid = self.queue()
        self.store.conn.execute("UPDATE order_intents SET state='uncertain', updated_ms=? WHERE id=?",
                               (self.at - CLAIM_LEASE_MS * 10, iid))
        self.assertEqual(self.store.requeue_expired_claims(at=self.at), 0)
        self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                         "uncertain")


class TestSignerOutage(Harness):
    """P14 D2's wallet-provider drill, as a test: the provider answers nothing and the executor must not die.

    Found by the drill and fixed in the same chunk. The provider's `sign` was the one external call in
    `handle_intent` that was not guarded (the venue calls below it already catch `TimeoutError`/`ConnectionError`),
    so a signer that raised propagated out of `tick`, killed the whole pass — every other intent in the batch
    unprocessed — and left the intent sitting in `signing` with nothing on the wire and no named reason.

    The state it lands in is `queued` rather than `uncertain`, and that is the load-bearing decision: the attempt
    row is written *after* signing, so a signer that answered nothing proves nothing was sent. `uncertain` is
    never auto-requeued, so using it here would turn a provider blip into a queue of orders a human has to clear
    one at a time — which is the failure mode this executor goes out of its way to avoid everywhere else.
    """

    class DownSigner:
        name = "provider-down"
        pubkey = "0x" + "d0" * 32

        def sign(self, payload: dict) -> dict:                            # noqa: ARG002
            raise RuntimeError("PROVIDER_DOWN: the signing provider returned no signature")

    def test_a_signer_that_answers_nothing_is_a_named_retry_and_not_a_crash(self) -> None:
        iid = self.queue()
        self.ex.signer = self.DownSigner()                                # type: ignore[assignment]
        rep = self.ex.tick(at=self.at + 10, reconcile=False)              # must not raise
        self.assertEqual(len(rep["handled"]), 1)
        verdict = rep["handled"][0]
        self.assertEqual(verdict["state"], "retryable")
        self.assertEqual(verdict["code"], "SIGNER_UNAVAILABLE")
        row = self.rows("SELECT state, risk_code FROM order_intents WHERE id=?", (iid,))[0]
        self.assertEqual(row["state"], "queued", "a provider outage is a retry, not a dead order")
        self.assertEqual(row["risk_code"], "SIGNER_UNAVAILABLE")
        # Nothing was signed, so nothing may claim to have been: no attempt row (which is what proves "not
        # sent"), no venue call, no order, no fill, no cash.
        self.assertEqual(self.rows("SELECT * FROM order_attempts"), [])
        self.assertEqual(self.tp.post_calls, 0)
        self.assertEqual(self.orders(), [])
        self.assertEqual(self.fills(), [])
        self.assertEqual(self.cash(), [])
        # The trail names the reason and stays inside the state machine's own vocabulary.
        trail = self.rows("SELECT state, reason FROM order_lifecycle WHERE intent_id=? ORDER BY id", (iid,))
        self.assertEqual(trail[-1]["state"], "draft")
        self.assertIn("SIGNER_UNAVAILABLE", trail[-1]["reason"])
        # The user is told, in the vocabulary the notification table already has.
        notes = self.rows("SELECT event, detail_json FROM order_notifications WHERE intent_id=?", (iid,))
        self.assertEqual([n["event"] for n in notes], ["queued"])
        self.assertIn("SIGNER_UNAVAILABLE", str(notes[0]["detail_json"]))

    def test_the_retry_after_the_provider_returns_is_exactly_one_order(self) -> None:
        iid = self.queue()
        self.ex.signer = self.DownSigner()                                # type: ignore[assignment]
        self.ex.tick(at=self.at + 10, reconcile=False)
        self.ex.signer = executor_main.TestSigner()                        # type: ignore[assignment]
        # The book has to be fresh for the retry, and that is the point rather than a fixture detail: the
        # requeued intent goes through the risk gate AGAIN, so a retry can never ride an old snapshot into the
        # book. (With a stale book this tick answers `rejected/STALE_QUOTE`, which the drill observed first.)
        self.store.conn.execute("UPDATE book_levels SET updated_ms=? WHERE market_id=?", (self.at, self.market))
        rep = self.ex.tick(at=self.at + 20, reconcile=False)
        self.assertEqual(rep["handled"][0]["state"], "submitted")
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(len(self.rows("SELECT * FROM order_attempts WHERE intent_id=?", (iid,))), 1)
        # A third pass must not re-POST: the attempt row already covers this intent.
        self.ex.tick(at=self.at + 30, reconcile=False)
        self.assertEqual(len(self.orders()), 1)


from store import CLAIM_LEASE_MS                                          # noqa: E402  (used above)

if __name__ == "__main__":
    unittest.main()
