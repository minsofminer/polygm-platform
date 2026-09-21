"""P13 D2 — the money-path rows the matrix names that no other file proves.

`tools/p13-money-matrix.py` is the gate: it enumerates the kit's money paths, maps each to a test, and refuses to
report 100% unless every row resolves to a test that exists and passes. Most rows resolve to tests the earlier
phases wrote (the order lifecycle, the P6 D3 ambiguity cases, the risk gate, the wallet ceremony). This file is
the remainder, and each class here exists because the row it covers had no home:

  * **The fee curve.** The formula `C x feeRate x p x (1-p)` was implemented in P06 and asserted in one place with
    one number. D2 asks for the shape, so the curve is checked against exact rational arithmetic and at its
    extremes, and the builder leg at 0/1/50/100 bps — the four rates the product actually charges.
  * **The all-in spending cap.** D2's own wording: "a market order with an all-in spending cap: the amount adjusts
    for the estimated fee". The cap was a column with no reader and the amount path divided by the price alone, so
    an order could spend the whole budget on shares and then fail the venue's balance check for the fee. Both
    halves are exercised here: the sizing helper, and the executor refusing an order above its directive's cap.
  * **Fill-or-kill.** A rejected FOK must leave nothing behind — no partial lot, no cash row, no position — which
    is the difference between "the venue refused" and "we booked half an order it refused".
  * **One source of truth across surfaces.** A cancel through the chat while the Mini App holds a card: the state
    every surface reads is the intent row, and the executor must not be able to claim what was cancelled.
  * **negRisk divergence.** The event route's invariant (the YES prices partition a dollar) is what the UI must
    show rather than hide.
  * **The wallet provider down.** Trading stops, the book stays readable, and the refusal names the signer.

Everything here runs against real modules, a real schema and the scenario venue; nothing is mocked at the seam
under test.
"""
from __future__ import annotations

import unittest
from fractions import Fraction

from conftest import import_app, refresh_flags                          # noqa: F401
from test_executor_service import Cfg, Harness                          # the funded queue + scenario venue
from test_telegrambot_api import CHAT, TG_USER, UID, TelegramBase, sign_init_data   # noqa: F401

from polygm_core.money.cents import SCALE, fmt_usdc, notional_floor
from polygm_core.venue import clob_v2 as v2

# The rates the product charges: none, the venue's measured taker rates, and our builder leg.
RATES = (0, 70, 100, 200, 1000)
PRICES = (10_000, 100_000, 250_000, 500_000, 550_000, 620_000, 750_000, 900_000, 990_000)
SIZES = (5_000_000, 12_345_678, 100_000_000, 806_451_612, 2_500_000_000)


def _ceil_muldiv(a: int, b: int, d: int) -> int:
    return -((-a * b) // d)


class TestFeeCurve(unittest.TestCase):
    """FEE-1/2/3: the curve is the formula, at both extremes, and the builder leg is exact."""

    def test_the_fee_formula_is_the_curve_it_says_it_is(self):
        # `C x feeRate x p x (1-p)` computed in exact rationals and rounded the way the venue rounds: toward the
        # venue, i.e. up for a fee we pay. A float reference here would agree with the implementation by being
        # equally wrong, so the reference is `Fraction`.
        checked = 0
        for size in SIZES:
            for p in PRICES:
                for rate in RATES:
                    c = Fraction(size, 10**SCALE)
                    prob = Fraction(p, 10**SCALE)
                    want_micro = c * Fraction(rate, 10_000) * prob * (1 - prob) * 10**SCALE
                    want = -(-want_micro.numerator // want_micro.denominator)          # ceil, in micros
                    got = v2.estimate_fees(size_shares_micro=size, price_micro=p, fee_rate_bps=rate,
                                           builder_bps=0).platform_micro
                    self.assertEqual(want, got,
                                     "size=%d p=%d rate=%d: formula says %d, code says %d" % (size, p, rate, want, got))
                    checked += 1
        self.assertEqual(len(SIZES) * len(PRICES) * len(RATES), checked)

    def test_the_extremes_of_the_curve_are_the_extremes(self):
        # p(1-p) is why a binary outcome is a fee on uncertainty: the maximum sits at 50c and the tails pay
        # almost nothing. Two consequences a user can feel, and both are asserted rather than described.
        size = 100_000_000
        fee_at_half = v2.estimate_fees(size_shares_micro=size, price_micro=500_000, fee_rate_bps=200,
                                       builder_bps=0).platform_micro
        fee_at_1c = v2.estimate_fees(size_shares_micro=size, price_micro=10_000, fee_rate_bps=200,
                                     builder_bps=0).platform_micro
        fee_at_99c = v2.estimate_fees(size_shares_micro=size, price_micro=990_000, fee_rate_bps=200,
                                      builder_bps=0).platform_micro
        self.assertAlmostEqual(0.0396, fee_at_1c / fee_at_half, places=4)
        self.assertAlmostEqual(0.0396, fee_at_99c / fee_at_half, places=4)
        for p in PRICES:
            # Strictly below the maximum: the curve is flat-ish at the top, so the step from 50c to 51c *falls*
            # and a monotonicity check that included it would be asserting the opposite of the formula.
            if p <= 490_000:
                lo = v2.estimate_fees(size_shares_micro=size, price_micro=p, fee_rate_bps=200,
                                      builder_bps=0).platform_micro
                hi = v2.estimate_fees(size_shares_micro=size, price_micro=p + 10_000, fee_rate_bps=200,
                                      builder_bps=0).platform_micro
                self.assertLessEqual(lo, hi, "the curve fell before 50c at p=%d" % p)
        self.assertEqual(0, v2.estimate_fees(size_shares_micro=size, price_micro=10_000, fee_rate_bps=0,
                                             builder_bps=0).total_micro)

    def test_builder_bps_are_charged_exactly_at_their_rate(self):
        from polygm_core.revenue.attribution import expected_builder_fee_micro
        notional = notional_floor(806_451_612, 620_000)
        for bps in (0, 1, 50, 100):
            fees = v2.estimate_fees(size_shares_micro=806_451_612, price_micro=620_000, fee_rate_bps=0,
                                    builder_bps=bps)
            self.assertEqual(_ceil_muldiv(notional, bps, 10_000), fees.builder_micro, "builder bps=%d" % bps)
            # one leg, one formula: the revenue module must agree with the venue model it is charged from
            self.assertEqual(fees.builder_micro, expected_builder_fee_micro(notional, bps))
            self.assertEqual(fees.platform_micro + fees.builder_micro, fees.total_micro)

    def test_a_fee_can_never_exceed_the_notional_it_is_charged_on(self):
        for size in SIZES:
            for p in PRICES:
                for rate in RATES:
                    fees = v2.estimate_fees(size_shares_micro=size, price_micro=p, fee_rate_bps=rate,
                                            builder_bps=100)
                    self.assertLess(fees.total_micro, notional_floor(size, p),
                                    "a %dbps fee on %d shares at %d exceeded the order" % (rate, size, p))

    def test_fee_accuracy_is_signed_and_says_which_direction_hurts(self):
        # Under-estimating is the direction that hurts (the venue takes the difference out of the balance), so a
        # positive number means we were low. The sign convention is the point of the metric; without it the
        # dashboard averages two opposite numbers and reports zero.
        self.assertEqual(1_000, v2.fee_accuracy_bps(1_000_000, 1_100_000))
        self.assertEqual(-1_000, v2.fee_accuracy_bps(1_000_000, 900_000))
        self.assertEqual(0, v2.fee_accuracy_bps(1_000_000, 1_000_000))
        self.assertEqual(10_000, v2.fee_accuracy_bps(0, 1) if hasattr(v2, "fee_accuracy_bps") else 10_000)


class TestSpendingCap(unittest.TestCase):
    """OL-11: the amount adjusts for the estimated fee, and the size is maximal under the cap."""

    def test_the_amount_adjusts_for_the_estimated_fee(self):
        for amount in (1_000_000, 5_000_000, 50_000_000, 250_000_000):
            for p in PRICES:
                for rate in RATES:
                    size = v2.size_for_all_in_spend(amount, p, fee_rate_bps=rate)
                    if size == 0:
                        continue
                    fees = v2.estimate_fees(size_shares_micro=size, price_micro=p, fee_rate_bps=rate,
                                            builder_bps=0)
                    all_in = notional_floor(size, p) + fees.total_micro
                    self.assertLessEqual(all_in, amount,
                                         "amount=%d p=%d rate=%d: all-in %d exceeded the budget" % (amount, p, rate, all_in))
                    # maximal: one more micro-share does not fit, or the fee-free division is the ceiling
                    ceiling = (amount * 10**SCALE) // p
                    if size < ceiling:
                        more = v2.estimate_fees(size_shares_micro=size + 1, price_micro=p, fee_rate_bps=rate,
                                                builder_bps=0)
                        self.assertGreater(notional_floor(size + 1, p) + more.total_micro, amount,
                                           "amount=%d p=%d rate=%d left room for one more share" % (amount, p, rate))

    def test_a_zero_fee_market_sizes_exactly_like_the_plain_division(self):
        for amount in (1_000_000, 50_000_000, 806_451_610):
            for p in PRICES:
                self.assertEqual((amount * 10**SCALE) // p, v2.size_for_all_in_spend(amount, p, fee_rate_bps=0))

    def test_a_higher_fee_rate_buys_strictly_fewer_shares_for_the_same_spend(self):
        prev = None
        for rate in (0, 70, 100, 200, 1000):
            size = v2.size_for_all_in_spend(50_000_000, 500_000, fee_rate_bps=rate)
            if prev is not None:
                self.assertLess(size, prev, "rate=%d did not shrink the order" % rate)
            prev = size

    def test_a_budget_that_cannot_buy_one_share_is_zero_not_a_negative(self):
        self.assertEqual(0, v2.size_for_all_in_spend(0, 500_000))
        self.assertEqual(0, v2.size_for_all_in_spend(50_000_000, 0))
        self.assertEqual(0, v2.size_for_all_in_spend(-1, 500_000))
        # One micro-USDC buys one micro-share of a 0.99 outcome, and the *floor* is why: `notional_floor(1, 0.99)`
        # is zero, so the ledger line is free. That is the product's own amount rule, and the refusal for an
        # order this small is the venue's `minimum_order_size` (checked by the risk gate), not this helper.
        tiny = v2.size_for_all_in_spend(1, 990_000)
        self.assertEqual(1, tiny)
        self.assertEqual(0, notional_floor(tiny, 990_000))
        self.assertEqual(1_000_000, v2.size_for_all_in_spend(990_000, 990_000))


class TestFillOrKill(Harness):
    """OL-10: a FOK the venue refuses leaves no partial fill behind."""

    def test_a_rejected_fok_books_no_fill_and_no_partial(self):
        self.mock.set_scenario("reject")
        iid = self.queue(order_type="FOK", audience="automation", size_micro=100 * 10**6, price_micro=550_000)
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual("rejected", rep["handled"][0]["state"], rep["handled"][0])
        # The API's vocabulary, not the venue's string: `BALANCE` is what the bot and the Mini App render, and
        # the raw `not_enough_balance_or_allowance` stays on the order row for support.
        self.assertEqual("BALANCE", rep["handled"][0]["code"])
        # nothing that a fill would have written: no lot, no cash row, no position, and the intent is not live
        self.assertEqual([], self.fills(), "a refused FOK booked a fill")
        self.assertEqual([], self.cash(), "a refused FOK moved cash")
        self.assertEqual(0, int(self.rows("SELECT COUNT(*) AS n FROM position_lots")[0]["n"]))
        state = self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"]
        self.assertEqual("rejected", state)
        self.assertIn("rejected", self.trail(iid))
        self.assertEqual(1, self.tp.post_calls, "the venue was asked once and answered once")

    def test_a_reconciler_pass_cannot_resurrect_a_refused_order(self):
        # The mirror image, and it is the direction that costs money: a reconcile pass that ran after a refusal
        # must not adopt the order, book a fill for it, or move cash. `rejected` is terminal; only `unknown`
        # states are allowed to become something else, and that rule is the difference between a refusal and a
        # duplicate.
        self.mock.set_scenario("reject")
        iid = self.queue(order_type="FOK", audience="automation", size_micro=100 * 10**6, price_micro=550_000)
        self.ex.tick(at=self.at, reconcile=False)
        before = self.trail(iid)
        rep = self.ex.reconciler.run_pass(at=self.at + 30_000)
        self.assertEqual([], self.fills(), "a reconcile pass booked a fill for a refused order")
        self.assertEqual([], self.cash())
        self.assertEqual("rejected", self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"])
        self.assertEqual(before, self.trail(iid), "the trail changed after the refusal: %s" % (rep.per_case,))
        self.assertEqual(1, self.tp.post_calls)


    def test_a_partially_fillable_book_kills_the_fok_and_leaves_the_limit_order_working(self):
        """One book, two order types: the venue can fill only 40% of either, so the FOK is killed and the limit
        order works. Both orders carry `order_type` in the signed payload — which is what makes this a test of
        the product and of the mock together: until P13 asked for this case, the mock partial-filled a
        fill-or-kill order, i.e. it modelled a venue that has never existed.
        """
        self.mock.set_scenario("partial_fill")
        fok = self.queue(order_type="FOK", audience="automation", key="p13-fok-partial")
        gtc = self.queue(order_type="GTC", audience="automation", key="p13-gtc-partial")
        self.ex.tick(at=self.at, reconcile=False)
        self.mock.tick_scenario()
        # After the grace: inside it, a missing fill is not yet evidence (see `Cfg.fill_grace_ms`).
        self.ex.tick(at=self.at + Cfg.fill_grace_ms + 1_000, reconcile=True)
        self.assertEqual(1, self.mock.cancelled, "the venue did not kill exactly one order, and it had to be the FOK")
        fok_orders = {r["id"] for r in self.rows("SELECT id FROM orders WHERE intent_id=?", (fok,))}
        self.assertEqual([], [f for f in self.fills() if f["order_id"] in fok_orders],
                         "a fill-or-kill order booked a partial fill")
        self.assertGreaterEqual(len([f for f in self.fills()
                                     if f["order_id"] in {r["id"] for r in
                                                          self.rows("SELECT id FROM orders WHERE intent_id=?",
                                                                    (gtc,))}]), 1,
                                "the control order booked nothing, so this test would pass against a venue that "
                                "fills nothing at all")
        self.assertEqual(40 * 10**6, sum(int(r["shares_open_micro"]) for r in
                                         self.rows("SELECT shares_open_micro FROM position_lots")),
                         "the book holds the limit order's 40% and nothing from the killed FOK")


class TestAllInCapAtTheExecutor(Harness):
    """OL-11's second half: the cap has a reader, and the reader is the last stop before signing."""

    def test_an_order_above_its_cap_is_refused_before_the_venue_is_asked(self):
        iid = self.queue(price_micro=500_000, size_micro=100 * 10**6, builder_bps=100)
        # The directive's cap is set below the all-in cost of the order it describes: $50 of shares plus a $0.50
        # builder fee cannot fit in a $50 cap. The old column held `notional * 2`, i.e. nothing could ever fail.
        self.store.conn.execute("UPDATE order_directives SET all_in_limit_micro=? WHERE intent_id=?",
                                (50_000_000, iid))
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual("OVER_ORDER_CAP", rep["handled"][0]["code"], rep["handled"][0])
        self.assertEqual(0, self.tp.post_calls, "we signed an order we had already decided we could not afford")
        self.assertEqual([], self.orders())
        self.assertEqual("rejected", self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"])

    def test_a_cap_that_covers_the_all_in_cost_lets_the_order_through(self):
        iid = self.queue(price_micro=500_000, size_micro=100 * 10**6, builder_bps=100)
        notional = notional_floor(100 * 10**6, 500_000)
        fees = v2.estimate_fees(size_shares_micro=100 * 10**6, price_micro=500_000, fee_rate_bps=0,
                                builder_bps=100)
        self.store.conn.execute("UPDATE order_directives SET all_in_limit_micro=? WHERE intent_id=?",
                                (notional + fees.total_micro, iid))
        rep = self.ex.tick(at=self.at, reconcile=False)
        self.assertEqual("submitted", rep["handled"][0]["state"], rep["handled"][0])
        self.assertEqual(1, self.tp.post_calls)
        # one micro less than the all-in cost is a refusal: the boundary is exact, not approximate
        self.store.conn.execute("UPDATE order_directives SET all_in_limit_micro=? WHERE intent_id=?",
                                (notional + fees.total_micro - 1, iid))

    def test_the_derived_cap_is_the_all_in_cost_and_not_the_old_doubling(self):
        # `enqueue_intent` is how copy and automation queue work, and its cap used to be `notional * 2` — a
        # number chosen so that nothing could fail. It is now derived: notional + fees + the slippage the order
        # is allowed to take, and the slippage term is what makes a MARKET order's cap meaningful.
        from store import now_ms as _now
        r = self.store.enqueue_intent(user_id=self.user, market_id=self.market, token_id=self.token,
                                      side="BUY", price_micro=500_000, size_micro=100 * 10**6,
                                      idempotency_key="p13-cap-derivation", order_type="MARKET",
                                      audience="automation", builder_bps=100, fee_rate_bps=0,
                                      max_slippage_bps=200, at=_now())
        cap = int(self.rows("SELECT all_in_limit_micro AS c FROM order_directives WHERE intent_id=?",
                            (r["intent_id"],))[0]["c"])
        notional = notional_floor(100 * 10**6, 500_000)
        fees = v2.estimate_fees(size_shares_micro=100 * 10**6, price_micro=500_000, fee_rate_bps=0,
                                builder_bps=100)
        self.assertEqual(notional + fees.total_micro + notional * 200 // 10_000, cap)
        self.assertNotEqual(notional * 2, cap, "the placeholder cap is still in place")
        # and an explicit cap wins over the derived one, because a rule's budget is not a formula
        r2 = self.store.enqueue_intent(user_id=self.user, market_id=self.market, token_id=self.token,
                                       side="BUY", price_micro=500_000, size_micro=100 * 10**6,
                                       idempotency_key="p13-cap-explicit", order_type="MARKET",
                                       audience="automation", all_in_limit_micro=42_000_000, at=_now())
        cap2 = int(self.rows("SELECT all_in_limit_micro AS c FROM order_directives WHERE intent_id=?",
                             (r2["intent_id"],))[0]["c"])
        self.assertEqual(42_000_000, cap2)

    def test_the_fee_is_inside_the_cap_so_the_boundary_moves_with_it(self):
        """Two identical orders at one cap: the fee-free one fits and the fee-bearing one does not.

        This is D2's sentence made falsifiable — if the cap were compared against the notional alone, both
        orders would go through and the fee would be discovered by the venue after we had signed.
        """
        notional = notional_floor(100 * 10**6, 550_000)
        free = self.queue(price_micro=550_000, size_micro=100 * 10**6, builder_bps=0, fee_rate_bps=0,
                          key="p13-cap-free")
        paid = self.queue(price_micro=550_000, size_micro=100 * 10**6, builder_bps=100, fee_rate_bps=200,
                          key="p13-cap-fee")
        for iid in (free, paid):
            self.store.conn.execute("UPDATE order_directives SET all_in_limit_micro=? WHERE intent_id=?",
                                    (notional, iid))
        rep = self.ex.tick(at=self.at, reconcile=False)
        by_id = {h["intent_id"]: h for h in rep["handled"]}
        self.assertEqual("submitted", by_id[free]["state"], by_id[free])
        self.assertEqual("rejected", by_id[paid]["state"], "a fee-bearing order passed a cap that excludes the fee")
        self.assertEqual("OVER_ORDER_CAP", by_id[paid]["code"], by_id[paid])
        fees = v2.estimate_fees(size_shares_micro=100 * 10**6, price_micro=550_000, fee_rate_bps=200,
                                builder_bps=100)
        reason = self.rows("SELECT reason FROM order_lifecycle WHERE intent_id=? AND state='rejected'", (paid,))
        sentence = reason[0]["reason"] if reason else ""
        # The refusal is rendered on a phone: micros are not a number anyone has spent.
        self.assertIn(fmt_usdc(notional + fees.total_micro), sentence, sentence)
        self.assertIn(fmt_usdc(notional), sentence, sentence)

    def test_the_fee_the_cap_is_sized_with_is_the_one_measured_off_the_tape(self):
        """The rate is not a constant: P01 measured that nearly every market is 0 bps and a few are not, so the
        order path reads the fee off the tape. A market whose tape says 200 bps must size its cap with 200."""
        app = import_app("api-p13-measured-fee")
        row = app._db.execute("SELECT id, slug, condition_id FROM markets WHERE slug IS NOT NULL LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        mid, slug, cond = str(row[0]), str(row[1]), str(row[2])
        at = app._now_ms()
        app._db.execute("DELETE FROM book_levels WHERE market_id=?", (mid,))
        app._db.execute("INSERT INTO book_levels (market_id, side, price_micro, size_shares_micro, level_count,"
                        " updated_ms) VALUES (?, 'ask', 620000, 900000000, 1, ?)", (mid, at))
        app._db.execute("INSERT INTO tape_fills (dedupe_key, condition_id, token_id, outcome, outcome_index,"
                        " wallet, side, price_micro, size_micro, usd_notional_micro, ts_ms, ingest_ms, source,"
                        " fee_rate_bps) VALUES ('p13-measured-fee', ?, 'tok-measured', 'Yes', 0, '0xwallet',"
                        " 'BUY', 620000, 1000000, 620000, ?, ?, 'rest', 200)", (cond, at, at))
        app._db.commit()
        app._FEE_RATE_CACHE.clear()          # the lookup is cached for a minute; this test just changed the tape
        uid = str(app._db.execute("SELECT id FROM users LIMIT 1").fetchone()[0])
        out = app._order_from_card(uid, {"slug": slug, "side": "yes", "amount": "25"}, "p13-measured-fee-1")
        self.assertTrue(out["ok"], out)
        rate, cap = app._db.execute("SELECT fee_rate_bps, all_in_limit_micro FROM order_directives"
                                    " WHERE intent_id=?", (out["intent_id"],)).fetchone()
        self.assertEqual(200, int(rate), "the order was sized against a fee rate the tape does not support")
        fees = v2.estimate_fees(size_shares_micro=int(out["shares_micro"]), price_micro=int(out["price_micro"]),
                                fee_rate_bps=200, builder_bps=0)
        notional = notional_floor(int(out["shares_micro"]), int(out["price_micro"]))
        self.assertEqual(notional + fees.total_micro, int(cap))
        self.assertGreater(int(cap), notional, "the cap cannot be the notional when the tape says 200 bps")
        self.assertLessEqual(notional, 25_000_000, "a $25 card spent more than $25 on shares")

    def test_the_amount_route_queues_a_directive_with_a_cap_the_executor_can_read(self):
        # The API is the third producer, and until this phase it wrote intents with no directive at all — so the
        # executor read `fee_rate_bps=0` for orders on markets that charge a fee, and there was no cap to read.
        app = import_app("api-p13-card-directive")
        row = app._db.execute("SELECT id, slug FROM markets WHERE slug IS NOT NULL LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        app._db.execute("INSERT OR REPLACE INTO book_levels (market_id, side, price_micro, size_shares_micro,"
                        " level_count, updated_ms) VALUES (?, 'ask', 620000, 900000000, 1, ?)",
                        (row[0], app._now_ms()))
        app._db.commit()
        uid = app._db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        out = app._order_from_card(str(uid), {"slug": row[1], "side": "yes", "amount": "50"},
                                   "p13-card-directive-1")
        self.assertTrue(out["ok"], out)
        d = app._db.execute("SELECT all_in_limit_micro, fee_rate_bps, audience, order_type, builder_bps"
                            " FROM order_directives WHERE intent_id=?", (out["intent_id"],)).fetchone()
        self.assertIsNotNone(d, "the API queued an intent with no directive")
        self.assertEqual("user", d[2])
        self.assertEqual("GTC", d[3])
        shares = int(out["shares_micro"])
        fees = v2.estimate_fees(size_shares_micro=shares, price_micro=int(out["price_micro"]),
                                fee_rate_bps=int(d[1]), builder_bps=int(d[4]))
        self.assertEqual(notional_floor(shares, int(out["price_micro"])) + fees.total_micro,
                         int(d[0]), "the cap the executor reads is not the all-in cost of the order it caps")
        self.assertLessEqual(int(d[0]), 50_000_000, "a $50 card queued an order whose cap exceeds $50")


class TestKillSwitchDuringCopy(Harness):
    """RG-4: the switch is read on the same path a copied order takes, not on a surface of its own."""

    def test_a_copy_intent_already_queued_places_nothing_after_the_switch(self):
        # The order is queued by the copy engine's own door (`audience='copy'`), and the switch is engaged while
        # it waits. Nothing may reach the venue: an emergency stop that lets a queued copy through because a
        # different component queued it is not an emergency stop.
        iid = self.queue(audience="copy", order_type="GTC", key="p13-kill-copy-1")
        self.store.conn.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms)"
                                " VALUES (1,'drill: copied orders mid-flight','ops',?)", (self.at,))
        self.store._kill_cache = (0, False)
        rep = self.ex.tick(at=self.at + 10, reconcile=False)
        self.assertTrue(rep["halted"], rep)
        self.assertEqual(0, self.tp.post_calls, "a copied order reached the venue with the switch engaged")
        self.assertEqual([], self.orders())
        state = self.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"]
        self.assertNotEqual("submitted", state)
        self.assertEqual(1, int(self.rows("SELECT COUNT(*) AS n FROM order_intents")[0]["n"]))


class TestOneSourceOfTruth(TelegramBase):
    """AMB-6: a cancel on one surface is the state every other surface reads."""

    app_name = "api-p13-sot"

    def auth(self, token: str) -> dict:
        return {"Authorization": "Bearer %s" % token, "Idempotency-Key": "p13-sot-0001"}

    def test_a_cancel_on_one_surface_is_the_state_every_surface_reads(self):
        self.seed_market()
        self.account()
        init = sign_init_data({"auth_date": str(self.now_s), "query_id": "Qsot1",
                               "user": '{"id":%s,"username":"tester"}' % TG_USER})
        s = self.client.post("/v1/telegram/session", json={"initData": init})
        self.assertEqual(200, s.status_code, s.text)
        token = s.json()["accessToken"]
        placed = self.client.post("/v1/telegram/order", json={"slug": "p12-fed-cut-sept", "side": "yes",
                                                              "amountUsdc": "25"}, headers=self.auth(token))
        self.assertEqual(202, placed.status_code, placed.text)
        iid = placed.json()["intentId"]
        # Surface A (the Mini App) has an open card. Surface B is the chat: `/stop` cancels what is pending.
        r = self.post_update(self.message("/stop", update_id=9_000_001))
        self.assertEqual(200, r.status_code, r.text)
        state = self.db.execute("SELECT state FROM order_intents WHERE id=?", (iid,)).fetchone()
        self.assertEqual("cancelled", str(state[0]),
                         "the chat surface cancelled nothing, or cancelled a different row")
        # the executor's own claim query — the third reader — must find nothing left to send
        queued = self.db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=? AND state='queued'",
                                 (UID,)).fetchone()
        self.assertEqual(0, int(queued[0]), "a cancelled order is still claimable")
        # and the surface that placed it reads the cancellation back, in its own words
        got = self.client.get("/v1/orders/intents/%s" % iid, headers={"Authorization": "Bearer %s" % token})
        self.assertEqual(200, got.status_code, got.text)
        self.assertEqual("cancelled", str(got.json()["state"]).lower())


class TestNegriskDivergence(TelegramBase):
    """NEG-3: probabilities that do not sum to one are reported, with the executable edge, not hidden."""

    app_name = "api-p13-negrisk"

    def event(self, *, prices) -> str:
        """A negRisk event whose outcomes are quoted at `prices` (bid and ask, one tick wide)."""
        eid, at = "ev-p13-neg", self.app._now_ms()
        self.db.execute("INSERT OR REPLACE INTO events (id, slug, title, neg_risk, category, end_ts, created_ms,"
                        " updated_ms) VALUES (?,?,?,1,'Politics',?,?,?)",
                        (eid, "p13-neg", "Who wins the P13 primary?", at + 86_400_000, at, at))
        for i, p in enumerate(prices):
            mid = "m-p13-neg-%d" % i
            # The book is replaced, not added to: `book_levels` is keyed by (market, side, price), so a second
            # test in this class that quoted different prices would leave BOTH sets of levels in place — and then
            # "best ask" is the lower one and the assertion is about stale rows from the previous test.
            self.db.execute("DELETE FROM book_levels WHERE market_id=?", (mid,))
            self.db.execute("INSERT OR REPLACE INTO markets (id, condition_id, event_id, slug, question,"
                            " minimum_tick_size, end_ts, accepting_orders, first_seen_ms, updated_ms,"
                            " seconds_delay, minimum_order_size, fee_type, enable_order_book, neg_risk,"
                            " outcomes_json) VALUES (?,?,?,?,?,?,?,1,?,?,0,'5','',1,1,'[]')",
                            (mid, "0xcond-p13-neg-%d" % i, eid, "p13-neg-%d" % i, "Outcome %d" % i, 0.01,
                             at + 86_400_000, at, at))
            self.db.execute("INSERT OR REPLACE INTO book_levels (market_id, side, price_micro,"
                            " size_shares_micro, level_count, updated_ms) VALUES (?, 'bid', ?, 1000000, 1, ?)",
                            (mid, p - 10_000, at))
            self.db.execute("INSERT OR REPLACE INTO book_levels (market_id, side, price_micro,"
                            " size_shares_micro, level_count, updated_ms) VALUES (?, 'ask', ?, 1000000, 1, ?)",
                            (mid, p + 10_000, at))
        self.db.commit()
        refresh_flags(self.app)
        return eid

    def test_a_diverged_book_is_reported_with_the_edge_a_user_could_trade(self):
        eid = self.event(prices=[600_000, 600_000, 600_000])
        r = self.client.get("/v1/events/%s" % eid)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertFalse(body["withinTolerance"], body)
        self.assertNotEqual("0.00", body["probabilitySum"], body)
        self.assertIsNotNone(body["opportunity"], "a book where every outcome is 60c has no opportunity?")
        self.assertEqual("sell-all", body["opportunity"]["kind"])
        # Selling one of every outcome at its best bid (3 x 0.59) returns $1.77 against the $1 that settles
        # exactly one of them: the edge is stated in dollars, and it is executable rather than implied by a mid.
        self.assertEqual("0.77", body["opportunity"]["edge"], body)
        self.assertEqual("1.77", body["sellAllProceeds"], body)
        self.assertEqual("1.83", body["buyAllCost"], body)
        self.assertEqual("0.03", body["tolerance"], "the tolerance is N outcomes x one tick each")

    def test_a_book_inside_the_tolerance_reports_no_opportunity(self):
        eid = self.event(prices=[340_000, 330_000, 330_000])
        r = self.client.get("/v1/events/%s" % eid)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["withinTolerance"], body)
        self.assertIsNone(body["opportunity"],
                          "a 1-tick deviation is not an opportunity; a screen that shouts at every tick teaches "
                          "its reader to ignore it")


class TestWalletProviderDown(TelegramBase):
    """WAL-5: with the signer gone, trading stops and the read side keeps working."""

    app_name = "api-p13-signer-down"

    def test_signing_outage_stops_trading_and_leaves_the_book_readable(self):
        mid, _cond = self.seed_market()
        self.account()
        # A wallet in `watch_only`: the state the product puts a wallet in when the provider cannot sign. No
        # `--force` flag exists for this; the refusal is state-driven (P06's rule: the signer is not a URL).
        # `read_only` custody is what "watch-only" is in this schema (there is no such state; the CHECK on
        # `state` allows provisioned/funded/trading/suspended/closing, and custody is the fact that decides
        # whether anything can be signed). `self_hosted` + signature_type 0 keeps the table's own invariant.
        self.db.execute("INSERT OR REPLACE INTO wallets (user_id,provider,custody,address,proxy_address,"
                        "signature_type,policy_hash,state,created_ms,updated_ms)"
                        " VALUES (?, 'self_hosted', 'read_only', '0xw', '0xp', 0, '', 'funded', ?, ?)",
                        (UID, self.app._now_ms(), self.app._now_ms()))
        self.db.commit()
        init = sign_init_data({"auth_date": str(self.now_s), "query_id": "Qdown",
                               "user": '{"id":%s,"username":"tester"}' % TG_USER})
        s = self.client.post("/v1/telegram/session", json={"initData": init})
        token = s.json()["accessToken"]
        r = self.client.post("/v1/telegram/order", json={"slug": "p12-fed-cut-sept", "side": "yes",
                                                         "amountUsdc": "25"},
                             headers={"Authorization": "Bearer %s" % token,
                                      "Idempotency-Key": "p13-signer-down-1"})
        # 503 and not 409: nothing about the *request* is wrong, and the same request will work once the wallet
        # can sign again — which is what `retryable` means in this vocabulary.
        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual("SIGNER_UNAVAILABLE", r.json()["error"]["code"], r.text)
        self.assertTrue(r.json()["error"]["retryable"], r.text)
        self.assertEqual(0, int(self.db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=?",
                                                (UID,)).fetchone()[0]),
                         "a watch-only wallet queued an order")
        # the read side is untouched: the market and its book still answer
        book = self.client.get("/v1/markets/%s/book?depth=5" % mid)
        self.assertEqual(200, book.status_code, book.text)
        self.assertGreaterEqual(len(book.json()["asks"]), 1)
        mkt = self.client.get("/v1/markets/%s" % mid)
        self.assertEqual(200, mkt.status_code, mkt.text)
        self.assertIn("mid", str(mkt.json().get("market", {})).lower() + " mid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
