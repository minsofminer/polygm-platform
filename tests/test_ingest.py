"""Ingest against the recorded live payloads. Nothing here touches the network: the fixtures under
tests/fixtures/p05/ were captured from the real venue by tools/p05-capture-fixtures.py, so a failure means the
venue moved or our reading of it did — and `--check` on that tool tells you which.
"""
from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "services" / "ingest"), str(ROOT / "packages")]
import books as B                                    # noqa: E402
import freshness as FR                               # noqa: E402
import normalise as N                                # noqa: E402
import tape as T                                     # noqa: E402
import universe as U                                   # noqa: E402

FX = ROOT / "tests" / "fixtures" / "p05"


def fx(name):
    return json.loads((FX / name).read_text())


def require(name):
    p = FX / name
    if not p.is_file():
        raise unittest.SkipTest("fixture %s missing — run `python3 tools/p05-capture-fixtures.py`" % name)
    return json.loads(p.read_text())



class TestTransportLifecycle(unittest.TestCase):
    """A socket that disappears mid-read is the normal shutdown, not a crash."""

    def test_a_close_during_poll_is_a_werror_not_an_attributeerror(self):
        import wsclient as WC

        class VanishingSock:
            """The first `settimeout` call stands in for the other thread closing us underneath the loop."""

            def __init__(self, owner):
                self.owner = owner

            def settimeout(self, _t):
                self.owner.sock = None       # the close lands between settimeout and the read, as it does live

            def recv(self, _n):
                raise OSError(9, "Bad file descriptor")

        c = WC.WsClient("wss://example.invalid", lambda m: None)
        c.sock = VanishingSock(c)
        c.last_msg_at = 0.0
        c.ping_every = 0
        with self.assertRaises(WC.WsError) as got:
            c.poll(1.0)
        self.assertIn("closed", str(got.exception).lower())
        self.assertEqual(c.closed_reason, "closed locally",
                         "the reason has to survive, or the outage page says nothing about why")

    def test_ping_failure_is_a_werror(self):
        import wsclient as WC

        class Dead(Vanishing := object):
            def __init__(self, *_a):
                pass

            def sendall(self, _b):
                raise OSError("broken pipe")

            def settimeout(self, _t):
                pass

        c = WC.WsClient("wss://example.invalid", lambda m: None)
        c.sock = Dead()
        c.last_msg_at = -1e9          # long enough ago that a ping is due
        c.ping_every = 1
        with self.assertRaises(WC.WsError):
            c.poll(0.2)


class TestNormaliseRealPayloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.markets = require("gamma_markets.json")
        cls.book = require("clob_book.json")
        cls.trades = require("data_trades.json")
        cls.frames = require("ws_frames.json")
        cls.history = require("clob_history.json")

    def test_a_gamma_market_row_becomes_a_typed_market(self):
        row = [m for m in self.markets if m.get("clobTokenIds")][0]
        m = N.market_from_gamma(row)
        self.assertEqual(len(m["tokens"]), len(m["outcomes"]))
        # Two ids, and which is which is a join, not a style choice: P04's `markets.id` is Gamma's numeric id
        # (what `/v1/markets/{id}` routes on) while every venue call takes the 0x condition id. An earlier
        # version of the normaliser put the condition id in `id`, which would have written a second market row
        # per market and joined the tape to nothing.
        self.assertTrue(m["condition_id"].startswith("0x"))
        self.assertEqual(m["id"], str(row["id"]))
        self.assertNotEqual(m["id"], m["condition_id"])
        self.assertIsInstance(m["accepting_orders"], bool)
        self.assertEqual(m["min_order_size"], str(row["orderMinSize"]))
        self.assertGreater(m["volume_24h_micro"], 0)
        # `outcomePrices` is a JSON string of decimal strings on the wire; a float in the result would mean
        # the venue's noise became our arithmetic.
        self.assertTrue(all(isinstance(p, int) for p in m["outcome_price_micro"]))

    def test_gamma_stats_with_float_noise_round_instead_of_raising(self):
        row = dict([m for m in self.markets if m.get("clobTokenIds")][0], volume24hr=2101490.4147200002)
        self.assertEqual(N.market_from_gamma(row)["volume_24h_micro"], 2_101_490_414_720)

    def test_the_book_shape_we_recorded_is_the_shape_we_parse(self):
        b = N.book_from_clob(self.book, now_ms=int(self.book["timestamp"]) + 1000)
        self.assertEqual(b["token_id"], str(self.book["asset_id"]))
        self.assertTrue(all(isinstance(p, int) and isinstance(s, int) for p, s in b["bids"]))
        self.assertEqual(b["age_ms"], 1000)
        # one-sided books are real, not hypothetical: the recorded market sits at 0.999 with asks empty
        self.assertEqual(len(b["asks"]), 0)
        self.assertEqual(b["tick_size"], "0.001")

    def test_timestamp_units_are_enforced_both_ways(self):
        with self.assertRaises(N.ShapeError):
            N.ts_ms_from_ws("1789645560")            # seconds passed for milliseconds
        with self.assertRaises(N.ShapeError):
            N.ts_s_from_rest(1789645560001)           # and the other direction
        self.assertEqual(N.age_ms(2000, 1000), 1000)
        with self.assertRaises(N.ShapeError):
            N.age_ms(1000, 2000 + 60_001)              # an event in the future is a unit bug, not a preview

    def test_a_fill_row_is_normalised_and_999_is_not_an_outcome_index(self):
        f = N.fill_from_rest(self.trades[0])
        self.assertEqual(f["ts_ms"], int(self.trades[0]["timestamp"]) * 1000)
        self.assertIsNone(f["outcome_index"], "999 is the venue's 'not applicable' sentinel")
        self.assertEqual(f["source"], "rest")
        self.assertGreater(f["usd_notional_micro"], 0)

    def test_notional_is_rounded_not_rejected_when_the_product_needs_more_scale(self):
        # price 0.001 x size 1.000001 is 9 decimals. Rejecting it would drop real rows from the tape; treating
        # it as an order would be the float bug. So: rounded, in micro, and the strict path stays strict.
        row = dict(self.trades[0], price=0.001, size=1.000001)
        self.assertEqual(N.fill_from_rest(row)["usd_notional_micro"], 1000)
        with self.assertRaises(N.ShapeError):
            N.to_micro("0.0000001", field="price")

    def test_price_change_frame_carries_the_venues_own_top_of_book(self):
        pc = [f for f in self.frames if f.get("_kind") == "price_change"]
        if not pc:
            self.skipTest("no price_change frame in this capture")
        chs = N.price_changes(pc[0])
        self.assertTrue(chs)
        self.assertTrue(any(c["best_bid_micro"] is not None or c["best_ask_micro"] is not None for c in chs),
                        "the resync detector depends on this field existing; if it stops, gap detection has to "
                        "fall back to the TTL alone, and that must be a decision, not a surprise")

    def test_history_is_seconds_and_price_micro(self):
        pts = N.history_points(self.history)
        self.assertTrue(pts)
        for t, p in pts:
            self.assertLess(t, 10 ** 12)
            self.assertTrue(0 < p < 10 ** 6)
        self.assertEqual(pts, sorted(pts))


class TestBooks(unittest.TestCase):
    def snap(self, **kw):
        base = {"bids": [(1000, 5), (900, 7)], "asks": [(1100, 3)], "ts_ms": 1, "hash": "h",
                "tick_size": "0.01", "min_order_size": "5"}
        base.update(kw)
        return base

    def test_derived_values(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(), 1000)
        self.assertEqual((bk.best_bid, bk.best_ask, bk.mid_micro(), bk.spread_micro()), (1000, 1100, 1050, 100))
        self.assertEqual(bk.depth_at(1), (12, 3))
        self.assertAlmostEqual(bk.imbalance(5), 0.6)

    def test_a_lost_delta_is_detected_from_the_venues_declared_top(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(), 1000)
        bk.apply_delta({"ts_ms": 1100, "side": "SELL", "price_micro": 1150, "size_micro": 4,
                        "best_bid_micro": 1000, "best_ask_micro": 1100}, 1100)
        self.assertIsNone(bk.needs_resync(1100, 30_000))
        del bk.bids[1000]                       # what a dropped BUY removal leaves behind
        why = bk.needs_resync(1100, 30_000)
        self.assertIn("best_bid", why)
        self.assertEqual(bk.gap_detections, 1)

    def test_ttl_is_a_second_line_of_defence(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(), 1000)
        self.assertIn("age", bk.needs_resync(5000, 3000) or "")
        self.assertEqual(bk.stale_reads, 1)

    def test_a_new_snapshot_never_merges(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(), 1000)
        bk.apply_snapshot(self.snap(bids=[(800, 1)], asks=[]), 2000)
        self.assertEqual(bk.best_bid, 800)
        self.assertEqual(len(bk.bids), 1, "a level the venue no longer reports must not survive here")

    def test_zero_size_removes_and_a_repeat_size_replaces(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(), 1000)
        bk.apply_delta({"ts_ms": 1001, "side": "SELL", "price_micro": 1100, "size_micro": 0}, 1001)
        self.assertEqual(bk.best_ask, None)
        bk.apply_delta({"ts_ms": 1002, "side": "BUY", "price_micro": 1000, "size_micro": 12}, 1002)
        self.assertEqual(bk.bids[1000], 12)          # replacement, not increment
        self.assertEqual(bk.dup_prices, 1)
        self.assertNotIn(1000 + 12, bk.bids)

    def test_out_of_order_deltas_are_dropped(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(ts_ms=5000), 5000)
        self.assertEqual(bk.apply_delta({"ts_ms": 4000, "side": "BUY", "price_micro": 95_000,
                                         "size_micro": 9}, 5000), "ignored")
        self.assertNotIn(950, bk.bids)

    def test_one_sided_book_has_no_mid(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(bids=[], asks=[(1000, 1)]), 1)
        self.assertTrue(bk.one_sided)
        self.assertIsNone(bk.mid_micro())
        self.assertIsNone(bk.spread_micro())
        self.assertEqual(bk.imbalance(5), -1.0)
        self.assertIsNone(bk.needs_resync(1, 3000), "a one-sided book is a real market state, not an error")

    def test_a_tick_cross_schedules_a_resync(self):
        bk = B.Book("t")
        bk.apply_snapshot(self.snap(), 1)
        self.assertEqual(bk.apply_delta({"ts_ms": 2, "side": "BUY", "price_micro": 970_000, "size_micro": 1}, 2),
                         "resync", "the venue changes tick size across 0.96, so our grid may no longer apply")

    def test_sharding_and_the_200_subscription_cap(self):
        bs = B.BookSet()
        for i in range(450):
            bs.watch("t%04d" % i)
        self.assertEqual([len(x) for x in bs.shard()], [200, 200, 50])

    def test_backpressure_drops_the_long_tail_first(self):
        bs = B.BookSet(max_books=3)
        for tok, prio in (("watch", 0), ("alert", 1), ("top1", 2), ("top2", 2)):
            bs.watch(tok, prio)
        # One over the cap, so exactly one victim; ties inside the lowest class are broken by name so two runs
        # over the same set make the same decision (an operator must be able to diff them).
        self.assertEqual(bs.dropped, ["top1"])
        self.assertEqual(sorted(bs.books), ["alert", "top2", "watch"])

    def test_memory_budget_is_stated_arithmetic_not_a_vibe(self):
        b40 = B.BookSet.memory_bytes(2000, 40) / 1e6
        self.assertLess(b40, 40, "2,000 books must fit in a small container's heap with room for the tape")
        self.assertGreater(B.BookSet.memory_bytes(2000, 500) / 1e6, 3 * b40)


class TestTape(unittest.TestCase):
    def row(self, tx, ts=1000, px=500_000, sz=10_000_000, side="BUY", token="t"):
        return {"tx_hash": tx, "token_id": token, "side": side, "price_micro": px, "size_micro": sz,
                "ts_ms": ts, "usd_notional_micro": px * sz // 10 ** 6, "wallet": "w1"}

    def test_the_dedupe_key_merges_the_same_fill_from_ws_and_rest(self):
        tp = T.Tape()
        self.assertTrue(tp.add(dict(self.row("0xh"), source="ws")))
        self.assertFalse(tp.add(dict(self.row("0xh"), source="rest")))
        self.assertEqual(len(tp.rows), 1)
        self.assertEqual(tp.dups, 1)

    def test_a_second_fill_in_one_transaction_is_not_a_duplicate(self):
        tp = T.Tape()
        tp.add(self.row("0xh", token="a"))
        self.assertTrue(tp.add(self.row("0xh", token="b")))
        self.assertTrue(tp.add(self.row("0xh", token="a", side="SELL")))

    def test_rows_stay_ordered_by_venue_time_and_late_arrivals_are_counted(self):
        tp = T.Tape(capacity=50)
        for ts in (5000, 4000, 6000, 3000):
            tp.add(self.row("0x%d" % ts, ts=ts))
        self.assertEqual([r["ts_ms"] for r in tp.rows], [3000, 4000, 5000, 6000])
        self.assertEqual(tp.late, 2)
        self.assertEqual(tp.newest_ts_ms, 6000)      # not rows[-1]: the newest is a max, not a position

    def test_dedupe_memory_is_bounded_with_the_window(self):
        tp = T.Tape(capacity=8)
        for i in range(40):
            tp.add(self.row("0x%d" % i, ts=1000 + i * 10))
        self.assertLessEqual(len(tp.seen), 8)
        self.assertTrue(tp.add(self.row("0x0", ts=1000)), "the evicted row may reappear and must not be "
                                                          "permanently remembered")

    def test_lag_is_huge_before_the_first_event(self):
        tp = T.Tape()
        self.assertGreater(tp.lag_ms(10_000), 10 ** 12, "no data must never read as perfectly fresh")

    def test_a_fill_stamped_ahead_of_our_clock_is_zero_lag_not_negative(self):
        """Measured live on 2026-09-17: the venue's trade indexer stamps rows up to ~4.3 s ahead of our wall
        clock (while its own HTTP `Date` agrees with us to within a second, so it is the indexer, not drift).
        A negative `lag_ms` is what a UI would print as "-4 s behind", and a p99 of it is meaningless, so the
        clamp is the product behaviour; the chaos tool keeps the signed value visible as a diagnostic."""
        tp = T.Tape()
        tp.add(self.row("0xfuture", ts=10_000_000))
        self.assertEqual(tp.lag_ms(10_000_000 - 4_300), 0)
        self.assertEqual(tp.lag_ms(10_000_000 + 5_000), 5_000, "the clamp is one-sided: real lag still shows")

    def test_fills_above_threshold_in_a_window(self):
        tp = T.Tape()
        tp.add(self.row("0xa", ts=1000, px=500_000, sz=40_000_000))     # $20,000
        tp.add(self.row("0xb", ts=2000, px=500_000, sz=2_000_000))      # $1,000
        self.assertEqual([r["tx_hash"] for r in tp.fills_above(10 * 10 ** 6, 0, 5000)], ["0xa"])


class TestUpDownLifecycle(unittest.TestCase):
    def test_the_bucket_is_in_the_slug_so_the_lifecycle_needs_no_lookup(self):
        l = T.UpDownLifecycle()
        for slug, unit, secs in (("btc-updown-5m-1789645500", "5m", 300), ("bnb-updown-15m-1789644600", "15m",
                                                                                900), ("eth-updown-1h-1", "1h", 3600)):
            p = l.parse(slug)
            self.assertEqual((p["unit"], p["bucket_s"]), (unit, secs), slug)
        self.assertIsNone(l.parse("fed-decision-in-september"))

    def test_dead_updown_markets_are_kept_longer_than_the_ordinary_prune(self):
        l = T.UpDownLifecycle(retain_ms=3600_000)
        self.assertFalse(l.should_prune("btc-updown-5m-1789645500", 1789645500 * 1000 + 30 * 60_000))
        self.assertTrue(l.should_prune("btc-updown-5m-1789645500", 1789645500 * 1000 + 3 * 3600_000))

    def test_next_bucket_is_derivable_so_the_poller_can_pre_register(self):
        self.assertEqual(T.UpDownLifecycle().next_bucket_start_s("btc-updown-5m-1789645500", 0), 1789645800)


class TestFreshness(unittest.TestCase):
    def test_down_beats_stale_beats_ok(self):
        s = FR.Source("ws.tape", "tape", 3000, transport_alive=True)
        now = 10 ** 13
        s.last_frame_at = 0.0
        self.assertEqual(s.state(now, now_mono=100.0), "silent")   # transport up, nothing arriving
        s.transport_alive = False
        self.assertEqual(s.state(now, now_mono=100.0), "down")

    def test_a_pong_does_not_advance_the_event_clock(self):
        f = FR.Freshness()
        f.add(FR.Source("ws.book", "book", 3000, transport_alive=True))
        f.note_event("ws.book", 1000)
        before = f.sources["ws.book"].last_event_ms
        f.note_event("ws.book", 9999, heartbeat=True)
        self.assertEqual(f.sources["ws.book"].last_event_ms, before,
                         "a heartbeat proves the socket is alive; it does not prove the market is")
        self.assertEqual(f.sources["ws.book"].heartbeats, 1)

    def test_event_lag_clamps_at_zero_for_a_venue_clock_ahead_of_ours(self):
        """Same measurement as the tape test: a venue timestamp ahead of `now_ms` must read "fresh", never
        "stale" and never a negative number. `frame_age_ms` deliberately does the opposite for a negative
        value, because that one is OUR monotonic clock and can only go backwards through a bug or a reboot."""
        s = FR.Source("ws.tape", "tape", 3000, transport_alive=True, last_event_ms=10_000_000)
        self.assertEqual(s.event_lag_ms(9_995_700), 0)
        s.last_frame_at = 1.0
        self.assertEqual(s.state(9_995_700, now_mono=1.0), "ok",
                         "a feed running a few seconds ahead of us is not an incident")

    def test_composite_is_the_worst_source_not_the_average(self):
        f = FR.Freshness()
        f.add(FR.Source("ws.tape", "tape", 3000, transport_alive=True))
        f.add(FR.Source("gamma.markets", "metadata", 120_000, transport_alive=False))
        now = int(10 ** 13)
        f.note_event("ws.tape", now)
        self.assertEqual(f.composite(now)["status"], "down",
                         "a live-but-silent source is 'silent'; the dead one is what the page should say")
        self.assertEqual(f.composite(now)["sources"]["ws.tape"], "ok",
                         "a source that just delivered is not the problem; the composite reports the worst")
        self.assertEqual(f.composite(now)["worst_reason"],
                         "live data is disconnected; prices shown are the last received")

    def test_only_down_and_silent_are_pageable(self):
        f = FR.Freshness()
        f.add(FR.Source("ws.tape", "tape", 3000, transport_alive=True))
        now = int(10 ** 13)
        self.assertEqual(f.pageable(now), ["ws.tape"], "no frame ever arrived, on an open socket")
        f.sources["ws.tape"].last_frame_at = 10 ** 9 + 10 ** 8
        f.note_event("ws.tape", now - 20_000)
        import time as _t
        f.sources["ws.tape"].last_frame_at = _t.monotonic()
        self.assertEqual(f.pageable(now), [], "lagging is a UI message, not a page")

    def test_drop_order_is_stable_and_priority_first(self):
        f = FR.Freshness()
        self.assertEqual(f.drop_order({"tail1": 3, "watch": 0, "tail2": 3}), ["tail1", "tail2", "watch"])


class TestUniverse(unittest.TestCase):
    def test_backfill_pages_on_id_keyset_and_resumes(self):
        b = U.Backfill()
        self.assertEqual(b.params()["id__gt"], 0)
        b.feed([{"id": "5"}, {"id": "6"}])
        self.assertEqual(b.cursor_id, 6)
        b2 = U.Backfill(cursor_id=b.cursor_id)
        self.assertEqual(b2.params()["id__gt"], 6, "a restart must continue, not restart")

    def test_a_full_page_never_marks_the_backfill_done(self):
        b = U.Backfill(page_size=2)
        b.feed([{"id": "1"}, {"id": "2"}])
        self.assertFalse(b.done)
        b.feed([{"id": "3"}])
        self.assertTrue(b.done)

    def test_backfill_drops_rows_it_has_already_seen(self):
        b = U.Backfill(cursor_id=10)
        self.assertEqual(b.feed([{"id": "9"}, {"id": "11"}]), [{"id": "11"}])

    def test_discovery_counts_overlap_so_a_gap_is_visible(self):
        d = U.Discover()
        self.assertEqual(len(d.feed([{"id": "3"}, {"id": "2"}])), 2)
        self.assertEqual(d.feed([{"id": "3"}]), [])
        self.assertEqual(d.overlap, 1)

    def test_resolution_is_not_a_price_move(self):
        r = U.Resolution
        self.assertEqual(r.classify(999_000, 1_000_000, closed=True, accepting=False), "resolution")
        self.assertEqual(r.classify(4_000, 0, closed=True, accepting=False), "resolution")
        self.assertEqual(r.classify(500_000, 560_000, closed=False, accepting=True), "move")
        self.assertEqual(r.classify(990_000, 999_000, closed=False, accepting=True), "move",
                         "0.999 on an open market is a quote, not a settlement")
        self.assertEqual(r.classify(999_000, 1_000_000, closed=False, accepting=True), "resolved-looking")
        self.assertTrue(r.suppresses_signal("resolution"))
        self.assertFalse(r.suppresses_signal("move"))

    def test_metadata_edits_are_versioned_with_old_and_new(self):
        v = U.MetaVersion()
        out = v.diff({"question": "old?", "endDate": "2026-01-01", "updatedAt": "1"},
                     {"question": "old?", "endDate": "2026-02-01", "updatedAt": "2"})
        self.assertEqual(out, [{"field": "endDate", "old": "2026-01-01", "new": "2026-02-01"}])
        self.assertEqual(v.diff({"question": "a"}, {"question": "a"}), [], "updatedAt moves on every row; only "
                          "tracked fields may create a version")

    def test_the_dead_rule_requires_quiet_and_small_and_no_reason_to_watch(self):
        p = U.Prune(now_ms=10 ** 13, idle_s=1000)
        dead = {"last_fill_ms": 0, "last_book_change_ms": 0, "volume_24h_micro": 0}
        self.assertTrue(p.is_dead(dead))
        self.assertFalse(p.is_dead(dict(dead, has_open_alert=True)))
        self.assertFalse(p.is_dead(dict(dead, in_watchlist=True)))
        self.assertFalse(p.is_dead(dict(dead, has_open_order=True)))
        self.assertFalse(p.is_dead(dict(dead, volume_24h_micro=p.floor_usd_micro + 1)))

    def test_waking_needs_more_volume_than_dying_took(self):
        p = U.Prune(now_ms=10 ** 13)
        self.assertFalse(p.should_wake({"volume_24h_micro": p.floor_usd_micro + 10}))
        self.assertTrue(p.should_wake({"volume_24h_micro": p.wake_volume_usd_micro}))
        self.assertTrue(p.should_wake({"volume_24h_micro": 0, "last_fill_ms": 10 ** 13 - 5000}))


class TestSourceMap(unittest.TestCase):
    """The endpoint inventory D1 asks for, kept honest by asserting against the recorded fixtures rather than
    against prose: one case per source, naming the file that proves it."""

    SOURCES = {
        "gamma.events": "gamma_events.json", "gamma.markets": "gamma_markets.json", "gamma.tags":
            "gamma_tags.json", "clob.book": "clob_book.json", "clob.books": "clob_books.json",
        "clob.midpoint": "clob_midpoint.json", "clob.spread": "clob_spread.json", "clob.price":
            "clob_price_buy.json", "clob.market": "clob_market.json", "clob.history": "clob_history.json",
        "data.trades": "data_trades.json", "data.positions": "data_positions.json", "data.activity":
            "data_activity.json", "lb.volume": "lb_volume.json", "ws.market": "ws_frames.json",
    }

    def test_every_documented_source_has_a_real_response_on_disk(self):
        missing = [k for k, f in self.SOURCES.items() if not (FX / f).is_file()]
        # /books is a live 400 ("Invalid payload") for every body shape we tried, so its absence is a finding
        # about the venue, not a gap in this test. Everything else must be present.
        self.assertEqual(missing, ["clob.books"], "the batch endpoint is the only one we could not capture")

    def test_the_batch_books_endpoint_still_rejects_the_documented_payload(self):
        """Recorded so that P06's budget maths does not silently assume a batch call exists.

        Measured 2026-09-17: POST /books returns 400 {"error":"Invalid payload"} for `{"token_ids": [...]}`,
        `{"asset_ids": [...]}`, a bare array, the same via query string, and with bids/asks flags added —
        including a single valid token id. `GET /book?token_id=` works (200). Until this is resolved, per-token
        polling at the clob.book budget is the only option, and the memory/shard arithmetic in books.py is
        sized for that.
        """
        self.assertFalse((FX / "clob_books.json").is_file())
        self.assertTrue((FX / "clob_book.json").is_file())


if __name__ == "__main__":
    unittest.main()
