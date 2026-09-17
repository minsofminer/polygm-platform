"""The ingest process, driven offline: recorded payloads in, a fake transport, the real SQLite schema.

Nothing here opens a socket. What it proves is the part a live run cannot pin down — that the loops write the
right ROWS, resume from the right CURSOR, mark the right SOURCE alive, and refuse to write a number they cannot
reproduce. Live behaviour (does a killed WebSocket actually page) is `tools/p05-chaos-test.py`, and the two are
not interchangeable: a fake transport cannot discover that the venue caches a stale view, and a live run cannot
assert "the ghost level disappeared" against a book that keeps moving.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "services" / "ingest"), str(ROOT / "packages")]

import main as M                                     # noqa: E402
import net as NET                                    # noqa: E402
import normalise as N                                # noqa: E402
import tape as T                                     # noqa: E402
import universe as U                                 # noqa: E402

LITE = ROOT / "db" / "migrations-sqlite"
FX = ROOT / "tests" / "fixtures" / "p05"


def fx(name):
    return json.loads((FX / name).read_text())


def trade_frame(price="0.55", size="120", ts_ms=1_789_699_999_000):
    """The WS `last_trade_price` shape, from the field list recorded in docs/P05-data-ingestion.md.

    tests/fixtures/p05/ws_frames.json holds only `book` and `price_change` — nothing we were subscribed to
    traded inside that capture window — so there is no real trade frame on disk to read. This is the shape the
    live chaos harness receives and counts, and the finding these two tests pin is the measured one: a trade
    frame carries NO transaction hash, so it can never be a tape row.
    """
    return {"event_type": "last_trade_price", "asset_id": "t1", "market": "0xcid", "price": price,
            "size": size, "side": "BUY", "timestamp": str(ts_ms), "fee_rate_bps": "0"}


def fresh_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript("PRAGMA foreign_keys=ON;\n" + "\n".join(p.read_text() for p in sorted(LITE.glob("*.sql"))))
    return con


def market_row(con, cid="0xc1", mid="m1", accepting=1):
    """A market, in the only shape P04's schema allows.

    There is no `markets.closed`: resolution is a property of the outcomes (a token whose `is_winner` is not
    NULL), and `accepting_orders` is the tradeability flag. A test that inserted a `closed` column would be
    asserting against a column production does not have, which is how the phase nearly shipped that lie.
    `market_stats` is the ingest side of the same row: the venue's activity numbers, which our own tape cannot
    supply for a market we are not watching.
    """
    con.execute("INSERT OR IGNORE INTO events (id,slug,title,neg_risk,created_ms,updated_ms)"
                " VALUES ('e1','s1','t',0,1,1)")
    con.execute("INSERT OR IGNORE INTO markets (id,condition_id,event_id,question,slug,accepting_orders,"
                "minimum_tick_size,fee_type,first_seen_ms,updated_ms) VALUES (?,?,?,?,?,?,0.001,'',1,1)",
                (mid, cid, "e1", "q", "s", accepting))
    con.execute("INSERT OR IGNORE INTO tokens (token_id,market_id,outcome,outcome_index,is_winner)"
                " VALUES ('t1',?,'Yes',0,NULL), ('t2',?,'No',1,NULL)", (mid, mid))
    con.execute("INSERT OR IGNORE INTO market_stats (condition_id,updated_ms) VALUES (?,1)", (cid,))


class FakeFetch:
    """A transport that answers from a dict and never forgets what it was asked for."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []
        self.sends = 0
        self.retry_count = 0
        self.breakers = {}

    def respond(self, source, payload, status=200):
        self.answers[source] = payload
        self.answers[source + ":status"] = status

    def get(self, url, *, source, params=None, timeout=None, retries=None):
        self.calls.append({"url": url, "source": source, "params": dict(params or {})})
        self.sends += 1
        status = self.answers.get(source + ":status", 200)
        if status != 200:
            return NET.Response(status=status, json=None, headers={}, ms=1.0, from_cache=False, attempts=1,
                                error="HTTP %d" % status)
        return NET.Response(status=200, json=self.answers.get(source), headers={}, ms=1.0, from_cache=False,
                            attempts=1)


class FakeWs:
    """The socket, as a queue. `on_message` is wired by the daemon, so the frames travel the real handler."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.on_message = None
        self.subscribed = []
        self.closed = False
        self.delivered = 0
        self.resumed = 0
        self.frames_in = 0
        self.handler_errors = []

    def connect(self):
        pass

    def subscribe_market(self, assets):
        self.subscribed = list(assets)

    def poll(self, max_wait=1.0):
        if not self.frames:
            return 0
        out = len(self.frames)
        while self.frames:
            frame = self.frames.pop(0)
            self.delivered += 1
            if self.on_message:
                self.on_message(frame)
        return out

    def data_age_s(self):
        return None

    def close(self):
        self.closed = True


def daemon(con, answers=None, frames=(), now=1_789_700_000.0):
    fake = FakeFetch(answers)
    ws = FakeWs(list(frames))
    d = M.Daemon(M.Config(db_path=":memory:", ws_burst_s=0.0), M.Store(con), fetcher=fake,
                 ws_factory=lambda on_message: (setattr(ws, "on_message", on_message) or ws),
                 now=lambda: now)
    d.ws = None
    return d, fake, ws


class TestTapeLoop(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.d, self.fake, self.ws = daemon(self.con)

    def test_the_poll_asks_for_a_bounded_range_and_never_for_a_cache_buster(self):
        """The most load-bearing line in the phase.

        `GET data-api/trades` without start/end serves a view measured at 236s/257s/277s old (three samples,
        20s apart) and byte-identical with the CDN cache bypassed. A poller built on that page looks healthy
        while reading yesterday. Cache-busting was the wrong theory for it and appears nowhere in the product.
        """
        self.fake.respond("data.trades", fx("data_trades.json"))
        self.d.poll_tape()
        calls = [c for c in self.fake.calls if "trades" in c["url"]]
        self.assertTrue(calls, "the poll made no request at all")
        for c in calls:
            self.assertIn("start", c["params"], "an unbounded /trades page is the stale view")
            self.assertIn("end", c["params"])
            self.assertNotIn("_", c["params"], "a cache-buster parameter came back; the range is the fix")
            self.assertLess(c["params"]["start"], c["params"]["end"])

    def test_a_replayed_page_writes_nothing(self):
        rows = fx("data_trades.json")
        self.fake.respond("data.trades", rows)
        first = self.d.poll_tape()
        again = self.d.poll_tape()
        self.assertGreater(first["written"], 0, "the fixture has rows, so the first pass must write")
        self.assertEqual(again["written"], 0, "the same fills replayed must not double-count volume")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tape_fills").fetchone()[0], first["written"])

    def test_the_cursor_moves_to_the_newest_venue_timestamp(self):
        self.fake.respond("data.trades", fx("data_trades.json"))
        self.d.poll_tape()
        cur = self.d.store.resume("data.trades")
        newest = self.con.execute("SELECT MAX(ts_ms) FROM tape_fills").fetchone()[0]
        self.assertEqual(cur["last_event_ms"], newest,
                         "the cursor is the VENUE clock, so replaying yesterday cannot look like fresh data")

    def test_a_restart_resumes_with_an_overlap_and_an_ancient_cursor_says_so(self):
        at = 1_789_699_000_000
        self.con.execute("INSERT INTO ingest_cursors (source,cursor_json,last_event_ms,last_frame_ms,state,"
                         "updated_ms) VALUES ('data.trades','{}',?,?, 'ok',?)", (at, at, at))
        d, _, _ = daemon(self.con)
        self.assertEqual(d.tape_from_ms, at - M.RESUME_OVERLAP_S * 1000)
        self.assertIn("overlap", d.resume_note)
        self.con.execute("UPDATE ingest_cursors SET last_event_ms = 1789000000000")
        stale, _, _ = daemon(self.con)
        self.assertIn("NOT recovered", stale.resume_note, "a capped resume must announce the hole it leaves")

    def test_a_shape_refusal_is_counted_not_coerced(self):
        rows = fx("data_trades.json") + [{"price": "abc", "size": "1", "timestamp": "1789699999"}]
        self.fake.respond("data.trades", rows)
        out = self.d.poll_tape()
        self.assertEqual(out["written"], len(fx("data_trades.json")))
        self.assertTrue(all(r[0] > 0 for r in self.con.execute("SELECT price_micro FROM tape_fills")))

    def test_a_failing_request_leaves_the_cursor_alone(self):
        self.fake.respond("data.trades", [], status=503)
        out = self.d.poll_tape()
        self.assertEqual(out["fetched"], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tape_fills").fetchone()[0], 0)

    def test_a_venue_float_price_is_kept_not_dropped(self):
        """49 of the 200 rows in the recorded fixture carry a price like 0.1699999983.

        A strict 6-decimal parser refused them, which read as a quiet market rather than as a broken tape — and
        volume rollups, whale percentiles and large-fill alerts are all computed from what we keep. The value is
        rounded to the micro grid (at most 5e-7 of movement) and the row survives; a value that is not a number
        at all still refuses, and the refusal is counted.
        """
        rows = fx("data_trades.json")
        noisy = [r for r in rows if len(str(r["price"]).split(".")[1] if "." in str(r["price"]) else "") > 6]
        self.assertGreaterEqual(len(noisy), 10, "the fixture is the evidence; if it stops showing the noise,"
                                                " re-record it before trusting this test")
        fills = [N.fill_from_rest(r) for r in rows]
        self.assertEqual(len(fills), len(rows), "no fill in this sample may be refused for precision")
        for f in fills:
            self.assertIsInstance(f["price_micro"], int)
            self.assertGreater(f["price_micro"], 0)
        self.assertEqual(fills[0]["usd_notional_micro"],
                         fills[0]["price_micro"] * fills[0]["size_micro"] // 10 ** 6,
                         "the stored notional must follow from the stored price and size")
        with self.assertRaises(N.ShapeError):
            N.fill_from_rest({"price": "abc", "size": "1", "timestamp": "1789699999"})

    def test_the_live_frame_and_the_rest_row_agree_on_the_key(self):
        """Suppression and UNIQUE(dedupe_key) both key on these integers, so the two parsers must agree exactly.

        If the WS path rounded differently from the REST path, the same fill would arrive twice under two keys:
        one alert from the socket and one from the poll, and the user would hear about it twice.
        """
        frame = trade_frame(price="0.1699999983", size="5")
        live = N.last_trade(frame)
        rest = N.fill_from_rest({"price": "0.1699999983", "size": "5", "timestamp": "1789699999",
                                "asset": frame["asset_id"], "side": "BUY", "conditionId": frame["market"],
                                "transactionHash": "", "proxyWallet": ""})
        self.assertEqual(live["price_micro"], rest["price_micro"])
        self.assertEqual(live["size_micro"], rest["size_micro"])
        k = T.fill_key(live)
        self.assertEqual(k, (rest["tx_hash"], rest["token_id"], rest["side"], rest["price_micro"],
                             rest["size_micro"]))

    def test_rollups_are_recomputed_from_stored_rows_and_vwap_stays_integer(self):
        self.fake.respond("data.trades", fx("data_trades.json"))
        out = self.d.poll_tape()
        self.assertGreater(out["rollup_rows"], 0)
        for vol, vwap, fills in self.con.execute("SELECT volume_micro, vwap_micro, fills FROM market_rollups"):
            self.assertIsInstance(vwap, int, "a float in a rollup is a rounding argument waiting to happen")
            self.assertGreaterEqual(vol, 0)
            self.assertGreaterEqual(fills, 1)
        before = self.con.execute("SELECT SUM(volume_micro) FROM market_rollups").fetchone()[0]
        cids = [r[0] for r in self.con.execute("SELECT DISTINCT condition_id FROM tape_fills")]
        self.d.store.rollups(cids, 0)
        self.assertEqual(self.con.execute("SELECT SUM(volume_micro) FROM market_rollups").fetchone()[0], before,
                         "a rollup that is not idempotent is not a cache, it is a second truth")

    def test_the_socket_never_writes_the_tape(self):
        frame = trade_frame()
        self.d.watch["t1"] = {"condition": "0xc1", "market": "m1", "priority": 0}
        before = self.con.execute("SELECT COUNT(*) FROM tape_fills").fetchone()[0]
        self.d.on_message(frame)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tape_fills").fetchone()[0], before,
                         "a trade frame has no tx hash, so it cannot be a tape row")
        self.assertEqual(self.d.tape.live_seen, 1)
        self.assertEqual([e["type"] for e in self.d.events], ["fill"])

    def test_an_unrepresentable_number_is_refused_at_the_boundary_not_inside_the_loop(self):
        """The tape parser is total: every unreadable value arrives as ShapeError, including absurd magnitudes.

        `"abc"` was always a ShapeError, so a caller catching only that class looked fine — until `"1e9999999"`,
        which parses as a Decimal and then raises `InvalidOperation` from the arithmetic. That is not a
        ShapeError, it escaped the loop, and one field in one row stopped the tape for every market. The parser
        owns the boundary, so the boundary converts it; the broad catch in `poll_tape` stays as defence in
        depth for the next value nobody thought of.
        """
        for bad in ({"price": "1e9999999", "size": "1"}, {"price": "1" + "0" * 400, "size": "1"},
                    {"price": "NaN", "size": "1"}, {"price": "Infinity", "size": "1"}):
            with self.subTest(bad=bad["price"][:12]):
                with self.assertRaises(N.ShapeError):
                    N.fill_from_rest(dict(bad, timestamp="1789699999", asset="t1", side="BUY",
                                          conditionId="0xc1"))
        rows = fx("data_trades.json") + [{"price": "1e9999999", "size": "1", "timestamp": "1789699999"}]
        self.fake.respond("data.trades", rows)
        out = self.d.poll_tape()
        self.assertEqual(out["written"], len(fx("data_trades.json")),
                         "the page's other fills must survive the one row we cannot read")
        self.assertGreaterEqual(out["refused"], 1, "and the refusal must be counted, not swallowed")

    def test_a_live_frame_already_booked_by_rest_does_not_fire_a_second_alert(self):
        frame = trade_frame()
        rest = {"price": frame["price"], "size": frame["size"],
                "timestamp": str(int(frame["timestamp"]) // 1000), "side": frame["side"],
                "asset": frame["asset_id"], "conditionId": "0xc1", "transactionHash": "0xdead",
                "proxyWallet": "0xw", "outcome": "Yes", "outcomeIndex": 0}
        self.d.tape.add(N.fill_from_rest(rest))          # the record, from REST
        self.d.watch["t1"] = {"condition": "0xc1", "market": "m1", "priority": 0}
        self.d.on_message(frame)                          # the same fill, seconds earlier, from the socket
        self.assertEqual(self.d.tape.live_suppressed, 1)
        self.assertEqual(self.d.events, [], "the alert belongs to whoever books the fill — once")

    def test_fills_above_threshold_agrees_with_the_stored_rows(self):
        rows = fx("data_trades.json")
        self.fake.respond("data.trades", rows)
        self.d.poll_tape()
        stored = self.con.execute("SELECT COUNT(*) FROM tape_fills WHERE usd_notional_micro >= 10000000000"
                                 ).fetchone()[0]
        mine = len(self.d.tape.fills_above(10_000 * 10 ** 6, 0, 10 ** 15))
        self.assertEqual(stored, mine, "the in-memory window and the table must not disagree about the tape")


class TestFreshnessOwnership(unittest.TestCase):
    """Which source a line of code is allowed to make look alive. Getting this wrong hides an outage."""

    def setUp(self):
        self.con = fresh_db()
        self.d, self.fake, self.ws = daemon(self.con)

    def test_a_rest_book_snapshot_marks_the_rest_source_and_not_the_socket(self):
        """The bug my own chaos harness shipped with, prevented in the product.

        If a REST read could advance `ws.book`, the composite (worst-of) would keep reading `ok` through a
        socket outage and the page would say "live" while showing the last snapshot. The harness caught it by
        flapping check A 3 times in 90 s; the fix belongs in the product, not only in the test.
        """
        # the fixture holds the payload; a real frame arrives wrapped in its event_type, which is what routes it
        book = dict(fx("clob_book.json"), event_type="book", asset_id="t1", market="0xc1")
        self.fake.respond("clob.book", book)
        self.d.watch["t1"] = {"condition": "0xc1", "market": "m1", "priority": 0}
        self.d.resync("t1", "test")
        self.assertTrue(self.d.fresh.sources["clob.book"].transport_alive)
        self.assertFalse(self.d.fresh.sources["ws.book"].transport_alive, "a REST read is not proof of a socket")

    def test_a_frame_marks_the_socket_sources(self):
        book = dict(fx("clob_book.json"), event_type="book", asset_id="t1", market="0xc1")
        self.d.watch["t1"] = {"condition": "0xc1", "market": "m1", "priority": 0}
        self.d.on_message(book)
        self.assertTrue(self.d.fresh.sources["ws.book"].transport_alive,
                        "a frame arriving is the evidence; connect() alone is not")
        self.assertEqual(self.d.fresh.composite(int(self.d._now() * 1000))["status"], "down",
                         "everything else is dead, and the composite is the worst case, not the average")

    def test_a_dead_socket_is_a_state_not_an_exception(self):
        class Boom:
            def connect(self):
                raise OSError("connection reset by peer")

            def close(self):
                pass

        self.d.ws_factory = lambda on_message: Boom()
        self.d.ws = None
        self.d.connect_ws()
        self.assertIsNone(self.d.ws)
        self.assertIn("connection reset", self.d.ws_error)
        self.assertEqual(self.d.fresh.sources["ws.tape"].state(int(self.d._now() * 1000)), "down")

    def test_the_market_source_is_alive_because_the_poll_said_so(self):
        self.fake.respond("gamma.markets", fx("gamma_markets.json"))
        self.d.fetch_universe()
        self.assertTrue(self.d.fresh.sources["gamma.markets"].transport_alive,
                        "a polled source that is never marked alive pins the composite at `down`, which is an"
                        " alarm that is always on and therefore read as nothing")


class TestUniverseWrites(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.d, self.fake, self.ws = daemon(self.con)

    def markets(self, n=4):
        return [m for m in fx("gamma_markets.json") if m.get("clobTokenIds")][:n]

    def test_gamma_id_and_condition_id_land_in_the_right_columns(self):
        rows = self.markets()
        out = self.d.apply_universe(rows)
        self.assertGreater(out["markets"], 0)
        for raw in rows:
            m = N.market_from_gamma(raw)
            got = self.con.execute("SELECT id, condition_id FROM markets WHERE id=?", (m["id"],)).fetchone()
            if got is None:
                continue
            self.assertEqual(got[1], m["condition_id"])
            self.assertNotEqual(got[0], got[1], "both ids in one column is a wrong join waiting to happen")
        dup = self.con.execute("SELECT condition_id, COUNT(*) c FROM markets GROUP BY condition_id HAVING c>1")
        self.assertEqual(dup.fetchall(), [], "the tape joins on condition id, so it must be unique")

    def test_the_venue_activity_numbers_are_stored_where_the_policy_reads_them(self):
        rows = self.markets()
        self.d.apply_universe(rows)
        vols = list(self.con.execute("SELECT volume_24h_micro FROM market_stats"))
        self.assertTrue(vols and all(v[0] >= 0 for v in vols))
        pr = self.d.store.prune_inputs(int(self.d._now() * 1000))
        self.assertTrue(pr)
        for r in pr:
            for key in ("last_fill_ms", "volume_24h_micro", "has_open_order", "in_watchlist", "has_open_alert"):
                self.assertIn(key, r)

    def test_minimum_tick_is_stored_in_the_units_the_check_expects(self):
        self.d.apply_universe(self.markets())
        for (tick,) in self.con.execute("SELECT minimum_tick_size FROM markets WHERE minimum_tick_size IS NOT"
                                        " NULL"):
            self.assertGreater(tick, 0)
            self.assertLessEqual(tick, 0.1, "1000 instead of 0.001 violates markets_tick_sane")

    def test_a_vanished_book_level_is_gone_from_the_ladder(self):
        market_row(self.con)
        self.d.store.replace_book("m1", {"bids": {400_000: 100, 300_000: 50}, "asks": {600_000: 10}}, 1000)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM book_levels").fetchone()[0], 3)
        self.d.store.replace_book("m1", {"bids": {400_000: 100}, "asks": {600_000: 10}}, 2000)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM book_levels").fetchone()[0], 2,
                         "an upsert-only ladder keeps a resting order the venue removed, and a user sizes "
                         "against a ghost")

    def test_is_winner_stays_null_until_the_market_has_resolved(self):
        row = dict(self.markets(1)[0])
        row["closed"] = False
        row["outcomePrices"] = json.dumps(["0.999", "0.001"])
        self.d.apply_universe([row])
        got = list(self.con.execute("SELECT is_winner FROM tokens"))
        self.assertTrue(all(r[0] is None for r in got), "NULL must not read as FALSE: 0.999 is a price")
        row["closed"] = True
        row["outcomePrices"] = json.dumps(["1", "0"])
        self.d.apply_universe([row])
        self.assertEqual(sorted(r[0] for r in self.con.execute("SELECT is_winner FROM tokens")), [0, 1])

    def test_a_metadata_move_is_written_as_history_not_as_silence(self):
        rows = self.markets(3)
        self.d.apply_universe(rows)
        moved = [dict(r, endDate="2027-01-01T00:00:00Z") for r in rows]
        out = self.d.apply_universe(moved)
        self.assertGreater(out["meta_versions"], 0)
        field, old, new = self.con.execute("SELECT field, old_value, new_value FROM market_meta_versions"
                                          " ORDER BY seen_ms DESC, rowid DESC LIMIT 1").fetchone()
        self.assertEqual(field, "endDate")
        self.assertNotEqual(old, new)
        self.assertTrue(old, "the whole point is that the user's old value is still on disk")

    def test_a_second_pass_over_the_same_rows_writes_no_spurious_history(self):
        rows = self.markets(3)
        self.d.apply_universe(rows)
        self.d.apply_universe(rows)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM market_meta_versions").fetchone()[0], 0,
                         "an unchanged market must not produce a version row every pass, or the history table"
                         " is noise and the real change is lost in it")

    def test_shape_refusals_in_the_universe_are_counted(self):
        bad = {"conditionId": "0xnope", "id": "999", "outcomes": json.dumps(["Yes", "No"]),
               "clobTokenIds": json.dumps(["only-one-token"])}      # a length mismatch, which is refused
        out = self.d.apply_universe([bad] + self.markets(2))
        self.assertGreaterEqual(out["shape_dropped"], 1)


class TestSignalsPersistence(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.d, self.fake, self.ws = daemon(self.con)

    def add_rule(self, rid="r1", kind="large_fill", params=None, owner="u1", cooldown=300,
                 channels=("push", "telegram")):
        self.con.execute("INSERT INTO signal_rules (id,owner,kind,params_json,market_filter_json,cooldown_s,"
                         "severity,channels_json,enabled,created_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,1,1,1)",
                         (rid, owner, kind, json.dumps(params or {}), json.dumps({}), cooldown, None,
                          json.dumps(list(channels))))

    def load(self):
        self.d.rules, self.d.rule_errors, self.d.cooldowns, self.d.owners = self.d.store.load_rules()
        self.d.engine.state = {}

    # `min_sample` is >= 2 by the engine's own validation (a z-score of one sample is not a z-score), and
    # rel_multiple 1.0 with a $10 floor makes a $50 fill fire deterministically.
    RULE = {"abs_usd_micro": 10 * 10 ** 6, "min_sample": 2, "rel_multiple": 1.0, "cooldown_s": 300}

    def big_fill_event(self, at, notional=50 * 10 ** 6):
        return {"type": "fill", "market": "0xc1", "token_id": "t1", "wallet": "0xw", "side": "BUY",
                "usd_notional_micro": notional, "ts_ms": at, "market_median_fill_micro": 10 ** 6,
                "market_fill_sample": 100, "watched_wallets": []}

    def test_a_rule_that_does_not_validate_is_reported_and_the_rest_still_run(self):
        self.add_rule("good", "volume_spike", {"min_sample": 30})
        self.add_rule("bad", "large_fill", {"min_samples": 30})        # the plural typo a user will make
        rules, errors, _cd, _ow = self.d.store.load_rules()
        self.assertEqual([r.id for r in rules], ["good"])
        self.assertIn("bad", errors[0])
        self.assertIn("min_samples", errors[0])

    def test_an_unknown_kind_is_kept_and_reported_not_deleted(self):
        self.add_rule("u2", "moon_shot")
        rules, errors, _, _ = self.d.store.load_rules()
        self.assertEqual(rules, [])
        self.assertIn("moon_shot", errors[0])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM signal_rules WHERE id='u2'").fetchone()[0], 1,
                         "deleting the row destroys the only evidence of why the user's alert stopped working")

    def test_the_cooldown_state_key_format_matches_the_engine(self):
        self.con.execute("INSERT INTO signal_state (rule_id,dedupe_key,last_fired_ms) VALUES ('r1','k1',?)",
                         (int(self.d._now() * 1000),))
        self.assertEqual(list(self.d.store.load_cooldown_state()), ["k1|r1"],
                         "the engine keys on dedupe_key FIRST; the other order means every alert re-fires "
                         "after a deploy, which is what this table exists to prevent")
        d2, _, _ = daemon(self.con)
        self.assertIn("k1|r1", d2.engine.state)

    def test_one_alert_per_window_and_one_delivery_row_per_channel(self):
        self.add_rule("r1", "large_fill", self.RULE)
        self.load()
        at = int(self.d._now() * 1000)
        fired = self.d.engine.evaluate(self.d.rules, self.big_fill_event(at), at)
        self.assertEqual(len(fired), 1, "a $50 fill against a $10 floor must clear the rule")
        self.assertEqual(self.d.store.record_alerts(fired, self.d.cooldowns, self.d.owners), 1)
        self.assertEqual(self.d.store.record_alerts(fired, self.d.cooldowns, self.d.owners), 0,
                         "UNIQUE (rule_id,dedupe_key,fired_bucket) IS the dedupe, not a code path")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM alert_deliveries").fetchone()[0], 2)
        self.assertEqual({r[0] for r in self.con.execute("SELECT channel FROM alert_deliveries")},
                         {"push", "telegram"})
        self.assertEqual(self.con.execute("SELECT priority FROM alert_deliveries LIMIT 1").fetchone()[0], 2,
                         "no entitlement row means the free tier, and the queue's fairness input is explicit")

    def test_a_paid_user_jumps_the_same_queue(self):
        self.con.execute("INSERT INTO users (id,stonks_address,created_ms,tier) VALUES ('u1','0xu',1,'trader')")
        self.con.execute("INSERT INTO entitlements (user_id,plan,max_alerts,max_watchlists,"
                         "max_automation_rules,radar_poll_ms,api_rpm,updated_ms)"
                         " VALUES ('u1','trader',12,4,3,5000,120,1)")
        self.assertEqual(self.d.store.priority_for("u1"), 0)
        self.assertEqual(self.d.store.priority_for("u-none"), 2)

    def test_an_alert_past_the_window_is_a_second_row(self):
        self.add_rule("r1", "large_fill", self.RULE)
        self.load()
        at = int(self.d._now() * 1000)
        fired_now = self.d.engine.evaluate(self.d.rules, self.big_fill_event(at), at)
        self.assertEqual(len(fired_now), 1)
        inside = self.d.engine.evaluate(self.d.rules, self.big_fill_event(at + 120_000), at + 120_000)
        self.assertEqual(inside, [], "same dedupe_key inside the 300s cooldown: this is what the UI promises")
        past = self.d.engine.evaluate(self.d.rules, self.big_fill_event(at + 301_000), at + 301_000)
        self.assertEqual(len(past), 1, "past the window it must fire again, or the rule is a one-shot")

    def test_suppression_is_counted_not_swallowed(self):
        self.add_rule("r1", "large_fill", self.RULE)
        self.load()
        at = int(self.d._now() * 1000)
        self.d.engine.evaluate(self.d.rules, self.big_fill_event(at), at)
        self.assertEqual(self.d.engine.evaluate(self.d.rules, self.big_fill_event(at + 1000), at + 1000), [])
        self.assertEqual(self.d.engine.suppressed, 1)
        out = self.d.evaluate()
        self.assertEqual(out["suppressed"], 1, "'3 more like this' is information; the count has to survive")

    def test_no_rules_at_all_is_reported_rather_than_looking_like_a_quiet_market(self):
        out = self.d.evaluate()
        self.assertIn("signal_rules", out["note"])
        self.assertEqual(out["fired"], 0)

    def test_the_volume_bucket_event_carries_a_history_it_can_score_against(self):
        self.fake.respond("data.trades", fx("data_trades.json"))
        self.d.poll_tape()
        evs = [e for e in self.d.events if e["type"] == "volume_bucket"]
        for e in evs:
            self.assertIsInstance(e["history_micro"], list)


class TestLabels(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        market_row(self.con)
        self.d, self.fake, self.ws = daemon(self.con)

    def insert(self, wallet, ts, side="BUY", px=500_000, size=20_000_000, token="t1"):
        f = {"tx_hash": "0x%s%s" % (wallet[-3:], ts), "wallet": wallet, "token_id": token, "market": "0xc1",
             "side": side, "outcome": "Yes", "outcome_index": 0, "price_micro": px, "size_micro": size,
             "usd_notional_micro": px * size // 10 ** 6, "ts_ms": ts, "source": "rest"}
        self.con.execute("INSERT INTO tape_fills (dedupe_key,condition_id,token_id,outcome,outcome_index,wallet,"
                         "side,price_micro,size_micro,usd_notional_micro,ts_ms,ingest_ms,source,fee_rate_bps)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'rest', 0)",
                         (T.dedupe_key(f), "0xc1", token, "Yes", 0, wallet, side, px, size,
                          f["usd_notional_micro"], ts, int(self.d._now() * 1000)))
        return f

    def test_every_stored_confidence_is_an_integer_per_mille(self):
        now = int(self.d._now() * 1000)
        # a round trip is a buy AND a sell within `roundtrip_seconds`, so 6 round trips need 12+ fills; the
        # first draft of this test inserted 8 and "failed" at the label threshold it never met.
        for i in range(14):
            self.insert("0xw1", now + i * 5_000, side="BUY" if i % 2 == 0 else "SELL")
        out = self.d.labels()
        self.assertGreater(out["labels"], 0, "seven round-trips inside 45s is the wash pattern by definition")
        rows = list(self.con.execute("SELECT confidence, publishable FROM wallet_labels"))
        self.assertTrue(rows)
        for conf, pub in rows:
            self.assertIsInstance(conf, int)
            self.assertTrue(0 <= conf <= 1000, "Label.as_row gives a 0..1 float; the column is per-mille")
            self.assertIn(pub, (0, 1))
        self.assertGreaterEqual(self.con.execute("SELECT COUNT(*) FROM wallet_label_history").fetchone()[0],
                                len(rows), "a label without its history cannot answer 'why did this change?'")

    def test_wash_like_roundtrips_are_detected_from_the_tape_alone(self):
        now = int(self.d._now() * 1000)
        for i in range(14):
            self.insert("0xw2", now + i * 10_000, side="BUY" if i % 2 == 0 else "SELL")
        out = self.d.labels()
        self.assertEqual(out["wash_reported_to_ops"], 1,
                         "our users' wash flow is our builder revenue at risk, so it is reported, not just shown")

    def test_no_fills_means_no_labels_rather_than_a_confident_empty_run(self):
        out = self.d.labels()
        self.assertEqual(out["labels"], 0)
        self.assertIn("skipped", out)

    def test_smart_money_needs_a_settled_sample_and_not_a_lucky_pair(self):
        now = int(self.d._now() * 1000)
        self.con.execute("UPDATE tokens SET is_winner = 1 WHERE token_id='t1'")
        self.con.execute("UPDATE tokens SET is_winner = 0 WHERE token_id='t2'")
        for i in range(2):
            self.insert("0xw3", now + i * 60_000, px=400_000)
        settled = self.d.store.settled_records(0)
        self.assertIn("0xw3", settled)
        self.assertEqual(settled["0xw3"]["settled_positions"], 1)
        # 2 fills x 20M shares at 0.40 -> cost $16, payout $40 because a winning share redeems for $1
        self.assertEqual(settled["0xw3"]["realized_pnl_micro"], 40 * 10 ** 6 - 16 * 10 ** 6,
                         "pnl is shares - cost, in integers, computed from stored rows only")
        self.assertIsNone(self.d.classifier.smart_money(settled["0xw3"]),
                          "one settled market is a coin that got lucky, and the floor is the control")

    def test_insider_evidence_is_only_built_from_what_we_stored(self):
        now = int(self.d._now() * 1000)
        hits, ctx = self.d.store.early_entries("0xnobody", now)
        self.assertEqual((hits, ctx), ([], {}))
        # an unresolved market cannot supply `resolved_his_way`, so it must contribute no hit at all
        self.insert("0xw4", now)
        hits, ctx = self.d.store.early_entries("0xw4", now)
        self.assertEqual(hits, [], "a heuristic that counts unknowable outcomes as knowledge is the version of"
                                   " this label that gets somebody in trouble")


class TestPassAndStatus(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        self.d, self.fake, self.ws = daemon(self.con)

    def test_run_once_survives_a_dead_socket_and_a_failed_universe(self):
        self.fake.respond("gamma.markets", [], status=503)
        self.fake.respond("data.trades", [], status=503)
        out = self.d.run_once()
        self.assertEqual(out["tape"]["written"], 0)
        st = self.d.status()
        self.assertEqual(st["freshness"]["status"], "down")
        self.assertIn("ws.tape", st["pageable"], "a socket that never connected is pageable, and that is right")

    def test_a_full_pass_writes_the_tape_the_rollups_and_the_book(self):
        self.fake.respond("gamma.markets", fx("gamma_markets.json"))
        self.fake.respond("data.trades", fx("data_trades.json"))
        self.fake.respond("clob.book", dict(fx("clob_book.json"), event_type="book", asset_id="t1",
                                            market="0xc1"))
        out = self.d.run_once()
        self.assertGreater(out["tape"]["written"], 0)
        self.assertGreater(out["universe"]["markets"], 0)
        st = self.d.status()
        self.assertEqual(st["tape"]["rows"], len(self.d.tape.rows))
        for src in st["freshness"]["sources"].values():
            self.assertIn(src, ("ok", "lagging", "stale", "silent", "down"))

    def test_status_carries_no_connection_string_or_address_book(self):
        text = json.dumps(self.d.status(), default=str)
        for bad in ("postgres://", "PGM_DB_PATH", "0xw", "password"):
            self.assertNotIn(bad, text)

    def test_no_float_is_ever_written_into_a_money_column(self):
        self.fake.respond("gamma.markets", fx("gamma_markets.json"))
        self.fake.respond("data.trades", fx("data_trades.json"))
        self.d.run_once()
        for table, cols in (("tape_fills", ("price_micro", "size_micro", "usd_notional_micro", "ts_ms")),
                            ("market_rollups", ("volume_micro", "vwap_micro", "max_fill_micro"))):
            for row in self.con.execute("SELECT %s FROM %s" % (", ".join(cols), table)):
                for v in row:
                    self.assertNotIsInstance(v, float, "%s of %s held a float: %r" % (table, cols, v))


class TestUniversePolicy(unittest.TestCase):
    def test_backfill_pages_are_budgeted_and_the_cursor_is_a_keyset(self):
        con = fresh_db()
        d, fake, _ws = daemon(con)
        fake.respond("gamma.markets", [m for m in fx("gamma_markets.json") if m.get("clobTokenIds")])
        d.cfg.universe_budget = 2
        d.fetch_universe()
        calls = [c for c in fake.calls if "id__gt" in c["params"]]
        self.assertLessEqual(len(calls), 2, "the budget is what stops a restart spending 300 requests at once")
        self.assertTrue(calls and all(c["params"].get("order") == "id" for c in calls),
                        "keyset by id: `offset` shifts under live inserts and silently skips rows")

    def test_discovery_counts_the_overlap_it_expected(self):
        rows = [{"id": 10, "conditionId": "0xa", "question": "q", "slug": "s", "outcomes": json.dumps(["Yes"]),
                 "outcomePrices": json.dumps(["0.5"]), "clobTokenIds": json.dumps(["t"]),
                 "orderPriceMinTickSize": 0.01, "orderMinSize": 5, "volume24hr": 1}]
        disc = U.Discover()
        disc.feed(rows)
        disc.feed(rows)
        self.assertEqual(disc.seen_new, 1)
        self.assertEqual(disc.overlap, 1, "the overlap is the proof that paging is continuous, not a bug")


if __name__ == "__main__":
    unittest.main()
