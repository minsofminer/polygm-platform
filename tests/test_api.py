"""The API: P04's acceptance line — "curl an order against executor-mock" — plus the envelope rules.

Written as contract assertions, not smoke tests: every one of these fails if an endpoint is "simplified" in
a way that breaks a client (dropping asOf, returning a one-sided book, leaking a Python exception into
`message`, or re-running the venue call on an idempotent retry).

Keys: tests call `post_order()` for a fresh key, or `post_order(tag="replay")` when two calls MUST share
one. Untagged calls get a per-test counter, because a test whose second order collides with its first on the
idempotency key spends its assertion on IDEM_IN_PROGRESS instead of on the rule it meant to check.
"""
from __future__ import annotations
import io
import contextlib
import json
import re
import unittest
import uuid

from conftest import import_app, refresh_flags  # noqa: F401

M = 10**6


class ApiBase(unittest.TestCase):
    app_name = "api-base"
    app = None
    client = None
    _n = 0

    @classmethod
    def setUpClass(cls):
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)

    def setUp(self):
        refresh_flags(self.app)
        self.con = self.app._db
        # One DB per class (the module-level app is imported once), so each test starts from a clean money
        # history: without this the 24h cap from an earlier test's orders denies the next test's order with
        # DAILY_CAP, and the failure looks like a product bug rather than a fixture one.
        # Directives first: the SQLite harness keeps a non-cascading foreign key (the transpiler drops
        # `ALTER TABLE ... ON DELETE CASCADE` and records it in DROPPED.json), so a delete that removes an
        # intent has to remove the intent's instructions with it. Postgres cascades.
        self.con.execute("DELETE FROM order_directives WHERE intent_id IN (SELECT id FROM order_intents)")
        self.con.execute("DELETE FROM order_intents")
        self.con.execute("DELETE FROM orders")
        self.con.execute("DELETE FROM idempotency_keys")
        self.con.execute("DELETE FROM cash_ledger")
        # kill_switch_state is append-only (that is the rule under test), so there is deliberately NO
        # cleanup here: an empty table means "never engaged" (see the API's newest-row read), and a test
        # that cleared the table with DELETE would pass for the wrong reason the moment the guard is widened.
        # The two kill-switch tests assert against their own baseline count for the same reason.
        self.k = re.sub(r"[^a-z0-9]", "-", self.id().lower())[-70:] + "-" + uuid.uuid4().hex[:8]

    def key(self, tag=None) -> str:
        if tag is None:
            type(self)._n += 1
            return "%s-auto%d" % (self.k, type(self)._n)
        return "%s-%s" % (self.k, tag)

    def body(self, **over) -> dict:
        b = {"marketId": "0xM1", "tokenId": "0xT10", "side": "BUY", "price": "0.50", "size": "10"}
        b.update(over)
        return b

    def post_order(self, *, key=None, tag=None, user="u-demo", **body):
        k = key if key is not None else self.key(tag)
        h = {} if k is None else {"Idempotency-Key": k}
        if user:
            h["X-User-Id"] = user
        return self.client.post("/v1/orders", json=self.body(**body), headers=h)

    def restore_book(self, market="0xM1"):
        self.con.execute("DELETE FROM book_levels WHERE market_id=?", (market,))
        now = self.app._now_ms()
        for side, p in (("bid", 490_000), ("ask", 510_000)):
            self.con.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,"
                             "level_count,updated_ms) VALUES (?,?,?,?,1,?)", (market, side, p, 10_000_000, now))


class TestReads(ApiBase):
    app_name = "api-reads"

    def test_market_carries_every_field_the_terminal_needs(self):
        r = self.client.get("/v1/markets/0xM1")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        for k in ("asOf", "staleAfter", "cache", "market"):
            self.assertIn(k, j)
        m = j["market"]
        for k in ("id", "question", "acceptingOrders", "secondsDelay", "minimumTickSize",
                  "minimumOrderSize", "feeType", "enableOrderBook", "endDate"):
            self.assertIn(k, m, "a field the UI reads went missing from the contract")
        self.assertLess(j["asOf"], j["staleAfter"])
        self.assertTrue(j["cache"]["public"])
        self.assertEqual(j["cache"]["key"], "market:0xM1")

    def test_unknown_market_is_an_envelope_not_a_404_page(self):
        r = self.client.get("/v1/markets/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "NOT_FOUND")
        self.assertNotIn("detail", r.json()["error"])
        self.assertIn("requestId", r.json()["error"])

    def test_book_returns_both_sides_and_a_spread(self):
        j = self.client.get("/v1/markets/0xM1/book?depth=8").json()
        self.assertGreater(len(j["bids"]), 0)
        # the single-query `ORDER BY side LIMIT 2` bug produced bids-only; this is that canary
        self.assertGreater(len(j["asks"]), 0)
        self.assertEqual(len(j["bids"]), 8)
        self.assertEqual(len(j["asks"]), 8)
        self.assertGreater(j["spreadTicks"], 0)
        self.assertTrue(all(re.fullmatch(r"0?\.\d+", b["price"]) for b in j["bids"] + j["asks"]),
                        "prices must be dot-decimal strings, never locale-formatted")
        # best-first on both sides: a ladder renders depth outward from the touch
        self.assertGreater(float(j["bids"][0]["price"]), float(j["bids"][-1]["price"]))
        self.assertLess(float(j["asks"][0]["price"]), float(j["asks"][-1]["price"]))

    def test_thin_book_is_not_padded(self):
        j = self.client.get("/v1/markets/0xM3/book?depth=12").json()
        self.assertLessEqual(len(j["bids"]), 12)
        self.assertGreaterEqual(len(j["bids"]), 1)

    def test_book_for_unknown_market_404s(self):
        self.assertEqual(self.client.get("/v1/markets/zzz/book").status_code, 404)

    def test_depth_is_clamped_by_validation(self):
        self.assertEqual(self.client.get("/v1/markets/0xM1/book?depth=0").status_code, 422)
        self.assertEqual(self.client.get("/v1/markets/0xM1/book?depth=401").status_code, 422)


class TestEnvelope(ApiBase):
    app_name = "api-envelope"

    def test_request_id_is_echoed_and_latency_reported(self):
        rid = "req-" + "a" * 8
        r = self.client.get("/v1/markets/0xM1", headers={"x-request-id": rid})
        self.assertEqual(r.headers["x-request-id"], rid)
        self.assertIn("app;dur=", r.headers["server-timing"])

    def test_a_generated_request_id_appears_on_errors_too(self):
        r = self.client.get("/v1/markets/zzz")
        self.assertEqual(len(r.json()["error"]["requestId"]), 16)

    def test_error_bodies_never_leak_python_detail(self):
        r = self.post_order(price="not-a-number")
        body = r.text
        self.assertEqual(r.status_code, 422, body)
        for needle in ("Traceback", "Decimal", "invalid literal", "polygm_core", 'File "', "int() arg"):
            self.assertNotIn(needle, body, "leaked %r" % needle)
        self.assertEqual(r.json()["error"]["code"], "BAD_AMOUNT")

    def test_retryable_and_permanent_codes_have_the_right_status_and_headers(self):
        r = self.post_order(price="0.5005")            # off tick for a 0.01 market: permanent 422
        self.assertEqual(r.status_code, 422, r.text)
        self.assertNotIn("retry-after", {k.lower() for k in r.headers})
        self.assertFalse(r.json()["error"]["retryable"])
        self.assertEqual(r.json()["error"]["code"], "OFF_TICK")

        self.con.execute("DELETE FROM book_levels WHERE market_id='0xM1'")   # no quote at all => stale
        r2 = self.post_order()
        self.assertEqual(r2.status_code, 503, r2.text)
        self.assertEqual(r2.json()["error"]["code"], "STALE_QUOTE")
        self.assertTrue(r2.json()["error"]["retryable"])
        self.assertEqual(r2.headers.get("retry-after"), "2")
        self.restore_book()
        self.assertEqual(self.post_order().status_code, 202, "the book did not come back")

    def test_every_documented_code_is_reachable_through_the_http_layer(self):
        # Not a list of names: each code below is *produced* by a request, so the envelope is exercised and
        # a code that has become unreachable (dead validation) is reported.
        probes = {
            # a raw call here, on purpose: post_order() ALONE can express "no key" because it mints one
            "IDEM_KEY_REQUIRED": lambda: self.client.post("/v1/orders", json=self.body(),
                                                          headers={"X-User-Id": "u-demo"}),
            "BAD_AMOUNT": lambda: self.post_order(price="x"),
            "OFF_TICK": lambda: self.post_order(price="0.5005"),
            "BELOW_MIN_SIZE": lambda: self.post_order(size="1"),
            "MARKET_NOT_ACCEPTING": lambda: self.post_order(marketId="0xM6"),
            "NOT_FOUND": lambda: self.client.get("/v1/markets/zzz"),
            "SIGNER_UNAVAILABLE": lambda: self.post_order(user=None),
        }
        probes.update({
            "VALIDATION": lambda: self.client.get("/v1/markets", params={"limit": 5000}),
            # a MISSING reason is a shape error (VALIDATION); a PRESENT-but-short one is the semantic rule
            # the DB enforces (BAD_REASON). Two codes, two probes, because collapsing them would tell an
            # operator "your body is wrong" when the truth is "say why you pulled the switch".
            "BAD_REASON": lambda: self.client.post("/v1/admin/kill-switch",
                                                    json={"engaged": True, "reason": "ab"},
                                                    headers={"X-Admin-Token": "t"}),
            "INTERNAL": lambda: self._crash_once(),
        })
        seen = {}
        for code, probe in probes.items():
            r = probe()
            try:
                code_seen = r.json().get("error", {}).get("code")
            except Exception:                                  # noqa: BLE001 - reported below, not hidden
                code_seen = "NON_JSON:%s:%s" % (r.status_code, r.text[:40])
            seen[code] = (r.status_code, code_seen)
            with self.subTest(code=code):
                self.assertEqual(seen[code][1], code)
        self.assertEqual(len(seen), 10)

    def _crash_once(self):
        return self._with_dead_db(lambda: self.client.get("/v1/markets/0xM1"))

    def _with_dead_db(self, call):
        """Run `call()` with the module's DB handle replaced by one that raises, then restore it.

        Deliberately NOT a test-registered route: a `def _boom(r: _R)` inside a test body has its
        annotations evaluated as strings against the MODULE globals (this file has
        `from __future__ import annotations`), so `_R` is unresolvable and FastAPI quietly turns the
        parameter into a required query param - the route answers 422 and the crash handler never runs. The
        leak from registering it is worse: the poisoned signature then breaks /openapi.json for every later
        test in the class.
        """
        class Dead:
            def execute(self, *a, **k):
                raise RuntimeError("db exploded")

        real = self.app._db
        self.app._db = Dead()
        try:
            return call()
        finally:
            self.app._db = real

    def test_malformed_body_gets_the_envelope_and_nothing_of_the_clients_own(self):
        # FastAPI's default validation body is {"detail":[{...,"input":<what the user sent>}]}: correct HTTP,
        # wrong contract, and the `input` echo is exactly the leak shape P04 forbids. A non-dict body is the
        # cheapest way to reach that path.
        r = self.client.post("/v1/orders", content=b"[1,2,3]",
                             headers={"Content-Type": "application/json", "Idempotency-Key": self.key(),
                                      "X-User-Id": "u-demo"})
        self.assertEqual(r.status_code, 422, r.text)
        body = r.json()
        self.assertNotIn("detail", body)
        self.assertEqual(body["error"]["code"], "VALIDATION")
        self.assertNotIn("[1,2,3]", r.text)
        self.assertIn("requestId", body["error"])

    def test_a_query_parameter_over_the_documented_cap_is_a_validation_envelope(self):
        # The endpoint declares le=100 because Gamma caps at 100 and ignores bigger limits (P01); a client
        # asking for 5000 must get "no", not a page of 100 it believes is 5000.
        r = self.client.get("/v1/markets", params={"limit": 5000})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        self.assertIn("limit", r.json()["error"]["message"])

    def test_an_unexpected_crash_is_an_envelope_with_a_request_id_and_no_message(self):
        # The handler is the last line of defence, so it is tested by crashing something on purpose. The
        # thing to prove is not just that "db exploded" is absent: the body must not carry the exception TYPE
        # either, because `sqlite3.OperationalError` names a driver and a column to anyone poking the API.
        r = self._with_dead_db(lambda: self.client.get(
            "/v1/markets/0xM1", headers={"X-Request-Id": "crashme01"}))
        self.assertEqual(r.status_code, 500, r.text)
        err = r.json()["error"]
        self.assertEqual(err["code"], "INTERNAL")
        self.assertNotIn("db exploded", r.text)
        self.assertNotIn("RuntimeError", r.text)
        self.assertEqual(err["requestId"], "crashme01")
        self.assertNotIn("OperationalError", r.text)
        self.assertNotIn("Traceback", r.text)
        self.assertTrue(err["retryable"])                    # retry with the SAME key, which is why 500 on an
        self.assertEqual(r.headers.get("retry-after"), "2")  # order is not the "give up" status it looks like

    def test_the_served_openapi_advertises_the_same_statuses_as_the_contract(self):
        # The hand-written yaml is the promise; /openapi.json is what a code generator actually reads. This
        # asserts the implementation itself declares them, so a route that quietly drops a status fails HERE
        # instead of in whoever generated a client from the yaml.
        d = self.client.get("/openapi.json").json()
        self.assertIn("paths", d, "no openapi document served: %s" % str(d)[:200])
        orders = sorted(int(k) for k in d["paths"]["/v1/orders"]["post"]["responses"])
        self.assertEqual(orders, sorted(self.app.ORDER_RESPONSES))
        self.assertIn(202, orders, "the served spec lost the 202 - the bug class this whole file exists for")
        for path, verb, table in (("/readyz", "get", "READYZ_RESPONSES"),
                                  ("/v1/markets", "get", "LIST_RESPONSES"),
                                  ("/v1/tape", "get", "TAPE_RESPONSES"),
                                  ("/v1/admin/kill-switch", "post", "KILL_RESPONSES")):
            with self.subTest(path=path):
                # +500 because the crash handler is app-wide while the per-endpoint tables list only the
                # statuses that ENDPOINT can decide on. A client generating from /openapi.json gets 500 on
                # everything either way; this is a comment about the tables, not a tolerance for drift.
                declared = {200, 500} | set(getattr(self.app, table))
                self.assertEqual(sorted(int(k) for k in d["paths"][path][verb]["responses"]),
                                 sorted(declared))

    def test_the_contract_and_the_implementation_agree_on_the_order_status_set(self):
        import yaml
        from pathlib import Path as _P
        contract = yaml.safe_load((_P(__file__).resolve().parents[1] / "contracts" / "openapi.yaml").read_text())
        want = {int(k) for k in contract["paths"]["/v1/orders"]["post"]["responses"]}
        self.assertEqual(want, set(self.app.ORDER_RESPONSES),
                         "openapi.yaml and services/api/app.py disagree about what POST /v1/orders can answer")
        self.assertEqual({404, 500} & want, {404, 500},
                         "the order endpoint lost its 404/500 in the contract; both are reachable")
        # and every code in the app's table must be in the contract envelope's enum
        enum = set(contract["components"]["schemas"]["Error"]["properties"]["error"]["properties"]["code"]["enum"])
        self.assertEqual(set(self.app.CODES) - enum, set(), "CODES has a code the contract does not define")


class TestListAndTape(ApiBase):
    """The two list reads. Paging is the substance here: both exist so a client can walk a changing set
    without repeating or skipping rows, which is the failure mode P01 measured at the venue."""

    app_name = "api-list"

    def test_markets_page_is_capped_at_the_venues_real_limit(self):
        r = self.client.get("/v1/markets", params={"limit": 100})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertLessEqual(len(body["items"]), 100)
        self.assertEqual(body["pageSizeHardCap"], 100)   # said out loud, so a client cannot "fix" it

    def test_walking_the_cursor_neither_repeats_nor_drops_a_market(self):
        # The property that makes cursor pagination worth having, checked against the whole set: walk pages
        # of 2 until nextCursor is null and require the union to equal the single big page, exactly once.
        big = self.client.get("/v1/markets", params={"limit": 100, "live": "false"}).json()
        want = [i["id"] for i in big["items"]]
        got, cursor, guard = [], None, 0
        while guard < 20:
            guard += 1
            params = {"limit": 2, "live": "false"}
            if cursor:
                params["cursor"] = cursor
            page = self.client.get("/v1/markets", params=params).json()
            got += [i["id"] for i in page["items"]]
            cursor = page["nextCursor"]
            if cursor is None:
                break
            self.assertTrue(page["items"], "a cursor that yields an empty page is a broken cursor")
        self.assertEqual(len(got), len(set(got)), "a market appeared on two pages: %s" % got)
        self.assertEqual(set(got), set(want), "walked %s but the full page is %s" % (sorted(set(got)),
                                                                                      sorted(set(want))))
        self.assertGreater(len(want), 2, "the seed is too small to prove paging; fix the seed, not the test")

    def test_every_row_carries_what_the_gate_needs_and_the_ui_paints(self):
        for it in self.client.get("/v1/markets", params={"limit": 100, "live": "false"}).json()["items"]:
            with self.subTest(market=it["id"]):
                self.assertIsInstance(it["acceptingOrders"], bool)
                self.assertIn(it["minimumTickSize"], ("0.001", "0.01"))   # canonical TEXT, same as the gate
                self.assertIsInstance(it["secondsDelay"], int)
                self.assertIsInstance(it["minimumOrderSize"], str)

    def test_undated_markets_sort_last_not_first(self):
        # endsSoon is the default order, and NULL end_ts must not outrank a market that resolves tomorrow.
        r = self.client.get("/v1/markets", params={"sortBy": "endsSoon", "live": "false"}).json()
        ends = [i["endTs"] for i in r["items"]]
        self.assertNotIn(None, ends[:-1], "a NULL end_ts sorted to the front: %s" % ends)

    def test_tape_is_newest_first_and_pages_on_the_venue_clock(self):
        r = self.client.get("/v1/tape", params={"marketId": "0xM1", "limit": 2})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        ts = [x["exchangeTs"] for x in body["rows"]]
        self.assertEqual(ts, sorted(ts, reverse=True), "the tape must arrive newest first")
        self.assertTrue(body["nextCursor"], "3+ seeded fills with limit=2 must advertise a cursor")
        older = self.client.get("/v1/tape", params={"marketId": "0xM1", "limit": 2,
                                                    "since": body["nextCursor"]}).json()
        seen = {x["exchangeTs"] for x in body["rows"]}
        self.assertEqual(seen & {x["exchangeTs"] for x in older["rows"]}, set(), "since must be EXCLUSIVE")
        self.assertLess(max(x["exchangeTs"] for x in older["rows"]), min(seen))

    def test_tape_never_returns_the_venue_payload_or_a_raw_micro_number(self):
        row = self.client.get("/v1/tape", params={"marketId": "0xM1", "limit": 1}).json()["rows"][0]
        self.assertNotIn("raw_json", json.dumps(row))
        self.assertNotIn("rawJson", row)
        self.assertIsInstance(row["price"], str)             # money is a decimal string in every payload
        self.assertTrue(re.match(r"^0?\.\d{1,6}$", row["price"]), row["price"])

    def test_tape_for_an_unknown_market_is_a_404_envelope(self):
        r = self.client.get("/v1/tape", params={"marketId": "0xNOPE"})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["error"]["code"], "NOT_FOUND")

    def test_a_bookless_market_can_still_have_an_empty_tape(self):
        # 0xM4 has no order book (a delayed governance market); its tape is legitimately empty, and an empty
        # array here is the right answer - unlike /book, where "no book" must be a 404 rather than a lie.
        r = self.client.get("/v1/tape", params={"marketId": "0xM4"}).json()
        self.assertEqual(r["rows"], [])
        self.assertIsNone(r["nextCursor"])
        self.assertIn("staleAfter", r)



class TestDurableFills(ApiBase):
    """`/v1/markets/{id}/fills` — the read that makes P05's `tape_fills` more than a table nobody serves."""

    app_name = "api-fills"

    def seed(self, condition_id, wallet="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"):
        # One DB per class, and `dedupe_key` is UNIQUE by design (that constraint IS the ingest's replay
        # protection), so a class that seeds twice must clear its own rows first. Clearing here rather than in
        # setUp keeps the reason next to the constraint it exists for.
        self.con.execute("DELETE FROM tape_fills WHERE condition_id=?", (condition_id,))
        self.con.execute("DELETE FROM wallet_labels")
        now = self.app._now_ms()
        for i, (px, size, side) in enumerate(((300_000, 5_000_000, "BUY"), (300_000, 50_000_000, "SELL"),
                                             (299_000, 1_000_000, "BUY"))):
            self.con.execute(
                "INSERT INTO tape_fills (dedupe_key,condition_id,token_id,outcome,outcome_index,wallet,side,"
                "price_micro,size_micro,usd_notional_micro,ts_ms,ingest_ms,source,fee_rate_bps)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                ("fillkey-%s-%d" % (condition_id, i), condition_id, "tok%d" % i, "Yes", 0, wallet, side, px, size,
                 px * size // 10 ** 6, now - (i + 1) * 60_000, now - i * 1000, "rest"))
        self.con.commit()
        return now

    def market_and_cid(self):
        mid = self.client.get("/v1/markets", params={"limit": 1, "live": "false"}).json()["items"][0]["id"]
        cid = self.con.execute("SELECT condition_id FROM markets WHERE id=?", (mid,)).fetchone()[0]
        return mid, str(cid)

    def test_the_route_answers_from_the_ingest_table_newest_first(self):
        mid, cid = self.market_and_cid()
        self.seed(cid)
        body = self.client.get("/v1/markets/%s/fills" % mid).json()
        self.assertEqual(len(body["rows"]), 3, "three fills were stored; a page that drops one is a lie")
        ts = [r["venueTs"] for r in body["rows"]]
        self.assertEqual(ts, sorted(ts, reverse=True), "newest first, like the tape widget expects")
        self.assertEqual(body["conditionId"], cid, "the ingest keys on condition id; the route must translate")

    def test_money_numbers_are_strings_off_integers(self):
        mid, cid = self.market_and_cid()
        self.seed(cid)
        row = self.client.get("/v1/markets/%s/fills" % mid).json()["rows"][0]
        for key in ("price", "shares", "notional"):
            self.assertIsInstance(row[key], str, "%s must not be a JSON number" % key)
            self.assertRegex(row[key], r"^[0-9]+(\.[0-9]{1,6})?$")
        self.assertEqual(row["notional"], "1.5", "0.30 x 5 shares, from the stored integers, not re-derived")

    def test_no_counterparty_address_survives_the_response(self):
        mid, cid = self.market_and_cid()
        wallet = "0x" + "ab" * 20
        self.seed(cid, wallet=wallet)
        text = self.client.get("/v1/markets/%s/fills" % mid).text
        self.assertNotIn(wallet, text, "the tape must not become an address book of our users' counterparties")
        anon = self.client.get("/v1/markets/%s/fills" % mid).json()["rows"][0]["anonWallet"]
        self.assertRegex(anon, r"^w_[0-9a-f]{10}$")
        again = self.client.get("/v1/markets/%s/fills" % mid).json()["rows"][0]["anonWallet"]
        self.assertEqual(anon, again, "a pseudonym that changes per request cannot group a trader's fills")

    def test_since_is_exclusive_on_the_venue_clock(self):
        mid, cid = self.market_and_cid()
        self.seed(cid)
        rows = self.client.get("/v1/markets/%s/fills" % mid).json()["rows"]
        cut = rows[1]["venueTs"]
        got = self.client.get("/v1/markets/%s/fills" % mid, params={"since": cut}).json()["rows"]
        self.assertEqual([r["venueTs"] for r in got], [rows[2]["venueTs"]])
        self.assertNotIn(cut, [r["venueTs"] for r in got], "an inclusive `since` re-serves the last row seen")

    def test_stale_after_is_the_age_of_the_fills_not_of_the_response(self):
        """The one field the UI's "live" badge reads, and the reason it used to be wrong.

        These fixtures are minutes old on purpose: with `staleAfter = now + budget`, a client would have painted a
        live dot over a fill log from ten minutes ago — the exact failure P05's gate names.
        """
        mid, cid = self.market_and_cid()
        self.seed(cid)
        body = self.client.get("/v1/markets/%s/fills" % mid).json()
        newest = max(r["venueTs"] for r in body["rows"])
        self.assertEqual(body["asOf"], newest, "asOf is the data's clock")
        self.assertLessEqual(body["staleAfter"], self.app._now_ms(),
                            "data minutes old must already read stale, not 'fresh for a second'")

    def test_published_labels_come_along_and_unpublished_ones_never_do(self):
        mid, cid = self.market_and_cid()
        wallet = "0x" + "cd" * 20
        self.seed(cid, wallet=wallet)
        for label, publishable in (("whale", 1), ("insider_suspect", 0)):
            self.con.execute("INSERT OR REPLACE INTO wallet_labels (wallet,label,confidence,publishable,"
                             "evidence_json,first_seen_ms,last_seen_ms) VALUES (?,?,?,?,?,1,1)",
                             (wallet, label, 900, publishable, "{}"))
        self.con.commit()
        rows = self.client.get("/v1/markets/%s/fills" % mid).json()["rows"]
        labels = {l for r in rows for l in r["labels"]}
        self.assertEqual(labels, {"whale"}, "insider_suspect is never publishable, so it can never be in a"
                                           " response — and the check belongs on the read, not on the writer")

    def test_an_unknown_market_is_404_not_an_empty_array(self):
        r = self.client.get("/v1/markets/does-not-exist/fills")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "NOT_FOUND")


class TestOrderPath(ApiBase):
    app_name = "api-orders"

    def test_happy_path_returns_202_with_a_poll_target_and_no_venue_claim(self):
        r = self.post_order()
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.headers["content-type"], "application/json")
        j = r.json()
        # 202 must be the status the BODY's response object carries, not just the decorator's default
        self.assertNotIn("detail", j)
        self.assertEqual(j["state"], "queued")
        self.assertEqual(j["notionalMicro"], 5 * M)
        self.assertTrue(j["poll"].startswith("/v1/orders/intents/"))
        self.assertIn("not 'accepted at the venue'", j["note"])
        self.assertEqual(self.con.execute("SELECT state FROM order_intents WHERE id=?",
                                         (j["intentId"],)).fetchone()[0], "queued")
        self.assertGreater(float(r.headers["x-risk-latency-ms"]), 0)
        self.assertGreater(int(r.headers["x-risk-checks"]), 8)

    def test_missing_idempotency_key_is_refused_before_any_work(self):
        before = self.con.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]
        r = self.client.post("/v1/orders", json=self.body(), headers={"X-User-Id": "u-demo"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "IDEM_KEY_REQUIRED")     # the 400 uses the same
        self.assertIn("requestId", r.json()["error"])                        # envelope as everything else
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0], before)

    def test_key_shape_is_enforced_both_ways(self):
        self.assertEqual(self.client.post("/v1/orders", json=self.body(),
                                        headers={"X-User-Id": "u", "Idempotency-Key": "tiny"}
                                        ).status_code, 400)
        self.assertEqual(self.client.post("/v1/orders", json=self.body(),
                                        headers={"X-User-Id": "u", "Idempotency-Key": "x" * 200}
                                        ).status_code, 400)          # >128 chars: still refused
        self.assertEqual(self.client.post("/v1/orders", json=self.body(),
                                        headers={"X-User-Id": "u", "Idempotency-Key": "bad key!"}
                                        ).status_code, 400)          # spaces/punctuation are not key material

    def test_replaying_the_key_replays_the_response_and_writes_nothing_new(self):
        a = self.post_order(tag="replay").json()
        before = self.con.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]
        b = self.post_order(tag="replay")
        self.assertEqual(b.status_code, 202)            # a replay keeps the same status, not 200
        self.assertEqual(b.json()["intentId"], a["intentId"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0], before)

    def test_same_key_different_body_is_a_conflict(self):
        self.post_order(tag="clash")
        r = self.post_order(tag="clash", size="11")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "IDEM_CONFLICT")

    def test_a_rejected_order_can_be_retried_with_the_same_key(self):
        # abandon(), not finish(): a rejection must not poison the key, or the user's next attempt after
        # fixing the price would be answered with the old rejection.
        r1 = self.post_order(tag="retry", size="1")
        self.assertEqual(r1.status_code, 422)
        r2 = self.post_order(tag="retry", size="10")
        self.assertEqual(r2.status_code, 202, r2.text)

    def test_rejected_orders_are_recorded_so_the_pager_can_count_them(self):
        self.post_order(tag="rec", size="1")
        row = self.con.execute("SELECT state, risk_code FROM order_intents WHERE idempotency_key=?",
                              (self.key("rec"),)).fetchone()
        self.assertEqual(row[0], "rejected")
        self.assertEqual(row[1], "BELOW_MIN_SIZE")

    def test_the_closed_market_denies_with_a_specific_code(self):
        r = self.post_order(marketId="0xM6")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["error"]["code"], "MARKET_NOT_ACCEPTING")

    def test_a_market_with_no_book_denies_rather_than_guessing_a_price(self):
        r = self.post_order(marketId="0xM4")
        self.assertEqual(r.json()["error"]["code"], "NO_ORDER_BOOK", r.text)

    def test_number_typed_amounts_are_refused_not_coerced(self):
        # The money path takes decimal STRINGS. Accepting a JSON number here would put a float in front of
        # parse_usdc, where it silently rounds (0.1 is not exactly representable).
        for patch in ({"price": 0.5}, {"size": 10}, {"price": "0.5", "size": 10.0}):
            r = self.post_order(**patch)
            with self.subTest(**{k: repr(v) for k, v in patch.items()}):
                self.assertEqual(r.status_code, 422, r.text)
                self.assertEqual(r.json()["error"]["code"], "BAD_AMOUNT")

    def test_a_price_beyond_the_supported_scale_is_refused_at_parse_time(self):
        r = self.post_order(price="0.4999999")
        self.assertEqual(r.json()["error"]["code"], "BAD_AMOUNT", r.text)

    def test_24h_cap_is_enforced_from_the_db_not_from_memory(self):
        now = self.app._now_ms()
        self.con.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,"
                         "size_micro,notional_micro,state,idempotency_key,created_ms) "
                         "VALUES ('bulk','u-demo','0xM1','0xT10','BUY',500000,10000000,?, 'submitted',"
                         "'k-bulk',?)", (25_000 * M, now))
        r = self.post_order()
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["error"]["code"], "DAILY_CAP")

    def test_intent_lookup_is_scoped_to_the_caller(self):
        j = self.post_order(tag="scope").json()
        self.assertEqual(self.client.get(j["poll"], headers={"X-User-Id": "u-demo"}).status_code, 200)
        self.assertEqual(self.client.get(j["poll"], headers={"X-User-Id": "someone-else"}).status_code, 404)
        self.assertEqual(self.client.get(j["poll"]).status_code, 404)   # anonymous: no row, no leak

    def test_no_user_context_means_no_signer(self):
        r = self.client.post("/v1/orders", json=self.body(),
                            headers={"Idempotency-Key": self.key("nouser")})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.json()["error"]["code"], "SIGNER_UNAVAILABLE")


class TestBodyShape(ApiBase):
    """Shape before semantics, and never a 500 for a client mistake."""

    app_name = "api-shape"

    def test_a_missing_field_names_the_field_and_says_nothing_about_its_value(self):
        # post_order() can express "null" but not "absent", so the key is dropped by hand: a JSON null and a
        # missing field are different requests and the required-check must be the thing that says so.
        body = {k: v for k, v in self.body().items() if k != "tokenId"}
        r = self.client.post("/v1/orders", json=body, headers={"Idempotency-Key": self.key(),
                                                               "X-User-Id": "u-demo"})
        self.assertEqual(r.status_code, 422, r.text)
        err = r.json()["error"]
        self.assertEqual(err["code"], "VALIDATION")
        self.assertIn("missing: tokenId", err["message"])
        self.assertNotIn("0xT10", r.text, "the body's own values must not come back in the error")

    def test_an_unknown_field_is_refused_rather_than_ignored(self):
        # additionalProperties:false in the contract, enforced here: silently dropping "sIze" is how a typo
        # turns into an order at the wrong size, and the client believes their number was used.
        r = self.client.post("/v1/orders", json={**self.body(), "szie": "9"},
                            headers={"Idempotency-Key": self.key(), "X-User-Id": "u-demo"})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("unknown: szie", r.json()["error"]["message"])

    def test_a_body_that_is_not_an_object_is_a_422_not_a_500(self):
        for payload in (b'"a string"', b"[1,2]", b"null", b"7"):
            with self.subTest(payload=payload[:8]):
                r = self.client.post("/v1/orders", content=payload,
                                     headers={"Content-Type": "application/json",
                                              "Idempotency-Key": self.key(), "X-User-Id": "u-demo"})
                self.assertIn(r.status_code, (422,), r.text)
                self.assertNotIn("Traceback", r.text)

    def test_a_rejected_body_does_not_burn_the_idempotency_key(self):
        # The whole reason the shape check runs BEFORE idem.begin: a key consumed by an invalid body would
        # answer the FIXED request with IDEM_IN_PROGRESS, and the user could not trade until it expired.
        k = self.key()
        bad = {k2: v for k2, v in self.body().items() if k2 != "size"}
        r1 = self.client.post("/v1/orders", json=bad, headers={"Idempotency-Key": k, "X-User-Id": "u-demo"})
        self.assertEqual(r1.status_code, 422, r1.text)
        r2 = self.post_order(key=k)
        self.assertEqual(r2.status_code, 202, "the retry after a shape error must be accepted: %s" % r2.text)
        row = self.con.execute("SELECT COUNT(*) FROM order_intents WHERE idempotency_key=?", (k,)).fetchone()
        self.assertEqual(row[0], 1, "the invalid attempt wrote an intent row")

    def test_kill_switch_separates_missing_from_too_short(self):
        h = {"X-Admin-Token": "t"}
        miss = self.client.post("/v1/admin/kill-switch", json={"engaged": True}, headers=h)
        self.assertEqual((miss.status_code, miss.json()["error"]["code"]), (422, "VALIDATION"))
        short = self.client.post("/v1/admin/kill-switch", json={"engaged": True, "reason": "ab"}, headers=h)
        self.assertEqual((short.status_code, short.json()["error"]["code"]), (422, "BAD_REASON"))


class TestRefusalsThatCouldNotBeRecorded(ApiBase):
    """F18 (P14 D1): a refusal has to be *sayable*.

    `order_intents` carries `CHECK (size_micro > 0)`, and the deny path records every risk-gate refusal as a row.
    A size of zero is a valid parse (`parse_usdc("0") == 0`), so it reached the gate, got denied `ZERO_SIZE`, and
    then died on the INSERT: the user saw `500 INTERNAL`, whose message is "retry with the same Idempotency-Key" —
    and every retry re-derived the same denial and 500'd again. The loop was unbreakable from the client side.

    The class name is the property, not the input: *any* input the gate denies must produce either an authored
    refusal or a record of it, never a crash on the way to the record. The matrix below is the cheapest way to
    keep that true as new gates are added.
    """

    app_name = "api-refusals"

    def test_a_zero_size_is_an_authored_refusal_and_not_a_500(self):
        for size in ("0", "0.0", "0.000000"):
            with self.subTest(size=size):
                r = self.post_order(size=size)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertEqual(r.json()["error"]["code"], "ZERO_SIZE", r.text)
                self.assertFalse(r.json()["error"]["retryable"],
                                 "a zero size is not retryable — the same request cannot succeed")

    def test_the_zero_size_refusal_leaves_no_row_and_does_not_burn_the_key(self):
        k = self.key("zero")
        r1 = self.post_order(key=k, size="0")
        self.assertEqual(r1.status_code, 422, r1.text)
        rows = self.con.execute("SELECT COUNT(*) FROM order_intents WHERE idempotency_key=?", (k,)).fetchone()[0]
        self.assertEqual(rows, 0, "the refusal wrote an intent row it could not have written")
        r2 = self.post_order(key=k, size="10")
        self.assertEqual(r2.status_code, 202, "the fixed retry must be accepted: %s" % r2.text)

    def test_the_schema_invariant_is_unmoved_so_a_zero_row_is_still_unstorable(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,"
                             "size_micro,notional_micro,state,idempotency_key,created_ms,updated_ms) "
                             "VALUES ('x-zero','u-demo','0xM1','0xT10','BUY',500000,0,0,'rejected','x-zero',1,1)")
        self.con.rollback()

    def test_no_malformed_money_input_produces_a_5xx(self):
        # The shapes a client gets wrong: an off-grid price, an out-of-range price, a size that is not a number,
        # and both ends of the size range. Each one has an authored answer; none of them is "INTERNAL".
        cases = [{"price": "0.5555"}, {"price": "0"}, {"price": "1"}, {"price": "1.5"}, {"price": "-0.5"},
                 {"price": 0.55}, {"size": "0"}, {"size": "-5"}, {"size": "ten"}, {"size": "1e30"},
                 {"size": 10}, {"size": "0.0000001"}, {"size": "1000000000"}]
        seen = set()
        for case in cases:
            with self.subTest(**case):
                r = self.post_order(**case)
                self.assertLess(r.status_code, 500, "%s answered %s: %s" % (case, r.status_code, r.text))
                self.assertNotEqual(r.json()["error"]["code"], "INTERNAL", r.text)
                seen.add(r.json()["error"]["code"])
        self.assertTrue(seen <= {"OFF_TICK", "BAD_AMOUNT", "ZERO_SIZE", "BELOW_MIN_SIZE", "OVER_ORDER_CAP",
                                 "VALIDATION"}, "unexpected refusal codes: %s" % sorted(seen))


class TestKillSwitch(ApiBase):
    app_name = "api-kill"

    def switch(self, engaged, reason=None, token="admin-1"):
        body = {"engaged": engaged}
        if reason is not None:
            body["reason"] = reason
        h = {} if token is None else {"X-Admin-Token": token}
        return self.client.post("/v1/admin/kill-switch", json=body, headers=h)

    def test_engaging_blocks_new_orders_and_keeps_the_switch_as_history(self):
        r = self.switch(True, "incident drill")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["engaged"])
        o = self.post_order()
        self.assertEqual(o.status_code, 503, o.text)
        self.assertEqual(o.json()["error"]["code"], "RISK_HALT")
        self.assertTrue(o.json()["error"]["retryable"])
        n = self.con.execute("SELECT COUNT(*) FROM kill_switch_state").fetchone()[0]
        self.assertEqual(self.switch(False, "drill over").status_code, 200)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM kill_switch_state").fetchone()[0], n + 1)
        self.assertEqual(self.switch(False, "drill over").status_code, 200)
        self.assertEqual(self.post_order().status_code, 202, "still blocked after disengage")

    def test_a_reason_is_mandatory_because_the_db_says_so(self):
        # The API must translate the DB CHECK into a 4xx, not a 500: the rule lives in the schema, the
        # courtesy lives here.
        before = self.con.execute("SELECT COUNT(*) FROM kill_switch_state").fetchone()[0]
        r = self.switch(True, "")
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["error"]["code"], "BAD_REASON")
        # nothing written: the DB CHECK is the rule and the API translated it rather than tripping it
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM kill_switch_state").fetchone()[0], before)

    def test_no_token_no_switch(self):
        self.assertEqual(self.switch(True, "boom", token=None).status_code, 503)


class TestHealth(ApiBase):
    app_name = "api-health"

    def test_healthz_never_depends_on_the_database(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_readyz_reports_boot_defaults_and_then_stops(self):
        # The check P04 must not ship incapable of failing: it reads the FLAG STORE, not the dataclass
        # (a `getattr(FLAGS, "boot_defaults_used")` on the frozen Flags object can never be True).
        store = self.app.STORE
        keep, keep_loaded = store.boot_defaults_used, store._loaded_ms
        try:
            store.boot_defaults_used = True
            r = self.client.get("/readyz")
            self.assertEqual(r.status_code, 503)
            self.assertIn("flags_from_defaults", r.json()["problems"])
            store.boot_defaults_used = False
            store._loaded_ms = 0
            store.current()
            r2 = self.client.get("/readyz")
            self.assertEqual(r2.status_code, 200, r2.text)
            self.assertEqual(r2.json()["problems"], [])
        finally:
            store.boot_defaults_used, store._loaded_ms = keep, keep_loaded

    def test_the_flag_store_field_the_health_check_reads_actually_exists(self):
        self.assertTrue(hasattr(self.app.STORE, "boot_defaults_used"))
        self.assertFalse(hasattr(self.app.Flags(), "boot_defaults_used"),
                         "Flags must NOT grow this field: the readiness check is supposed to read the store")

    def test_readyz_flags_a_dead_database(self):
        store, real = self.app.STORE, self.app._db
        keep = store.boot_defaults_used
        store.boot_defaults_used = False
        try:
            self.app._db = None                       # every read raises; readiness must notice
            self.assertEqual(self.client.get("/readyz").json()["problems"], ["db_unreachable"])
            self.assertEqual(self.client.get("/healthz").status_code, 200)   # liveness stays green
        finally:
            self.app._db = real
            store.boot_defaults_used = keep


class TestLogHygiene(ApiBase):
    app_name = "api-logs"

    def test_a_request_line_is_json_with_the_id_and_no_body(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.post_order()
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 1, "the access log printed nothing at all")
        rec = json.loads(lines[-1])
        self.assertTrue({"ts", "level", "ev", "rid", "path", "status", "dur_ms"} <= set(rec), rec)
        blob = json.dumps(rec)
        self.assertNotIn("tokenId", blob)                 # no body, no field of a body
        self.assertNotIn("0xT10", blob)                  # ...not even an innocuous-looking one
        self.assertNotIn("0.50", blob)
        self.assertEqual(rec["ev"], "http")


if __name__ == "__main__":
    unittest.main()
