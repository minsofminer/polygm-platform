"""P10 · D5 Wallet Radar, through the HTTP layer.

What these tests defend, in the order a user meets it:

  * a scan of more than ten markets is refused rather than silently trimmed to the first ten;
  * the profit ranking will not place a wallet under the sample gate, and the refusal is visible as its own
    sentence rather than as an absence;
  * a repeated scan is free (the cache is the reason the second scan is cheap, so charging for it would price
    the thing the design sells);
  * past the latency line the scan becomes a job, the job runs exactly once, and the second poll reads it;
  * the budget is counted from the append-only audit log, and spending it is a 429 that names the plan;
  * nothing in the payload is a float.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "packages"), str(ROOT / "services" / "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import import_app                                                       # noqa: E402

USER = {"X-User-Id": "u-demo"}


class RadarTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = import_app("radar-api")
        cls.client = cls.app.TestClient() if hasattr(cls.app, "TestClient") else None
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)
        # Four fills is the line the seed clears 133 times over (`0xM1` alone holds 405). The threshold is here
        # rather than a hardcoded market id because the seed's shape is what decides whether a ranking is
        # meaningful: one market would make "shared exposure" and "most active" the same list.
        rows = cls.app._db.execute(
            "SELECT m.id, COUNT(*) AS n FROM tape_fills f JOIN markets m ON m.condition_id=f.condition_id"
            " GROUP BY m.id HAVING n >= 4 ORDER BY n DESC LIMIT 12").fetchall()
        cls.markets = [str(r[0]) for r in rows]
        assert len(cls.markets) >= 8, "the seed must hold at least eight markets with fills for these tests"

    def scan(self, markets, **kw):
        return self.client.post("/v1/radar/runs", json={"marketIds": markets, **kw},
                                headers={**USER, "Idempotency-Key": "radar-" + str(abs(hash(tuple(markets))))[:12]})

    def fresh_markets(self, n=2):
        """A combination no other test has scanned, so the cache cannot make a test pass by accident."""
        self.__class__._rot = (getattr(self.__class__, "_rot", 0) + n) % len(self.markets)
        rot = self.__class__._rot
        out = [self.markets[(rot + i) % len(self.markets)] for i in range(n)]
        self.app._RADAR_CACHE.clear()                 # the cache is per-process state; a test owns its window
        return out

    def scan_as(self, markets, uid, **kw):
        """A scan as somebody else, which now needs the account to EXIST: a P10 write records its key in
        idempotency_keys, and that table carries an FK to users — so a made-up uid spends the budget fine
        (audit_log has no such FK) and then dies in the insert. Found by this test turning 500 after the
        idempotency record half landed, which is the good version of the failure: the alternative is a
        service that 500s for a real user whose row was deleted."""
        self.app._db.execute("INSERT OR IGNORE INTO users (id, created_ms) VALUES (?,?)", (uid, self.app._now_ms()))
        return self.client.post("/v1/radar/runs", json={"marketIds": markets, **kw},
                                headers={"X-User-Id": uid, "Idempotency-Key": "radar-" + uid[-6:] + "-1"})

    def spend(self, n=1, *, uid=USER["X-User-Id"]):
        """Burn `n` scans of budget through the audit log, which is how the real counter is fed."""
        before = self.app._radar_used_today(uid)
        for _ in range(n):
            self.app._audit_scan(uid, {"markets": ["m-budget"], "job": None, "cached": False})
        return before + n

    # ---------------------------------------------------------------- the shape of a scan

    def test_two_markets_return_four_rankings_and_their_questions(self):
        r = self.scan(self.fresh_markets(2))
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        for key in ("asOf", "staleAfter", "serverAsOf"):
            self.assertIn(key, body)
        self.assertEqual(sorted(["active", "earliest", "overlap", "profit"]), sorted(body["rankings"].keys()))
        self.assertEqual(4, len(body["rankingsMeta"]))
        self.assertTrue(all(m.get("question") for m in body["rankingsMeta"]))
        self.assertEqual(2, len(body["markets"]))
        self.assertEqual(20, body["sampleGate"])
        self.assertEqual("done", body["status"])
        self.assertIsNone(body["quota"]["jobId"])
        self.assertFalse(body["quota"]["cached"])

    def test_every_ranking_row_says_why_it_is_there_and_nothing_is_a_float(self):
        r = self.scan(self.fresh_markets(3))
        body = r.json()
        rows = [row for rows in body["rankings"].values() for row in rows]
        self.assertTrue(rows, "the seeded tape must produce at least one ranked wallet")
        for row in rows:
            self.assertTrue(row["reason"], row)
            self.assertTrue(row["anonWallet"].startswith("w_"), row["anonWallet"])
            self.assertTrue(row["matched"], row)
            self.assertTrue(all(m["marketId"] for m in row["matched"]), row)
            self.assertGreaterEqual(row["rank"], 1)
        # A float anywhere in this payload is a float in a money field. `parse_float` is called for each JSON
        # number with a fraction, so a non-empty list here is the API having sent one - the same rule the gate's
        # c7 scanner enforces on the source, checked on the wire instead of on the file.
        seen: list[str] = []
        json.loads(r.text, parse_float=seen.append)
        self.assertEqual([], seen, "the payload carried a JSON float")

    def test_the_profit_ranking_excludes_the_ungated_and_keeps_them_visible(self):
        r = self.scan(self.fresh_markets(3))
        body = r.json()
        for row in body["rankings"]["profit"]:
            self.assertFalse(row["insufficientSample"], row)
            self.assertIsNotNone(row["winRateBps"], row)
            self.assertIn("gate", row["reason"])
        for row in body["rankings"]["profit"]:
            self.assertNotIn(row["anonWallet"], [u["anonWallet"] for u in body["unranked"]])
        # Every ranked wallet is also in `active`, so a table cannot rank somebody it did not see.
        active = {row["anonWallet"] for row in body["rankings"]["active"]}
        self.assertTrue({row["anonWallet"] for row in body["rankings"]["profit"]} <= active)

    def test_the_earliest_ranking_names_the_moment_it_measured(self):
        body = self.scan(self.fresh_markets(2)).json()
        for row in body["rankings"]["earliest"]:
            self.assertIn("first fill in the set at", row["reason"])
            self.assertIn("UTC", row["reason"])

    def test_shared_exposure_lists_only_wallets_that_are_in_more_than_one_of_the_markets(self):
        body = self.scan(self.fresh_markets(4)).json()
        self.assertTrue(body["rankings"]["overlap"],
                        "the seeded wallets trade across markets; an empty overlap list would make this test "
                        "vacuous rather than failing")
        for row in body["rankings"]["overlap"]:
            self.assertGreaterEqual(len(row["matched"]), 2, row)
            self.assertIn("one piece of news", row["reason"])

    # ---------------------------------------------------------------- scope and budget

    def test_eleven_markets_is_refused_rather_than_trimmed(self):
        # The app's own sentence, not the request schema's. `minItems`/`maxItems` are deliberately left off the
        # schema for this field: FastAPI's 422 would arrive first and say "outside the range", which is true and
        # useless, while the sentence a user needs is the one that says what the limit is and what they sent.
        sent = self.markets + [self.markets[0]]
        r = self.scan(sent)
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("RADAR_SCOPE", r.json()["error"]["code"])
        message = r.json()["error"]["message"]
        self.assertIn("up to 10", message)
        self.assertIn("%d were sent" % len(sent), message)

    def test_market_ids_that_are_not_an_array_are_refused_with_a_sentence(self):
        r = self.client.post("/v1/radar/runs", json={"marketIds": "0xM1"},
                             headers={**USER, "Idempotency-Key": "radar-not-a-list"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("RADAR_SCOPE", r.json()["error"]["code"])
        self.assertIn("array", r.json()["error"]["message"])

    def test_no_markets_is_refused(self):
        r = self.scan([])
        self.assertEqual(422, r.status_code, r.text)

    def test_a_missing_market_ids_field_names_the_field(self):
        r = self.client.post("/v1/radar/runs", json={"ranking": "profit"},
                             headers={**USER, "Idempotency-Key": "radar-nofield"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("marketIds", json.dumps(r.json()))

    def test_an_unknown_ranking_is_refused_by_the_schema(self):
        r = self.scan(self.fresh_markets(2), ranking="best")
        self.assertEqual(422, r.status_code, r.text)

    def test_a_scan_without_a_session_is_401(self):
        r = self.client.post("/v1/radar/runs", json={"marketIds": self.markets[:2]},
                             headers={"Idempotency-Key": "radar-noauth"})
        self.assertEqual(401, r.status_code, r.text)

    def test_a_mutation_without_an_idempotency_key_is_400(self):
        r = self.client.post("/v1/radar/runs", json={"marketIds": self.markets[:2]}, headers=USER)
        self.assertEqual(400, r.status_code, r.text)
        self.assertEqual("IDEM_KEY_REQUIRED", r.json()["error"]["code"])
        self.assertEqual("idem", r.json()["error"]["requestId"])       # no work happened; no request id either
        malformed = self.client.post("/v1/radar/runs", json={"marketIds": self.markets[:2]},
                                     headers={**USER, "Idempotency-Key": "short"})
        self.assertEqual(422, malformed.status_code, malformed.text)   # present but wrong shape is a 422
        self.assertIn("Idempotency-Key", malformed.json()["error"]["message"])

    # ---------------------------------------------------------------- the cache and the budget

    def test_the_same_scan_twice_is_cached_and_costs_nothing(self):
        markets = self.fresh_markets(2)
        first = self.scan(markets).json()
        self.assertFalse(first["quota"]["cached"])
        used = first["quota"]["usedToday"]
        second = self.scan(list(reversed(markets))).json()      # order-insensitive: the same scan
        self.assertTrue(second["quota"]["cached"], second["quota"])
        self.assertEqual(used, second["quota"]["usedToday"])
        self.assertEqual(used - 1, first["quota"]["usedToday"] - 1)
        self.assertIn("cost you nothing", second["quota"]["note"])

    def test_the_budget_refusal_names_the_plan_and_leaves_the_cache_free(self):
        """A separate account, because this test's whole point is to spend a budget to the end. Spending
        `u-demo`'s would make every later test in this class a 429 - which is a test-ordering bug wearing a
        product failure's clothes, and it cost a debugging run to see that."""
        uid = "u-radar-broke"
        markets = self.fresh_markets(2)
        self.spend(self.app._radar_plan(uid)[1], uid=uid)
        r = self.scan_as(markets, uid)
        self.assertEqual(429, r.status_code, r.text)
        err = r.json()["error"]
        self.assertEqual("QUOTA_EXCEEDED", err["code"])
        self.assertTrue(err["retryable"])
        self.assertIn("free", err["message"])       # the sentence states that a cached repeat costs nothing
        self.assertIn("scans used today", err["message"])
        # ... and a scan the user already ran before the budget was spent is still answered from the cache.
        self.app._RADAR_CACHE["%s|%s" % (uid, self.app._radar.cache_key(markets))] = {"atMs": self.app._now_ms()}
        cached = self.scan_as(markets, uid)
        self.assertEqual(200, cached.status_code, cached.text)
        self.assertTrue(cached.json()["quota"]["cached"])

    def test_the_cost_note_states_the_cache_window_the_async_line_and_the_order_rule(self):
        body = self.scan(self.fresh_markets(2)).json()
        note = body["costNote"]
        self.assertIn("60 seconds", note)
        self.assertIn("different order is the same scan", note)
        self.assertIn("runs as a job", note)

    # ---------------------------------------------------------------- the async half

    def test_a_large_scan_becomes_a_job_that_runs_once_and_is_cached_afterwards(self):
        markets = self.fresh_markets(8)
        first = self.scan(markets)
        self.assertEqual(200, first.status_code, first.text)
        body = first.json()
        job_id = body["quota"]["jobId"]
        self.assertTrue(job_id, body["quota"])
        self.assertEqual("queued", body["status"])
        self.assertIn("runs as a job", body["quota"]["note"])
        self.assertIsNone(self.app._RADAR_JOBS[job_id]["payload"] if job_id in self.app._RADAR_JOBS
                          and "payload" in self.app._RADAR_JOBS[job_id] else None)

        done = self.client.get("/v1/radar/runs/%s" % job_id, headers=USER)
        self.assertEqual(200, done.status_code, done.text)
        payload = done.json()
        self.assertEqual("done", payload["status"])
        self.assertIsNone(payload["quota"]["jobId"])
        self.assertIn("cached", payload["quota"]["note"])
        self.assertTrue(payload["rankings"]["active"], "the job must return the same ranking set")
        again = self.client.get("/v1/radar/runs/%s" % job_id, headers=USER).json()
        self.assertEqual(payload["rankings"]["active"], again["rankings"]["active"])
        self.assertIsNone(again["quota"]["jobId"])

    def test_an_unknown_job_is_404_and_a_job_belongs_to_one_account(self):
        r = self.client.get("/v1/radar/runs/rr-nope", headers=USER)
        self.assertEqual(404, r.status_code, r.text)
        self.assertEqual("NO_SUCH_RESOURCE", r.json()["error"]["code"])
        self.assertEqual(401, self.client.get("/v1/radar/runs/rr-nope").status_code)

    def test_a_job_is_not_readable_by_another_account(self):
        markets = self.fresh_markets(7)
        job_id = self.scan(markets).json()["quota"]["jobId"]
        other = self.client.get("/v1/radar/runs/%s" % job_id, headers={"X-User-Id": "u-someone-else"})
        self.assertEqual(404, other.status_code, other.text)
        self.assertEqual(0, other.json().get("rankings", {}).get("active", []) and 1 or 0)


class RadarRankingsTestCase(unittest.TestCase):
    """The pure half: the four rankings, with no database and no HTTP in the way."""

    def setUp(self):
        from polygm_core.radar import rankings as mod
        self.mod = mod

    def fills(self):
        out = []
        # w_big: many fills, every market resolved, one loss.
        for i in range(25):
            out.append({"wallet": "w_big", "marketId": "m1" if i % 2 else "m2", "side": "BUY",
                        "notionalMicro": 10_000_000, "tsMs": 1_000 + i, "resolved": True,
                        "won": i != 3, "realisedMicro": (-5_000_000 if i == 3 else 4_000_000)})
        # w_early: one fill, earliest of all, and it lost.
        out.append({"wallet": "w_early", "marketId": "m1", "side": "BUY", "notionalMicro": 9_000_000,
                    "tsMs": 5, "resolved": True, "won": False, "realisedMicro": -9_000_000})
        # w_wide: in both markets, few fills.
        out.append({"wallet": "w_wide", "marketId": "m1", "side": "SELL", "notionalMicro": 2_000_000,
                    "tsMs": 900, "resolved": False})
        out.append({"wallet": "w_wide", "marketId": "m2", "side": "BUY", "notionalMicro": 3_000_000,
                    "tsMs": 950, "resolved": False})
        return out

    def test_the_four_rankings_ask_four_different_questions(self):
        r = self.mod.rank(self.fills(), market_ids=["m1", "m2"], sample_gate_n=20)
        self.assertEqual("w_big", r["active"][0]["wallet"])                    # most fills
        self.assertEqual("w_early", r["earliest"][0]["wallet"])                # first fill in the set
        # Both of these wallets trade both markets, and the ranking says so with the count it measured.
        self.assertEqual({"w_big", "w_wide"}, {row["wallet"] for row in r["overlap"]})
        self.assertTrue(all("of the 2 markets you picked" in row["reason"] for row in r["overlap"]))
        self.assertEqual([], r["profit"])                                      # nobody clears a gate of 20
        self.assertTrue(all(row["insufficientSample"] for row in r["unranked"]))

    def test_a_losing_wallet_can_top_the_earliest_ranking_and_that_is_not_a_bug(self):
        r = self.mod.rank(self.fills(), market_ids=["m1", "m2"], sample_gate_n=20)
        top = r["earliest"][0]
        self.assertEqual("w_early", top["wallet"])
        self.assertLess(top["realisedMicro"], 0)
        self.assertIn("first fill in the set at", top["reason"])

    def test_the_gate_is_a_gate_and_the_profit_list_orders_by_realised_money(self):
        fills = self.fills()
        for i in range(20):                                        # push w_big over a gate of 20 settled markets
            fills.append({"wallet": "w_big", "marketId": "m%d" % (100 + i), "side": "BUY",
                          "notionalMicro": 1_000_000, "tsMs": 2_000 + i, "resolved": True, "won": True,
                          "realisedMicro": 1_000_000})
        r = self.mod.rank(fills, market_ids=["m1", "m2"] + ["m%d" % (100 + i) for i in range(20)], sample_gate_n=20)
        self.assertEqual(1, len(r["profit"]))
        self.assertEqual("w_big", r["profit"][0]["wallet"])
        self.assertFalse(r["profit"][0]["insufficientSample"])
        self.assertGreater(r["profit"][0]["winRateBps"], 8000)

    def test_each_ranking_carries_its_own_reason_sentence(self):
        r = self.mod.rank(self.fills(), market_ids=["m1", "m2"], sample_gate_n=20)
        reasons = {row["wallet"]: row["reason"] for row in r["active"]}
        self.assertIn("fills across", reasons["w_big"])
        self.assertIn("first fill", r["earliest"][0]["reason"])
        self.assertIn("one piece of news", r["overlap"][0]["reason"])
        self.assertNotEqual(r["active"][0]["reason"], r["earliest"][0]["reason"])

    def test_scope_validation_refuses_over_ten_and_reports_what_it_received(self):
        ids, refusal = self.mod.validate_markets([])
        self.assertEqual([], ids)
        self.assertIn("at least one", refusal)
        ids, refusal = self.mod.validate_markets(["m%d" % i for i in range(11)])
        self.assertEqual(10, len(ids))
        self.assertEqual("radar scans up to 10 markets; 11 were sent", refusal)
        ids, refusal = self.mod.validate_markets(["a", "b"])
        self.assertIsNone(refusal)

    def test_the_cache_key_ignores_the_order_of_the_selection(self):
        self.assertEqual(self.mod.cache_key(["b", "a"]), self.mod.cache_key(["a", "b"]))

    def test_the_meta_sentences_state_the_gate_rather_than_the_word_profit(self):
        r = self.mod.rank(self.fills(), market_ids=["m1", "m2"], sample_gate_n=20)
        by_id = {m["id"]: m for m in r["rankingsMeta"]}
        self.assertIn("20 or more settled markets", by_id["profit"]["question"])
        self.assertIn("losing wallet can top this list", by_id["earliest"]["question"])


if __name__ == "__main__":
    unittest.main()
