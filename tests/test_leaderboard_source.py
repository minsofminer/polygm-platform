"""P11 · D2's read path: which rows a board is ranked from, and the arithmetic that turns fills into results.

The engine's own tests (`test_leaderboard_rank.py`) pin the ordering rules. These pin the *evidence* rules, which
are the other half of the same promise: a board is only as honest as the rows it was allowed to see. Four things
are asserted here, each because a plausible implementation gets it wrong:

1. one result per settled MARKET, not per fill (busy is not better);
2. `realised_micro` mirrors the dossier's arithmetic on both sides and both outcomes;
3. the volume board reads the window's fills while every other board reads lifetime fills — the eligibility
   floor is written in lifetime turnover, and a windowed read would make the same wallet eligible or not
   depending on which tab a user opened;
4. the rising board reads fourteen days, because its metric subtracts two weeks.
"""
from __future__ import annotations

import unittest

from polygm_core.leaderboard import boards as bd
from polygm_core.leaderboard import rank, source

DAY = source.DAY_MS
AT = 1_700_000_000_000
MICRO = 10 ** 6


def fill(wallet, *, market, side="BUY", price_micro=400_000, shares=1_000, at=AT - 10 * DAY,
         winner=1, category="Politics", token=None):
    return {"wallet": wallet, "conditionId": market, "tokenId": token or (market + "Y"), "side": side,
            "priceMicro": price_micro, "sizeMicro": shares * MICRO,
            "notionalMicro": price_micro * shares, "tsMs": at, "category": category,
            "winner": (None if winner is None else winner), "outcome": "Yes"}


def wallet_record(wallet, markets, *, wins=1, at=AT - 10 * DAY, category="Politics", price_micro=400_000,
                  shares=1_000):
    """`markets` settled results for one wallet: the first `wins` of them winning.

    Bought at 0.40 and held: a win is +$600 on 1,000 shares, a loss is −$400, both exactly. Volumes are large
    enough (2,000 shares a market) that twenty markets clear the $500 turnover floor by a wide margin.
    """
    out = []
    for i, mid in enumerate(markets):
        out.append(fill(wallet, market=mid, price_micro=price_micro, shares=shares, at=at - i * 3_600_000,
                        winner=1 if i < wins else 0, category=category))
    return out


class TestWindows(unittest.TestCase):
    def test_window_starts_are_milliseconds_and_all_is_zero(self):
        self.assertEqual(source.window_start_ms("7d", AT), AT - 7 * DAY)
        self.assertEqual(source.window_start_ms("30d", AT), AT - 30 * DAY)
        self.assertEqual(source.window_start_ms("all", AT), 0)
        with self.assertRaises(ValueError):
            source.window_start_ms("12h", AT)

    def test_the_volume_board_reads_the_window_and_the_others_read_lifetime(self):
        vol = source.read_plan(board_id="volume", window="24h", at_ms=AT)
        self.assertEqual(vol["fillsFromMs"], AT - DAY)
        self.assertEqual(vol["settledFromMs"], AT - DAY)
        sk = source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT)
        self.assertEqual(sk["fillsFromMs"], 0, "the turnover floor is a LIFETIME floor")
        self.assertEqual(sk["settledFromMs"], AT - 30 * DAY)
        self.assertIn("LIFETIME", sk["fillsRule"])

    def test_the_rising_board_reads_fourteen_days_of_results(self):
        plan = source.read_plan(board_id="rising", window="7d", at_ms=AT)
        self.assertEqual(plan["settledFromMs"], AT - 14 * DAY)
        self.assertEqual(plan["fillsFromMs"], 0)
        self.assertIn("7 days before", plan["settledRule"])

    def test_a_window_a_board_does_not_read_is_refused_not_snapped(self):
        with self.assertRaises(ValueError):
            source.read_plan(board_id="rising", window="30d", at_ms=AT)
        with self.assertRaises(ValueError):
            source.read_plan(board_id="risk_adjusted", window="24h", at_ms=AT)
        with self.assertRaises(ValueError):
            source.read_plan(board_id="nope", window="30d", at_ms=AT)


class TestRealised(unittest.TestCase):
    def test_buy_and_sell_both_outcomes(self):
        r = source.realised_micro
        # 1,000 shares at 0.40: cost 400. A winning buy pays a dollar a share.
        self.assertEqual(r(side="BUY", size_micro=1_000 * MICRO, notional_micro=400 * MICRO, winner=1,
                           resolved=True), 600 * MICRO)
        self.assertEqual(r(side="BUY", size_micro=1_000 * MICRO, notional_micro=400 * MICRO, winner=0,
                           resolved=True), -400 * MICRO)
        # The mirror: a sell of the winning token gives up the dollar but keeps what it was paid.
        self.assertEqual(r(side="SELL", size_micro=1_000 * MICRO, notional_micro=400 * MICRO, winner=1,
                           resolved=True), -600 * MICRO)
        self.assertEqual(r(side="SELL", size_micro=1_000 * MICRO, notional_micro=400 * MICRO, winner=0,
                           resolved=True), 400 * MICRO)

    def test_an_unresolved_market_realises_nothing_and_that_is_not_a_zero(self):
        self.assertEqual(source.realised_micro(side="BUY", size_micro=1_000 * MICRO, notional_micro=400 * MICRO,
                                               winner=None, resolved=False), 0)
        # …and the marker itself is what refuses it: a None winner never becomes a result.
        self.assertEqual(source.settled_results([fill("0xa", market="0xM1", winner=None)]), [])


class TestSettledResults(unittest.TestCase):
    def test_forty_fills_in_one_market_are_one_result(self):
        rows = [fill("0xa", market="0xM1", at=AT - DAY + i * 1_000) for i in range(40)]
        out = source.settled_results(rows)
        self.assertEqual(len(out), 1, "per settled MARKET, not per fill")
        self.assertEqual(out[0]["fills"], 40)
        self.assertEqual(out[0]["realisedMicro"], 40 * 600 * MICRO)

    def test_the_window_filters_results_and_the_at_is_the_last_fill(self):
        rows = wallet_record("0xa", ["0xM%d" % i for i in range(4)], at=AT - 10 * DAY)
        rows += wallet_record("0xa", ["0xN1"], at=AT - DAY)
        inside = source.settled_results(rows, since_ms=AT - 7 * DAY)
        self.assertEqual([r["conditionId"] for r in inside], ["0xN1"])
        self.assertEqual(inside[0]["atMs"], AT - DAY)
        self.assertEqual(len(source.settled_results(rows)), 5)

    def test_a_result_carries_its_category_from_any_fill_in_the_market(self):
        rows = [fill("0xa", market="0xM1", category="", at=AT - DAY),
                fill("0xa", market="0xM1", category="Sports", at=AT - DAY + 60_000)]
        self.assertEqual(source.settled_results(rows)[0]["category"], "Sports")


class TestEvidence(unittest.TestCase):
    def test_the_volume_board_sees_the_window_and_the_skill_boards_see_lifetime(self):
        fills = []
        for w in ("0xaa", "0xbb"):
            fills += wallet_record(w, ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY)
        # 20 days ago: outside a 7-day window, inside a 30-day one, and part of lifetime turnover.
        fills += wallet_record("0xaa", ["0xOLD"], at=AT - 20 * DAY)
        latest = source.evidence(fills=fills, at_ms=AT,
                                 plan=source.read_plan(board_id="volume", window="7d", at_ms=AT))
        got = {ev["wallet"]: ev for ev in latest}
        self.assertEqual(len(got["0xaa"]["settled"]), 21, "the 20-day-old result is outside a 7-day window")
        self.assertEqual(len(got["0xaa"]["fills"]), 21)
        allof = source.evidence(fills=fills, at_ms=AT,
                                plan=source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT))
        again = {ev["wallet"]: ev for ev in allof}
        self.assertEqual(len(again["0xaa"]["settled"]), 22)
        self.assertEqual(len(again["0xaa"]["fills"]), 22, "lifetime fills, because the floor is lifetime")

    def test_turnover_is_wash_adjusted_inside_the_window_the_board_reads(self):
        # A round trip inside the window: bought and sold at the same price two minutes apart. It is volume the
        # venue charged for and nobody risked, and the volume board must not count it.
        rows = wallet_record("0xaa", ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY)
        rows += [fill("0xaa", market="0xWASH", shares=5_000, at=AT - 2 * DAY),
                 fill("0xaa", market="0xWASH", side="SELL", shares=5_000, at=AT - 2 * DAY + 120_000)]
        ev = {e["wallet"]: e for e in source.evidence(
            fills=rows, at_ms=AT, plan=source.read_plan(board_id="volume", window="7d", at_ms=AT))}["0xaa"]
        vol = __import__("polygm_core.leaderboard.integrity", fromlist=["x"]).wash_volume(ev["fills"])
        self.assertEqual(vol["roundTrips"], 1)
        self.assertEqual(vol["washedMicro"], 5_000 * 400_000)

    def test_a_provisional_wallet_is_dated_by_whichever_comes_first(self):
        rows = wallet_record("0xa", ["0xM%d" % i for i in range(21)], at=AT - 2 * DAY)
        ev = {e["wallet"]: e for e in source.evidence(
            fills=rows, at_ms=AT, plan=source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT),
            created_ms={"0xa": AT - 10 * DAY})}["0xa"]
        self.assertEqual(ev["firstSeenMs"], AT - 10 * DAY, "the wallet row's creation time is earlier")

    def test_copiers_are_counted_from_enabled_configs_only(self):
        rows = wallet_record("0xaa", ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY)
        cfgs = [{"source": "0xaa", "enabled": 1}, {"source": "0xaa", "enabled": 1},
                {"source": "0xaa", "enabled": 0}, {"source": "0xbb", "enabled": 1}]
        ev = {e["wallet"]: e for e in source.evidence(
            fills=rows, at_ms=AT, plan=source.read_plan(board_id="copied", window="30d", at_ms=AT),
            copy_configs=cfgs)}["0xaa"]
        self.assertEqual(ev["copiers"], 2, "a disabled config is not a copier")

    def test_disputed_markets_travel_as_blocked_conditions_and_are_withheld_not_zeroed(self):
        rows = wallet_record("0xaa", ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY)
        board = rank.rank_board(board_id="risk_adjusted", wallets=source.evidence(
            fills=rows, at_ms=AT, plan=source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT),
            blocked_conditions={"0xM0"}), at_ms=AT, window="30d")
        row = board["rows"][0]
        self.assertEqual(row["settledMarkets"], 20, "the disputed result is withheld from the ranking")
        self.assertEqual(row["disputedExcluded"], 1, "…and counted, because unknown is not zero")

    def test_the_same_input_produces_the_same_evidence(self):
        rows = wallet_record("0xaa", ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY)
        plan = source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT)
        a = source.evidence(fills=rows, at_ms=AT, plan=plan)
        b = source.evidence(fills=list(reversed(rows)), at_ms=AT, plan=plan)
        self.assertEqual(a, b, "evidence must not depend on the order the tape was read in")

    def test_the_turnover_floor_refuses_a_wallet_that_is_otherwise_eligible(self):
        rows = wallet_record("0xa", ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY,
                             price_micro=400_000, shares=1)     # $0.40 of turnover a market
        board = rank.rank_board(board_id="risk_adjusted", wallets=source.evidence(
            fills=rows, at_ms=AT, plan=source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT)),
            at_ms=AT, window="30d")
        self.assertEqual(board["rows"], [])
        self.assertEqual(len(board["unranked"]), 1)
        self.assertIn("below the %d micro floor" % bd.MIN_VERIFIED_VOLUME_MICRO,
                      board["unranked"][0]["reasons"][0])

    def test_summarise_counts_the_whole_board_and_names_the_blow_ups(self):
        # One wallet that was up and is now under water, one provisional wallet, one disputed result.
        rows = wallet_record("0xUP", ["0xM%d" % i for i in range(20)], wins=20, at=AT - 12 * DAY)
        # The blow-up has to actually blow up: three deep losses that take the wallet under water after it was
        # well above it. A fixture where the "blew up" wallet is still up tests nothing but the label's absence.
        rows += wallet_record("0xUP", ["0xL%d" % i for i in range(3)], wins=0, at=AT - 2 * DAY,
                              price_micro=900_000, shares=10_000)
        fresh = wallet_record("0xNEW", ["0xN%d" % i for i in range(21)], at=AT - 2 * DAY)
        board = rank.rank_board(board_id="risk_adjusted", wallets=source.evidence(
            fills=rows + fresh, at_ms=AT,
            plan=source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT),
            blocked_conditions={"0xM0"}), at_ms=AT, window="30d", limit=1)
        self.assertEqual(len(board["rows"]), 1, "the page is one row")
        full = rank.rank_board(board_id="risk_adjusted", wallets=source.evidence(
            fills=rows + fresh, at_ms=AT,
            plan=source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT),
            blocked_conditions={"0xM0"}), at_ms=AT, window="30d", limit=1_000)
        s = source.summarise(full, excluded=1)
        self.assertEqual(s["rankedTotal"], 2)
        self.assertEqual(s["blewUpCount"], 1, "a wallet that was up and is now down is counted, not dropped")
        self.assertEqual(s["provisionalCount"], 1)
        self.assertEqual(s["disputedWithheld"], 1)
        self.assertEqual(s["excludedTotal"], 1)
        self.assertGreater(s["medianSettledMarkets"], 0)


class TestRankWindowPassThrough(unittest.TestCase):
    def test_a_row_states_the_window_its_turnover_is_from(self):
        rows = wallet_record("0xa", ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY)
        ev = source.evidence(fills=rows, at_ms=AT,
                             plan=source.read_plan(board_id="volume", window="7d", at_ms=AT))
        board = rank.rank_board(board_id="volume", wallets=ev, at_ms=AT, window="7d")
        self.assertEqual(board["window"], "7d")
        self.assertEqual(board["rows"][0]["volumeWindow"], "7d")
        self.assertEqual(board["rows"][0]["verifiedVolumeMicro"], 21 * 1_000 * MICRO * 400_000 // MICRO)
        ev2 = source.evidence(fills=rows, at_ms=AT,
                              plan=source.read_plan(board_id="risk_adjusted", window="30d", at_ms=AT))
        board2 = rank.rank_board(board_id="risk_adjusted", wallets=ev2, at_ms=AT, window="30d")
        self.assertEqual(board2["rows"][0]["volumeWindow"], "lifetime",
                         "the skill boards' floor is lifetime, and the row says so")

    def test_an_impossible_window_is_refused_by_the_engine_too(self):
        rows = wallet_record("0xa", ["0xM%d" % i for i in range(21)], at=AT - 3 * DAY)
        with self.assertRaises(ValueError):
            rank.rank_board(board_id="volume", wallets=source.evidence(
                fills=rows, at_ms=AT, plan=source.read_plan(board_id="volume", window="7d", at_ms=AT)),
                at_ms=AT, window="90d")

    def test_the_rising_row_counts_settled_markets_in_the_seven_day_window(self):
        rows = wallet_record("0xR", ["0xA%d" % i for i in range(6)], wins=6, at=AT - 10 * DAY)
        rows += wallet_record("0xR", ["0xB%d" % i for i in range(5)], wins=5, at=AT - 2 * DAY)
        board = rank.rank_board(board_id="rising", wallets=source.evidence(
            fills=rows, at_ms=AT, plan=source.read_plan(board_id="rising", window="7d", at_ms=AT)),
            at_ms=AT, window="7d")
        row = board["rows"][0]
        self.assertEqual(row["settledMarkets"], 5, "the row shows the window's results, not the 14-day read")
        self.assertEqual(row["priorWeekMicro"], 6 * 600 * MICRO)
        self.assertEqual(row["weekMicro"], 5 * 600 * MICRO)


if __name__ == "__main__":
    unittest.main()
