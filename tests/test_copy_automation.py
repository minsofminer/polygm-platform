"""P06 D5 (copy) and D6 (automation): the decision logic, the refusals, and the run history.

Two standards apply to every test here:

* **A refusal must be a fact in the database, not an absence.** A skipped copy and a skipped rule both leave a
  row with a reason; a test that only asserted "no order was created" would pass when the code forgets to
  write the log, which is the failure mode that kills an automation product (nobody notices for three days).
* **The money numbers are integers, asserted exactly.** Track-record drawdown, break-even win rate, and copy
  sizing are all computed in micros, so every assertion here is a literal integer, not a float comparison.

`Harness` is imported from the executor suite on purpose: the copy and automation engines have no venue
client of their own (they write *intents*), so the only way to test D5.5's claim — "a copied order passes the
same risk gate as a human order" — is to run it through the executor's real queue. A second, simpler harness
here would quietly test a copy engine that had no gate to pass.
"""
from __future__ import annotations

import unittest

from test_executor_service import Harness                                       # noqa: F402  (shared fixture)

from polygm_core.automation import engine as au
from polygm_core.copy import engine as cp
from polygm_core.dbtypes import TimeColumnError, seconds_until, to_epoch_ms     # noqa: F402
from polygm_core.money.cents import notional_floor                              # noqa: F402


def fill(**kw) -> cp.SourceFill:
    base = dict(wallet="u-src", intent_id="i-src-1", token_id="t-1", market_id="0xM1", side="BUY",
                price_micro=450_000, size_shares_micro=200 * 10**6, at_ms=1_000_000)
    base.update(kw)
    return cp.SourceFill(**base)


def quote(**kw) -> cp.Quote:
    base = dict(best_bid_micro=440_000, best_ask_micro=452_000, age_ms=100, stale_ms=3_000)
    base.update(kw)
    return cp.Quote(**base)


# ============================================================================== D5: what gets copied ==
class TestCopyDecisions(unittest.TestCase):
    def cfg(self, **kw) -> cp.CopyConfig:
        base = dict(id="c-1", user_id="u-demo", source_user="u-src", mode="ratio", ratio_bps=5_000)
        base.update(kw)
        return cp.CopyConfig(**base)

    def decide(self, cfg, f, *, q=None, m=None, day_spent=0, day_copies=0, last=0, now=1_000_001,
               source_state="active"):
        return cp.decide(cfg, f, quote=q or quote(), market=m or cp.MarketClock(True, 3_600),
                         day_spent_micro=day_spent, day_copies=day_copies, last_copy_ms=last, now_ms=now,
                         source_state=source_state)

    def test_copies_at_the_ask_not_at_the_mid(self):
        # The copier pays the ask. A product that quotes the mid here is showing the user a price they cannot
        # get, and the deviation check below would be measuring against a fiction.
        d = self.decide(self.cfg(max_order_micro=100_000_000), fill())
        self.assertTrue(d.copied)
        self.assertEqual(d.entry_price_micro, 452_000, "no tick given, so no alignment")
        self.assertEqual(d.size_shares_micro, 100 * 10**6)          # 200 shares x 50% ratio
        self.assertEqual(d.notional_micro, notional_floor(100 * 10**6, 452_000))
        self.assertEqual(d.latency_ms, 1)

    def test_the_per_trade_cap_shrinks_the_copy_and_never_the_other_way(self):
        # 200 source shares at 45.2c is $45.20; the config's $25 ceiling must bind, in SHARES, measured at
        # the price WE pay and floored, because `notional_floor` rounds up and scaling by the ratio would
        # then land above the cap the user set.
        d = self.decide(self.cfg(max_order_micro=25_000_000), fill())
        self.assertTrue(d.copied)
        self.assertLessEqual(d.notional_micro, 25_000_000)
        self.assertEqual(d.size_shares_micro, 55_309_734)
        self.assertLessEqual(d.notional_micro, 25_000_000)

    def test_deviation_over_the_bound_is_a_skip_and_never_a_smaller_order(self):
        cfg = self.cfg(max_entry_deviation_bps=150)
        f = fill(price_micro=400_000)                               # ask 452000 is 13% above their fill
        d = self.decide(cfg, f)
        self.assertEqual(d.action, "skipped")
        self.assertTrue(d.reason.startswith("deviation:"), d.reason)
        self.assertEqual(d.deviation_bps, 1300)
        self.assertEqual(d.size_shares_micro, 0, "a chased copy must not be quietly resized")

    def test_an_improved_price_is_reported_as_negative_deviation_and_copies(self):
        d = self.decide(self.cfg(), fill(price_micro=470_000), q=quote(best_bid_micro=440_000,
                                                                       best_ask_micro=452_000))
        self.assertTrue(d.copied)
        self.assertEqual(d.deviation_bps, -383, "rounded half-away-from-zero, so the improvement is not "
                                                "flattered into -382 by a floor divide")

    def test_no_quote_and_stale_quote_are_both_refused(self):
        self.assertEqual(self.decide(self.cfg(), fill(), q=quote(best_bid_micro=None,
                                                                best_ask_micro=None)).reason, "no_quote")
        self.assertEqual(self.decide(self.cfg(), fill(), q=quote(age_ms=3_001)).reason, "stale_quote:3001ms")

    def test_never_copies_into_a_market_about_to_resolve(self):
        d = self.decide(self.cfg(), fill(), m=cp.MarketClock(True, 899))
        self.assertEqual(d.reason, "resolving_soon:899s")
        # An unknown clock is worse than a short one, and the engine reads it the same way (-1 < 900).
        self.assertEqual(self.decide(self.cfg(), fill(), m=cp.MarketClock(True, -1)).reason,
                         "resolving_soon:-1s")

    def test_a_market_not_accepting_orders_is_its_own_reason(self):
        self.assertEqual(self.decide(self.cfg(), fill(), m=cp.MarketClock(False, 3_600)).reason,
                         "market_not_accepting")

    def test_blocked_market_min_interval_and_paused_are_each_visible(self):
        self.assertEqual(self.decide(self.cfg(blocked_markets=("0xM1",)), fill()).reason, "blocked_market")
        self.assertEqual(self.decide(self.cfg(min_interval_ms=60_000), fill(), last=950_000).reason,
                         "min_interval")
        self.assertEqual(self.decide(self.cfg(paused_reason="insider_flagged"), fill()).reason,
                         "paused:insider_flagged")
        self.assertEqual(self.decide(self.cfg(enabled=False), fill()).reason, "disabled")

    def test_source_stopped_and_insider_flagged_stop_new_copies(self):
        self.assertEqual(self.decide(self.cfg(), fill(), source_state="stopped").reason, "source_stopped")
        self.assertEqual(self.decide(self.cfg(), fill(), source_state="insider_flagged").reason,
                         "insider_flagged")

    def test_max_price_bound_uses_the_entry_price_not_the_source_price(self):
        cfg = self.cfg(max_price_micro=450_000)
        self.assertEqual(self.decide(cfg, fill()).reason, "price_bound:452000>450000")

    def test_a_copied_limit_is_aligned_onto_the_markets_tick(self):
        # The ask is 452000 and the market's grid is 0.01, so 452000 cannot be sent. Floored for a BUY (never
        # rounded up: that would offer more than the book asked), and the deviation bound is then measured
        # against the price we would actually pay.
        d = self.decide(self.cfg(), fill(), m=cp.MarketClock(True, 3_600, tick_micro=10_000))
        self.assertEqual(d.entry_price_micro, 450_000)
        self.assertEqual(list(d.notes), ["tick-aligned 452000->450000 (tick 10000)"])
        self.assertEqual(d.deviation_bps, 0)
        sell = self.decide(self.cfg(), fill(side="SELL"),
                          m=cp.MarketClock(True, 3_600, tick_micro=10_000))
        self.assertEqual(sell.entry_price_micro, 440_000, "the bid 440000 is already on the grid, so it is "
                                                           "left alone")
        up = self.decide(self.cfg(), fill(side="SELL"), q=quote(best_bid_micro=441_000, best_ask_micro=452_000),
                        m=cp.MarketClock(True, 3_600, tick_micro=10_000))
        self.assertEqual(up.entry_price_micro, 450_000, "a SELL is ceiled onto the grid: we never accept "
                                                         "less than the book offered, even to get filled")
        near = self.decide(self.cfg(), fill(side="SELL"),
                          q=quote(best_bid_micro=999_000, best_ask_micro=999_500),
                          m=cp.MarketClock(True, 3_600, tick_micro=10_000))
        self.assertEqual(near.reason, "bad_entry_price:1000000", "a SELL ceil must not invent a $1.00 price")
        # and an on-grid ask that is BELOW the copier's max price is untouched: alignment must never be the
        # reason an order that was legal becomes illegal.
        fine = self.decide(self.cfg(max_price_micro=450_000), fill(),
                          q=quote(best_bid_micro=440_000, best_ask_micro=449_000),
                          m=cp.MarketClock(True, 3_600, tick_micro=10_000))
        self.assertEqual(fine.entry_price_micro, 440_000, "449000 floors to 440000, which is also the bid")
        self.assertEqual(fine.action, "copied")

    def test_daily_and_count_caps_win_over_the_price_checks(self):
        # Order matters: a user who is capped out must be told "daily cap", not shown a price excuse.
        self.assertEqual(self.decide(self.cfg(), fill(), day_spent=250_000_000).reason, "daily_cap")
        self.assertEqual(self.decide(self.cfg(), fill(), day_copies=20).reason, "daily_count")

    def test_mirror_mode_is_still_bound_by_the_per_trade_cap(self):
        # mirror is the mode that turns a whale into a margin call: 200 source shares at 50c is $100, and the
        # config's $25 ceiling must bind, sized DOWN in shares and never up in notional.
        cfg = self.cfg(mode="mirror", max_order_micro=25_000_000)
        size, why = cp.size_for(cfg, fill(price_micro=500_000), remaining_daily_micro=10**12)
        self.assertEqual(size, 50 * 10**6, why)
        self.assertLessEqual(notional_floor(size, 500_000), 25_000_000)

    def test_cap_mode_is_a_dollar_cap_not_a_share_cap(self):
        # $10 at 50c is 20 shares. Reading max_order_micro as a share count (an earlier draft of size_for did
        # exactly that) would have copied half a position on this market and ten shares on a 10c one.
        capped = self.cfg(mode="cap", max_order_micro=10 * 10**6)
        self.assertEqual(cp.size_for(capped, fill(price_micro=500_000),
                                    remaining_daily_micro=10**12)[0], 20 * 10**6)
        self.assertEqual(cp.size_for(capped, fill(price_micro=100_000),
                                    remaining_daily_micro=10**12)[0], 100 * 10**6)

    def test_a_daily_remainder_smaller_than_a_share_is_zero_not_one(self):
        tiny = self.cfg(mode="ratio", ratio_bps=1)
        size, why = cp.size_for(tiny, fill(price_micro=900_000), remaining_daily_micro=1)
        self.assertEqual(size, 0, why)
        self.assertIn("cap", why)

    def test_a_sub_share_ratio_refuses_rather_than_rounding_up_or_down_into_noise(self):
        # 0.01% of half a share is 50 micro-shares: not a position anyone can close, merge or redeem. The
        # engine refuses it (a signed order for nothing, plus a lifecycle trail, plus a venue round trip) and
        # says which cap bound it. Below the VENUE's minimum is the gate's job, and the gate still gets the
        # order when the size is a whole share — see the queue test for that half.
        size, why = cp.size_for(self.cfg(ratio_bps=1), fill(size_shares_micro=500_000, price_micro=500_000),
                               remaining_daily_micro=10**12)
        self.assertEqual(size, 0, why)
        self.assertIn("below one share", why)
        d = cp.decide(self.cfg(ratio_bps=1), fill(size_shares_micro=500_000, price_micro=500_000),
                      quote=quote(), market=cp.MarketClock(True, 3_600), day_spent_micro=0, day_copies=0,
                      last_copy_ms=0, now_ms=1_000_001)
        self.assertEqual((d.action, d.reason.split(":")[0]), ("skipped", "zero_size"), d.reason)
        self.assertEqual(d.size_shares_micro, 0)
        # One whole share survives the floor and goes to the queue, where the market's own minimum decides
        # (10% of 10 shares = 1 share exactly, so the guard binds at the boundary and not before it).
        ok, why2 = cp.size_for(self.cfg(ratio_bps=1_000),
                              fill(size_shares_micro=10 * 10**6, price_micro=500_000),
                              remaining_daily_micro=10**12)
        self.assertEqual(ok, 10**6, why2)

    def test_the_same_source_fill_cannot_be_copied_twice(self):
        cfg = self.cfg()
        f = fill()
        self.assertEqual(cp.idempotency_key_for(cfg, f), cp.idempotency_key_for(cfg, f))
        self.assertNotEqual(cp.idempotency_key_for(cfg, f), cp.idempotency_key_for(cfg, fill(price_micro=1)))

    def test_every_skip_reason_is_declared(self):
        # `EngineStats.skip` asserts this at runtime; the assertion here is that the list is the same one the
        # docs and the UI iterate over, so a new reason cannot be added without being written down.
        seen = set()
        for f, q, m, kw in [
            (fill(), quote(), cp.MarketClock(True, 3_600), {"cfg": self.cfg(enabled=False)}),
            (fill(), quote(), cp.MarketClock(True, 3_600), {"cfg": self.cfg(paused_reason="x")}),
            (fill(), quote(), cp.MarketClock(True, 3_600), {"cfg": self.cfg(), "source_state": "stopped"}),
            (fill(), quote(), cp.MarketClock(False, 3_600), {"cfg": self.cfg()}),
            (fill(), quote(), cp.MarketClock(True, 1), {"cfg": self.cfg()}),
            (fill(), quote(best_bid_micro=None, best_ask_micro=None), cp.MarketClock(True, 3_600),
             {"cfg": self.cfg()}),
            (fill(price_micro=100_000), quote(), cp.MarketClock(True, 3_600), {"cfg": self.cfg()}),
            (fill(), quote(), cp.MarketClock(True, 3_600), {"cfg": self.cfg(), "day_spent": 250_000_000}),
            (fill(), quote(), cp.MarketClock(True, 3_600), {"cfg": self.cfg(), "day_copies": 20}),
        ]:
            d = cp.decide(kw.pop("cfg"), f, quote=q, market=m, source_state=kw.pop("source_state", "active"),
                          day_spent_micro=kw.pop("day_spent", 0), day_copies=kw.pop("day_copies", 0),
                          last_copy_ms=0, now_ms=1_000_001)
            self.assertEqual(d.action, "skipped")
            seen.add(d.reason.split(":")[0])
        self.assertTrue(seen <= set(cp.SKIP_REASONS), sorted(seen - set(cp.SKIP_REASONS)))
        self.assertGreaterEqual(len(seen), 8, "the refusal table is thinner than the code's branches")


class TestTrackRecord(unittest.TestCase):
    def test_empty_record_is_not_a_hundred_percent_win_rate(self):
        s = cp.track_record([])
        self.assertEqual(s["closed_trades"], 0)
        self.assertEqual(s["win_rate_bp"], 0)
        self.assertIn("no closed trades", s["sample"])

    def test_drawdown_streaks_and_net_after_fees(self):
        t = [cp.ClosedTrade(realized_micro=10_000_000, fee_micro=1_000_000),      # +9
             cp.ClosedTrade(realized_micro=-8_000_000, fee_micro=1_000_000),       # -9
             cp.ClosedTrade(realized_micro=-5_000_000, fee_micro=1_000_000),       # -6
             cp.ClosedTrade(realized_micro=20_000_000, fee_micro=1_000_000)]       # +19
        s = cp.track_record(t, source_user_id="u-src")
        self.assertEqual(s["closed_trades"], 4)
        self.assertEqual(s["realized_pnl_micro"], 17_000_000)
        self.assertEqual(s["fees_micro"], 4_000_000)
        self.assertEqual(s["net_after_fees_micro"], 13_000_000)
        self.assertEqual(s["win_rate_bp"], 5_000)
        # equity: 9,0,-6,13 (millions) -> peak 9, trough -6 -> drawdown 15
        self.assertEqual(s["max_drawdown_micro"], 15_000_000)
        self.assertEqual(s["longest_losing_streak"], 2)
        self.assertEqual(s["drawdown_over_profit"], 1)

    def test_a_win_before_fees_that_loses_after_them_is_recorded_as_a_loss(self):
        # The venue fee and our builder fee are what make a copied strategy unprofitable, and a record that
        # scored this as a win is the number a copier would make a decision on.
        s = cp.track_record([cp.ClosedTrade(realized_micro=1_000_000, fee_micro=1_500_000)])
        self.assertEqual(s["win_rate_bp"], 0)
        self.assertEqual(s["net_after_fees_micro"], -500_000)

    def test_latency_is_measured_from_the_venue_timestamp(self):
        s = cp.track_record([cp.ClosedTrade(realized_micro=1, fee_micro=0, source_fill_ms=1_000,
                                           our_fill_ms=2_500)])
        self.assertEqual(s["avg_latency_ms"], 1_500)

    def test_required_disclosure_fields_exist_on_every_record(self):
        for s in (cp.track_record([]), cp.track_record([cp.ClosedTrade(realized_micro=5, fee_micro=1)])):
            for f in cp.REQUIRED_DISCLOSURE:
                self.assertIn(f, s)


class TestCopyChain(unittest.TestCase):
    def test_copying_a_copier_is_refused_at_depth_two(self):
        cfgs = [{"user_id": "a", "source_user": "b", "enabled": True},
                {"user_id": "b", "source_user": "c", "enabled": True},
                {"user_id": "c", "source_user": "d", "enabled": True}]
        r = cp.walk_chain(cfgs, "a", "b", max_hops=1)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["code"], "COPY_DEPTH")
        self.assertGreaterEqual(r["hop_depth"], 2)

    def test_a_two_node_cycle_is_refused_however_shallow(self):
        cfgs = [{"user_id": "a", "source_user": "b", "enabled": True},
                {"user_id": "b", "source_user": "a", "enabled": True}]
        r = cp.walk_chain(cfgs, "a", "b")
        self.assertTrue(r["cycle"])
        self.assertEqual(r["code"], "COPY_CYCLE")
        self.assertFalse(r["allowed"])

    def test_a_source_who_copies_nobody_is_one_hop_and_allowed(self):
        # `configs` is the whole copy graph. b appears in nobody's config, so a copying b is one hop deep.
        r = cp.walk_chain([{"user_id": "z", "source_user": "y", "enabled": True}], "a", "b", max_hops=1)
        self.assertTrue(r["allowed"])
        self.assertEqual(r["hop_depth"], 1)

    def test_a_source_who_is_himself_a_copier_is_refused(self):
        # b copies c, so a is two hops from a market-moving decision. This is the refusal the phase's rule
        # exists for: the last hop pays every earlier hop's slippage.
        r = cp.walk_chain([{"user_id": "b", "source_user": "c", "enabled": True}], "a", "b", max_hops=1)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["code"], "COPY_DEPTH")


class TestCopyEconomics(unittest.TestCase):
    def test_a_positive_source_record_can_still_be_a_negative_product(self):
        # 400 copiers, $100k of volume, a source that made $2k after fees, our 100 bps builder fee plus the
        # venue's taker fee: the copiers are net-negative as a group even though the strategy "won".
        stats = cp.track_record([cp.ClosedTrade(realized_micro=6_000_000, fee_micro=4_000_000)])
        e = cp.economics("u-src", stats, copier_count=400, copier_volume_micro=100_000 * 10**6,
                        builder_bps=100, fee_rate_bps=70)
        self.assertEqual(e["fees_micro"], 700_000_000)
        self.assertEqual(e["builder_fees_micro"], 1_000_000_000)
        self.assertTrue(e["verdict"].startswith("negative"))
        self.assertEqual(e["median_volume_per_copier_micro"], 250_000_000),  # $100k / 400 = $250 each
        self.assertTrue(e["shown_to_users"])

    def test_zero_copiers_does_not_divide_by_zero(self):
        e = cp.economics("u-src", {}, copier_count=0, copier_volume_micro=0, builder_bps=100)
        self.assertEqual(e["median_volume_per_copier_micro"], 0)


# ==================================================================== D5 against the real queue =======
class TestCopyEngineAgainstTheQueue(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.at = 1_789_000_000_000
        # The seed's 0xM1 ladder sits at 0.94-0.97 and its best ask (965000) is NOT on that market's 0.01
        # tick, so inheriting it would make every copied order an OFF_TICK refusal and hide the thing under
        # test. A tight on-grid book at 49/50 is written here instead: a fixture that sets its own market
        # data is a fixture that keeps testing what it says it tests.
        self.store.conn.execute("DELETE FROM book_levels WHERE market_id=?", (self.market,))
        for side, price in (("bid", 490_000), ("ask", 500_000)):
            self.store.conn.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,"
                                    "level_count,updated_ms) VALUES (?,?,?,?,?,?)",
                                    (self.market, side, price, 100 * 10**6, 1, self.at))
        self.store.conn.execute("UPDATE markets SET end_ts=? WHERE id=?", (self.at // 1000 + 86_400,
                                                                          self.market))
        self.eng = cp.CopyEngine(self.store, builder_bps=100, fee_rate_bps=0)

    config_id = "c-1"

    def add_config(self, *, source: str = "u-src", mode: str = "ratio", ratio_bps: int = 5_000,
                   max_order: int = 25_000_000, max_daily: int = 250_000_000, enabled: bool = True,
                   config_id: str = "c-1", deviation: int = 150, tp: int | None = None,
                   sl: int | None = None, min_secs: int = 900) -> None:
        self.store.conn.execute("INSERT OR REPLACE INTO copy_configs (id,user_id,source_user,mode,ratio_bps,"
                                "max_order_micro,max_daily_micro,blocked_markets,enabled,created_ms) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (config_id, self.user, source, mode, ratio_bps, max_order, max_daily, "[]",
                                 1 if enabled else 0, self.at))
        self.store.save_copy_config_policy(config_id=config_id, at=self.at, max_entry_deviation_bps=deviation,
                                          take_profit_bp=tp, stop_loss_bp=sl, min_seconds_to_resolution=min_secs)

    def src_fill(self, **kw) -> cp.SourceFill:
        # The source's fill is placed just under the real best ask, so that these tests exercise sizing, caps
        # and idempotency rather than tripping the deviation bound by accident. A fixture that quietly relies
        # on the market's numbers is a fixture that fails when somebody regenerates the seed.
        # The source's fill is placed at the ask: on the market's tick grid, and at zero deviation, so these
        # tests exercise the rule in their name rather than the deviation bound by accident.
        base = dict(wallet="u-src", intent_id="i-src-9", token_id=self.token, market_id=self.market,
                    side="BUY", price_micro=500_000, size_shares_micro=10 * 10**6, at_ms=self.at + 1)
        base.update(kw)
        return cp.SourceFill(**base)

    def test_a_copied_fill_lands_in_the_queue_as_an_intent_not_an_order(self):
        self.add_config()
        out = self.eng.on_source_fill(self.src_fill(), at=self.at + 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "copied", out[0])
        rows = self.rows("SELECT * FROM order_intents")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "queued")
        self.assertEqual(rows[0]["notional_micro"], out[0]["notional_micro"])
        d = self.rows("SELECT * FROM order_directives")[0]
        self.assertEqual(d["audience"], "copy")
        self.assertEqual(d["order_type"], "FAK", "a copy is a fill-or-kill, not an order left on the book")
        self.assertEqual(d["builder_bps"], 100, "the builder code rides the copied order, or D8 has no revenue")
        self.assertEqual(self.rows("SELECT * FROM orders"), [], "copy must never reach the venue itself")

    def test_the_copy_intent_passes_the_same_gate_a_human_order_passes(self):
        # 1 share is below the market's 5-share minimum. A copy path that bypassed the gate would place it;
        # this asserts the refusal arrives from `risk.gate`, with the intent recorded and no order created.
        self.add_config(mode="mirror", ratio_bps=10_000)
        out = self.eng.on_source_fill(self.src_fill(size_shares_micro=10**6), at=self.at + 1)
        self.assertEqual([o["action"] for o in out], ["copied"], out)
        rep = self.ex.tick(at=self.at + 1, reconcile=False)
        self.assertEqual([h["state"] for h in rep["handled"]], ["rejected"])
        self.assertEqual(rep["handled"][0]["code"], "BELOW_MIN_SIZE")
        self.assertEqual(self.rows("SELECT * FROM orders"), [])
        trail = self.trail(self.rows("SELECT id FROM order_intents ORDER BY rowid")[0]["id"])
        self.assertIn("preflight", trail, "the refusal must come from the pre-flight, and be visible")
        self.assertNotIn("signing", trail, "an order refused by the gate was never signed; if it had been, "
                                           "we would have a client order id for an order the venue never saw")
        self.assertNotIn("submitted", trail)

    def test_a_replayed_source_fill_creates_exactly_one_intent(self):
        self.add_config()
        first = self.eng.on_source_fill(self.src_fill(), at=self.at + 1)
        again = self.eng.on_source_fill(self.src_fill(), at=self.at + 5)
        self.assertEqual(len(self.rows("SELECT id FROM order_intents")), 1)
        self.assertEqual(again[0]["action"], "skipped")
        self.assertTrue(again[0]["reason"].startswith("duplicate"), again[0]["reason"])
        self.assertEqual(again[0]["intent_id"], first[0]["intent_id"])
        actions = [r["action"] for r in self.rows("SELECT action FROM copy_events WHERE copier_id=? "
                                                  "ORDER BY id", (self.config_id,))]
        self.assertEqual(actions, ["copied", "skipped"],
                         "the skip is visible in this config's history, and only this config's: "
                         "`copier_id` carries the config id, and the seed's demo history is `cfg-seed01`")

    def test_a_skipped_copy_still_writes_its_reason(self):
        self.add_config(deviation=50)
        out = self.eng.on_source_fill(self.src_fill(price_micro=int(300_000)), at=self.at + 1)
        self.assertEqual(out[0]["action"], "skipped")
        self.assertTrue(out[0]["reason"].startswith("deviation"))
        self.assertEqual(self.rows("SELECT id FROM order_intents"), [])
        ev = self.rows("SELECT action,reason,deviation_bps FROM copy_events WHERE copier_id=? ORDER BY id",
                       (self.config_id,))[0]
        self.assertEqual(ev["action"], "skipped")
        self.assertGreater(ev["deviation_bps"], 50)
        self.assertIn("deviation", self.eng.stats.skipped)

    def test_a_disabled_config_is_not_even_queried_for_a_quote(self):
        self.add_config(enabled=False)
        # `copy_configs_for_source` filters on enabled, so a paused source produces no evaluation at all; the
        # test exists so that filter cannot be "improved" into evaluating-and-then-skipping without notice.
        self.assertEqual(self.eng.on_source_fill(self.src_fill(), at=self.at + 1), [])
        self.assertEqual(self.rows("SELECT id FROM copy_events WHERE copier_id=?", (self.config_id,)), [],
                         "no evaluation at all - not even a skip event was written")

    def test_take_profit_exit_is_queued_when_the_bid_reaches_it(self):
        self.add_config(tp=1_000)
        f = self.src_fill()
        self.eng.on_source_fill(f, at=self.at + 1)
        # The target is measured on OUR queued entry (the ask we would pay), not on the source's fill price.
        entry = f.price_micro
        tp = (entry * 11_000) // 10_000
        self.store.conn.execute("DELETE FROM book_levels WHERE market_id=? AND side='bid'", (self.market,))
        self.store.conn.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,"
                                "level_count,updated_ms) VALUES (?,?,?,?,?,?)",
                                (self.market, "bid", tp + 1_000, 100 * 10**6, 1, self.at))
        out = self.eng.manage_tp_sl(cp.CopyConfig.from_rows(self.store.copy_config("c-1")), f, at=self.at + 1,
                                   entry_price_micro=entry)
        self.assertEqual(out["action"], "tp", out)
        # rowid, not id: `order_intents.id` is a hash, so ordering by it is alphabetical, not chronological.
        rows = self.rows("SELECT side,state FROM order_intents ORDER BY rowid")
        self.assertEqual([r["side"] for r in rows], ["BUY", "SELL"])
        # the copy's own event is first; the exit is the newest row
        self.assertEqual(self.rows("SELECT action FROM copy_events ORDER BY id DESC LIMIT 1")[0]["action"],
                         "tp_filled")

    def test_stop_loss_exit_is_queued_on_the_way_down(self):
        self.add_config(sl=1_000)
        f = self.src_fill()
        self.eng.on_source_fill(f, at=self.at + 1)
        entry = self.rows("SELECT price_micro FROM order_intents ORDER BY rowid")[0]["price_micro"]
        self.store.conn.execute("DELETE FROM book_levels WHERE market_id=? AND side='bid'", (self.market,))
        self.store.conn.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,"
                                "level_count,updated_ms) VALUES (?,?,?,?,?,?)",
                                (self.market, "bid", (entry * 8_000) // 10_000, 100 * 10**6, 1, self.at))
        out = self.eng.manage_tp_sl(cp.CopyConfig.from_rows(self.store.copy_config("c-1")), f, at=self.at + 1,
                                   entry_price_micro=entry)
        self.assertEqual(out["action"], "sl")
        self.assertEqual(self.rows("SELECT side FROM order_intents ORDER BY rowid DESC LIMIT 1")[0]["side"],
                         "SELL")

    def test_no_exit_rule_means_no_exit_intent(self):
        self.add_config()
        self.assertIsNone(self.eng.manage_tp_sl(cp.CopyConfig.from_rows(self.store.copy_config("c-1")),
                                               self.src_fill(), at=self.at + 1))

    def test_source_stopped_pauses_every_copier_and_tells_them(self):
        self.add_config(config_id="c-1")
        self.add_config(config_id="c-2", source="u-src")
        r = self.eng.on_source_stopped("u-src", at=self.at)
        self.assertEqual(r, {"paused_copiers": 2, "notified": 2})
        for cid in ("c-1", "c-2"):
            self.assertIn("source stopped", self.store.copy_config(cid)["paused_reason"])
        self.assertEqual(self.rows("SELECT action FROM copy_events WHERE copier_id IN ('c-1','c-2') ORDER BY id"),
                         [{"action": "source_stopped"}, {"action": "source_stopped"}])

    def test_an_insider_suspect_label_pauses_without_showing_the_accusation(self):
        self.add_config()
        r = self.eng.on_label("u-src", "insider_suspect", at=self.at)
        self.assertEqual(r["paused_copiers"], 1)
        self.assertFalse(r["publishable"])
        cfg = self.store.copy_config("c-1")
        self.assertEqual(cfg["paused_reason"], "insider_flagged")
        # the code the user sees is the pause reason; the label itself never reaches a user-facing string here
        self.assertNotIn("insider", self.rows("SELECT reason FROM copy_events")[0]["reason"].lower())
        self.assertEqual(self.eng.on_label("u-src", "smart_money", at=self.at)["paused_copiers"], 0)

    def test_stats_are_persisted_for_the_leaderboard(self):
        s = self.eng.recompute_stats("u-src", at=self.at, trades=[
            cp.ClosedTrade(realized_micro=4_000_000, fee_micro=1_000_000),
            cp.ClosedTrade(realized_micro=-2_000_000, fee_micro=1_000_000)])
        self.assertEqual(s["net_after_fees_micro"], 0)
        row = self.rows("SELECT * FROM copy_source_stats WHERE source_user_id='u-src'")
        self.assertEqual(len(row), 1, "the leaderboard reads the table, not the return value")


# ================================================================================== D6: rule shape ====
class TestRuleValidation(unittest.TestCase):
    def ok(self, trigger, actions=None, **kw):
        rule = {"id": "r", "user_id": "u", "trigger": trigger,
                "actions": actions or [{"kind": "set_alert", "message": "hi"}]}
        # Alert-only rules need no ceiling; a test that gave every rule one would hide the rule's own check.
        rule.update(kw)
        return rule

    def test_every_trigger_kind_validates_with_its_own_arguments(self):
        for t in ({"kind": "price_cross", "op": "<", "price_micro": 500_000},
                  {"kind": "price_cross", "op": ">=", "price_micro": 1, "uses": "ask"},
                  {"kind": "time", "at_ms": 1_789_000_000_000, "once": True},
                  {"kind": "signal", "label": "insider_suspect"},
                  {"kind": "book_imbalance", "op": ">=", "imbalance_bps": 7_000},
                  {"kind": "new_market", "within_ms": 120_000}):
            self.assertEqual(au.validate_rule(self.ok(t)), [], t)

    def test_a_bad_leaf_reports_the_problem_not_a_traceback(self):
        cases = [
            ({"kind": "price_cross", "op": "~", "price_micro": 1}, "op"),
            ({"kind": "price_cross", "op": "<", "price_micro": 1_000_000}, "inside (0, 1000000)"),
            ({"kind": "price_cross", "op": "<", "price_micro": "500000"}, "integer"),
            ({"kind": "price_cross", "op": "<", "price_micro": True}, "integer"),
            ({"kind": "time", "at_ms": "yesterday"}, "integer at_ms"),
            ({"kind": "signal", "label": "vibes"}, "signal label"),
            ({"kind": "book_imbalance", "op": "<", "imbalance_bps": 100}, "op must be"),
            ({"kind": "book_imbalance", "op": ">", "imbalance_bps": 0}, "imbalance_bps"),
            ({"kind": "new_market", "within_ms": 500}, "race"),
            ({"kind": "lunar_eclipse"}, "unknown trigger kind"),
        ]
        for t, needle in cases:
            errs = au.validate_rule(self.ok(t))
            self.assertEqual(len(errs), 1, (t, errs))
            self.assertIn(needle, errs[0], t)

    def test_nesting_is_limited_and_said_so_with_numbers(self):
        deep = {"all": [{"all": [{"all": [{"kind": "time", "at_ms": 1}]}]}]}
        errs = au.validate_rule(self.ok(deep))
        self.assertIn("nesting is limited to 2 levels", errs[0])
        wide = {"all": [{"kind": "time", "at_ms": i} for i in range(9)]}
        self.assertIn("9 leaves, the maximum is 8", " ".join(au.validate_rule(self.ok(wide))))
        self.assertEqual(au.validate_rule(self.ok({"all": [{"kind": "time", "at_ms": i} for i in range(8)]})),
                         [])

    def test_an_empty_group_is_refused_rather_than_vacuously_true(self):
        # `all([])` is True in Python, which would make the rule fire every pass, forever.
        self.assertIn("non-empty list", " ".join(au.validate_rule(self.ok({"all": []}))))
        self.assertIn("non-empty list", " ".join(au.validate_rule(self.ok({"any": []}))))

    def test_a_node_may_not_mix_all_and_any(self):
        self.assertIn("may not mix", " ".join(au.validate_rule(self.ok({"all": [], "any": []}))))

    def test_action_shapes_are_checked_where_the_money_is(self):
        cases = [
            ([{"kind": "limit", "side": "HODL", "price_micro": 1, "size_shares_micro": 10**6}], "side BUY"),
            ([{"kind": "limit", "side": "BUY", "price_micro": 0, "size_shares_micro": 10**6}], "price_micro"),
            ([{"kind": "limit", "side": "BUY", "price_micro": 500_000, "size_shares_micro": 0}],
             "positive integer size"),
            ([{"kind": "market", "side": "BUY", "size_shares_micro": 10**6, "max_slippage_bps": 5_000}],
             "max_slippage_bps"),
            ([{"kind": "cancel_open", "scope": "all"}], "may not cancel ALL"),
            ([{"kind": "cancel_open", "scope": "universe"}], "scope must be"),
            ([{"kind": "close_position", "method": "pray"}], "method must be"),
            ([{"kind": "set_alert", "message": "  "}], "needs a message"),
            ([{"kind": "set_alert", "message": "x", "channel": "carrier_pigeon"}], "channel"),
            ([{"kind": "tp_sl_set"}], "at least one of"),
            ([{"kind": "tp_sl_set", "take_profit_bp": 0}], "take_profit_bp"),
            ([{"kind": "teleport"}], "unknown action kind"),
            ([{"kind": "limit", "side": "BUY", "price_micro": 500_000, "size_shares_micro": 10**6},
              {"kind": "market", "side": "SELL", "size_shares_micro": 10**6}], "at most one order-placing"),
            ([{"kind": "limit", "side": "BUY", "price_micro": 500_000, "size_shares_micro": 10**6}],
             "must carry a positive max_loss_micro"),
        ]
        # A ceiling is what the DB's own CHECK demands of a money-moving rule; pass it explicitly so the
        # single-case assertions test the case and not the missing ceiling.
        cases = [(a, n) for a, n in cases if "max_loss" not in n]
        for actions, needle in cases:  # noqa: PLR1702
            errs = au.validate_rule(self.ok({"kind": "time", "at_ms": 1}, actions, max_loss_micro=10**6))
            self.assertIn(needle, " ".join(errs), actions)

    def test_several_problems_come_back_together(self):
        errs = au.validate_rule({"id": "r", "user_id": "u", "trigger": {"kind": "nonsense"},
                                "actions": [{"kind": "limit", "side": "MAYBE", "price_micro": -1,
                                             "size_shares_micro": "lots"}], "max_loss_micro": -5})
        self.assertGreaterEqual(len(errs), 4, errs)

    def test_a_rule_with_no_actions_or_no_trigger_is_not_half_saved(self):
        self.assertIn("actions must be a non-empty list", " ".join(
            au.validate_rule({"trigger": {"kind": "time", "at_ms": 1}, "actions": []})))
        self.assertIn("trigger must be an object", " ".join(
            au.validate_rule({"trigger": "always", "actions": [{"kind": "set_alert", "message": "x"}]})))


class TestRuleEvaluation(unittest.TestCase):
    def facts(self, **kw):
        base = dict(last_price_micro=480_000, best_bid_micro=470_000, best_ask_micro=490_000,
                    quote_age_ms=100, now_ms=1_789_000_000_000, accepting_orders=True)
        base.update(kw)
        return au.Facts(**base)

    def test_price_cross_against_each_reference_price(self):
        t = {"kind": "price_cross", "op": "<", "price_micro": 485_000, "uses": "ask"}
        r = au.evaluate(t, self.facts())
        self.assertFalse(r["fires"], "the ask is 490000, which is not below 485000")
        self.assertEqual(r["leaves"][0]["observed"], 490_000)
        self.assertTrue(au.evaluate({"kind": "price_cross", "op": "<", "price_micro": 495_000},
                                    self.facts())["fires"], "mid 480000 < 495000")

    def test_a_missing_reference_price_is_a_non_firing_leaf_not_an_error(self):
        r = au.evaluate({"kind": "price_cross", "op": ">", "price_micro": 1, "uses": "bid"},
                        au.Facts(best_bid_micro=None, last_price_micro=1, quote_age_ms=0))
        self.assertFalse(r["fires"])
        self.assertIn("no price", str(r["leaves"][0]["observed"]))

    def test_a_one_shot_time_trigger_only_fires_once_per_pass(self):
        t = {"kind": "time", "at_ms": 1_789_000_000_000, "once": True}
        self.assertTrue(au.evaluate(t, self.facts())["fires"])
        self.assertFalse(au.evaluate({"kind": "time", "at_ms": self.facts().now_ms + 1, "once": True},
                                     self.facts())["fires"])

    def test_imbalance_and_new_market_and_signal(self):
        self.assertTrue(au.evaluate({"kind": "book_imbalance", "op": ">=", "imbalance_bps": 7_000},
                                   self.facts(imbalance_bps=7_000))["fires"])
        self.assertFalse(au.evaluate({"kind": "book_imbalance", "op": ">=", "imbalance_bps": 7_000},
                                    self.facts(imbalance_bps=6_999))["fires"])
        now = 1_789_000_000_000
        self.assertTrue(au.evaluate({"kind": "new_market", "within_ms": 120_000},
                                   self.facts(market_open_ms=now - 60_000))["fires"])
        self.assertFalse(au.evaluate({"kind": "new_market", "within_ms": 120_000},
                                    self.facts(market_open_ms=now - 600_000))["fires"])
        self.assertTrue(au.evaluate({"kind": "signal", "label": "wash_like"},
                                   self.facts(labels=("large_whale", "wash_like")))["fires"])
        self.assertFalse(au.evaluate({"kind": "signal", "label": "wash_like"}, self.facts())["fires"])

    def test_eager_evaluation_logs_every_leaf(self):
        f = self.facts(imbalance_bps=8_000, labels=("volume_spike",))
        t = {"all": [{"any": [{"kind": "book_imbalance", "op": ">=", "imbalance_bps": 7_000},
                             {"kind": "signal", "label": "insider_suspect"}]},
                     {"not": {"kind": "signal", "label": "smart_money"}}]}
        r = au.evaluate(t, f)
        self.assertTrue(r["fires"])
        self.assertEqual([l["kind"] for l in r["leaves"]],
                         ["book_imbalance", "signal", "signal"], "short-circuit would hide a leaf")

    def test_all_requires_every_leaf_and_any_requires_one(self):
        leaves = [{"kind": "book_imbalance", "op": ">=", "imbalance_bps": 7_000},
                  {"kind": "signal", "label": "volume_spike"}]
        # one leaf true, one false: exactly the case where all() and any() must disagree
        half = self.facts(imbalance_bps=100, labels=("volume_spike",))
        self.assertFalse(au.evaluate({"all": leaves}, half)["fires"])
        self.assertTrue(au.evaluate({"any": leaves}, half)["fires"])
        none = self.facts(imbalance_bps=100)
        self.assertFalse(au.evaluate({"any": leaves}, none)["fires"], "an empty-truth ANY would fire forever")

    def test_usable_quote_is_a_property_of_the_fact_not_a_prayer(self):
        self.assertFalse(au.Facts(best_bid_micro=1, best_ask_micro=2, last_price_micro=1,
                                 quote_age_ms=3_001).quote_usable)
        self.assertTrue(au.Facts(best_bid_micro=1, best_ask_micro=2, last_price_micro=1,
                                 quote_age_ms=3_000).quote_usable)
        self.assertIsNone(au.Facts().mid_micro)


# ========================================================================= D6: the engine and its caps ==
class TestAutomationEngine(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.eng = au.AutomationEngine(self.store, canceller=self._cancel)
        self.cancels = []

    def _cancel(self, **kw):
        self.cancels.append(kw)
        return {"queued": 1, "scope": kw["scope"]}

    def save(self, rule_id="r-1", kind="entry", enabled=True, markets=None,
             max_per_day=24, min_interval_ms=60_000, human_priority_ms=120_000, rule=None,
             max_loss_micro=5_000_000):
        rule = rule or {"trigger": {"kind": "time", "at_ms": self.at - 1_000, "once": True},
                        "actions": [{"kind": "limit", "side": "BUY", "price_micro": 500_000,
                                     "size_shares_micro": 10 * 10**6}]}
        # The real seeded market and token, because `order_intents` has foreign keys to both and a rule that
        # names a token nobody holds must fail for a REASON, not on a constraint in the middle of a pass.
        markets = markets if markets is not None else [(self.market, self.token)]
        return self.eng.save_rule(rule_id=rule_id, user_id=self.user, kind=kind, rule=rule, enabled=enabled,
                                 at=self.at, market_ids=list(markets), max_per_day=max_per_day,
                                 min_interval_ms=min_interval_ms, human_priority_ms=human_priority_ms,
                                 max_loss_micro=max_loss_micro)

    def test_an_invalid_rule_is_refused_at_save_time(self):
        with self.assertRaises(au.RuleError) as cm:
            self.save(rule={"trigger": {"kind": "time"}, "actions": [{"kind": "set_alert", "message": ""}]})
        self.assertGreaterEqual(len(cm.exception.errors), 2)
        self.assertEqual(self.rows("SELECT id FROM automation_rules"), [], "nothing half-saved")

    def view(self, rule_id="r-1", **over):
        row = self.store.automation_rule(rule_id)
        v = au.rule_view(row, state=self.store.rule_state(rule_id),
                        target={"market_id": self.market, "token_id": self.token})
        v.update(over)
        return v

    def arm(self, rule_id="r-1"):
        """The only legal way a rule goes live: a dry run happened, it was marked, then enable()."""
        self.eng.tick(at=self.at + 500, mode="dry_run")
        m = self.eng.mark_dry_run_done(rule_id, at=self.at + 501)
        self.assertTrue(m["marked"], m)
        e = self.eng.enable(rule_id, at=self.at + 502)
        self.assertTrue(e["enabled"], e)

    def test_a_new_rule_cannot_be_enabled_without_a_dry_run(self):
        r = self.save()
        self.assertEqual(r["enabled"], False)
        self.assertEqual(r["deny_code"], "AUTOMATION_NO_DRY_RUN")
        e = self.eng.enable("r-1", at=self.at)
        self.assertEqual((e["enabled"], e["code"]), (False, "AUTOMATION_NO_DRY_RUN"))
        self.assertEqual(self.store.automation_rule("r-1")["enabled"], 0)

    def test_a_dry_run_cannot_be_claimed_without_being_observed(self):
        self.save()
        r = self.eng.mark_dry_run_done("r-1", at=self.at)
        self.assertEqual((r["marked"], r["code"]), (False, "AUTOMATION_NO_DRY_RUN"),
                         "the client does not get to say it dry-ran")

    def test_dry_run_places_nothing_but_records_would_place(self):
        self.save()
        out = self.eng.tick(at=self.at + 1_000, mode="dry_run")
        self.assertEqual([o["outcome"] for o in out["outcomes"]], ["would_place"], out)
        self.assertEqual(self.rows("SELECT id FROM order_intents"), [])
        rows = self.rows("SELECT mode,outcome,reason FROM automation_runs ORDER BY id")
        self.assertEqual(rows[-1]["mode"], "dry_run")
        self.assertEqual(rows[-1]["outcome"], "would_place")
        self.assertEqual(self.store.automation_rule("r-1")["last_fire_ms"] or 0, 0,
                         "a dry run must not consume the rule's real min-interval budget")

    def test_after_the_dry_run_the_live_pass_queues_an_intent(self):
        self.save()
        self.eng.tick(at=self.at + 1_000, mode="dry_run")
        self.eng.mark_dry_run_done("r-1", at=self.at + 1_001)
        self.eng.enable("r-1", at=self.at + 1_002)
        out = self.eng.tick(at=self.at + 2_000, mode="live")
        self.assertEqual([o["outcome"] for o in out["outcomes"]], ["placed"], out)
        intents = self.rows("SELECT * FROM order_intents")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["state"], "queued")
        d = self.rows("SELECT audience,order_type,rule_id FROM order_directives")[0]
        self.assertEqual((d["audience"], d["order_type"], d["rule_id"]), ("automation", "GTC", "r-1"))
        self.assertEqual(self.store.automation_rule("r-1")["last_fire_ms"], self.at + 2_000)

    def test_the_rule_intent_then_passes_the_venue_gate_like_any_other(self):
        # The claim is "automation has no second door": after the engine queues the intent, the executor's
        # own pre-flight decides, and here the user has no funded balance... which the seeded 100$ covers, so
        # instead we take the allowance away and expect the venue-side refusal.
        self.save()
        self.eng.tick(at=self.at + 1_000, mode="dry_run")
        self.eng.mark_dry_run_done("r-1", at=self.at + 1_001)
        self.eng.enable("r-1", at=self.at + 1_002)
        self.store.set_allowance(user_id=self.user, token="pUSD", spender="0xexchange", amount_micro=0,
                                at=self.at)
        self.eng.tick(at=self.at + 2_000, mode="live")
        rep = self.ex.tick(at=self.at + 2_000, reconcile=False)
        self.assertEqual([h["state"] for h in rep["handled"]], ["rejected"])
        self.assertEqual(rep["handled"][0]["code"], "ALLOWANCE_REQUIRED")
        self.assertEqual(self.rows("SELECT id FROM orders"), [])

    def test_the_same_minute_cannot_produce_two_orders(self):
        self.save(min_interval_ms=1_000)
        self.arm()
        first = self.eng.tick(at=self.at + 2_000, mode="live")
        second = self.eng.tick(at=self.at + 3_000, mode="live")
        self.assertEqual([o["outcome"] for o in first["outcomes"]], ["placed"])
        self.assertEqual([o["outcome"] for o in second["outcomes"]], ["skipped"])
        self.assertIn("min interval", second["outcomes"][0]["reason"])
        self.assertEqual(len(self.rows("SELECT id FROM order_intents")), 1)

    def test_the_daily_cap_is_counted_from_the_run_log(self):
        self.save(max_per_day=2)
        self.arm()
        # Two placed runs already today, from a previous process: the cap must see them.
        for i in (1, 2):
            self.store.record_run(rule_id="r-1", user_id=self.user, mode="live", outcome="placed",
                                 reason="", deny_code="", intent_id="i-x%d" % i, at=self.at - i * 1000)
            self.store.set_rule_last_fire("r-1", self.at - 61_000 - i)
        out = self.eng.tick(at=self.at, mode="live")
        self.assertEqual(out["outcomes"][0]["outcome"], "skipped")
        self.assertEqual(out["outcomes"][0]["deny_code"], "AUTOMATION_DAY_CAP")

    def test_a_human_order_makes_the_rule_stand_down_and_pause(self):
        self.save(human_priority_ms=120_000)
        self.arm()
        self.queue(audience="user", key="human-first", size_micro=10 * 10**6, price_micro=500_000)
        out = self.eng.tick(at=self.at + 30_000, mode="live")
        self.assertEqual(out["outcomes"][0]["outcome"], "skipped")
        self.assertIn("human traded 30s ago", out["outcomes"][0]["reason"])
        self.assertEqual(self.store.rule_state("r-1")["paused_reason"], "human_active")
        self.assertEqual(len(self.rows("SELECT id FROM order_intents")), 1, "only the human's")
        self.assertEqual(self.rows("SELECT audience FROM order_directives")[0]["audience"], "user")

    def test_a_stale_quote_refuses_before_the_trigger_is_even_evaluated(self):
        self.save()
        self.arm()
        self.store.conn.execute("UPDATE book_levels SET updated_ms=?", (self.at - 4_000_000,))
        out = self.eng.tick(at=self.at, mode="live")
        self.assertEqual(out["outcomes"][0]["deny_code"], "STALE_QUOTE")
        self.assertEqual(out["outcomes"][0]["detail"].get("leaves"), None,
                         "the trigger never ran, and the log must not imply it did")

    def test_cancel_open_asks_the_executor_and_never_cancels_everything(self):
        self.save(rule={"trigger": {"kind": "signal", "label": "volume_spike"},
                       "actions": [{"kind": "cancel_open", "scope": "market"}]})
        facts = au.Facts(now_ms=self.at, labels=("volume_spike",), quote_age_ms=0, last_price_micro=1)
        res = self.eng.run_rule(self.view(), facts=facts, mode="live", at=self.at, enabled=True,
                                dry_run_completed_ms=self.at, notional_today_micro=0, run_count_today=0)
        self.assertEqual(res.outcome, "placed", res.reason)
        self.assertEqual(self.cancels[0]["scope"], "market")
        errs = au.validate_rule({"trigger": {"kind": "time", "at_ms": 1},
                                "actions": [{"kind": "cancel_open", "scope": "all"}]})
        self.assertIn("may not cancel ALL", " ".join(errs))

    def test_set_alert_reaches_the_shared_notification_queue(self):
        self.save(rule={"trigger": {"kind": "book_imbalance", "op": ">=", "imbalance_bps": 1},
                       "actions": [{"kind": "set_alert", "message": "book is one-sided", "channel": "in_app"}]})
        self.eng.mark_dry_run_done("r-1", at=self.at)
        facts = au.Facts(now_ms=self.at, best_bid_micro=1, best_ask_micro=2, last_price_micro=1,
                        imbalance_bps=5_000, quote_age_ms=0)
        res = self.eng.run_rule(self.view(), facts=facts, mode="live", at=self.at, enabled=True,
                               dry_run_completed_ms=self.at, run_count_today=0, notional_today_micro=0,
                               last_fire_ms=0, human_last_ms=0)
        self.assertEqual(res.outcome, "placed")
        n = self.rows("SELECT event,intent_id FROM order_notifications ORDER BY id")[-1]
        self.assertEqual(n["event"], "alert")
        self.assertEqual(n["intent_id"], "", "an alert belongs to no order, and must not pretend otherwise")

    def test_tp_sl_arming_writes_rule_state_not_an_order(self):
        self.save(rule={"trigger": {"kind": "time", "at_ms": self.at},
                       "actions": [{"kind": "tp_sl_set", "take_profit_bp": 800, "stop_loss_bp": 500}]})
        facts = au.Facts(now_ms=self.at + 1, last_price_micro=1, quote_age_ms=0)
        res = self.eng.run_rule(self.view(), facts=facts, mode="live", at=self.at + 1, enabled=True,
                                dry_run_completed_ms=self.at, run_count_today=0, notional_today_micro=0,
                                last_fire_ms=0, human_last_ms=0)
        self.assertEqual(res.outcome, "placed")
        st = self.store.rule_state("r-1")
        self.assertEqual((st["take_profit_bp"], st["stop_loss_bp"]), (800, 500))
        self.assertEqual(self.rows("SELECT id FROM order_intents"), [])

    def test_a_failing_action_counts_and_the_rule_is_held_after_three(self):
        self.eng.canceller = lambda **kw: (_ for _ in ()).throw(RuntimeError("venue down"))
        self.save(rule={"trigger": {"kind": "time", "at_ms": self.at - 1},
                       "actions": [{"kind": "cancel_open", "scope": "market"}]})
        for i in range(3):
            res = self.eng.run_rule(self.view(),
                                   facts=au.Facts(now_ms=self.at + i, last_price_micro=1, quote_age_ms=0),
                                   mode="live", at=self.at + i + (i + 1) * 61_000, enabled=True,
                                   dry_run_completed_ms=self.at, run_count_today=i, notional_today_micro=0,
                                   last_fire_ms=0, human_last_ms=0)
            self.assertEqual(res.outcome, "failed", res.reason)
            self.assertEqual(res.deny_code, "AUTOMATION_ACTION_FAILED")
            st = self.store.rule_state("r-1")
            self.assertEqual(st["failure_count"], i + 1)
            self.assertIn("venue down", st["last_error"])
        # The rule is never enabled here (three failures, and arming clears the counter), so the pass that
        # must notice the pause is the dry-run one, which sees every saved rule.
        out = self.eng.tick(at=self.at + 300_000, mode="dry_run")
        self.assertEqual(len(out["outcomes"]), 1, out)
        self.assertEqual(out["outcomes"][0]["outcome"], "skipped")
        self.assertIn("paused after 3 consecutive failures", out["outcomes"][0]["reason"])

    def test_a_transient_failure_is_retriable_and_a_policy_refusal_is_not(self):
        self.assertTrue(au.run_row_is_retriable({"outcome": "failed", "deny_code": "AUTOMATION_ACTION_FAILED"}))
        self.assertFalse(au.run_row_is_retriable({"outcome": "failed", "deny_code": "RULE_INVALID"}))
        self.assertFalse(au.run_row_is_retriable({"outcome": "skipped", "deny_code": "AUTOMATION_DAY_CAP"}))

    def test_skips_are_rows_too_because_absence_is_not_an_explanation(self):
        self.save(rule={"trigger": {"kind": "signal", "label": "smart_money"},
                       "actions": [{"kind": "set_alert", "message": "hi"}]})
        self.arm()
        before = len(self.rows("SELECT id FROM automation_runs"))
        out = self.eng.tick(at=self.at + 1_000_000, mode="live")
        self.assertEqual(out["outcomes"][0]["outcome"], "skipped")
        self.assertEqual(out["outcomes"][0]["reason"], "trigger not met")
        self.assertEqual(len(self.rows("SELECT id FROM automation_runs")) - before, 1,
                         "one evaluation, one row — and `arm()` may not add a row to this count")
        self.assertEqual(self.rows("SELECT id FROM order_intents"), [])

    def test_a_rule_watching_two_markets_is_evaluated_twice(self):
        # One market has a fresh book and one has none at all: both must still produce a run row, because a
        # rule that could not be evaluated and a rule that decided not to fire are different facts.
        self.save(markets=(("0xM1", "t-1"), ("0xM9", "t-9")))
        out = self.eng.tick(at=self.at + 1, mode="dry_run")
        self.assertEqual(out["evaluated"], 2)
        self.assertEqual(len(self.rows("SELECT id FROM automation_runs")), 2)
        reasons = sorted(o["reason"] for o in out["outcomes"])
        self.assertTrue(any("would_place" in r or "dry run" in r for r in reasons), reasons)
        self.assertTrue(any(r in ("quote is stale or missing", "trigger not met", "dry run: no order sent")
                            for r in reasons), reasons)

    def test_the_platform_ceiling_clamps_a_user_who_asks_for_more_than_we_will_run(self):
        # A rule cannot outrank the venue's rate limit just because a user set max_per_day to 288 on ten
        # rules; the engine clamps to its own ceiling and says which one bound it.
        self.save(max_per_day=1)
        self.arm()
        self.store.record_run(rule_id="r-1", user_id=self.user, mode="live", outcome="placed", reason="",
                             deny_code="", intent_id="i-old", at=self.at - 1)
        self.store.set_rule_last_fire("r-1", self.at - 61_000)
        out = self.eng.tick(at=self.at, mode="live")
        self.assertEqual(out["outcomes"][0]["deny_code"], "AUTOMATION_DAY_CAP")
        self.assertIn("(1)", out["outcomes"][0]["reason"])


# ================================================================== D6: the template and its arithmetic ==
class TestAutomationEconomics(unittest.TestCase):
    def test_break_even_is_price_plus_fees(self):
        b = au.break_even_win_rate_bp(500_000, fee_rate_bps=70, builder_bps=100)
        # 70 bps taker on p(1-p): 1750 micro per share per leg; 100 bps builder on notional: 5000; two legs.
        self.assertEqual(b["fees_per_share_micro"], (1750 + 5000) * 2)
        self.assertEqual(b["break_even_win_rate_bp"], 5_135)
        self.assertEqual(b["edge_needed_bp"], 135)
        self.assertTrue(b["hurdle_exists"])

    def test_the_extremes_carry_almost_no_venue_fee_and_the_builder_fee_still_bites(self):
        # Recomputed here from the definition rather than from the helper, so a bug in the helper cannot be
        # hidden by a test that calls the same helper twice.
        p, one, rate, bps = 50_000, 10**6, 70, 100
        per_leg = p * (one - p) * rate // (10_000 * one)
        builder_leg = p * bps // 10_000
        cheap = au.break_even_win_rate_bp(p, fee_rate_bps=rate, builder_bps=bps)
        self.assertEqual(cheap["fees_per_share_micro"], (per_leg + builder_leg) * 2)
        self.assertEqual(cheap["break_even_win_rate_bp"], (p + (per_leg + builder_leg) * 2) * 10_000 // one)
        self.assertLess(cheap["fees_per_share_micro"], 5_000, "a 5c contract cannot be worth 2.5c of fee")
        self.assertGreater(cheap["fees_per_share_micro"], 0, "the builder fee is on notional, so it follows "
                          "the price down but never to zero")

    def test_a_maker_entry_pays_one_leg_and_may_be_rebated(self):
        m = au.break_even_win_rate_bp(500_000, fee_rate_bps=70, builder_bps=100, maker=True)
        self.assertLess(m["fees_per_share_micro"], au.break_even_win_rate_bp(
            500_000, fee_rate_bps=70, builder_bps=100)["fees_per_share_micro"])

    def test_the_off_grid_price_is_an_error_not_a_zero(self):
        for bad in (0, 10**6, 1_000_001, -1, 500000.0, "500000", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                au.break_even_win_rate_bp(bad, fee_rate_bps=70)

    def test_the_template_ships_no_entry_rule_when_the_fee_schedule_is_unknown(self):
        r = au.crypto_5m_template(fee_type="None", fee_rate_bps=None)
        self.assertEqual(r["verdict"], "NOT SHIPPED")
        self.assertEqual(r["blocking_reason"], "fee_type_unknown")
        self.assertFalse(r["ship_entry_rule"])
        self.assertIn("told us nothing", r["why"])
        self.assertEqual(len(r["shipped_rules"]), 3, "the protective half always ships")

    def test_the_template_ships_no_entry_rule_without_a_measured_edge(self):
        r = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, edge_available_bp=None)
        self.assertFalse(r["ship_entry_rule"])
        self.assertEqual(r["blocking_reason"], "no_measured_edge")
        self.assertIn("dry", r["recommended_but_dry_run_only"]["why_dry_run"])

    def test_the_entry_rule_ships_only_when_the_measured_edge_clears_the_hurdle(self):
        blocked = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, edge_available_bp=100,
                                       latency_ms=2_000)
        self.assertFalse(blocked["ship_entry_rule"])
        self.assertEqual(blocked["numbers"]["edge_needed_bp"], 135 + 100 + 4)
        self.assertIn("does not clear the hurdle", blocked["why"])
        shipped = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, edge_available_bp=400,
                                       latency_ms=2_000)
        self.assertTrue(shipped["ship_entry_rule"])
        self.assertEqual(shipped["verdict"], "SHIPPED")
        self.assertIsNone(shipped["recommended_but_dry_run_only"])

    def test_a_maker_rebate_market_needs_the_least(self):
        r = au.crypto_5m_template(fee_type="maker_rebate", fee_rate_bps=0, edge_available_bp=50,
                                 spread_ticks=1, tick_micro=10_000, latency_ms=0)
        n = r["numbers"]
        self.assertTrue(n["maker"])
        self.assertEqual(n["legs"], 1)
        self.assertLess(n["edge_needed_bp"], 200, "one tick of spread and a rebate should be cheaper than a "
                                                 "two-leg taker round trip, or the pricing is inverted")

    def test_the_hurdle_includes_our_own_builder_fee_because_we_charge_it(self):
        zero_builder = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, builder_bps=0,
                                            edge_available_bp=400, latency_ms=0)
        ours = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, builder_bps=100,
                                    edge_available_bp=400, latency_ms=0)
        self.assertGreater(ours["numbers"]["edge_needed_bp"], zero_builder["numbers"]["edge_needed_bp"],
                          "a template that hides the platform's own fee from the hurdle is marketing")

    def test_the_shipped_protective_rules_all_validate(self):
        # The template's rules must survive the same validator a user-authored rule faces. Otherwise the
        # shipped half of the product is a rule that fails at fire time, which is the thing D6.2 forbids.
        r = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70)
        for rule in r["shipped_rules"] + [x for x in [r.get("recommended_but_dry_run_only")] if x]:
            errs = au.validate_rule({"trigger": rule["trigger"], "actions": rule["actions"]})
            self.assertEqual(errs, [], rule["id"])


class TestTimeColumns(unittest.TestCase):
    """`dbtypes` exists because a 60x timestamp scale error is silent and decides trading."""

    def test_the_two_engines_read_the_same_column(self):
        import datetime as dt
        ms = 1_790_000_000_000
        self.assertEqual(to_epoch_ms(ms), ms)
        self.assertEqual(to_epoch_ms(ms // 1000), ms - ms % 1000)
        self.assertEqual(to_epoch_ms(dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)), ms)
        self.assertEqual(to_epoch_ms("2026-09-18T00:00:00+00:00"), 1_789_689_600_000)
        self.assertEqual(to_epoch_ms(dt.date(2026, 9, 18)), 1_789_689_600_000)

    def test_a_bare_number_in_a_text_column_is_read_by_magnitude_and_not_by_assumption(self):
        self.assertEqual(to_epoch_ms("1790000000000"), 1_790_000_000_000)     # milliseconds
        self.assertEqual(to_epoch_ms("1790000000"), 1_790_000_000_000)         # seconds, x1000
        for impossible in ("1790000", "4102444801000000", "not a date", ""):
            if impossible == "":
                self.assertIsNone(to_epoch_ms(impossible))                    # empty is NULL, not zero
                continue
            with self.assertRaises(TimeColumnError, msg=impossible):
                to_epoch_ms(impossible)

    def test_values_outside_the_plausible_window_are_refused(self):
        for bad in (1, 0.5, 999_999_999, 10**18, -1, True, object()):
            with self.assertRaises(TimeColumnError, msg=repr(bad)):
                to_epoch_ms(bad)
        self.assertIsNone(to_epoch_ms(None))

    def test_seconds_until_keeps_the_sign(self):
        # An ended market is not an unbounded one: the negative is the fact the caller needs.
        self.assertEqual(seconds_until(1_790_000_000_000, now_ms=1_789_000_000_000), 1_000_000)
        self.assertLess(seconds_until(1_789_000_000_000, now_ms=1_790_000_000_000), 0)
        self.assertIsNone(seconds_until(None, now_ms=1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
