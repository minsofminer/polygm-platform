"""P11 · D2's API, walked as the phase's own acceptance criterion.

The class at the bottom is the gate: **a trader at rank 47 with fewer resolved markets than the trader at rank
12, and the explanation of why the ranking is still correct.** It runs against `seed_leaderboard`'s population —
sixty wallets plus five adversarial specimens — because the property is a fact about a real board and not about
a fixture that was tuned until it agreed.

The tests around it pin the rules that make the board worth reading at all:

  1. **No address ever leaves the API.** Every payload is grepped for a `0x…` shape: the boards name traders by
     pseudonym, and one endpoint that forgot would undo the scheme for every wallet the leaderboard ranks.
  2. **A refusal is a sentence, not a blank.** A wallet under the sample gate or the turnover floor appears in
     `unranked` with the number that refused it — "why am I not on it" is the question every leaderboard gets.
  3. **Nothing hides a loss.** A blown-up wallet keeps its negative score, stays on the board, and is counted in
     the board's own summary; a wash is subtracted and printed; a disputed market is withheld and counted.
  4. **The specification is served, not transcribed.** `/methodology` returns the same object the engine ranked
     from, so a formula cannot exist in the docs and not in the code.
"""
from __future__ import annotations

import json
import re
import unittest

from conftest import import_app, refresh_flags  # noqa: F401

MICRO = 10 ** 6
USER = {"X-User-Id": "u-demo"}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")


class LeaderboardBase(unittest.TestCase):
    app_name = "api-leaderboard"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        cls.app = import_app(cls.app_name)
        import seed_leaderboard
        cls.seeded = seed_leaderboard.seed_sqlite()          # on top of the base fixture, into the same file
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)

    def setUp(self):
        refresh_flags(self.app)

    def get(self, url, **params):
        r = self.client.get(url, params=params)
        self.assertEqual(r.status_code, 200, "%s -> %d %s" % (url, r.status_code, r.text[:400]))
        return r.json()

    def board(self, board="risk_adjusted", **params):
        return self.get("/v1/leaderboard", board=board, **params)

    def rows_by_rank(self, board="risk_adjusted", **params):
        return {r["rank"]: r for r in self.board(board, limit=200, **params)["rows"]}

    def payloads(self):
        """Every public leaderboard payload, for the grep-everything tests."""
        out = [self.get("/v1/leaderboard"), self.get("/v1/leaderboard/boards"),
               self.get("/v1/leaderboard/methodology"), self.get("/v1/leaderboard/runs")]
        for board, extra in (("risk_adjusted", {}), ("win_rate", {"window": "90d"}), ("volume", {"window": "7d"}),
                             ("rising", {}), ("category", {"category": "Politics"}), ("copied", {})):
            out.append(self.get("/v1/leaderboard", board=board, limit=200, **extra))
        anon = self.rows_by_rank()[1]["anon"]
        out.append(self.get("/v1/leaderboard/why", a=anon, b=self.rows_by_rank()[2]["anon"]))
        out.append(self.get("/v1/leaderboard/snapshots", anon=anon))
        return out

    def wallet_of(self, anon: str) -> str:
        import app as app_mod
        for i in range(60):
            import seed_leaderboard
            w = seed_leaderboard.wallet_for(i)
            if app_mod._anon(w) == anon:
                return w
        import seed_leaderboard
        for w in (seed_leaderboard.WALLET_LEAD, seed_leaderboard.WALLET_FARM, seed_leaderboard.WALLET_LUCKY,
                  seed_leaderboard.WALLET_BLOWN, seed_leaderboard.WALLET_WASH, seed_leaderboard.WALLET_NEW,
                  seed_leaderboard.WALLET_THIN, seed_leaderboard.WALLET_DUST):
            if app_mod._anon(w) == anon:
                return w
        return ""


class TestTheGateQuestion(LeaderboardBase):
    """The phase's acceptance criterion, as a test: rank 47, rank 12, and the sentence between them."""

    def test_a_wallet_at_rank_47_with_fewer_resolved_markets_than_the_wallet_at_rank_12(self):
        rows = self.rows_by_rank()
        r12, r47 = rows.get(12), rows.get(47)
        self.assertIsNotNone(r12, "the fixture has to have a 12th place")
        self.assertIsNotNone(r47, "the fixture has to have a 47th place")
        self.assertLess(r47["settledMarkets"], r12["settledMarkets"],
                        "the whole point: a small sample is not a demerit, and the population must contain the "
                        "pair that makes that visible")
        self.assertGreater(r47["scoreBps"], 0)
        self.assertGreater(r12["scoreBps"], 0)

    def test_the_api_explains_it_with_both_component_sets(self):
        rows = self.rows_by_rank()
        why = self.get("/v1/leaderboard/why", a=rows[47]["anon"], b=rows[12]["anon"])
        self.assertTrue(why["why"].strip())
        self.assertIn("scoreBps", why["why"], "the sentence names the metric that decided it")
        self.assertIn("the sample size did not decide this", why["why"])
        for side in ("a", "b"):
            self.assertIn("components", why[side])
            self.assertIn("formula", why[side]["components"])
            self.assertIn("scaleMicro", why[side]["components"])
        self.assertGreaterEqual(why["a"]["settledMarkets"], 1)
        self.assertEqual(why["sampleGate"], 20)

    def test_the_other_direction_is_answerable_too(self):
        """The interesting case is fewer markets ABOVE more; the reverse must also have a sentence, because a
        user who reports "small sample ranked high" will be sent the same endpoint."""
        rows = self.rows_by_rank()
        big = max(rows.values(), key=lambda r: r["settledMarkets"])
        small = min(rows.values(), key=lambda r: r["settledMarkets"])
        why = self.get("/v1/leaderboard/why", a=big["anon"], b=small["anon"])
        self.assertTrue(why["why"].strip())
        self.assertIn("settled markets", why["why"])


class TestPrivacyAndShape(LeaderboardBase):
    def test_no_wallet_address_appears_anywhere(self):
        for payload in self.payloads():
            text = json.dumps(payload)
            leaked = sorted(set(ADDRESS.findall(text)))
            self.assertEqual(leaked, [], "an address reached a public leaderboard payload: %s" % leaked[:3])

    def test_every_row_is_a_pseudonym_and_the_pseudonym_is_stable(self):
        first = self.rows_by_rank()[1]["anon"]
        second = self.rows_by_rank()[1]["anon"]
        self.assertTrue(first.startswith("w_"), first)
        self.assertEqual(first, second)

    def test_money_is_shipped_as_micro_integers_and_display_strings(self):
        row = self.rows_by_rank()[1]
        for key in ("realisedMicro", "trimmedMicro", "verifiedVolumeMicro", "maxDrawdownMicro", "volatilityMicro"):
            self.assertIsInstance(row[key], int, key)
        for key in ("realised", "trimmed", "verifiedVolume", "drawdown", "volatility"):
            self.assertIsInstance(row[key], str, key)
            self.assertRegex(row[key], r"^-?[0-9]+(\.[0-9]{1,6})?$", key)

    def test_every_ranked_row_carries_the_components_it_was_ranked_on(self):
        row = self.rows_by_rank()[1]
        for key in ("scoreBps", "settledMarkets", "winRateBps", "insufficientSample", "sampleNote",
                    "bestTradeShareBps", "maxDrawdownMicro", "volatilityMicro", "components", "rankBadge"):
            self.assertIn(key, row, key)
        self.assertEqual(row["rankBadge"]["rank"], row["rank"])
        self.assertFalse(row["insufficientSample"], "a ranked row's win rate is above the gate by definition")


class TestTheRulesRefuseProperly(LeaderboardBase):
    def unranked_for(self, wallet: str, board="risk_adjusted", **params):
        anon = self.app.app.__dict__ and __import__("app")._anon(wallet)
        out = self.board(board, limit=200, **params)
        return next((u for u in out["unranked"] if u["anon"] == anon), None)

    def test_a_wallet_under_the_sample_gate_is_listed_with_its_number(self):
        import seed_leaderboard
        thin = self.unranked_for(seed_leaderboard.WALLET_THIN)
        self.assertIsNotNone(thin, "a wallet with nine settled markets must be listed, not hidden")
        self.assertIn("this board needs 20", " ".join(thin["reasons"]))
        self.assertEqual(thin["settledMarkets"], 9)
        self.assertIn("not ranked", thin["note"])

    def test_the_turnover_floor_is_a_separate_refusal_from_the_sample_gate(self):
        """Both refusals exist in the population because they fail differently: a count can be manufactured
        with $1 positions, and turnover can be manufactured by churning one position."""
        import seed_leaderboard
        dust = self.unranked_for(seed_leaderboard.WALLET_DUST)
        self.assertIsNotNone(dust)
        self.assertIn("below the 500000000 micro floor", " ".join(dust["reasons"]))
        self.assertEqual(dust["settledMarkets"], 21, "the sample was never the problem here")

    def test_the_washer_is_refused_for_its_sample_and_its_subtraction_is_printed(self):
        import seed_leaderboard
        wash = self.unranked_for(seed_leaderboard.WALLET_WASH)
        self.assertIsNotNone(wash)
        self.assertGreater(wash["washedMicro"], 0, "the refusal row still states what was removed")
        self.assertTrue(wash["washNote"])

    def test_the_copy_farm_is_refused_on_the_copied_board_and_only_for_being_a_farm(self):
        import seed_leaderboard
        copied = self.board("copied", limit=200)
        anon = __import__("app")._anon(seed_leaderboard.WALLET_FARM)
        farm = next((u for u in copied["unranked"] if u["anon"] == anon), None)
        self.assertIsNotNone(farm, "the farm has to be on the copied board's own list")
        reasons = " ".join(farm["reasons"])
        self.assertIn("mechanically derived from", reasons)
        self.assertNotIn("nobody is copying", reasons, "it HAS a copier: the farm rule is the refusal")
        self.assertNotIn("turnover", reasons, "and it clears the floor: the farm rule is the only refusal")
        self.assertTrue(any(r["copiers"] >= 1 for r in copied["rows"]), "the board itself has rows")

    def test_a_blown_up_wallet_stays_on_the_board_with_its_negative_score_and_is_counted(self):
        out = self.board(limit=200)
        blown = [r for r in out["rows"] if r["state"] == "blew_up"]
        self.assertTrue(blown, "a board that quietly drops blown-up accounts is lying")
        self.assertGreaterEqual(out["summary"]["blewUpCount"], len(blown))
        self.assertTrue(any("blew up" in " ".join(r["labels"]) for r in blown))
        self.assertTrue(any(r["scoreBps"] <= 0 for r in blown),
                        "the score is the real one, not a placeholder")

    def test_the_lucky_gambler_shows_its_best_trade_share(self):
        out = self.board(limit=200)
        lucky = [r for r in out["rows"] if r["bestTradeShareBps"] >= 5_000]
        self.assertTrue(lucky, "one 100x bet must be visible on the row that carries it")
        self.assertTrue(any("one trade" in " ".join(r["labels"]) for r in lucky))
        for row in lucky:
            # The row states the share AND the amount the trim took out, which are the two numbers a reader
            # needs to see the 100x bet for themselves rather than take the label's word for it.
            self.assertIn("%", " ".join(row["labels"]))
            self.assertGreater(row["components"]["trimRemovedMicro"], 0)
            self.assertEqual(row["trimmedMicro"], row["realisedMicro"] - row["components"]["trimRemovedMicro"])

    def test_a_provisional_wallet_is_labelled_and_barred_from_rising_by_its_age(self):
        import seed_leaderboard
        anon = __import__("app")._anon(seed_leaderboard.WALLET_NEW)
        ranked = [r for r in self.board(limit=200)["rows"] if r["anon"] == anon]
        self.assertTrue(ranked, "a three-day-old wallet with a real record is ranked, and labelled")
        self.assertTrue(any("provisional" in lab for lab in ranked[0]["labels"]))
        rising = self.board("rising", limit=200)
        refusal = next((u for u in rising["unranked"] if u["anon"] == anon), None)
        self.assertIsNotNone(refusal)
        self.assertIn("no 7-day history to have improved on", " ".join(refusal["reasons"]))

    def test_a_disputed_market_is_withheld_and_counted(self):
        out = self.board(limit=200)
        self.assertGreaterEqual(out["summary"]["disputedWithheld"], 1)
        rows = [r for r in out["rows"] if r["disputedExcluded"]]
        self.assertTrue(rows, "the result is withheld from the ranking and counted on the row")

    def test_the_category_board_only_admits_specialists(self):
        out = self.board("category", category="Politics", limit=200)
        self.assertTrue(out["rows"])
        for row in out["rows"]:
            self.assertEqual(row["category"], "Politics")
            self.assertGreaterEqual(row["categoryShareBps"], 5_000, "at least half of its markets")
            self.assertGreaterEqual(row["categorySettled"], 20)

    def test_a_window_a_board_does_not_read_is_refused(self):
        r = self.client.get("/v1/leaderboard", params={"board": "risk_adjusted", "window": "24h"})
        self.assertEqual(r.status_code, 422, "there is no 24-hour SKILL board")
        r = self.client.get("/v1/leaderboard", params={"board": "nope"})
        self.assertEqual(r.status_code, 422)
        ok = self.get("/v1/leaderboard", board="volume", window="24h")
        self.assertEqual(ok["window"], "24h", "…while a day of volume is a fact, and is offered")


class TestFreshnessAndHistory(LeaderboardBase):
    def test_freshness_names_the_cadence_it_is_measured_against(self):
        out = self.board()
        fresh = out["freshness"]
        self.assertEqual(fresh["cadenceMs"], 3_600_000)
        self.assertEqual(fresh["source"], "live")
        self.assertIn("ledger", fresh["note"])
        self.assertTrue(fresh["stale"] or fresh["snapshotMs"] is None or fresh["ageMs"] >= 0)

    def test_the_recompute_writes_the_history_the_sparkline_reads(self):
        rows = self.rows_by_rank()
        anon = rows[1]["anon"]
        before = self.get("/v1/leaderboard/snapshots", anon=anon)["snapshots"]
        r = self.client.post("/v1/leaderboard/recompute", json={},
                            headers={**USER, "Idempotency-Key": "lb-recompute-0001"})
        self.assertEqual(r.status_code, 200, r.text[:400])
        body = r.json()
        self.assertEqual(len(body["runs"]), 9, "six boards, four of them the category board's categories")
        self.assertGreater(body["snapshots"], 200)
        self.assertGreater(body["integrity"], 0)
        after = self.get("/v1/leaderboard/snapshots", anon=anon)
        self.assertGreater(after["snapshots"], before)
        board = next(b for b in after["boards"] if b["board"] == "risk_adjusted")
        self.assertTrue(board["points"])
        point = board["points"][-1]
        for key in ("tsMs", "rank", "scoreBps", "settledMarkets", "maxDrawdown"):
            self.assertIn(key, point)
        self.assertEqual(board["latestRank"], point["rank"])

    def test_the_recompute_is_idempotent_per_key(self):
        first = self.client.post("/v1/leaderboard/recompute", json={},
                                 headers={**USER, "Idempotency-Key": "lb-recompute-0002"}).json()
        second = self.client.post("/v1/leaderboard/recompute", json={},
                                  headers={**USER, "Idempotency-Key": "lb-recompute-0002"}).json()
        self.assertEqual(first["snapshots"], second["snapshots"])
        self.assertEqual(first["bucketHour"], second["bucketHour"])

    def test_the_recompute_needs_an_idempotency_key(self):
        r = self.client.post("/v1/leaderboard/recompute", json={}, headers=USER)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/v1/leaderboard/recompute", json={}, headers={**USER, "Idempotency-Key": "short"})
        self.assertEqual(r.status_code, 422)

    def test_the_run_record_is_public_and_says_what_it_wrote(self):
        self.client.post("/v1/leaderboard/recompute", json={},
                         headers={**USER, "Idempotency-Key": "lb-recompute-0003"})
        runs = self.get("/v1/leaderboard/runs")
        self.assertTrue(runs["rows"])
        self.assertEqual(runs["cadences"]["risk_adjusted"], 3_600_000)
        row = runs["rows"][0]
        for key in ("board", "window", "ranked", "unranked", "blewUp", "durationMs", "computedMs"):
            self.assertIn(key, row)


class TestMethodologyAndCounts(LeaderboardBase):
    def test_the_methodology_is_the_engine_specification(self):
        from polygm_core.leaderboard import boards
        m = self.get("/v1/leaderboard/methodology")
        self.assertEqual([b["id"] for b in m["boards"]], list(boards.BOARD_IDS))
        self.assertEqual(m["defaultBoard"], "risk_adjusted")
        self.assertEqual(m["sampleGate"], 20)
        self.assertEqual(m["minVerifiedVolumeMicro"], 500 * MICRO)
        self.assertEqual(len(m["integrity"]), 6)
        for rule in m["integrity"]:
            self.assertTrue(rule["does"] and rule["doesNot"], "every rule says what it does NOT do")
        served = {b["id"]: b["formula"] for b in m["boards"]}
        self.assertEqual(served, {b["id"]: b["formula"] for b in boards.BOARDS},
                         "the published formula and the ranked one are the same string")
        self.assertIn("risk_adjusted:30d", m["windows"])

    def test_the_picker_offers_exactly_the_boards_that_exist(self):
        from polygm_core.leaderboard import boards
        picker = self.get("/v1/leaderboard/boards")
        self.assertEqual([b["id"] for b in picker["boards"]], list(boards.BOARD_IDS))
        self.assertEqual(picker["defaultBoard"], "risk_adjusted")
        self.assertEqual(picker["categories"], list(boards.CATEGORIES))
        self.assertTrue(next(b for b in picker["boards"] if b["id"] == "risk_adjusted")["isDefault"])

    def test_paging_never_changes_the_counts(self):
        page = self.board(limit=5)
        whole = self.board(limit=200)
        self.assertEqual(page["summary"], whole["summary"], "the counts are over the board, not over the page")
        self.assertEqual(page["page"]["rankedTotal"], whole["page"]["rankedTotal"])
        self.assertTrue(page["page"]["hasMore"])
        self.assertEqual(len(page["rows"]), 5)

    def test_search_filters_rows_without_lying_about_totals(self):
        anon = self.rows_by_rank()[1]["anon"]
        found = self.get("/v1/leaderboard", q=anon[:8])
        self.assertTrue(found["page"]["filtered"])
        self.assertEqual(found["summary"]["rankedTotal"], self.board()["summary"]["rankedTotal"])
        self.assertTrue(all(anon[:8] in r["anon"] for r in found["rows"]))
        self.assertEqual(found["summary"]["filteredTotal"], len(found["rows"]))

    def test_an_excluded_wallet_leaves_the_board_and_the_total_still_adds_up(self):
        import seed_leaderboard
        victim = seed_leaderboard.wallet_for(3)
        before = self.board(limit=200)
        anon = __import__("app")._anon(victim)
        self.assertTrue(any(r["anon"] == anon for r in before["rows"]))
        self.app._db.execute("INSERT INTO leaderboard_exclusions (wallet, board, action, reason, actor, at_ms)"
                             " VALUES (?, '', 'exclude', 'test: operator action', 'test', ?)", (victim, 1))
        self.app._db.commit()
        after = self.board(limit=200)
        self.assertFalse(any(r["anon"] == anon for r in after["rows"]), "an exclusion is applied, not annotated")
        self.assertEqual(after["excludedTotal"], 1)
        self.assertIsNone(after["excluded"], "the reasons are an operator surface, not a public wall of shame")
        self.assertEqual(after["summary"]["rankedTotal"], before["summary"]["rankedTotal"] - 1)
        # …and `include` puts them back, because a decision that cannot be reversed is not a decision.
        self.app._db.execute("INSERT INTO leaderboard_exclusions (wallet, board, action, reason, actor, at_ms)"
                             " VALUES (?, '', 'include', 'test: cleared', 'test', ?)", (victim, 2))
        self.app._db.commit()
        back = self.board(limit=200)
        self.assertTrue(any(r["anon"] == anon for r in back["rows"]))
        self.assertEqual(back["excludedTotal"], 0)


class TestThePhaseGate(LeaderboardBase):
    """The kit's acceptance sentence, end to end: show me the pair, then explain it."""

    def test_the_gate(self):
        board = self.board(limit=200)
        rows = {r["rank"]: r for r in board["rows"]}
        low, high = rows[47], rows[12]
        self.assertLess(low["settledMarkets"], high["settledMarkets"])
        why = self.get("/v1/leaderboard/why", a=low["anon"], b=high["anon"])
        self.assertIn("did not decide this", why["why"])
        # …and the board's own summary proves nothing was hidden to get there.
        self.assertGreater(board["summary"]["blewUpCount"], 0)
        self.assertGreater(board["summary"]["unrankedTotal"], 0)
        self.assertEqual(board["summary"]["excludedTotal"], 0)


if __name__ == "__main__":
    unittest.main()
