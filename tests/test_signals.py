"""The signal engine: that each rule fires on its event, does not fire on the near-miss, and cannot fire twice
inside its cooldown. The suppression tests are the point — a signal engine without them is a notification spam
generator with a nice API.
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))

from polygm_core.signals.engine import (Alert, Engine, RuleError, build_rule)  # noqa: E402

NOW = 10 ** 13


def rules():
    return [
        build_rule("large", "large_fill"),
        build_rule("spike", "volume_spike"),
        build_rule("move", "rapid_move", {"pct": 2.0}),
        build_rule("wallet", "watched_wallet"),
        build_rule("flip", "imbalance_flip"),
        build_rule("negrisk", "negrisk_divergence", {"tolerance_micro": 20_000}),
        build_rule("resolm", "resolution_imminent"),
        build_rule("newm", "new_market", {"categories": ["crypto"]}),
    ]


def fill(**kw):
    base = {"type": "fill", "market": "0xM", "token_id": "t1", "side": "BUY", "wallet": "w1",
            "usd_notional_micro": 15_000 * 10 ** 6, "market_median_fill_micro": 5_000_000,
            "market_fill_sample": 500, "watched_wallets": ["w1"]}
    base.update(kw)
    return base


class TestBuiltIns(unittest.TestCase):
    def setUp(self):
        self.e, self.r = Engine(), rules()

    def kinds(self, alerts):
        return sorted({a.kind for a in alerts})

    def test_large_fill_fires_on_absolute_and_relative(self):
        self.assertEqual(self.kinds(self.e.evaluate(self.r, fill(), NOW)), ["large_fill", "watched_wallet"])

    def test_large_fill_needs_a_sample_before_the_relative_arm_counts(self):
        # Without min_sample, a market's first trade is "20x the median" of a median of zero.
        out = self.e.evaluate(self.r, fill(market_fill_sample=3), NOW)
        self.assertNotIn("large_fill", self.kinds(out))

    def test_large_fill_below_the_absolute_floor_is_silent(self):
        # $900 is under the $10k floor: the large-fill rule stays quiet even though another rule (watched
        # wallet, $500 floor) legitimately fires on the same event.
        self.assertNotIn("large_fill", self.kinds(self.e.evaluate(self.r, fill(usd_notional_micro=900 * 10 ** 6),
                                                                   NOW)))

    def test_volume_spike_needs_variance(self):
        history = [10 ** 8 + (i % 5) * 2 * 10 ** 7 for i in range(40)]     # variance, so a z-score exists
        hit = self.e.evaluate(self.r, {"type": "volume_bucket", "market": "0xM", "value_micro": 9 * 10 ** 9,
                                       "history_micro": history}, NOW)
        self.assertEqual(len(hit), 1)
        self.assertGreater(hit[0].body["z"], 3.0)
        flat = self.e.evaluate(self.r, {"type": "volume_bucket", "market": "0xM", "value_micro": 10 ** 8,
                                        "history_micro": [10 ** 8] * 40}, NOW + 10 ** 7)
        self.assertEqual(flat, [])

    def test_rapid_move_requires_depth(self):
        thin = {"type": "price_move", "market": "0xM", "old_micro": 500_000, "new_micro": 560_000,
                "usd_depth_micro": 100 * 10 ** 6}
        self.assertEqual(self.e.evaluate(self.r, thin, NOW), [])
        deep = dict(thin, usd_depth_micro=9_000 * 10 ** 6)
        self.assertEqual(self.kinds(self.e.evaluate(self.r, deep, NOW)), ["rapid_move"])

    def test_a_settlement_is_not_a_move(self):
        ev = {"type": "price_move", "market": "0xM", "old_micro": 999_000, "new_micro": 1_000_000,
              "usd_depth_micro": 9_000 * 10 ** 6, "resolution_event": True}
        self.assertEqual(self.e.evaluate(self.r, ev, NOW), [],
                         "0.999 -> 1.000 on a resolving market must not page anyone")

    def test_imbalance_flip_needs_both_sides_of_the_line(self):
        self.assertEqual(self.kinds(self.e.evaluate(self.r, {"type": "book", "market": "0xM",
                                                              "imbalance_prev": -0.5, "imbalance": 0.6}, NOW)),
                         ["imbalance_flip"])
        self.assertEqual(self.e.evaluate(self.r, {"type": "book", "market": "0xM", "imbalance_prev": -0.1,
                                                   "imbalance": 0.2}, NOW + 10 ** 6), [])

    def test_negrisk_divergence_reports_the_deviation_in_ticks(self):
        out = self.e.evaluate(self.r, {"type": "negrisk_sum", "event": "0xE", "sum_micro": 1_045_000,
                                       "tick_micro": 1000, "prices_micro": [1] * 4}, NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].body["deviation_micro"], 45_000)
        self.assertEqual(self.e.evaluate(self.r, {"type": "negrisk_sum", "event": "0xE2", "sum_micro": 1_005_000,
                                                   "tick_micro": 1000}, NOW + 10 ** 6), [],
                         "5,000 micro is inside the 20,000 tolerance")

    def test_resolution_imminent_only_for_a_user_with_a_position(self):
        ev = {"type": "market_clock", "market": "0xM", "end_ts_ms": NOW + 2 * 3_600_000,
              "user_open_position_micro": 10 ** 6}
        self.assertEqual(self.kinds(self.e.evaluate(self.r, ev, NOW)), ["resolution_imminent"])
        self.assertEqual(self.e.evaluate(self.r, dict(ev, user_open_position_micro=0), NOW + 10 ** 7), [])
        self.assertEqual(self.e.evaluate(self.r, dict(ev, end_ts_ms=NOW + 40 * 3_600_000), NOW + 2 * 10 ** 7),
                         [], "40 hours out is not imminent")

    def test_new_market_respects_the_category_filter(self):
        self.assertEqual(self.e.evaluate(self.r, {"type": "new_market", "market": "0xN", "question": "q",
                                                   "tags": ["sports"]}, NOW), [])
        self.assertEqual(self.kinds(self.e.evaluate(self.r, {"type": "new_market", "market": "0xN2",
                                                              "question": "q", "tags": ["crypto"]}, NOW + 10 ** 7)),
                         ["new_market"])


class TestSuppression(unittest.TestCase):
    def test_repeat_inside_cooldown_is_suppressed_then_allowed(self):
        e, r = Engine(), rules()
        first = e.evaluate(r, fill(), NOW)
        self.assertEqual(len(first), 2)
        self.assertEqual(e.evaluate(r, fill(), NOW + 1_000), [])
        self.assertEqual(e.suppressed, 2)
        self.assertEqual(len(e.evaluate(r, fill(), NOW + 301_000)), 2, "cooldown is 300s")

    def test_the_dedupe_key_is_per_rule_and_per_market(self):
        e, r = Engine(), [build_rule("large", "large_fill")]
        e.evaluate(r, fill(market="0xA"), NOW)
        self.assertEqual(len(e.evaluate(r, fill(market="0xB"), NOW + 10)), 1,
                         "a different market is a different alert, not a repeat")

    def test_state_survives_a_restart_only_if_the_caller_persists_it(self):
        # The engine keeps state in a plain dict for exactly that reason: the ingest loop writes
        # `signal_state` and reloads it, and this assertion documents the shape it must not change.
        e = Engine()
        e.evaluate([build_rule("large", "large_fill")], fill(), NOW)
        key = list(e.state)[0]
        self.assertTrue(key.startswith("large:0xM:"), key)
        # A fill an order of magnitude bigger is a DIFFERENT alert: the bucket in the dedupe key is what makes
        # "$10k whale fill" and "$400k whale fill" both reach a user who asked for either.
        big = e.evaluate([build_rule("large", "large_fill")], fill(usd_notional_micro=400_000 * 10 ** 6),
                         NOW + 1)
        self.assertEqual(len(big), 1, "a much larger fill must not be swallowed by the first one's cooldown")
        e2 = Engine(state=dict(e.state))
        self.assertEqual(e2.evaluate([build_rule("large", "large_fill")], fill(), NOW + 1000), [])


class TestUserRules(unittest.TestCase):
    def test_an_unknown_kind_is_refused_not_ignored(self):
        with self.assertRaises(RuleError):
            build_rule("x", "made_up")

    def test_an_unknown_param_is_refused_not_ignored(self):
        with self.assertRaises(RuleError) as cm:
            build_rule("x", "large_fill", {"mim_sample": 5})
        self.assertIn("does not take", str(cm.exception))

    def test_money_params_must_be_integer_micro(self):
        for bad in (1000.5, "1000", True, -1, 0):
            with self.assertRaises(RuleError, msg=repr(bad)):
                build_rule("x", "large_fill", {"abs_usd_micro": bad})

    def test_min_sample_below_two_is_refused(self):
        with self.assertRaises(RuleError):
            build_rule("x", "volume_spike", {"min_sample": 1})

    def test_severity_and_channel_are_validated(self):
        with self.assertRaises(RuleError):
            build_rule("x", "large_fill", {}, severity="critical")
        with self.assertRaises(RuleError):
            build_rule("x", "large_fill", {}, channels=["carrier_pigeon"])

    def test_defaults_come_from_the_kind_not_from_the_caller(self):
        r = build_rule("x", "large_fill")
        self.assertEqual((r.cooldown, r.severity_level, r.channel_list), (300, "urgent", ("push", "telegram")))

    def test_a_user_override_wins(self):
        # A user who lowers the absolute floor still has to clear the relative arm, and that is the right
        # behaviour: a $2 fill in a market whose median is $5 is not "large" in any sense we would defend to
        # the user who was paged. So the override that matters here is the whole rule, not one number.
        loose = build_rule("x", "large_fill", {"abs_usd_micro": 10 ** 6, "rel_multiple": 0.4},
                           cooldown_s=0, severity="info", channels=["inapp"])
        self.assertEqual((loose.cooldown, loose.severity_level, loose.channel_list), (0, "info", ("inapp",)))
        e = Engine()
        self.assertEqual(e.evaluate([loose], fill(usd_notional_micro=2 * 10 ** 6), NOW)[0].suppressed, 0)
        self.assertEqual(len(e.evaluate([loose], fill(usd_notional_micro=2 * 10 ** 6), NOW + 1)), 1,
                         "cooldown 0 means the user asked for every fill, and we honour that")
        strict = build_rule("y", "large_fill", {"abs_usd_micro": 10 ** 6})
        self.assertEqual(e.evaluate([strict], fill(usd_notional_micro=2 * 10 ** 6), NOW + 2), [],
                         "abs alone must not fire under the median")


class TestAlertShape(unittest.TestCase):
    def test_alert_carries_what_a_user_needs_to_act(self):
        e, r = Engine(), [build_rule("negrisk", "negrisk_divergence")]
        a = e.evaluate(r, {"type": "negrisk_sum", "event": "0xE", "sum_micro": 1_045_000,
                           "tick_micro": 1000}, NOW)[0]
        self.assertIsInstance(a, Alert)
        for k in ("dedupe_key", "severity", "at_ms", "market_id", "channels", "body"):
            self.assertTrue(getattr(a, k), "%s must be set on every alert" % k)
        self.assertNotIn("detail", a.__dict__)          # the envelope rule again: no free text

    def test_the_body_never_carries_the_users_own_input_back(self):
        e = Engine()
        addr = "0x" + "de" * 20
        a = e.evaluate([build_rule("w", "watched_wallet")],
                       fill(wallet=addr, watched_wallets=[addr]), NOW)[0]
        self.assertEqual(a.body["wallet"], addr)               # an address IS the subject here, and is not a
        self.assertNotIn("watched_wallets", a.body)              # secret — but the rule list is the user's own


if __name__ == "__main__":
    unittest.main()
