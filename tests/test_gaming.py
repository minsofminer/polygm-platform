"""P11 · D7's detection engine: four rules, and the innocent near-miss beside each one.

The tests that matter here are not "does the detector fire". They are:

  1. **Every detector fires on a planted farm** — the specimen is written here, in full, so the rule it is meant
     to catch is visible next to the rule's sentence.
  2. **Every detector stays quiet on the near-miss planted next to it.** A detector that flags a real market maker
     and a real copy-trader is a detector whose output a human learns to skip, and a skipped dashboard is worse
     than no dashboard. Every threshold below is tested from both sides.
  3. **A finding is never a verdict and never a leak.** `suggested` is an action word, not an action; the digest a
     chain finding was built from is counted, never printed; and the rule sentence travels with the finding.
  4. **The dashboard is assembled, not recomputed** — counts, the `limit`, and the carried-in human decisions
     (`decided`) are what the screen reads.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from polygm_core import gaming as gm            # noqa: E402

D = gm.detect
DAY = D.DAY_MS


def snapshot(wallet, *, rank, settled, ms, board="risk_adjusted", score=100, drawdown=0):
    return {"wallet": wallet, "board": board, "windowKey": "30d", "rank": rank, "settled": settled,
            "scoreBps": score, "drawdownMicro": drawdown, "computedMs": ms}


def fill(wallet, *, ts, token, side, price=500_000, size=1_000_000, cond="0xcond", market="m1"):
    return {"wallet": wallet, "tsMs": ts, "conditionId": cond, "tokenId": token, "side": side,
            "priceMicro": price, "sizeMicro": size, "notionalMicro": price * size // 1_000_000,
            "winner": None, "category": "politics", "marketId": market}


def referral(referrer, referee, *, signed, qual, notional=25_000_000, funding="", device="", accrual=100_000):
    return {"referrer": referrer, "referee": referee, "signedUpMs": signed, "qualifyMs": qual,
            "notionalMicro": notional, "fundingDigest": funding, "deviceDigest": device, "ipDigest": "",
            "accrualMicro": accrual}


def attribution(wallet, *, ms, market, notional=10_000_000, observed=200_000, user="u1"):
    return {"wallet": wallet, "userId": user, "orderId": "o%d" % ms, "marketId": market,
            "notionalMicro": notional, "feeMicroExpected": 300_000, "feeMicroObserved": observed,
            "placedMs": ms, "builderCode": "openout"}


class TestClimbFindings(unittest.TestCase):
    def test_a_fast_climb_on_a_thin_record_is_the_loudest_thing_on_the_screen(self):
        history = []
        # Fourteen ordinary traders: a couple of places each, decent samples. They set the population.
        for i in range(14):
            history += [snapshot("w_ord%02d" % i, rank=200 + i, settled=60, ms=1000),
                        snapshot("w_ord%02d" % i, rank=198 + i, settled=61, ms=1000 + 7 * DAY)]
        # ...and one that went from 900th to 30th with nine settled markets behind it.
        history += [snapshot("w_thin", rank=900, settled=9, ms=1000),
                    snapshot("w_thin", rank=30, settled=9, ms=1000 + 7 * DAY)]
        found = D.climb_findings(history=history, at_ms=1000 + 7 * DAY)
        self.assertEqual([f["wallet"] for f in found], ["w_thin"])
        f = found[0]
        self.assertEqual(f["severity"], 3)
        self.assertEqual((f["fromRank"], f["toRank"]), (900, 30))
        self.assertIn("9 settled market(s) against a median of", " ".join(f["evidence"]))
        self.assertEqual(f["suggested"], "flag")
        self.assertEqual(f["rule"], gm.RULES["fast_climb"])

    def test_an_ordinary_climb_is_silent(self):
        history = []
        for i in range(20):
            history += [snapshot("w_%02d" % i, rank=300 - i, settled=80, ms=1000),
                        snapshot("w_%02d" % i, rank=297 - i, settled=84, ms=1000 + 7 * DAY)]
        self.assertEqual(D.climb_findings(history=history, at_ms=1000 + 7 * DAY), [])

    def test_a_top_percentile_climb_on_a_thick_record_is_worth_a_look_but_not_the_top_of_the_list(self):
        history = []
        for i in range(40):
            history += [snapshot("w_%02d" % i, rank=500, settled=90, ms=1000),
                        snapshot("w_%02d" % i, rank=499, settled=92, ms=1000 + 7 * DAY)]
        history += [snapshot("w_big", rank=800, settled=120, ms=1000),
                    snapshot("w_big", rank=700, settled=121, ms=1000 + 7 * DAY)]
        found = D.climb_findings(history=history, at_ms=1000 + 7 * DAY)
        self.assertEqual([f["wallet"] for f in found], ["w_big"])
        self.assertEqual(found[0]["severity"], 2)
        self.assertIn("peer percentile", " ".join(found[0]["evidence"]))

    def test_snapshots_outside_the_window_do_not_count_as_a_climb(self):
        history = [snapshot("w_old", rank=900, settled=5, ms=0),
                   snapshot("w_old", rank=10, settled=5, ms=1000 + D.CLIMB_WINDOW_MS + 10 * DAY),
                   snapshot("w_old", rank=9, settled=5, ms=2000 + D.CLIMB_WINDOW_MS + 11 * DAY)]
        at = 2000 + D.CLIMB_WINDOW_MS + 11 * DAY
        self.assertEqual(D.climb_findings(history=history, at_ms=at), [])


class TestClusterFindings(unittest.TestCase):
    def _mirrored(self, a, b, *, n=10, offset=5_000, token="tok1", side="buy", start=1_000_000):
        rows_a, rows_b = [], []
        for i in range(n):
            rows_a.append(fill(a, ts=start + i * 60_000, token=token, side=side))
            rows_b.append(fill(b, ts=start + i * 60_000 + offset, token=token, side=side))
        return rows_a + rows_b

    def test_an_overlap_can_never_exceed_one_hundred_percent(self):
        """One fill of the smaller tape can sit near several fills of the larger one.

        The first implementation counted the LARGER wallet's fills and divided by the smaller wallet's count, so a
        busy pair produced an overlap of 100.34% — a ratio above one, printed next to a 60% threshold, which is
        the kind of number that gets the threshold raised instead of the bug fixed.
        """
        # Twelve fills crammed into eleven seconds against nine spread across seventeen: every one of the nine
        # is within the window of at least one of the twelve, and several of the twelve share a partner.
        rows = [fill("w_big", ts=1_000_000 + i * 1_000, token="tok1", side="buy") for i in range(12)]
        rows += [fill("w_small", ts=1_000_000 + i * 2_000 + 500, token="tok1", side="buy") for i in range(9)]
        found = D.cluster_findings(fills=rows, at_ms=2_000_000)
        self.assertEqual(len(found), 1)
        self.assertLessEqual(found[0]["worstOverlapBps"], 10_000)
        self.assertEqual(found[0]["coTimed"], 9, "every one of the smaller wallet's fills has a counterpart")

    def test_two_mirrored_wallets_become_one_cluster(self):
        found = D.cluster_findings(fills=self._mirrored("w_a", "w_b"), at_ms=2_000_000)
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f["wallets"], ["w_a", "w_b"])
        self.assertEqual(f["coTimed"], 10)
        self.assertEqual(f["worstOverlapBps"], 10_000)
        self.assertEqual(f["severity"], 3)
        self.assertEqual(f["suggested"], "flag")

    def test_wallets_trading_the_same_markets_on_opposite_sides_are_not_a_cluster(self):
        rows = [fill("w_a", ts=1_000_000 + i * 60_000, token="tok1", side="buy") for i in range(10)]
        rows += [fill("w_b", ts=1_000_000 + i * 60_000 + 5_000, token="tok1", side="sell") for i in range(10)]
        self.assertEqual(D.cluster_findings(fills=rows, at_ms=2_000_000), [])

    def test_fills_further_apart_than_the_window_are_not_co_timed(self):
        rows = [fill("w_a", ts=1_000_000 + i * 300_000, token="tok1", side="buy") for i in range(10)]
        rows += [fill("w_b", ts=1_000_000 + i * 300_000 + 120_000, token="tok1", side="buy") for i in range(10)]
        self.assertEqual(D.cluster_findings(fills=rows, at_ms=2_000_000), [])

    def test_a_farm_arrives_as_one_cluster_not_as_pairs(self):
        rows = [fill("w_a", ts=1_000_000 + i * 60_000, token="tok1", side="buy") for i in range(10)]
        rows += [fill("w_b", ts=1_000_000 + i * 60_000 + 3_000, token="tok1", side="buy") for i in range(10)]
        # c mirrors only the tail of a's tape: still ten co-timed fills against its own ten.
        rows += [fill("w_c", ts=1_000_000 + i * 60_000 + 7_000, token="tok1", side="buy") for i in range(10)]
        found = D.cluster_findings(fills=rows, at_ms=2_000_000)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["wallets"], ["w_a", "w_b", "w_c"])
        self.assertEqual(found[0]["pairs"], 3)

    def test_a_thin_tape_is_not_evidence(self):
        rows = [fill("w_a", ts=1_000_000 + i * 60_000, token="tok1", side="buy") for i in range(3)]
        rows += [fill("w_b", ts=1_000_000 + i * 60_000 + 2_000, token="tok1", side="buy") for i in range(3)]
        self.assertEqual(D.cluster_findings(fills=rows, at_ms=2_000_000), [])


class TestChainFindings(unittest.TestCase):
    def test_a_tree_whose_referees_share_a_funder_is_the_loudest(self):
        rows = [referral("w_ref", "w_r1", signed=1_000, qual=1_000 + 3_600_000, funding="f_aaa"),
                referral("w_ref", "w_r2", signed=1_000, qual=1_000 + 7_200_000, funding="f_aaa"),
                referral("w_ref", "w_r3", signed=1_000, qual=1_000 + 9_000_000, funding="f_bbb")]
        found = D.chain_findings(referrals=rows, at_ms=10_000_000)
        self.assertEqual([f["referrer"] for f in found], ["w_ref"])
        self.assertEqual(found[0]["severity"], 3)
        self.assertEqual(found[0]["sharedFunding"], 1)
        self.assertIn("funding digest(s) shared", " ".join(found[0]["evidence"]))

    def test_the_digest_value_never_appears_in_a_finding(self):
        rows = [referral("w_ref", "w_r1", signed=1_000, qual=1_000 + 3_600_000, funding="f_supersecret"),
                referral("w_ref", "w_r2", signed=1_000, qual=1_000 + 7_200_000, funding="f_supersecret"),
                referral("w_ref", "w_r3", signed=1_000, qual=1_000 + 9_000_000, funding="f_other")]
        blob = repr(D.chain_findings(referrals=rows, at_ms=10_000_000))
        self.assertNotIn("f_supersecret", blob)
        self.assertNotIn("f_other", blob)

    def test_floor_hits_signed_the_same_day_are_a_finding(self):
        rows = [referral("w_ref", "w_r%d" % i, signed=1_000, qual=1_000 + 3_600_000,
                         notional=25_010_000) for i in range(1, 4)]
        found = D.chain_findings(referrals=rows, at_ms=10_000_000)
        self.assertEqual(found[0]["severity"], 2)
        self.assertEqual(len(found[0]["floorReferees"]), 3)
        self.assertEqual(len(found[0]["fastReferees"]), 3)

    def test_an_honest_referrer_with_two_referees_is_silent(self):
        rows = [referral("w_ref", "w_r1", signed=1_000, qual=1_000 + 40 * 3_600_000, notional=900_000_000),
                referral("w_ref", "w_r2", signed=1_000, qual=1_000 + 90 * 3_600_000, notional=400_000_000)]
        self.assertEqual(D.chain_findings(referrals=rows, at_ms=10_000_000), [])

    def test_a_third_referee_who_is_unremarkable_does_not_make_a_tree(self):
        rows = [referral("w_ref", "w_r1", signed=1_000, qual=1_000 + 40 * 3_600_000, notional=900_000_000),
                referral("w_ref", "w_r2", signed=1_000, qual=1_000 + 90 * 3_600_000, notional=400_000_000),
                referral("w_ref", "w_r3", signed=1_000, qual=1_000 + 120 * 3_600_000, notional=300_000_000)]
        self.assertEqual(D.chain_findings(referrals=rows, at_ms=10_000_000), [])


class TestBuilderFindings(unittest.TestCase):
    def test_a_burst_across_markets_is_a_finding(self):
        rows = [attribution("w_script", ms=1_000_000 + i * 30_000, market="m%d" % i) for i in range(6)]
        found = D.builder_findings(attributions=rows, at_ms=2_000_000)
        self.assertEqual([f["wallet"] for f in found], ["w_script"])
        self.assertIn("markets inside", " ".join(found[0]["evidence"]))
        self.assertEqual(found[0]["burstMarkets"], 6)

    def test_volume_that_never_paid_a_fee_is_a_finding(self):
        rows = [attribution("w_free", ms=1_000_000 + i * 3_600_000, market="m%d" % i,
                            notional=10_000_000, observed=(0 if i < 4 else 200_000)) for i in range(6)]
        found = D.builder_findings(attributions=rows, at_ms=99_000_000)
        self.assertEqual([f["wallet"] for f in found], ["w_free"])
        self.assertEqual(found[0]["unpaidBps"], 6666)
        self.assertEqual(found[0]["severity"], 2)

    def test_an_ordinary_trader_using_our_code_is_silent(self):
        rows = [attribution("w_real", ms=1_000_000 + i * 86_400_000, market="m%d" % i) for i in range(6)]
        self.assertEqual(D.builder_findings(attributions=rows, at_ms=999_000_000), [])

    def test_dust_below_the_floor_is_silent(self):
        rows = [attribution("w_dust", ms=1_000_000 + i * 1_000, market="m%d" % i, notional=1_000_000)
                for i in range(6)]
        self.assertEqual(D.builder_findings(attributions=rows, at_ms=2_000_000), [])


class TestDashboard(unittest.TestCase):
    def test_one_payload_carries_the_four_lists_the_rules_and_the_counts(self):
        history = []
        for i in range(14):
            history += [snapshot("w_ord%02d" % i, rank=200 + i, settled=60, ms=1000),
                        snapshot("w_ord%02d" % i, rank=198 + i, settled=61, ms=1000 + 7 * DAY)]
        history += [snapshot("w_thin", rank=900, settled=9, ms=1000),
                    snapshot("w_thin", rank=30, settled=9, ms=1000 + 7 * DAY)]
        rows = [fill("w_a", ts=1_000_000 + i * 60_000, token="tok1", side="buy") for i in range(10)]
        rows += [fill("w_b", ts=1_000_000 + i * 60_000 + 5_000, token="tok1", side="buy") for i in range(10)]
        refs = [referral("w_ref", "w_r1", signed=1_000, qual=2_000, funding="f_x"),
                referral("w_ref", "w_r2", signed=1_000, qual=3_000, funding="f_x"),
                referral("w_ref", "w_r3", signed=1_000, qual=4_000, funding="f_y")]
        attrs = [attribution("w_script", ms=1_000_000 + i * 30_000, market="m%d" % i) for i in range(6)]
        out = D.dashboard(history=history, fills=rows, referrals=refs, attributions=attrs, at_ms=1000 + 7 * DAY)
        self.assertEqual(sorted(out["rules"]), ["builder_anomaly", "correlated_cluster", "fast_climb",
                                                "synthetic_chain"])
        self.assertTrue(all(set(v) == {"rule", "innocent"} for v in out["rules"].values()))
        self.assertEqual([f["wallet"] for f in out["climbers"]], ["w_thin"])
        self.assertEqual(out["clusters"][0]["wallets"], ["w_a", "w_b"])
        self.assertEqual(out["chains"][0]["referrer"], "w_ref")
        self.assertEqual(out["builder"][0]["wallet"], "w_script")
        self.assertEqual(out["counts"], {"climbers": 1, "clusters": 1, "chains": 1, "builder": 1, "reviewed": 0})

    def test_a_human_decision_travels_with_the_finding_it_answers(self):
        # A population, because the climb rule is a comparison: the specimen alone would be its own median, and a
        # detector with nothing to compare against must stay quiet rather than invent a bar.
        history = []
        for i in range(12):
            history += [snapshot("w_ord%02d" % i, rank=300 + i, settled=60, ms=1000),
                        snapshot("w_ord%02d" % i, rank=298 + i, settled=61, ms=1000 + 7 * DAY)]
        history += [snapshot("w_thin", rank=900, settled=9, ms=1000),
                    snapshot("w_thin", rank=30, settled=9, ms=1000 + 7 * DAY)]
        out = D.dashboard(history=history, fills=[], referrals=[], attributions=[], at_ms=1000 + 7 * DAY,
                          flags={"w_thin": {"action": "flag", "atMs": 5, "actor": "operator"}})
        self.assertEqual(out["climbers"][0]["decided"]["action"], "flag")
        self.assertEqual(out["counts"]["reviewed"], 1)

    def test_the_limit_caps_every_list(self):
        history = []
        # Twenty-six ordinary climbers (a place or two each) set a median of 2, so the four planted climbs are
        # each more than 3x the median: the limit has something to cap.
        for i in range(26):
            history += [snapshot("w_ord%02d" % i, rank=400 + i, settled=70, ms=1000),
                        snapshot("w_ord%02d" % i, rank=398 + i, settled=71, ms=1000 + 7 * DAY)]
        for i, climb in enumerate((400, 380, 360, 340)):
            history += [snapshot("w_fast%d" % i, rank=900, settled=40, ms=1000),
                        snapshot("w_fast%d" % i, rank=900 - climb, settled=41, ms=1000 + 7 * DAY)]
        out = D.dashboard(history=history, fills=[], referrals=[], attributions=[], at_ms=1000 + 7 * DAY, limit=4)
        self.assertEqual(len(out["climbers"]), 4)
        self.assertEqual(out["limit"], 4)

    def test_every_finding_kind_ships_its_rule_and_the_other_reading_of_the_same_shape(self):
        """The product rule is "a visible rule and a disclaimer", and prose assertions on prose are a bad test.

        So the disclaimer is a field: every kind must carry both texts, at a length that can actually explain
        something, and the pair is what the API serves and the panel renders.
        """
        self.assertEqual(sorted(gm.RULES), sorted(gm.INNOCENT))
        for kind in gm.RULES:
            self.assertGreater(len(gm.RULES[kind]), 200, kind)
            self.assertGreater(len(gm.INNOCENT[kind]), 100, kind)
            self.assertNotEqual(gm.RULES[kind], gm.INNOCENT[kind], kind)


if __name__ == "__main__":
    unittest.main()
