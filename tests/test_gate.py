"""Risk gate: the only thing standing between a click and a signature (P04 rule: every order goes through it).

Two properties are tested that a naive "each rule rejects" suite misses:
  * ORDER — a rejected order must not leak which later checks would also have failed, and the kill switch
    must beat every other verdict;
  * COMPLETENESS — every code the gate can emit has an API-visible meaning, so adding a rule without
    adding its user-safe message becomes a failing test rather than a 500 with a Python message.
"""
from __future__ import annotations
import re
import unittest
from decimal import Decimal
from pathlib import Path

from conftest import ROOT  # noqa: F401
from polygm_core.risk.gate import Decision, Intent, Limits, MarketState, evaluate, norm_tick

LIM = Limits()


def state(**kw) -> MarketState:
    base = dict(accepting_orders=True, seconds_delay=0, enable_order_book=True, minimum_tick_size="0.01",
                minimum_order_size="5", fee_type="None", best_bid_micro=490_000, best_ask_micro=510_000,
                snap_age_ms=0)
    base.update(kw)
    return MarketState(**base)


def intent(**kw) -> Intent:
    base = dict(user_id="u", token_id="0x1", side="BUY", price_micro=500_000, size_shares_micro=10_000_000,
                idempotency_key="k", market_id="m")
    base.update(kw)
    return Intent(**base)


def ev(i=None, m=None, **kw):
    a = dict(limits=kw.pop("limits", LIM), open_orders=kw.pop("open_orders", 0),
             spent_24h_micro=kw.pop("spent_24h_micro", 0), kill_switch=kw.pop("kill_switch", False))
    a.update(kw)
    return evaluate(i or intent(), m or state(), **a)


class TestHappyPath(unittest.TestCase):
    def test_accepts_and_reports_notional(self):
        d = ev()
        self.assertTrue(d.allowed, d.code)
        self.assertEqual(d.code, "OK")
        self.assertEqual(d.notional_micro, 10_000_000 * 500_000 // 10**6)      # $5.00
        self.assertIn("price_band", d.checks_run)
        self.assertGreater(d.latency_ms, 0.0)          # a latency of exactly 0 would mean the timer is dead

    def test_delayed_market_is_not_a_denial_but_is_recorded(self):
        d = ev(m=state(seconds_delay=3))
        self.assertTrue(d.allowed)
        self.assertIn("delayed_market", d.checks_run)   # the UI needs to know, the gate must not block

    def test_20_cent_price_on_0_01_tick_is_fine(self):
        self.assertTrue(ev(intent(price_micro=200_000), m=state(best_bid_micro=190_000,
                                                                best_ask_micro=210_000)).allowed)


class TestDenials(unittest.TestCase):
    CASES = [
        ("ZERO_SIZE", dict(size_shares_micro=0), dict(minimum_order_size="0")),
        ("MARKET_NOT_ACCEPTING", {}, dict(accepting_orders=False)),
        ("NO_ORDER_BOOK", {}, dict(enable_order_book=False)),
        ("STALE_QUOTE", {}, dict(snap_age_ms=5_001)),
        ("BAD_SIDE", dict(side="buy"), None),
        ("UNKNOWN_TICK", {}, dict(minimum_tick_size="0.005")),
        ("OFF_TICK", dict(price_micro=500_500), None),
        ("BELOW_MIN_SIZE", dict(size_shares_micro=4_000_000), None),
        ("BAD_MARKET_META", {}, dict(minimum_order_size="abc")),
        ("OVER_ORDER_CAP", dict(size_shares_micro=10**10), None),
        ("PRICE_FAR_FROM_MID", dict(price_micro=900_000), None),
        ("TOO_MANY_OPEN", dict(), None),
        ("DAILY_CAP", dict(), None),
    ]

    def test_every_rule_denies(self):
        for code, ikw, mkw in self.CASES:
            extra = {"kill_switch": True} if code == "RISK_HALT" else {}
            if code == "TOO_MANY_OPEN":
                extra["open_orders"] = LIM.max_open_orders_per_user
            if code == "DAILY_CAP":
                extra["spent_24h_micro"] = LIM.max_24h_notional_micro
            d = ev(intent(**ikw), m=state(**(mkw or {})), **extra)
            with self.subTest(code=code):
                self.assertFalse(d.allowed)
                self.assertEqual(d.code, code)
                self.assertTrue(d.message)             # empty message => the client sees nothing
                self.assertEqual(d.notional_micro, 0)  # a denial must not carry a spend figure

    def test_stale_beats_side_and_tick(self):
        # Order is a design decision (cheapest + most certain first). A stale book with an off-tick price
        # must say "stale, retryable" and NOT "off tick", which would tell the user their price was wrong
        # when the real problem is our feed.
        d = ev(intent(side="nope", price_micro=500_500), m=state(snap_age_ms=60_000))
        self.assertEqual(d.code, "STALE_QUOTE")
        self.assertEqual(d.checks_run, ("kill_switch", "market_state", "freshness"))

    def test_kill_switch_beats_everything_and_stops_at_two_checks(self):
        d = ev(intent(side="nope"), m=state(accepting_orders=False, snap_age_ms=99_000),
               kill_switch=True, open_orders=999)
        self.assertEqual(d.code, "RISK_HALT")
        self.assertEqual(d.checks_run, ("kill_switch",))
        self.assertTrue(d.allowed is False)

    def test_zero_size_is_refused_even_when_min_size_is_zero(self):
        # Regression found by writing this test: the market's minimum_order_size is per-market and we have
        # seen it defaulted to 0, so `size < min` alone let a 0-share order be signed.
        d = ev(intent(size_shares_micro=0), m=state(minimum_order_size="0"))
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "ZERO_SIZE")


from polygm_core.risk.gate import _aligned     # module-level on purpose: as a CLASS attribute a function
                                                # becomes a method and `self` steals the first argument


class TestCompleteness(unittest.TestCase):
    def test_every_deny_code_in_the_source_is_covered_by_the_api_envelope(self):
        """Cross-module invariant, checked against the real source text.

        If someone adds `return deny("RISK_NEW_THING", ...)` to the gate and forgets CODES in the API,
        the client gets the generic 400 with no message. That is exactly the class of bug this test exists
        to make impossible.
        """
        gate_src = (ROOT / "packages/polygm_core/risk/gate.py").read_text()
        codes = set(re.findall(r'deny\(\s*"([A-Z_0-9]+)"', gate_src))
        self.assertGreaterEqual(len(codes), 12, f"pattern matched almost nothing: {codes}")
        app_src = (ROOT / "services/api/app.py").read_text()
        declared = set(re.findall(r'^\s{4}"([A-Z_0-9]+)":\s*\(', app_src, re.M))
        missing = sorted(codes - declared)
        self.assertEqual(missing, [], f"gate codes with no API message/status: {missing}")

    def test_limits_defaults_match_measured_venue_facts(self):
        # 5 shares and the two tick sizes are MEASURED (P01). If a future edit "tidies" them, the tests
        # against the mock venue will start rejecting real-shaped orders, and someone will spend an evening
        # finding out why. Pinning them here turns that into one clear failure.
        self.assertEqual(LIM.min_order_size_shares_micro, 5 * 10**6)
        self.assertEqual(LIM.max_tick_sizes, ("0.001", "0.01"))
        self.assertEqual(LIM.max_snap_age_ms, 5_000)

    def test_alignment_is_integer_only(self):
        self.assertTrue(_aligned(501_000, "0.001"))
        self.assertFalse(_aligned(501_000, "0.01"))
        self.assertTrue(_aligned(500_000, "0.01"))
        self.assertTrue(_aligned(round(Decimal("0.501") * 10**6), "0.001"))

    def test_the_float_alignment_alternative_is_measurably_wrong(self):
        # This is the D4 argument, not a hypothetical. Scanning every on-tick price in (0,1):
        #   int(price_micro/1e6 / 0.01) != exact quotient  for  6 of 99  on-tick prices
        #   int(price_micro/1e6 / 0.001) != exact quotient for 126 of 999 on-tick prices
        # i.e. the float version rejects orders the venue would happily accept, and it does so for
        # specific prices, so it passes a smoke test and fails in production.
        for tick_micro, tick_f in ((10_000, 0.01), (1_000, 0.001)):
            wrong = [m for m in range(tick_micro, 10**6, tick_micro)
                     if int((m / 10**6) / tick_f) != m // tick_micro]
            self.assertGreater(len(wrong), 0, "expected the float path to be broken; if it is not, "
                                              "delete this test and re-argue D4")
            for m in wrong:
                self.assertTrue(_aligned(m, str(Decimal(tick_micro) / 10**6)), m)



class TestTickNormalisationAtTheBoundary(unittest.TestCase):
    """The regression this class exists for: Postgres hands back NUMERIC(6,4) as the STRING "0.0100",
    asyncpg as a Decimal, and SQLite as the FLOAT 0.01. A gate that compares tick sizes as text therefore
    rejects every real market while passing fixtures that wrote "0.01" by hand — which is what happened, and
    `tools/p04-mutation-test.py` re-plants the bug (mutant `tick-raw-string`) to keep this class from rotting
    back into a smoke test.
    """

    def test_all_three_driver_shapes_normalise_to_the_same_text(self):
        for value, want in (("0.0100", "0.01"), (Decimal("0.0100"), "0.01"), (0.01, "0.01"),
                            ("0.0010000", "0.001"), (1e-3, "0.001"), ("0.01", "0.01"), (Decimal("0.001"), "0.001")):
            self.assertEqual(norm_tick(value), want, "norm_tick(%r) as %s" % (value, type(value).__name__))

    def test_an_integer_valued_tick_keeps_a_decimal_point(self):
        # "1" would be a price of one cent times a hundred; the venue has no such tick, and the text must not
        # look like a string that could be compared against "0.01" by a future refactor.
        self.assertEqual(norm_tick(Decimal("0.10")), "0.1")

    def test_unusable_ticks_raise_instead_of_becoming_a_default(self):
        for bad in (None, 0, "0", "1", "1.0", Decimal("-0.01")):
            with self.assertRaises(ValueError, msg="norm_tick(%r) accepted an unusable tick" % (bad,)):
                norm_tick(bad)

    def test_a_postgres_shaped_tick_still_orders(self):
        d = ev(m=state(minimum_tick_size="0.0100"))
        self.assertTrue(d.allowed, d.code)
        self.assertIn("tick_alignment", d.checks_run)

    def test_a_postgres_shaped_tick_outside_the_allowed_set_is_UNKNOWN_TICK_not_off_tick(self):
        # 0.0050 is a real tick size the venue does not use; silently accepting it would align prices to a
        # grid the exchange rejects, and the user would see a venue error we could have named first.
        d = ev(m=state(minimum_tick_size="0.0050"))
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "UNKNOWN_TICK")


if __name__ == "__main__":
    unittest.main()
