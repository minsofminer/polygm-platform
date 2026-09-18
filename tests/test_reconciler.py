"""P06 D3: the reconciliation taxonomy, one executed test per case.

`CASE_ORDER` is a list of eight questions the trading plane can be asked and must answer without guessing:
did we sign something the venue never got, did the venue acknowledge and go quiet, did a cancel and a fill
cross, is a fill lagging, did the market close under an order, does the venue still show what we cancelled,
is there an order at the venue that we do not own, is there a trade we cannot attribute. Each is a different
failure, needs a different action, and — the part that matters — each must be *reachable*. A case that no
fixture can produce is decoration in the module and a hole in production.

The fixture is the same `Harness` the executor suite uses, deliberately: these tests are about what the
reconciler does to the same tables the live path writes, and a second, parallel, simplified schema would
prove that the code agrees with itself instead of with the database.

Graces are zeroed almost everywhere. Zeroing them is not cheating: the grace is a *timer*, and every test
here is about the decision the reconciler makes once a timer has expired. The two places where the timer is
the subject (an in-flight write that must not be mistaken for a lost order) keep their real values and say
so, because that is exactly where a faked clock would hide a bug.
"""
from __future__ import annotations

import unittest
from dataclasses import replace

# Import order is load-bearing: `test_executor_service` is what puts `packages/`, `services/executor/` and
# `services/executor-mock/` on sys.path (via `conftest`). Importing `polygm_core` before it works when the
# whole suite runs in one process and fails when this file is run alone, which is the only way a broken
# import order is ever caught.
from test_executor_service import Harness                                            # noqa: F401,E402

from polygm_core.reconcile.reconciler import CASE_ORDER, Cfg, Reconciler               # noqa: E402
from polygm_core.venue import clob_v2 as v2                                          # noqa: E402


class TestReconcileTaxonomy(Harness):
    """One test per case, in `CASE_ORDER`, plus the two invariants the whole module rests on."""

    def machine(self, **kw) -> Reconciler:
        cfg = Cfg(no_ack_grace_ms=0, unacked_grace_ms=0, cancel_race_grace_ms=0, fill_grace_ms=0,
                  closing_grace_ms=0, ghost_grace_ms=0, orphan_grace_ms=0, ambiguous_grace_ms=0)
        return Reconciler(self.store, self.tp, cfg=replace(cfg, **kw))

    def signed_but_unanswered(self, *, key: str) -> tuple[str, str]:
        """The one shape that produces `no_ack`: we hold a signed attempt, the intent is `uncertain`, and the
        venue has never given us an order id. Built directly, because the crash that makes it real is the
        subject of `tools/p06-chaos-test.py`."""
        iid = self.queue(key=key)
        h = "0x%064x" % (abs(hash(key)) & (16 ** 64 - 1))
        self.store.conn.execute(
            "INSERT INTO order_attempts (client_order_hash,intent_id,user_id,token_id,side,price_micro,"
            "size_micro,order_type,signature_type,builder_code,payload_digest,signed_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, iid, self.user, self.token, "BUY", 550_000, 100 * 10**6, "GTC", 3, "0x" + "0" * 64,
             "0x" + ("%064x" % 7), self.at))
        self.store.conn.execute("UPDATE order_intents SET state='uncertain', client_order_hash=?, "
                                "updated_ms=? WHERE id=?", (h, self.at, iid))
        self.store.conn.commit()
        return iid, h

    def venue_order(self, client_order_hash: str, *, maker: str = "someone-else") -> str:
        """Put an order on the venue that our local tables have no `orders` row for — the shape a lost write
        leaves behind. Returns the venue's order id."""
        tick = self.rows("SELECT minimum_tick_size AS t FROM markets WHERE id=?", (self.market,))[0]["t"]
        resp = self.mock.post_order({"version": "v2", "price": 0.5, "size": 10.0, "side": "BUY",
                                    "token_id": self.token, "maker": maker, "builder": "0x" + "0" * 64,
                                    "tick_size": str(tick), "client_order_hash": client_order_hash})
        self.assertIn("orderID", resp, resp)
        return str(resp["orderID"])

    # ------------------------------------------------------------------------------------------ 1 no_ack
    def test_no_ack_absence_is_only_established_after_repeated_sightings(self) -> None:
        iid, _h = self.signed_but_unanswered(key="rec-noack")
        r = self.machine()
        p = r.run_pass(at=self.at + 1_000)
        self.assertEqual(p.per_case["no_ack"]["found"], 1, p.per_case)
        open_rows = self.store.unreconciled()
        self.assertEqual([x["case_name"] for x in open_rows], ["no_ack"], open_rows)
        self.assertEqual(self.tp.post_calls, 0, "the first pass must never re-POST, that is the duplicate")
        self.assertEqual(self.orders(), [], "we invented an order the venue never confirmed")
        cfg = r.cfg
        for i in range(cfg.max_sightings):
            p = r.run_pass(at=self.at + 2_000 + i * 1_000)
        state = self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"]
        self.assertEqual(state, "cancelled",
                         "the venue's 'no such order' is not a rejection; only `cancelled` is a claim we can"
                         " stand behind, because there is no venue record of a refusal to explain")
        self.assertEqual(self.trail(iid)[-2:], ["cancelled", "reconciled"], self.trail(iid))
        acts = self.rows("SELECT action FROM reconcile_actions WHERE case_name='no_ack' ORDER BY id")
        self.assertTrue(acts and "declare_absent" in acts[0]["action"], acts)
        self.assertEqual(self.tp.post_calls, 0)
        # Establishing the absence closes the item: an alarm that keeps ringing about an intent we have already
        # cancelled is how a team learns to ignore the alarm. The age-based alarm is covered by
        # `test_the_metric_is_the_shape_the_dashboard_reads` and by the ghost case below.
        self.assertEqual(self.store.unreconciled(), [], "a resolved intent left an open case behind")

    def test_no_ack_found_at_the_venue_is_adopted_without_a_second_post(self) -> None:
        iid = self.queue(key="rec-adopt")
        self.mock.set_scenario("timeout_after_accept")
        self.ex.tick(at=self.at, reconcile=False)
        posts = self.tp.post_calls
        self.assertEqual(self.orders(), [])
        p = self.ex.reconciler.run_pass(at=self.at + 60_000)
        self.assertEqual(self.tp.post_calls, posts, "adoption re-POSTed the order")
        self.assertEqual(len(self.orders()), 1, p.per_case)
        self.assertEqual(self.orders()[0]["intent_id"], iid)
        self.assertIn("reconciled", self.trail(iid))
        self.assertEqual(self.store.unreconciled(), [])

    # ----------------------------------------------------------------------------- 2 submitted_unacked
    def test_submitted_unacked_is_repaired_from_the_venue_and_closed(self) -> None:
        self.queue(key="rec-unacked")
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        # The venue said "accepted" and our row never got the second half of that conversation.
        self.store.conn.execute("UPDATE orders SET acknowledged=0, placed_ms=? WHERE id=?",
                                (self.at - 1_000, oid))
        self.store.conn.commit()
        p = self.machine().run_pass(at=self.at + 2_000)
        self.assertEqual(p.per_case["submitted_unacked"]["acted"], 1, p.per_case)
        self.assertEqual(self.orders()[0]["acknowledged"], 1)
        self.assertEqual(self.store.unreconciled(), [], "a repaired order must not keep an open case")
        # The action is recorded once per case, so a second pass cannot double-notify the user.
        p2 = self.machine().run_pass(at=self.at + 3_000)
        self.assertEqual(p2.per_case.get("submitted_unacked", {}).get("acted", 0), 0)

    def test_submitted_unacked_when_the_venue_has_nothing_is_a_case_not_a_deletion(self) -> None:
        self.queue(key="rec-unacked-gone")
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        self.mock.orders.pop(oid, None)                                 # the venue has genuinely lost it
        self.store.conn.execute("UPDATE orders SET acknowledged=0, placed_ms=? WHERE id=?",
                                (self.at - 1_000, oid))
        self.store.conn.commit()
        self.machine().run_pass(at=self.at + 2_000)
        rows = self.store.unreconciled()
        self.assertEqual([x["case_name"] for x in rows], ["submitted_unacked"], rows)
        self.assertIn("no record", rows[0]["note"])
        self.assertEqual(len(self.orders()), 1, "the reconciler deleted our only evidence of the order")

    # ------------------------------------------------------------------------- 3 cancelled_race
    def test_cancelled_race_books_the_fill_and_tells_the_user_filled(self) -> None:
        self.queue(key="rec-race")
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.55, size=100.0)                     # the fill won, whatever we asked for
        p = self.machine().run_pass(at=self.at + 6_000)
        self.assertEqual(p.per_case["cancelled_race"]["found"], 1, p.per_case)
        self.assertEqual(self.orders()[0]["state"], "filled")
        self.assertEqual(len(self.fills()), 1)
        events = [n["event"] for n in self.rows("SELECT event FROM order_notifications")]
        self.assertIn("filled", events, events)

    # ---------------------------------------------------------------------------- 4 lagging_fill
    def test_lagging_fill_is_booked_and_the_case_closes(self) -> None:
        self.queue(key="rec-lag")
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.55, size=40.0)                      # 40 of 100 shares, venue-side only
        p = self.machine().run_pass(at=self.at + 4_000)
        self.assertEqual(p.per_case["lagging_fill"]["found"], 1, p.per_case)
        self.assertEqual(int(self.orders()[0]["size_matched_micro"]), 40 * 10**6)
        self.assertEqual(self.store.unreconciled(), [], "the case outlived the fill it was about")

    def test_a_lagging_fill_inside_the_grace_is_left_alone(self) -> None:
        # The grace is the subject here, so it keeps its real value and the fixture stays inside it: a fill
        # that is merely slow must not be chased at all, or every WS hiccup becomes a cancel storm.
        self.queue(key="rec-lag-grace")
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        self.mock.fill(oid, price=0.55, size=40.0)
        p = self.machine(fill_grace_ms=3_000, cancel_race_grace_ms=3_000).run_pass(at=self.at + 1_000)
        # `found` is allowed: the difference was SEEN. What the grace buys is the action — nothing is booked,
        # nothing is cancelled, and no case is opened while our own write may still be in flight.
        self.assertEqual(p.per_case.get("lagging_fill", {}).get("closed", 0), 0, p.per_case)
        self.assertEqual(p.per_case.get("lagging_fill", {}).get("opened", 0), 0, p.per_case)
        self.assertEqual(int(self.orders()[0]["size_matched_micro"]), 0)

    # --------------------------------------------------------------------------- 5 closing_market
    def test_closing_market_cancels_the_order_it_cannot_hedge(self) -> None:
        self.queue(key="rec-closing")
        self.ex.tick(at=self.at, reconcile=False)
        self.store.conn.execute("UPDATE markets SET accepting_orders=0 WHERE id=?", (self.market,))
        self.store.conn.commit()
        p = self.machine().run_pass(at=self.at + 20_000)
        self.assertEqual(p.per_case["closing_market"]["acted"], 1, p.per_case)
        self.assertEqual(self.orders()[0]["state"], "cancelled")
        self.assertEqual(self.store.unreconciled(), [])

    # ----------------------------------------------------------------------------- 6 ghost_order
    def test_ghost_order_escalates_and_keeps_the_state_we_wrote(self) -> None:
        self.queue(key="rec-ghost")
        self.ex.tick(at=self.at, reconcile=False)
        oid = self.orders()[0]["id"]
        self.mock.set_scenario("ghost_order")
        self.ex.cancel(v2.CancelRequest(scope="order", reason="user cancelled", order_ids=(oid,)),
                       at=self.at + 1)
        r = self.machine()
        for i in range(r.cfg.max_sightings + 1):
            r.run_pass(at=self.at + 31_000 + i * 1_000)
        rows = self.store.unreconciled()
        self.assertTrue(any(x["case_name"] == "ghost_order" for x in rows), rows)
        self.assertTrue(all(x["escalated_ms"] for x in rows), rows)
        self.assertEqual(self.orders()[0]["state"], "cancelled",
                         "the reconciler overwrote our row to agree with a venue that contradicts itself")

    # ------------------------------------------------------------------------------------ 7 orphan
    def test_orphan_is_adopted_from_the_signed_attempt(self) -> None:
        # The venue has the order and we hold the signed attempt, but the intent never reached `uncertain`:
        # this is the write that lost, seen from the side that has no live-path story to tell. `no_ack` is
        # kept out of it (the intent is `pending`), so whatever happens must be `orphan`'s doing.
        iid, h = self.signed_but_unanswered(key="rec-orphan")
        self.store.conn.execute("UPDATE order_intents SET state='pending' WHERE id=?", (iid,))
        self.store.conn.commit()
        oid = self.venue_order(h)
        p = self.machine(orphan_grace_ms=0).run_pass(at=self.at + 200_000)
        self.assertEqual(p.per_case.get("orphan", {}).get("acted", 0), 1, p.per_case)
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(self.orders()[0]["id"], oid)
        self.assertEqual(self.orders()[0]["intent_id"], iid)
        self.assertEqual(self.tp.post_calls, 0, "adoption re-POSTed the order")
        self.assertEqual(self.store.unreconciled(), [])

    def test_an_orphan_with_no_local_evidence_is_cancelled_not_adopted(self) -> None:
        oid = self.venue_order("0x" + "a" * 64)
        self.assertEqual(list(self.mock.orders), [oid], "the fixture did not reach the venue")
        r = self.machine(orphan_grace_ms=0)
        for i in range(r.cfg.max_sightings + 2):
            r.run_pass(at=self.at + 200_000 + i * 1_000)
        self.assertEqual(self.orders(), [], "we adopted an order that is not ours")
        self.assertEqual(self.mock.orders[oid]["status"], "canceled", "someone else's order is still on the book")
        rows = [x for x in self.store.unreconciled() if x["case_name"] == "orphan"]
        self.assertTrue(rows, "an unowned order left no case for a human to read")

    # --------------------------------------------------------------------------- 8 ambiguous_settlement
    def test_ambiguous_settlement_opens_a_case_and_books_nothing(self) -> None:
        oid = self.venue_order("0x" + "b" * 64)
        self.mock.fill(oid, price=0.5, size=10.0)
        cash_before = len(self.cash())
        p = self.machine().run_pass(at=self.at + 400_000)
        self.assertEqual(p.per_case["ambiguous_settlement"]["opened"], 1, p.per_case)
        self.assertEqual(self.fills(), [], "the fill was booked against the closest order")
        self.assertEqual(len(self.cash()), cash_before, "a balance moved on an unattributable trade")
        row = [x for x in self.store.unreconciled() if x["case_name"] == "ambiguous_settlement"][0]
        self.assertIn("NOT booked", row["note"])

    # ---------------------------------------------------------------------------------- invariants
    def test_every_case_in_the_taxonomy_is_exercised_by_this_file(self) -> None:
        """The map is checked against the names, so adding a case without a fixture is a test failure."""
        methods = {m[len("case_"):] for m in dir(Reconciler) if m.startswith("case_")}
        self.assertEqual(tuple(sorted(methods)), tuple(sorted(CASE_ORDER)),
                         "a case in CASE_ORDER with no handler, or a handler outside the taxonomy")
        named = [t for t in dir(self) if t.startswith("test_")]
        for case in CASE_ORDER:
            self.assertTrue(any(case in t for t in named), "no test names the %s case" % case)

    def test_a_failed_lookup_is_not_evidence_of_absence(self) -> None:
        iid, _h = self.signed_but_unanswered(key="rec-lookup-down")
        real = self.tp.find_order

        def broken(client_order_hash, *, timeout_ms: int = 2000):
            raise ConnectionError("venue is down")

        self.tp.find_order = broken
        try:
            r = self.machine()
            p = r.run_pass(at=self.at + 1_000)
            self.assertTrue(p.errors, "a lookup failure was swallowed as a clean pass")
            row = self.store.unreconciled()[0]
            self.assertIn("cannot reach the venue", row["note"], row)
            self.assertEqual(row["attempts"], 0, "a failed sighting counted as a sighting")
            self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                             "uncertain", "an unreachable venue cancelled our intent")
            for i in range(r.cfg.max_sightings + 2):
                r.run_pass(at=self.at + 2_000 + i * 1_000)
            self.assertEqual(self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"],
                             "uncertain", "8 failed lookups were treated as 8 confirmed absences")
        finally:
            self.tp.find_order = real

    def test_the_metric_is_the_shape_the_dashboard_reads(self) -> None:
        self.signed_but_unanswered(key="rec-metric")
        r = self.machine()
        r.run_pass(at=self.at + 1_000)
        m = r.metric(at=self.at + 2_000)
        self.assertEqual(set(("unreconciled_orders", "oldest_age_ms", "alarm", "by_case", "escalated"))
                         - set(m), set(), m)
        self.assertEqual(sorted(m["by_case"]), sorted(CASE_ORDER), "the dashboard groups by a case we do not have")
        self.assertGreater(m["oldest_age_ms"], 0)
        self.assertFalse(m["alarm"], "60 seconds have not passed; an alarm that fires immediately is no alarm")
        self.assertTrue(r.metric(at=self.at + 62_000)["alarm"], "60s + 1ms must be enough to alarm")

    def test_the_watermark_moves_and_the_cursor_survives_a_restart(self) -> None:
        r = self.machine()
        p1 = r.run_pass(at=self.at + 1_000)
        p2 = r.run_pass(at=self.at + 61_000)
        self.assertGreaterEqual(p2.watermark_ms, p1.watermark_ms)
        state = self.store.cursor_load()
        self.assertIsNotNone(state, "the pass wrote no cursor: a restart re-reads the whole venue")
        # A restarted process must not re-open a case that is already open: the key, not the pass, owns it.
        rows_before = len(self.store.unreconciled())
        Reconciler(self.store, self.tp, cfg=r.cfg).run_pass(at=self.at + 121_000)
        self.assertEqual(len(self.store.unreconciled()), rows_before)


if __name__ == "__main__":
    unittest.main()
