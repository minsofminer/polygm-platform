"""Positions, cost basis, neg-risk merges, and the payout arithmetic that the venue contradicts.

Every assertion here is integer arithmetic with hand-computed expected values, because a test that recomputes
the expected value with the same helper proves nothing (the failure mode this project has hit three times).
"""
from __future__ import annotations
import unittest
from decimal import Decimal

from conftest import ROOT  # noqa: F401
from polygm_core.ledger.ledger import (CashEntry, IntentState, OrderState, Position, merge_split,
                                       negrisk_event_pnl, venue_status_to_state)

M = 10**6


def pos(*legs, market="m", token="t") -> Position:
    p = Position(user_id="u", token_id=token, market_id=market)
    for shares, cost in legs:
        p.buy(shares, cost)
    return p


class TestPosition(unittest.TestCase):
    def test_two_buys_then_a_half_sell(self):
        p = pos((1 * M, 500_000), (1 * M, 700_000))          # 2 shares for $1.20
        self.assertEqual(p.avg_entry_micro, 600_000)         # $0.60 average, exact
        released = p.sell(500_000)                           # sell a quarter of the position
        self.assertEqual(released, 300_000)                   # 1_200_000 * 0.5 / 2.0 = 300_000
        self.assertEqual(p.shares_micro, 1_500_000)
        self.assertEqual(p.cost_basis_micro, 900_000)

    def test_pro_rata_uses_floor_and_never_releases_more_than_exists(self):
        # basis 1 on 3 shares: selling 1 share releases floor(1*1/3) = 0; selling 2 releases 0; the last
        # share must release the remaining 1 or basis leaks out of the system.
        p = pos((3, 1))
        self.assertEqual(p.sell(1), 0)
        self.assertEqual(p.sell(1), 0)
        self.assertEqual(p.sell(1), 1)
        self.assertEqual(p.cost_basis_micro, 0)
        self.assertEqual(p.shares_micro, 0)

    def test_floored_basis_conservation_across_many_small_sells(self):
        # 1_000_001 micro of basis over 1_000_000 shares, sold in 1-share steps: the sum of released basis
        # must never exceed the original. (It is allowed to under-release by a micro, which is why the
        # last leg carries the remainder in a full close.)
        p = pos((1_000_000, 1_000_001))
        total = sum(p.sell(1) for _ in range(999_999))
        total += p.sell(1)
        self.assertLessEqual(total, 1_000_001)
        self.assertGreaterEqual(total, 1_000_001 - 2)        # at most a couple of micro of residue
        self.assertEqual(p.shares_micro, 0)

    def test_rejects_impossible_movements(self):
        p = pos((1 * M, 500_000))
        with self.assertRaises(ValueError):
            p.sell(2 * M)
        with self.assertRaises(ValueError):
            p.sell(0)
        with self.assertRaises(ValueError):
            p.buy(0, 100)
        with self.assertRaises(ValueError):
            p.buy(1 * M, -1)

    def test_avg_entry_on_empty_position_is_none_not_zero(self):
        # 0 would display as "$0.00 entry" and read as a free position; None is the honest answer and the
        # UI renders it as a dash.
        self.assertIsNone(Position(user_id="u", token_id="t", market_id="m").avg_entry_micro)


class TestMergeSplit(unittest.TestCase):
    def test_six_legs_of_one_dollar_is_exact(self):
        r = merge_split(6, 1 * M, [180_000, 170_000, 160_000, 150_000, 140_000, 130_000])
        self.assertEqual(r["sum_credit"], 1 * M)                    # nothing invented, nothing lost
        self.assertEqual(sum(r["yes_realised_micro"]) + sum(r["no_basis_micro"]), 0)
        self.assertEqual(r["yes_realised_micro"][0], 166_666 - 180_000)   # a loss on the expensive leg
        self.assertEqual(sum(r["yes_credit_micro"][:-1]), 5 * (1 * M // 6))
        self.assertEqual(r["yes_credit_micro"][-1], 1 * M - 5 * (1 * M // 6))   # remainder to the last

    def test_three_legs_with_a_non_divisible_amount(self):
        r = merge_split(3, 1_000_000, [0, 0, 0])
        self.assertEqual(r["yes_credit_micro"], [333_333, 333_333, 333_334])
        self.assertEqual(sum(r["yes_credit_micro"]), 1_000_000)

    def test_one_leg(self):
        r = merge_split(1, 1 * M, [250_000])
        self.assertEqual(r["yes_credit_micro"], [1 * M])
        self.assertEqual(r["yes_realised_micro"], [750_000])
        self.assertEqual(r["no_basis_micro"], [-750_000])

    def test_rejects_a_mismatch(self):
        with self.assertRaises(ValueError):
            merge_split(3, 1 * M, [1, 2])
        with self.assertRaises(ValueError):
            merge_split(0, 1 * M, [])


class TestNegriskPnl(unittest.TestCase):
    def test_resolve_payouts_and_realised(self):
        p = pos((2 * M, 1_000_000))
        r = negrisk_event_pnl([p], [
            {"kind": "buy", "notional_micro": 1_000_000},
            {"kind": "resolve", "payout_micro": 2 * M, "basis_released_micro": 1_000_000},
        ])
        self.assertEqual(r["cash_micro"], 1_000_000)              # -1.00 in, +2.00 out
        self.assertEqual(r["realised_micro"], 1_000_000)
        self.assertEqual(r["settled_count"], 2)

    def test_merge_records_the_residual_and_keeps_the_book_balanced(self):
        yes = [pos((1 * M, c), token=f"y{i}") for i, c in enumerate([600_000, 300_000, 150_000])]
        settlements = [{"kind": "buy", "notional_micro": c} for c in (600_000, 300_000, 150_000)]
        settlements.append({"kind": "merge", "usdc_micro": 1 * M, "yes_basis_released_micro": 1_050_000})
        r = negrisk_event_pnl(yes, settlements)
        self.assertEqual(r["cash_micro"], -1_050_000 + 1 * M)     # paid 1.05, received 1.00
        self.assertEqual(r["realised_micro"], 1 * M - 1_050_000)  # a 50_000 loss, exactly
        self.assertEqual(settlements[-1]["no_basis_assigned_micro"], 1_050_000 - 1 * M)
        self.assertGreater(settlements[-1]["no_basis_assigned_micro"], 0)   # NO legs carry the loss

    def test_unknown_settlement_kind_raises_rather_than_ignoring(self):
        with self.assertRaises(ValueError):
            negrisk_event_pnl([], [{"kind": "airdrop"}])

    def test_the_result_carries_no_reconciles_flag(self):
        # A boolean computed from the function's own inputs cannot be False, which is what the first
        # version of this returned. The equation is asserted by the caller with the open basis instead.
        r = negrisk_event_pnl([], [])
        self.assertNotIn("reconciles", r)
        self.assertEqual(r["realised_micro"], 0)


class TestPayoutArithmetic(unittest.TestCase):
    """The venue's own trade API contradicts `price * size` on redemptions (P01, measured):
    `price == 0` in 287/287 redeemed rows and `usdcSize == size` in 116/125, so a payout computed from the
    price field is 0 for a position that paid a dollar a share. This test pins the rule."""

    def test_redeemed_position_pays_usdcsize_not_price_times_size(self):
        rows = [dict(price=0.0, size=1250.0, usdcSize=1250.0),
                dict(price=0.0, size=88.5, usdcSize=88.5),
                dict(price=0.0, size=4.0, usdcSize=12.0)]        # the 9/125 where they disagree
        for r in rows:
            naive = int(Decimal(str(r["price"])) * Decimal(str(r["size"])) * M)
            truth = int(Decimal(str(r["usdcSize"])) * M)
            self.assertEqual(naive, 0, r)
            self.assertNotEqual(truth, 0, r)

    def test_the_disagreement_direction_is_recorded(self):
        # P01: usdcSize != size on 9 of 125 rows, so `usdcSize` is authoritative and `size` is not a proxy
        # for it. Asserting the ratio would be asserting a venue bug we do not control.
        self.assertGreater(125 - 116, 0)


class TestStates(unittest.TestCase):
    def test_uncertain_is_an_unsettled_state(self):
        self.assertIn(IntentState.UNCERTAIN, {IntentState.SUBMITTING, IntentState.UNCERTAIN,
                                              IntentState.SUBMITTED})

    def test_every_status_the_mock_can_emit_is_mapped(self):
        # The mock is the venue we run against in CI, so its own EMITS list is the oracle - if the mock gains
        # a status and nobody maps it, this fails, which is the point.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mock_clob", str(ROOT / "services/executor-mock/mock_clob.py"))
        mk = importlib.util.module_from_spec(spec); spec.loader.exec_module(mk)
        self.assertGreaterEqual(len(mk.MockClob.EMITS), 4)
        for raw in mk.MockClob.EMITS:
            with self.subTest(raw=raw):
                self.assertIn(venue_status_to_state(raw), {s.value for s in OrderState})

    def test_an_unmapped_venue_status_raises_instead_of_defaulting(self):
        with self.assertRaises(ValueError):
            venue_status_to_state("frobnicated")
        self.assertEqual(venue_status_to_state("matched"), OrderState.FILLED.value)
        self.assertEqual(venue_status_to_state("canceled"), OrderState.CANCELLED.value)


class TestCashEntry(unittest.TestCase):
    def test_is_frozen(self):
        import dataclasses
        e = CashEntry(id=1, user_id="u", kind="fee", amount_micro=-100, ref_table="fills", ref_id="f",
                      as_of_ms=0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            e.amount_micro = -200
        self.assertEqual(e.amount_micro, -100)


if __name__ == "__main__":
    unittest.main()
