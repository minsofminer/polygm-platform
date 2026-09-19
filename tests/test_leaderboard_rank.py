"""P11 · The leaderboard engine, unit-tested on evidence rather than on a database.

What these tests defend, in the order a user meets it:

  * the ordering comes from the score and the stated tie-breaks, and **never** from the sample size on its own —
    including the case a user will report as a bug (a 12-market wallet above a 61-market one) and the case they
    will not notice (the reverse). Both get the same treatment because both are the same rule;
  * every number is an integer: the variance is `isqrt` of an exact integer quotient, and a wallet's ordering
    cannot move because of a float rounding difference;
  * a blown-up wallet keeps its negative score and stays on the board, a provisional wallet is labelled, a farm is
    flagged where it matters, a disputed market is withheld as unknown rather than zeroed;
  * one 100× trade cannot carry a placing, and the row states what share of the PnL that trade is;
  * the methodology the API serves is the same object the engine ranks from.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "packages"), str(ROOT / "services" / "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polygm_core.leaderboard import boards as bd                                  # noqa: E402
from polygm_core.leaderboard import integrity as ig                               # noqa: E402
from polygm_core.leaderboard import rank as lr                                    # noqa: E402

DAY = 86_400_000
AT = 1_789_000_000_000


def fill(wallet: str, condition: str, side: str, price_micro: int, notional_micro: int, ts_ms: int) -> dict:
    return {"wallet": wallet, "conditionId": condition, "side": side, "priceMicro": price_micro,
            "notionalMicro": notional_micro, "tsMs": ts_ms}


def settled(condition: str, realised_micro: int, *, at_ms: int, category: str = "Politics",
            disputed: bool = False) -> dict:
    return {"conditionId": condition, "realisedMicro": realised_micro, "atMs": at_ms, "category": category,
            "disputed": disputed}


def wallet(handle: str, *, results: list[int], category: str = "Politics", age_days: int = 90,
           turnover_micro: int = 2_000_000_000, copiers: int = 0, fills: list[dict] | None = None,
           at_ms: int = AT, categories: list[str] | None = None) -> dict:
    """A wallet whose settled results start `len(results)` days before `at_ms`, oldest first."""
    rows = []
    for i, r in enumerate(results):
        cat = (categories or [])[i] if categories and i < len(categories) else category
        rows.append(settled("%s-%s-%d" % (handle, cat, i), r, at_ms=at_ms - (len(results) - i) * DAY, category=cat))
    return {"wallet": handle, "firstSeenMs": at_ms - age_days * DAY, "atMs": at_ms, "copiers": copiers,
            "settled": rows, "fills": fills if fills is not None else
            [fill(handle, "m-%d" % i, "BUY", 500_000, turnover_micro // max(1, len(results)), AT - 1_000 * i)
             for i in range(len(results))],
            "verifiedVolumeMicro": turnover_micro}


class TheOrderIsTheScoreTest(unittest.TestCase):
    def test_sample_size_does_not_decide_the_order_in_either_direction(self):
        """The gate's own question: a smaller sample above a larger one, and below it, explained the same way."""
        # 24 against 61, both above the gate: "fewer resolved markets" is a comparison between eligible wallets,
        # and the gate is a separate rule with its own test below.
        small = wallet("w_small", results=[2_000_000] * 24, turnover_micro=2_000_000_000)
        big = wallet("w_big", results=[100_000] * 61, turnover_micro=2_000_000_000)
        out = lr.rank_board(board_id="risk_adjusted", wallets=[small, big], at_ms=AT)
        rows = {r["wallet"]: r for r in out["rows"]}
        self.assertEqual(2, out["rankedTotal"])
        self.assertLess(rows["w_small"]["rank"], rows["w_big"]["rank"],
                        "12 settled markets with a strong record outrank 61 with a weak one")
        why = lr.explain(board_id="risk_adjusted", wallets=[small, big], at_ms=AT, a="w_small", b="w_big")
        self.assertTrue(why["ok"])
        self.assertIn("sample size did not decide this", why["why"])
        self.assertIn("24", why["why"])
        self.assertIn("61", why["why"])
        # And the same pair reversed by the data (more markets, better record) is ordered by the same rule.
        tiny = wallet("w_tiny", results=[50_000] * 24, turnover_micro=2_000_000_000)
        out2 = lr.rank_board(board_id="risk_adjusted", wallets=[tiny, big], at_ms=AT)
        rows2 = {r["wallet"]: r for r in out2["rows"]}
        self.assertLess(rows2["w_big"]["rank"], rows2["w_tiny"]["rank"])

    def test_ties_fall_through_the_stated_tie_breaks_in_order(self):
        a = wallet("w_a", results=[1_000_000] * 20)
        b = wallet("w_b", results=[1_000_000] * 30)
        out = lr.rank_board(board_id="risk_adjusted", wallets=[a, b], at_ms=AT)
        self.assertEqual(["w_b", "w_a"], [r["wallet"] for r in out["rows"]],
                         "equal scores fall to the larger sample, then drawdown, then the wallet id")
        c = wallet("w_c", results=[1_000_000] * 20)
        d = wallet("w_c2", results=[1_000_000] * 20)
        out2 = lr.rank_board(board_id="risk_adjusted", wallets=[d, c], at_ms=AT)
        self.assertEqual(["w_c", "w_c2"], [r["wallet"] for r in out2["rows"]],
                         "the last tie-break is the wallet id, so the same data always produces the same board")

    def test_the_score_is_integer_arithmetic_all_the_way_down(self):
        w = wallet("w", results=[1_234_567, -234_567, 987_654, -12_345] + [100] * 18)
        out = lr.rank_board(board_id="risk_adjusted", wallets=[w], at_ms=AT)
        row = out["rows"][0]
        for key in ("scoreBps", "volatilityMicro", "maxDrawdownMicro", "trimmedMicro", "verifiedVolumeMicro"):
            self.assertIsInstance(row[key], int, key)
        # The variance is an exact integer quotient and sigma is its integer square root, so ordering two
        # wallets can never turn on a float rounding difference.
        xs = [1_234_567, -234_567, 987_654, -12_345] + [100] * 18
        n = len(xs)
        var = (n * sum(x * x for x in xs) - sum(xs) ** 2) // (n * n)
        sigma = lr.volatility_micro(xs)
        self.assertIsInstance(sigma, int)
        self.assertLessEqual(sigma * sigma, var)
        self.assertGreater((sigma + 1) * (sigma + 1), var)


class TheIntegrityRulesTest(unittest.TestCase):
    def test_a_round_trip_is_subtracted_and_the_row_says_how_much(self):
        w = wallet("w_wash", results=[10_000] * 20, turnover_micro=0, fills=[
            fill("w_wash", "0xM", "BUY", 500_000, 1_000_000_000, AT - 600_000),
            fill("w_wash", "0xM", "SELL", 501_000, 1_000_000_000, AT - 300_000),      # back out in 5 min
            fill("w_wash", "0xM2", "BUY", 500_000, 250_000_000, AT - 200_000),
        ])
        out = lr.rank_board(board_id="volume", wallets=[w], at_ms=AT)
        row = out["rows"][0]
        self.assertEqual(1_000_000_000, row["washedMicro"])
        # Two of the three fills cancel; the unrelated third keeps its own notional.
        self.assertEqual(1_250_000_000, row["verifiedVolumeMicro"])
        self.assertIn("round-tripped volume", row["washNote"])

    def test_a_scalp_that_took_a_move_is_not_a_round_trip(self):
        """The tolerance is what separates a wash from a scalp; a rule that flagged both would be unusable."""
        fills = [fill("w", "0xM", "BUY", 400_000, 100_000_000, AT - 600_000),
                 fill("w", "0xM", "SELL", 460_000, 100_000_000, AT - 300_000)]           # +15%: a real trade
        self.assertEqual(0, ig.wash_volume(fills)["washedMicro"])
        self.assertEqual(200_000_000, ig.wash_volume(fills)["verifiedMicro"])

    def test_a_blown_up_wallet_keeps_its_negative_score_and_stays_on_the_board(self):
        w = wallet("w_dead", results=[2_000_000, -5_000_000, 1_000_000, -4_000_000, -1_000_000] + [-100_000] * 15)
        self.assertLess(sum(w["settled"][i]["realisedMicro"] for i in range(len(w["settled"]))), 0)
        out = lr.rank_board(board_id="risk_adjusted", wallets=[w, wallet("w_alive", results=[500_000] * 20)],
                            at_ms=AT)
        dead = next(r for r in out["rows"] if r["wallet"] == "w_dead")
        self.assertEqual("blew_up", dead["state"])
        self.assertEqual(1, out["blewUpCount"])
        self.assertIn("blew up", " ".join(dead["labels"]))
        self.assertIsInstance(dead["scoreBps"], int)

    def test_the_lucky_gambler_is_trimmed_and_the_share_is_published(self):
        """One 100x bet must not carry a placing, and the row has to say what that bet was worth."""
        lucky = wallet("w_lucky", results=[0] * 19 + [1_000_000_000])
        steady = wallet("w_steady", results=[60_000] * 20)
        out = lr.rank_board(board_id="risk_adjusted", wallets=[lucky, steady], at_ms=AT)
        rows = {r["wallet"]: r for r in out["rows"]}
        self.assertLess(rows["w_steady"]["rank"], rows["w_lucky"]["rank"])
        self.assertGreater(rows["w_lucky"]["bestTradeShareBps"], bd.LUCKY_TRADE_SHARE_BPS)
        self.assertIn("one trade", " ".join(rows["w_lucky"]["labels"]))
        self.assertGreater(rows["w_lucky"]["components"]["trimRemovedMicro"], 0)
        self.assertIn("EXCLUDING the single best market", bd.board("risk_adjusted")["formula"])

    def test_a_provisional_wallet_is_labelled_and_kept(self):
        fresh = wallet("w_new", results=[300_000] * 20, age_days=3)
        out = lr.rank_board(board_id="risk_adjusted", wallets=[fresh], at_ms=AT)
        row = out["rows"][0]
        self.assertEqual(1, out["provisionalCount"])
        self.assertIn("3 of 7 days", " ".join(row["labels"]))

    def test_a_copy_farm_cannot_rank_as_copied_demand(self):
        own = [fill("w_source", "0xM%d" % i, "BUY", 500_000, 100_000_000, AT - 20 * 60_000 - i * 1_000)
               for i in range(12)]
        base = wallet("w_source", results=[400_000] * 20, fills=own)
        # Five seconds behind, same markets, same side, on every fill: a copy, not a coincidence.
        farm_fills = [fill("w_farm", "0xM%d" % i, "BUY", 500_000, 100_000_000,
                           AT - 20 * 60_000 - i * 1_000 + 5_000) for i in range(12)]
        farm = wallet("w_farm", results=[1_000] * 20, copiers=4, fills=farm_fills)
        out = lr.rank_board(board_id="copied", wallets=[base, farm], at_ms=AT)
        farmed = [u for u in out["unranked"] if u["wallet"] == "w_farm"]
        self.assertTrue(farmed, "a farm must not rank on the copied board")
        self.assertIn("derived from", " ".join(farmed[0]["reasons"]))
        self.assertEqual("w_source", ig.copy_farm(wallet="w_farm", own=farm_fills,
                                                  candidates={"w_source": own[:12]})["derivedFrom"])

    def test_a_disputed_market_is_withheld_as_unknown_and_counted(self):
        w = wallet("w_dispute", results=[800_000] * 21)
        w["blockedConditions"] = {"w_dispute-Politics-20"}
        out = lr.rank_board(board_id="risk_adjusted", wallets=[w], at_ms=AT)
        row = out["rows"][0]
        # Withheld, not zeroed — and the row says how many were withheld, so a user reading "20 settled" against
        # their own count of 21 has the explanation on the row.
        self.assertEqual(1, row["disputedExcluded"])
        self.assertEqual(20, row["settledMarkets"])


class TheBoardsAndTheMethodologyTest(unittest.TestCase):
    def test_the_gates_refuse_with_the_number_that_refused_them(self):
        thin = wallet("w_thin", results=[900_000] * 3)
        poor = wallet("w_poor", results=[900_000] * 25, turnover_micro=10_000_000)
        out = lr.rank_board(board_id="risk_adjusted", wallets=[thin, poor], at_ms=AT)
        self.assertEqual(0, out["rankedTotal"])
        reasons = {u["wallet"]: " ".join(u["reasons"]) for u in out["unranked"]}
        self.assertIn("3 settled markets", reasons["w_thin"])
        self.assertIn("20", reasons["w_thin"])
        self.assertIn("turnover", reasons["w_poor"])

    def test_the_category_board_admits_specialists_only(self):
        specialist = wallet("w_pol", results=[500_000] * 20, category="Politics",
                            categories=["Politics"] * 20)
        # Enough Politics markets to clear the count, not enough share to be a specialist: 20 of 45 = 44%.
        generalist = wallet("w_mix", results=[500_000] * 45,
                            categories=["Politics"] * 20 + ["Sports"] * 25)
        out = lr.rank_board(board_id="category", wallets=[specialist, generalist], at_ms=AT, category="Politics")
        self.assertEqual(["w_pol"], [r["wallet"] for r in out["rows"]])
        held = [u for u in out["unranked"] if u["wallet"] == "w_mix"]
        self.assertIn("44% of this wallet's resolved markets are in Politics", " ".join(held[0]["reasons"]))

    def test_the_rising_board_measures_improvement_between_two_weeks(self):
        improving = wallet("w_up", results=[100_000] * 10 + [900_000] * 10)
        fading = wallet("w_down", results=[900_000] * 10 + [100_000] * 10)
        out = lr.rank_board(board_id="rising", wallets=[improving, fading], at_ms=AT)
        rows = {r["wallet"]: r for r in out["rows"]}
        self.assertLess(rows["w_up"]["rank"], rows["w_down"]["rank"])
        self.assertEqual(rows["w_up"]["weekMicro"] - rows["w_up"]["priorWeekMicro"],
                         rows["w_up"]["improvementMicro"])

    def test_the_methodology_is_the_engine_s_own_object(self):
        doc = bd.methodology()
        self.assertEqual(["risk_adjusted", "win_rate", "volume", "rising", "category", "copied"],
                         [b["id"] for b in doc["boards"]])
        self.assertEqual(bd.MIN_RESOLVED, doc["sampleGate"])
        for b in doc["boards"]:
            for field in ("formula", "gate", "tieBreaks", "cadence", "rewards", "punishes", "note"):
                self.assertTrue(str(b.get(field) or "").strip(), "%s has no %s" % (b["id"], field))
        ids = [r["id"] for r in doc["integrity"]]
        self.assertEqual(["wash", "copy_farm", "provisional", "blown_up", "lucky", "disputed"], ids)

    def test_the_win_rate_board_is_ordered_by_win_rate(self):
        """A board named after a field must be ordered by that field.

        These two wallets are built so the two orders disagree: the accurate one wins 85% of its (just-large
        enough) sample with small, flat results, and the other wins 55% with a much better risk-adjusted score.
        A win-rate board that serves the risk-adjusted order is a heading that lies about its own contents.
        """
        accurate = wallet("w_accurate", results=[600_000] * 35 + [-1_500_000] * 3 + [400_000] * 12)
        # 55% wins, but the wins are large and the losses are small: a strong risk-adjusted record.
        spiky = wallet("w_spiky", results=[4_000_000] * 25 + [-300_000] * 20)
        out = lr.rank_board(board_id="win_rate", wallets=[accurate, spiky], at_ms=AT, window="90d")
        rows = {r["wallet"]: r for r in out["rows"]}
        self.assertGreater(rows["w_accurate"]["winRateBps"], rows["w_spiky"]["winRateBps"])
        self.assertEqual(rows["w_accurate"]["rank"], 1, "the board is ordered by its own formula")
        default = lr.rank_board(board_id="risk_adjusted", wallets=[accurate, spiky], at_ms=AT, window="90d")
        self.assertEqual(default["rows"][0]["wallet"], "w_spiky",
                         "and the default board, which ranks risk-adjusted return, still prefers the other one")

    def test_the_win_rate_board_serves_the_rate_of_the_sample_printed_beside_it(self):
        out = lr.rank_board(board_id="win_rate", wallets=[wallet("w_a", results=[600_000] * 30 + [-900_000] * 6)],
                            at_ms=AT, window="90d")
        row = out["rows"][0]
        self.assertEqual(row["settledMarkets"], 36)
        self.assertEqual(row["wins"], 30)
        self.assertEqual(row["winRateBps"], 30 * 10_000 // 36)
        self.assertFalse(row["insufficientSample"])

    def test_a_category_row_is_sampled_on_the_categorys_own_results(self):
        """The sample printed beside a win rate is the sample that rate was taken from."""
        mix = wallet("w_mix", results=[500_000] * 21 + [-400_000] * 4, categories=["Politics"] * 21 + ["Sports"] * 4)
        out = lr.rank_board(board_id="category", wallets=[mix], at_ms=AT, category="Politics")
        row = out["rows"][0]
        self.assertEqual(row["settledMarkets"], 21, "21 of this wallet's markets are Politics")
        self.assertEqual(row["wins"], 21)
        self.assertEqual(row["winRateBps"], 10_000)
        self.assertEqual(row["categorySettled"], 21, "and the share is published alongside it")

    def test_the_default_board_is_the_risk_adjusted_one_and_says_so(self):
        default = next(b for b in bd.BOARDS if b["isDefault"])
        self.assertEqual("risk_adjusted", default["id"])
        self.assertEqual(bd.DEFAULT_BOARD, default["id"])
        self.assertIn("drawdown", default["formula"])
        self.assertIn("2 x", default["formula"])


class TestBestTradeShareIsAShare(unittest.TestCase):
    def test_the_share_is_clamped_when_the_rest_of_the_book_is_red(self):
        """A wallet can have a best market LARGER than its realised total, and "140% of the PnL" is not a share
        of anything. The row says 100% — one trade is all of it and then some — which is also the value the
        schema's own CHECK allows, so the clamp lives in the engine rather than being discovered by an INSERT."""
        state = ig.wallet_state(first_seen_ms=AT - 30 * DAY, at_ms=AT,
                                curve=[{"cumMicro": 1}, {"cumMicro": -9}], best_micro=1_900, realised_micro=100)
        self.assertEqual(state["bestTradeShareBps"], 10_000)
        self.assertTrue(state["luckyGambler"])
        self.assertIn("100%", " ".join(state["labels"]))

    def test_no_share_when_there_is_no_profit_to_share(self):
        flat = ig.wallet_state(first_seen_ms=AT - 30 * DAY, at_ms=AT, curve=[{"cumMicro": -5}],
                               best_micro=600, realised_micro=-5)
        self.assertEqual(flat["bestTradeShareBps"], 0)
        self.assertFalse(flat["luckyGambler"])


if __name__ == "__main__":
    unittest.main()
