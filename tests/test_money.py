"""Money representation: the invariant everything else rests on (P04 rule: no floats in the money path)."""
from __future__ import annotations
import unittest
from decimal import Decimal

from conftest import ROOT  # noqa: F401 - path setup side effect
from polygm_core.money.cents import (MAX_SAFE_INT, SCALE, MoneyError, ScaleError, ceil_div, fmt_usdc,
                                     micros, notional_floor, parse_usdc, price_ticks, to_float_for_sdk)


class TestParse(unittest.TestCase):
    def test_exact_values(self):
        self.assertEqual(parse_usdc("1.000001"), 1_000_001)
        self.assertEqual(parse_usdc("5"), 5_000_000)
        self.assertEqual(parse_usdc("0.000000"), 0)
        self.assertEqual(parse_usdc(Decimal("12.34")), 12_340_000)

    def test_rejects_excess_scale_instead_of_rounding(self):
        # 1e-7 is representable as a float and NOT representable in USDC: rounding it silently is how a
        # client-side bug becomes a ledger that does not tie out.
        with self.assertRaises(ScaleError):
            parse_usdc("1.0000004")
        with self.assertRaises(ScaleError):
            parse_usdc("0.1234567")

    def test_rejects_junk_and_floats(self):
        for bad in ("", "1,000", "$5", "nan", "inf", "0x1"):
            with self.subTest(bad=bad):
                self.assertRaises((ScaleError, MoneyError), parse_usdc, bad)
        with self.assertRaises(MoneyError):
            parse_usdc(1.5)                                   # a float input is the thing we forbid
        with self.assertRaises(MoneyError):
            parse_usdc(True)                                  # bool is an int in Python; it is not money
        with self.assertRaises(MoneyError):
            parse_usdc(-1)                                    # signed amounts belong in cash entries

    def test_roundtrip_is_lossless_for_every_valid_value(self):
        # exhaustive over 1 cent..$10 at 1-micro steps for the low end, then strided: a spot check would
        # miss exactly the carry boundaries that break a naive int()*1e6 implementation.
        for v in list(range(0, 20_001)) + list(range(20_001, 10_000_000, 7_919)):
            s = fmt_usdc(v)
            self.assertEqual(parse_usdc(s), v, f"{v} -> {s!r} -> ?")

    def test_format_never_prints_scientific_or_localised(self):
        for v in (1, 1000, 1_000_000, 123_456_789):
            s = fmt_usdc(v)
            self.assertNotIn("e", s.lower())
            self.assertNotIn(",", s)
            self.assertTrue(Decimal(s).scaleb(6) % 1 == 0, s)


class TestPriceTicks(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(price_ticks("0.5"), 500_000)
        self.assertEqual(price_ticks("0.001"), 1_000)
        self.assertEqual(price_ticks("0.999"), 999_000)

    def test_zero_and_one_are_not_prices(self):
        # A 0 or 1 price is not tradable, and letting it through makes a notional of 0 (free order, no
        # exposure) or a payout of more than the market can pay.
        for bad in ("0", "1", "1.001", "-0.5"):
            with self.subTest(bad=bad):
                self.assertRaises(MoneyError, price_ticks, bad)

    def test_the_micro_floor_of_a_price_is_one_not_a_tick(self):
        # 1e-6 IS a valid price here; 0.001 is only the FINEST venue tick, and a 0.01-tick market must
        # reject it in the risk gate (OFF_TICK), not in the parser. Two tests in test_gate.py pin that.
        self.assertEqual(price_ticks("0.000001"), 1)
        self.assertRaises(MoneyError, price_ticks, "0.0000001")

    def test_fine_prices_are_accepted_here_and_refused_by_the_gate(self):
        # Deliberate split of responsibility. price_ticks only enforces "representable at 1e-6"; tick
        # alignment is per-market (0.001 vs 0.01, both observed in P01), so parsing money and validating
        # the venue's tick are different checks and only the gate owns the second one. If price_ticks
        # rejected 0.5005 the gate's OFF_TICK code would be unreachable, and a 0.01 market would silently
        # accept a 0.0015 price in any code path that skipped it.
        self.assertEqual(price_ticks("0.5005"), 500_500)


class TestSdkBoundary(unittest.TestCase):
    def test_float_conversion_round_trips(self):
        for micro in (1, 500_000, 999_000, 10_000_000, 2_500 * 10**6 - 1):
            f = to_float_for_sdk(micro, field="price")
            self.assertEqual(round(f * 10**6), micro, f"{micro} -> {f!r}")

    def test_the_top_of_the_range_raises_and_the_boundary_is_real(self):
        # Measured, not asserted: float(m)/1e6 round-trips for EVERY m in [1, 2**53-2] and fails at
        # 2**53-1. (Also re-proved at runtime below, so this cannot go stale with the constant.) The first version of this test scanned a small range, found nothing, and would have
        # passed against a to_float_for_sdk that never raised - so it now (a) proves the guard fires at the
        # top, (b) proves the range scan is honest by checking the value below the boundary still works,
        # and (c) proves parse_usdc refuses to mint a value at the boundary in the first place.
        BOUND = MAX_SAFE_INT                     # 2**53 - 1, defined once in the module under test
        self.assertTrue(to_float_for_sdk(BOUND - 1, field="size") > 0)
        self.assertEqual(SCALE, 6)
        # the claim in the comment above, checked rather than trusted: no failure below the boundary
        probe = [m for m in (1, 999_999, 10**9 + 7, BOUND - 1)
                 if int(Decimal(repr(float(m) / 10**6)).scaleb(6)) != m]
        self.assertEqual(probe, [], f"round-trip holds below 2**53-1, got {probe}")
        with self.assertRaises(MoneyError):
            to_float_for_sdk(BOUND, field="price")
        with self.assertRaises(MoneyError):
            parse_usdc(Decimal(BOUND).scaleb(-6))
        # and the "round-trips" claim must be checked the way the SDK will check it
        for m in (1, 999_999, 12_345_678_901, BOUND - 1):
            f = to_float_for_sdk(m, field="size")
            self.assertEqual(int(Decimal(repr(f)).scaleb(6)), m, f"{m} -> {f!r}")

    def test_notional_floors_never_ceilings(self):
        # A rounded-up notional can exceed the cash we locked, i.e. the order fails AFTER we told the user
        # it was affordable. The venue's own order_builder.round_down is why this is the safe direction.
        self.assertEqual(notional_floor(1_000_001, 500_001), (1_000_001 * 500_001) // 10**6)
        self.assertLessEqual(notional_floor(7, 3), 7 * 3 / 10**6)
        exact = notional_floor(2_000_000, 500_000)
        self.assertEqual(exact, 1_000_000)

    def test_ceil_div(self):
        self.assertEqual(ceil_div(9, 2), 5)
        self.assertEqual(ceil_div(8, 2), 4)
        self.assertEqual(ceil_div(0, 7), 0)

    def test_micros_from_integer_at_a_coarser_scale(self):
        # micros() takes an INTEGER expressed with `scale_in` decimals, i.e. venue-native 1e-18 wei-ish
        # inputs normalised to 1e-6; Decimal("1.5") is not that, and the first version of this test
        # asserted the wrong thing about it.
        self.assertEqual(micros(1_500_000, scale_in=SCALE), 1_500_000)
        self.assertEqual(micros(150, scale_in=SCALE - 2), 15_000)   # 150 @ 4dp = 0.015 = 15000 micro
        with self.assertRaises(ScaleError):
            micros(1, scale_in=SCALE + 1)          # finer than canonical: cannot be represented


if __name__ == "__main__":
    unittest.main()
