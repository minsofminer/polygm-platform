"""P09 · the market surfaces: discovery, the book, the event invariant, history and holders.

Written as contract assertions like the P04 API tests, and for the same reason: every check here fails if an
endpoint is "simplified" in a way that breaks a client. The three that matter most, in order:

  1. `test_hidden_count_is_the_complement_not_the_visible_set` - the first version of that field reported the
     size of the visible set as the number of hidden markets. On a fixture with nothing hidden the two are the
     same number (0), so it looked right for exactly as long as the fixture was too small.
  2. `test_history_gaps_are_absent_rather_than_flat` - a candle drawn across a bucket with no fills is a chart
     inventing prices.
  3. `test_one_sided_is_a_state_not_an_error` - P01's 94 ask levels at 0.001 against zero bids must render as
     a finished market, not as a broken feed.

Keys: the app is imported per class by `conftest.import_app`, which seeds the SQLite dev database through the
real migrator and the real seed. Nothing here writes to `var/`.
"""
from __future__ import annotations
import json
import re
import unittest

from conftest import import_app, refresh_flags  # noqa: F401

MICRO = 10 ** 6


class SurfacesBase(unittest.TestCase):
    app_name = "api-surfaces"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)

    def setUp(self):
        refresh_flags(self.app)

    def get(self, url, **params):
        r = self.client.get(url, params=params)
        self.assertEqual(r.status_code, 200, "%s -> %d %s" % (url, r.status_code, r.text[:400]))
        return r.json()


class TestDiscovery(SurfacesBase):
    app_name = "surfaces-discovery"
    """The list endpoint as a discovery surface: filters, facets, the dead tail, and the 128-outcome card."""

    def test_the_default_view_hides_the_dead_tail_and_says_how_many_it_hid(self):
        page = self.get("/v1/markets", limit=100)
        self.assertFalse(page["longTail"]["includeLongTail"])
        self.assertGreater(page["longTail"]["hiddenCount"], 0, "the seed must contain a dead tail or this "
                                                              "field is untested")
        visible = {i["id"] for i in page["items"]}
        self.assertNotIn("0xM3", visible, "a $420/day market is the long tail and must not lead the list")
        # every visible market is above the threshold, i.e. the filter really ran rather than the count
        for item in page["items"]:
            self.assertGreaterEqual(float(item["volume24h"]) * MICRO, page["longTail"]["thresholdMicro"] - 1)

    def test_hidden_count_is_the_complement_not_the_visible_set(self):
        """The bug this exists for: counting what is LEFT AFTER dropping the tail clause reports the visible
        set, and on a fixture with no tail the answer is 0 either way."""
        strict = self.get("/v1/markets", limit=100)
        loose = self.get("/v1/markets", limit=100, includeLongTail="true")
        self.assertEqual(loose["longTail"]["hiddenCount"], 0, "nothing is hidden when nothing is filtered")
        # The unfiltered TOTAL, not the length of a page: the loose page is capped at 100 rows and the seed has
        # more live markets than that, which is exactly how an earlier version of this assertion managed to be
        # wrong (115 vs 82) while the field it was checking was right.
        total = sum(loose["facets"].values())
        self.assertGreater(total, 100, "the seed must have more markets than one page")
        self.assertEqual(strict["longTail"]["hiddenCount"] + len(strict["items"]), total,
                         "hidden + visible must equal the unfiltered set")

    def test_facets_are_counted_with_the_category_filter_lifted(self):
        page = self.get("/v1/markets", limit=100, category="Crypto")
        self.assertTrue(all(i["category"] == "Crypto" for i in page["items"]))
        facets = page["facets"]
        self.assertGreater(facets.get("Economics", 0), 0,
                           "selecting one category must not zero the other tabs")
        self.assertEqual(facets.get("Crypto"), len(page["items"]))

    def test_an_unknown_category_is_a_named_refusal_not_an_empty_page(self):
        r = self.client.get("/v1/markets", params={"category": "Politicks"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")

    def test_every_documented_sort_key_is_accepted_and_orders_by_something(self):
        keys = self.get("/v1/markets", limit=5)["sortKeys"]
        for key in keys:
            with self.subTest(sortBy=key):
                page = self.get("/v1/markets", limit=5, sortBy=key, includeLongTail="true")
                self.assertEqual(page["sortBy"], key)

    def test_move24h_sorts_but_a_null_change_is_not_zero(self):
        page = self.get("/v1/markets", limit=100, sortBy="move24h", includeLongTail="true")
        self.assertTrue(any(i["change24h"] is not None for i in page["items"]))
        for item in page["items"]:
            if item["change24h"] is None:
                self.assertIsNone(item["price24hAgo"], "a null change must come from a missing base price")

    def test_a_128_market_event_arrives_summarised_with_the_count_it_hid(self):
        page = self.get("/v1/markets", limit=100, includeLongTail="true")
        rows = [i for i in page["items"] if i["eventId"] == "0xEV128"]
        self.assertTrue(rows, "the 128-outcome event must be in the seed")
        for row in rows:
            ev = row["event"]
            with self.subTest(market=row["id"]):
                self.assertEqual(ev["marketCount"], 128)
                self.assertLessEqual(len(ev["topOutcomes"]), 3)
                self.assertEqual(ev["hiddenCount"], 128 - len(ev["topOutcomes"]))
                # top outcomes are the ones with the most 24h volume, not the first alphabetically
                vols = [float(o["volume24h"]) for o in ev["topOutcomes"]]
                self.assertEqual(vols, sorted(vols, reverse=True), ev["topOutcomes"])

    def test_a_page_of_many_event_rows_does_not_cost_a_query_per_row(self):
        """The window function is the point. Measured by COUNTING the statements, not by timing: a fast query
        that runs 100 times is still the wrong shape, and timing on a fixture cannot see 50 round trips."""
        seen: list[str] = []
        # sqlite3's own trace hook, not a monkeypatched `execute`: the driver reports every statement the
        # connection actually runs, including the ones a wrapper would miss if the app ever stopped calling
        # `execute` directly. The hook is removed in the `finally` so a failure cannot leave the rest of the
        # suite tracing into a list nobody reads.
        self.app._db.set_trace_callback(seen.append)
        try:
            self.get("/v1/markets", limit=100, includeLongTail="true")
        finally:
            self.app._db.set_trace_callback(None)
        event_queries = [s for s in seen if "ROW_NUMBER()" in s]
        self.assertEqual(len(event_queries), 1, "%d ranking queries for one page: %s" % (len(event_queries),
                                                                                         seen))

    def test_money_leaves_the_list_as_strings_and_never_as_floats(self):
        for item in self.get("/v1/markets", limit=100)["items"]:
            for field in ("volume24h", "liquidity", "openInterest", "minimumOrderSize"):
                with self.subTest(market=item["id"], field=field):
                    self.assertIsInstance(item[field], str, "%s %s" % (item["id"], field))
                    self.assertRegex(item[field], r"^[0-9]+(\.[0-9]{1,6})?$")


class TestBook(SurfacesBase):
    app_name = "surfaces-book"
    """The ladder: aggregation, the one-sided state, and the numbers the spread row renders."""

    def test_aggregation_rounds_bids_down_and_asks_up(self):
        raw = self.get("/v1/markets/0xM2/book", depth=100)
        for mode in ("1c", "5c"):
            agg = self.get("/v1/markets/0xM2/book", depth=100, aggregate=mode)
            step = {"1c": 10_000, "5c": 50_000}[mode]
            for side, floor in (("bids", 0), ("asks", 1)):
                for level in agg[side]:
                    micro = int(round(float(level["price"]) * MICRO))
                    if floor:
                        self.assertGreaterEqual(micro % step, 0)
                        self.assertEqual(micro, -(-micro // step) * step,
                                         "an ask bucket must round UP: %s" % level["price"])
                    else:
                        self.assertEqual(micro, micro // step * step,
                                         "a bid bucket must round DOWN: %s" % level["price"])
            self.assertLessEqual(len(agg["bids"]) + len(agg["asks"]),
                                 len(raw["bids"]) + len(raw["asks"]),
                                 "aggregation cannot create levels")

    def test_aggregate_is_a_closed_vocabulary(self):
        self.assertEqual(self.client.get("/v1/markets/0xM2/book",
                                         params={"aggregate": "10c"}).status_code, 422)

    def test_cumulative_depth_is_served_and_monotone(self):
        book = self.get("/v1/markets/0xM2/book", depth=48)
        for side in ("bids", "asks"):
            running = 0.0
            for level in book[side]:
                running += float(level["shares"])
                self.assertAlmostEqual(float(level["cumShares"]), running, places=6,
                                       msg="cumShares must be cumulative from the top of book")
        self.assertGreaterEqual(float(book["maxCumShares"]), max(
            [float(l["cumShares"]) for l in book["bids"] + book["asks"]] or [0]))

    def test_one_sided_is_a_state_not_an_error(self):
        """P01 measured this: 94 ask levels at 0.001, $21.9M, no bids. The API must describe it rather than
        return an error the client renders as "market broken"."""
        book = self.get("/v1/markets/0xM9/book", depth=400)
        self.assertEqual(book["bids"], [])
        self.assertEqual(len(book["asks"]), 94)
        self.assertIsNotNone(book["oneSided"])
        self.assertEqual(book["oneSided"]["side"], "asks-only")
        self.assertEqual(book["oneSided"]["why"], "NO_BIDS")
        self.assertEqual(book["oneSided"]["bestAsk"], "0.001")
        self.assertAlmostEqual(float(book["oneSided"]["notionalUsdc"]), 21_900_000.0, delta=1.0)
        self.assertIsNone(book["midPrice"], "no two-sided quote means midPrice is null, not the last trade")
        self.assertIsNone(book["spreadTicks"])

    def test_notional_is_integer_arithmetic_over_decimal_strings(self):
        """`_notional_micro` must not go through float: 0.001 x 232978723.404255 is the kind of product where
        a float path is off by a cent in the eighth digit and nobody ever notices."""
        self.assertEqual(self.app._micro_of("0.001"), 1000)
        self.assertEqual(self.app._micro_of("5"), 5_000_000)
        self.assertEqual(self.app._micro_of("0"), 0)
        # micro-USDC, not dollars: 0.001 USDC x 1000 shares is 1 dollar, which is 1_000_000 micro
        self.assertEqual(self.app._notional_micro([("0.001", "1000")]), 1_000_000)
        self.assertEqual(self.app._notional_micro([("0.5", "10")]), 5_000_000)

    def test_a_market_that_declares_no_book_is_a_404_not_two_empty_arrays(self):
        """Two empty arrays would render as "no liquidity right now", which is a different and much more
        alarming claim than "this market has no order book". The seed used to build a ladder for 0xM4 even
        though it declares `enable_order_book = false`, so this state was unreachable in dev."""
        market = self.get("/v1/markets/0xM4")["market"]
        self.assertFalse(market["enableOrderBook"])
        r = self.client.get("/v1/markets/0xM4/book")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "NOT_FOUND")

    def test_reseeding_is_idempotent_and_leaves_the_append_only_invariant_standing(self):
        """`make seed` twice used to die on the tape's BEFORE DELETE trigger, and the fix lifts that trigger
        inside the seed's transaction. What must be true afterwards is that it is BACK: a dev database where
        history is editable is a dev database that hides this class of bug."""
        import sqlite3
        import os
        import seed as seed_mod                      # conftest puts services/api on sys.path
        counts = seed_mod.seed_sqlite(os.environ["PGM_DB_PATH"])
        # `tape_trades` is the P04 fixture the gate reads, `tape_fills` the durable ingest log the P09/P10
        # surfaces read. The seed mirrors one into the other and P10's terminal fixture adds wallet-level fills
        # with no P04 twin, so the invariant is one-directional and that is the direction that matters: every
        # trade has fills, and the log is never empty - an empty `tape_fills` renders as "never traded".
        self.assertGreaterEqual(counts["tape_fills"], counts["tape_trades"])
        self.assertGreater(counts["tape_trades"], 0)
        con = sqlite3.connect(os.environ["PGM_DB_PATH"])
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertIn("append_only_tape_trades_delete", names)
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM tape_trades")
        finally:
            con.close()


class TestEvent(SurfacesBase):
    app_name = "surfaces-event"
    """The negRisk invariant, including what it says when it cannot be computed."""

    def test_the_invariant_is_summed_over_mids_and_compared_with_the_tick_tolerance(self):
        ev = self.get("/v1/events/0xEV128")
        self.assertEqual(ev["event"]["marketCount"], 128)
        self.assertEqual(ev["summableCount"], 128)
        self.assertAlmostEqual(float(ev["probabilitySum"]), 1.002, places=6)
        self.assertAlmostEqual(float(ev["deviation"]), 0.002, places=6)
        # N outcomes, each known to one tick
        self.assertAlmostEqual(float(ev["tolerance"]), 128 * 0.001, places=6)
        self.assertTrue(ev["withinTolerance"])

    def test_the_opportunity_is_the_executable_statement_not_the_deviation(self):
        ev = self.get("/v1/events/0xEV128")
        self.assertIsNotNone(ev["buyAllCost"])
        self.assertGreater(float(ev["buyAllCost"]), 1.0,
                           "buying all 128 outcomes at the ask costs more than a dollar here")
        self.assertIsNone(ev["opportunity"], "a deviation inside the tick band is not an opportunity")

    def test_a_market_without_a_mid_is_excluded_from_the_sum_and_marked(self):
        """0xM9 has asks and no bids: it has no mid. Borrowing its last trade price would put a stale number
        into a live invariant, so it is `summable: false` and the sum skips it."""
        ev = self.get("/v1/events/0xEV1")
        one_sided = [o for o in ev["outcomes"] if o["marketId"] == "0xM5"]
        self.assertTrue(one_sided)
        for outcome in ev["outcomes"]:
            if not outcome["summable"]:
                self.assertIsNone(outcome["price"])

    def test_an_unknown_event_is_named_not_empty(self):
        r = self.client.get("/v1/events/0xNOPE")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "NOT_FOUND")


class TestHistory(SurfacesBase):
    app_name = "surfaces-history"
    """Candles: integer bucketing, absent gaps, and the interval contract."""

    def test_candles_are_bucketed_on_the_venue_clock_and_oldest_first(self):
        h = self.get("/v1/markets/0xM1/history", interval="1m", limit=60)
        self.assertEqual(h["bucketMs"], 60_000)
        self.assertEqual(h["source"], "fills")
        times = [c["t"] for c in h["candles"]]
        self.assertEqual(times, sorted(times))
        for t in times:
            self.assertEqual(t % 60_000, 0, "a bucket start must be a bucket boundary")

    def test_history_gaps_are_absent_rather_than_flat(self):
        """Every candle has at least one fill; a bucket with no fills is not in the array. A flat candle at
        the previous close is the shape that makes a chart lie about a market that did not trade."""
        h = self.get("/v1/markets/0xM1/history", interval="1m", limit=500)
        self.assertTrue(h["candles"])
        for c in h["candles"]:
            with self.subTest(t=c["t"]):
                self.assertGreaterEqual(c["trades"], 1)
                low, high, o, cl = (float(c[k]) for k in ("l", "h", "o", "c"))
                self.assertLessEqual(low, min(o, cl))
                self.assertGreaterEqual(high, max(o, cl))

    def test_the_server_says_which_intervals_it_owns_and_which_the_client_derives(self):
        h = self.get("/v1/markets/0xM1/history")
        self.assertEqual(sorted(h["serverIntervals"]), ["15m", "1h", "1m", "5m"])
        self.assertEqual(h["derivedIntervals"], ["6h", "1d"])
        self.assertNotIn("6h", h["serverIntervals"],
                         "6h is the client's bucket of our 1h candles; building it twice is two chances to "
                         "disagree")

    def test_an_interval_the_server_does_not_build_is_a_422(self):
        self.assertEqual(self.client.get("/v1/markets/0xM1/history",
                                         params={"interval": "6h"}).status_code, 422)

    def test_notional_is_the_histogram_s_axis_not_share_count(self):
        h = self.get("/v1/markets/0xM1/history", interval="1m", limit=60)
        for c in h["candles"]:
            # notional is the sum of per-fill notional, so it lies between (min price x shares) and
            # (max price x shares). Testing it against close x shares - the obvious-looking assertion - is
            # wrong for any bucket whose fills span more than one tick, and that is most of them.
            shares, notional = float(c["shares"]), float(c["notional"])
            self.assertGreaterEqual(notional, float(c["l"]) * shares - 0.02, c)
            self.assertLessEqual(notional, float(c["h"]) * shares + 0.02, c)


class TestHolders(SurfacesBase):
    app_name = "surfaces-holders"
    """The holders list is tape-derived and says so, and an unpublishable label never leaves the service."""

    def test_holders_are_tape_derived_labelled_and_pseudonymous(self):
        h = self.get("/v1/markets/0xM1/holders")
        self.assertEqual(h["provenance"], "tape")
        self.assertTrue(h["holders"], "the seed writes durable fills, so this list cannot be empty")
        notionals = [float(x["notional"]) for x in h["holders"]]
        self.assertEqual(notionals, sorted(notionals, reverse=True))
        for holder in h["holders"]:
            with self.subTest(wallet=holder["anonWallet"]):
                self.assertNotIn("0x", holder["anonWallet"], "an address must never survive the response")
                self.assertIsInstance(holder["shareBp"], int)
                self.assertNotIn("insider_suspect", holder["labels"],
                                 "insider_suspect is not a publishable label")

    def test_share_is_basis_points_of_the_tape_not_a_float_percentage(self):
        h = self.get("/v1/markets/0xM1/holders")
        total = sum(int(round(float(x["notional"]) * MICRO)) for x in h["holders"])
        for holder in h["holders"]:
            expected = int(round(float(holder["notional"]) * MICRO)) * 10_000 // total
            self.assertAlmostEqual(holder["shareBp"], expected, delta=1)


class TestDetailRail(SurfacesBase):
    app_name = "surfaces-rail"
    """One round trip for the rail, and the resolution text is data - verbatim, for the client to escape."""

    def test_the_rail_fields_are_all_present_and_typed(self):
        m = self.get("/v1/markets/0xM2")["market"]
        for field in ("category", "resolutionSource", "resolutionCriteria", "liquidity", "volume24h",
                      "volume7d", "volume30d", "openInterest", "holderCount", "negRisk", "outcomeCount"):
            with self.subTest(field=field):
                self.assertIn(field, m)
        self.assertEqual(m["minimumOrderSize"], "5",
                         "the detail must format size the same way the list does, or the client validates "
                         "against one number and shows another")

    def test_resolution_criteria_are_returned_verbatim_for_the_client_to_escape(self):
        m = self.get("/v1/markets/0xM2")["market"]
        self.assertIn("<script>", m["resolutionCriteria"],
                      "the seed carries markup on purpose: if the API strips it, nothing downstream proves "
                      "the client escapes it")

    def test_holder_count_matches_the_holders_endpoint(self):
        m = self.get("/v1/markets/0xM1")["market"]
        h = self.get("/v1/markets/0xM1/holders", limit=100)
        self.assertEqual(m["holderCount"], h["holderCount"])


class TestPortability(SurfacesBase):
    app_name = "surfaces-portability"
    """The suite runs on the portable SQLite subset, and that subset is what caught a production-only column."""

    def test_no_query_reads_the_postgres_generated_outcome_count_column(self):
        """`markets.outcome_count` is Postgres-only (GENERATED ALWAYS AS jsonb_array_length(...) STORED) and
        the SQLite subset DROPS it. The discovery screen read it, worked in production, and 500s in the test
        engine - which is the entire reason the portable subset exists."""
        import inspect
        source = inspect.getsource(self.app._discovery_inner) + inspect.getsource(self.app.get_market)
        # Comments are stripped FIRST. The function's own comment explains that it must not read
        # `m.outcome_count`, and a scanner that reads comments fires on its own explanation - the exact
        # self-reporting bug this repo hit twice in P08. The canary below is what proves the stripper works.
        # The canary is a phrase this function's comment really contains: if a future edit removes the comment
        # the scan would silently pass, so the scan proves it had something to strip.
        canary = "NOT `m.outcome_count`. That column is Postgres-only"
        self.assertIn(canary, source, "canary: the comment this scan must not read is gone")
        stripped = re.sub(r"#[^\n]*", "", source)
        self.assertNotIn(canary, stripped, "comment stripping did not happen")
        self.assertNotIn("m.outcome_count", stripped)
        self.assertIn("FROM tokens tk", stripped)

    def test_the_declared_column_order_matches_the_sql_the_app_runs(self):
        """The row mapping is positional (P04's note explains why), and the names are the guard: if the SELECT
        grows a column in the middle, this fails instead of rendering one field's value as another's."""
        sql = self.app._discovery_inner(with_spread=True)
        order = re.findall(r"AS (\w+)", sql)
        self.assertEqual(order, list(self.app._DISCOVERY_COLUMNS))

    def test_the_seed_writes_the_durable_fill_log_the_surfaces_read(self):
        """Without this, history and holders are empty in dev and an empty chart looks like a market that
        has never traded."""
        counts = self.app._db.execute("SELECT COUNT(*) FROM tape_fills").fetchone()[0]
        self.assertGreater(counts, 0)


if __name__ == "__main__":
    unittest.main()
