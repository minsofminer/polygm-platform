"""P11 D5 · The referral rules, unit-tested on evidence rather than on a database.

What these tests defend, in the order a referrer or an attacker meets it:

  * **a signup earns nothing, and neither does a deposit**: the reward starts at a matched order over the
    threshold, so the funnel's first two steps are worth zero dollars by construction;
  * **a flat bounty's failure mode cannot happen here**: what is paid is a share of a fee we were *observed* to
    be paid, so a manufactured referee costs its maker more than it pays them — asserted as arithmetic, not as
    a policy statement;
  * **self-referral is refused with the revocation ground named**, and a second wallet funded from the same source
    is refused as the same person, while a shared office IP is only *reviewed* — the three collisions the kit
    names get three different answers, in a fixed order;
  * **nothing is paid for recruiting**: there is no second level in the model, and the published rules say so;
  * **a clawback takes unpaid accruals first**, and under the floor it is written off rather than invoiced;
  * every number is an integer: `share_of` floors, and payout arithmetic cannot drift by a micro-dollar.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "packages"), str(ROOT / "services" / "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polygm_core.referrals import code as rc                                        # noqa: E402
from polygm_core.referrals import sybil as sy                                       # noqa: E402
from polygm_core.referrals import terms as rt                                       # noqa: E402

DAY = 86_400_000
NOW = 1_760_000_000_000
SALT = "s" * 32


def sig(kind: str, value: str) -> dict:
    return {"kind": kind, "hash": sy.hash_(value, SALT, kind=kind)}


class TestTheRewardModel(unittest.TestCase):
    def test_a_signup_earns_nothing_and_neither_does_a_deposit(self):
        # The kit's trap, as an assertion: deposits are not an input to this module at all. There is no function
        # that turns money arriving into money owed, so "deposit, withdraw, never trade" earns exactly zero.
        ok, why = rt.qualifies(matched=False, notional_micro=5_000 * 1_000_000)
        self.assertFalse(ok)
        self.assertIn("unfilled order pays no fee", why)
        self.assertEqual(rt.share_of(0), 0)

    def test_only_a_matched_order_over_the_threshold_opens_a_referral(self):
        self.assertEqual(rt.QUALIFY_NOTIONAL_MICRO, 25 * 1_000_000)
        self.assertFalse(rt.qualifies(matched=True, notional_micro=24_999_999)[0])
        self.assertTrue(rt.qualifies(matched=True, notional_micro=25_000_000)[0])
        ok, why = rt.qualifies(matched=True, notional_micro=25_000_000, self_cross=True)
        self.assertFalse(ok, why)
        ok, why = rt.qualifies(matched=True, notional_micro=25_000_000, excluded_reason="cycle")
        self.assertFalse(ok)
        self.assertEqual(why, "cycle", "the venue's own exclusion reason is passed through, not re-invented")

    def test_the_reward_is_a_share_of_a_fee_we_were_actually_paid(self):
        # $10 of observed builder fee at 25% is $2.50 to the referrer, floored.
        self.assertEqual(rt.share_of(10_000_000), 2_500_000)
        self.assertEqual(rt.share_of(3), 0, "three micro-dollars at 25% is zero, and flooring is published")
        self.assertEqual(rt.share_of(10_000_000, 10_000), 10_000_000)
        with self.assertRaises(ValueError):
            rt.share_of(1, 10_001)

    def test_a_manufactured_referee_costs_its_maker_more_than_it_pays(self):
        # The property that replaced a bounty: to pay a referrer $25, the referee must cause $100 of builder fee
        # to be collected — money that comes out of the attacking account's own trading. The loop is loss-making
        # for the attacker, and that is arithmetic rather than a rule somebody has to enforce.
        fee_for_25_payout = 25_000_000 * 10_000 // rt.SHARE_BPS
        self.assertEqual(fee_for_25_payout, 100_000_000)
        self.assertGreater(fee_for_25_payout, 25_000_000)

    def test_the_term_runs_from_the_qualifying_order_not_from_signup(self):
        qualified = NOW
        self.assertEqual(rt.term_ends_ms(qualified), qualified + rt.TERM_DAYS * DAY)
        self.assertTrue(rt.in_term(qualified, qualified + 300 * DAY))
        self.assertFalse(rt.in_term(qualified, qualified + 366 * DAY))
        self.assertEqual(rt.term_days_left(qualified, qualified + 364 * DAY + 1), 1,
                         "a part-day left is a day the term is still open")
        self.assertEqual(rt.term_days_left(qualified, qualified + 400 * DAY), 0)

    def test_an_accrual_row_exists_only_when_a_day_earns_something(self):
        args = dict(referrer="u-a", referee="u-b", day="2026-09-18", observed_fee_micro=4_000_000,
                    state="qualified", qualified_ms=NOW - DAY, at_ms=NOW)
        row = rt.accrual(**args)
        self.assertEqual(row["share_micro"], 1_000_000)
        self.assertEqual(row["share_bps"], rt.SHARE_BPS)
        self.assertIsNone(rt.accrual(**{**args, "state": "review"}),
                          "a referral under review earns nothing until a person clears it")
        self.assertIsNone(rt.accrual(**{**args, "state": "refused"}))
        self.assertIsNone(rt.accrual(**{**args, "observed_fee_micro": 0}))
        self.assertIsNone(rt.accrual(**{**args, "at_ms": NOW + 400 * DAY}), "outside the term")

    def test_nothing_is_paid_for_recruiting_a_recruiter(self):
        # There is no function here that pays a referrer for an attribution to another referrer, and the published
        # rules say so in a sentence a user can quote back at us.
        rules = " ".join(rt.PUBLISHED_RULES).lower()
        self.assertIn("no second level", rules)
        self.assertIn("nothing is paid for recruiting a recruiter", rules)
        self.assertNotIn("tier", rules)
        self.assertEqual(rt.TERMS["model"], "builder-fee share")
        self.assertIn("never the fee we expected", rt.TERMS["paid_from"])


class TestPayoutArithmetic(unittest.TestCase):
    def rows(self, *shares, age_days=0):
        return [{"share_micro": s, "created_ms": NOW - age_days * DAY, "state": "qualified"} for s in shares]

    def test_the_three_buckets_are_disjoint_and_sum_to_what_was_accrued(self):
        got = rt.payable(self.rows(30_000_000, age_days=40), at_ms=NOW)
        self.assertEqual(got["settled_micro"], 30_000_000)
        self.assertEqual(got["holding_micro"], 0)
        got = rt.payable(self.rows(30_000_000, age_days=40) + self.rows(5_000_000, age_days=3), at_ms=NOW)
        self.assertEqual((got["settled_micro"], got["holding_micro"]), (30_000_000, 5_000_000))
        self.assertEqual(got["accrued_micro"], 35_000_000)
        self.assertEqual(got["payable_micro"], 30_000_000, "the held day is not payable yet")

    def test_the_settlement_hold_is_what_stops_a_reversed_fill_being_paid(self):
        held = rt.payable(self.rows(50_000_000, age_days=29), at_ms=NOW)
        self.assertEqual((held["payable_micro"], held["to_minimum_micro"]), (0, 20_000_000 - 0))
        self.assertIn("inside the 30-day settlement hold", held["note"])
        self.assertEqual(rt.payable(self.rows(50_000_000, age_days=31), at_ms=NOW)["payable_micro"], 50_000_000)

    def test_below_the_minimum_carries_forward_rather_than_vanishing(self):
        got = rt.payable(self.rows(6_000_000, age_days=60), at_ms=NOW)
        self.assertEqual(got["payable_micro"], 0)
        self.assertEqual(got["to_minimum_micro"], 14_000_000)
        self.assertEqual(got["accrued_micro"], 6_000_000, "the balance is still the referrer's")
        self.assertIn("carries forward", " ".join(rt.PUBLISHED_RULES).lower())

    def test_a_month_over_the_review_threshold_is_reviewed_before_it_is_paid(self):
        under = rt.payable(self.rows(2_000_000_000, age_days=60), at_ms=NOW)
        self.assertFalse(under["review_required"], "exactly at the threshold is not over it")
        over = rt.payable(self.rows(2_000_000_001, age_days=60), at_ms=NOW)
        self.assertTrue(over["review_required"])
        self.assertGreater(over["payable_micro"], 0, "review delays a payout; it does not cancel it")

    def test_a_clawed_back_accrual_is_not_owed(self):
        rows = self.rows(30_000_000, age_days=60) + [{"share_micro": 9_000_000, "created_ms": NOW - 60 * DAY,
                                                     "state": "clawed_back"}]
        got = rt.payable(rows, at_ms=NOW)
        self.assertEqual(got["accrued_micro"], 30_000_000)

    def test_the_schedule_is_a_date_a_referrer_can_put_in_a_calendar(self):
        import datetime as dt
        period = rt.payout_period(NOW)
        at = dt.datetime.fromtimestamp(rt.payout_at_ms(period) / 1000, dt.timezone.utc)
        self.assertEqual((at.day, at.month, at.hour), (10, int(period.split("-")[1]) % 12 + 1, 9))
        self.assertEqual(rt.payout_period(rt.payout_at_ms("2026-12")), "2027-01")
        with self.assertRaises(ValueError):
            rt.payout_at_ms("2026-13")

    def test_every_money_field_is_an_integer(self):
        with self.assertRaises(ValueError):
            rt.share_of(1_000_000.5)
        with self.assertRaises(ValueError):
            rt.payable([{"share_micro": 1.5, "created_ms": NOW}], at_ms=NOW)
        with self.assertRaises(ValueError):
            rt.term_ends_ms("soon")


class TestTheFunnel(unittest.TestCase):
    def test_a_monotone_money_funnel_is_accepted_and_an_impossible_one_is_refused(self):
        self.assertEqual(rt.funnel_findings({"clicks": 40, "signups": 12, "funded": 5, "trading": 5, "earned": 4}),
                         [])
        findings = rt.funnel_findings({"clicks": 0, "signups": 12, "funded": 13, "trading": 5, "earned": 4})
        self.assertEqual(len(findings), 1)
        self.assertIn("signups (12) is below funded (13)", findings[0])
        self.assertIn("cannot have more", findings[0])

    def test_clicks_are_not_a_ceiling_on_signups(self):
        # A short code shouted on a stream produces signups with no click at all, so the invariant that would
        # have made clicks a ceiling fires on a legitimate referrer - which is how a check gets switched off
        # instead of fixed. The leading counter is checked for what a counter can be wrong about on its own.
        self.assertEqual(rt.funnel_findings({"clicks": 0, "signups": 9, "funded": 0, "trading": 0, "earned": 0}), [])
        self.assertEqual(rt.funnel_findings({"clicks": -1, "signups": 0, "funded": 0, "trading": 0, "earned": 0}),
                         ["clicks is negative, which no counter can be"])
        self.assertEqual(rt.FUNNEL[0], "signups")
        self.assertNotIn("clicks", rt.FUNNEL)
        self.assertEqual(rt.LEADING, ("clicks",))

    def test_each_state_says_what_it_means_in_a_sentence(self):
        for state in rt.STATES:
            self.assertTrue(rt.state_text(state))
            self.assertNotEqual(rt.state_text(state), "unknown state")
        self.assertIn("earned nothing", rt.state_text("pending"))
        self.assertIn("revocation", rt.state_text("refused"))
        self.assertEqual(rt.state_text("wat"), "unknown state")

    def test_every_step_says_what_it_is_a_count_of(self):
        # "funded" and "trading" are two different claims about a stranger, and a dashboard that leaves the
        # difference to the reader's imagination is a dashboard whose numbers get argued about instead of read.
        for step in rt.FUNNEL + rt.LEADING:
            self.assertTrue(rt.FUNNEL_MEANING[step])
        self.assertIn("matched order", rt.FUNNEL_MEANING["funded"])
        self.assertIn("actually paid", rt.FUNNEL_MEANING["trading"])
        self.assertIn("clawback", rt.FUNNEL_MEANING["earned"])


class TestTheThreeCollisions(unittest.TestCase):
    def test_self_referral_is_refused_and_names_the_revocation_ground(self):
        got = sy.decision([sig("funding", "0xabc")], [sig("funding", "0xabc")])
        self.assertEqual(got["state"], "refused")
        self.assertEqual(got["reason"], "self_referral")
        self.assertEqual(got["builder_code_ground"], "builder_code_self_dealing")
        self.assertTrue(got["review"])
        self.assertIn("reven", got["sentence"] + rt.PUBLISHED_RULES[3].lower())

    def test_the_same_account_is_a_self_referral_with_no_shared_signal_at_all(self):
        got = sy.decision([], [], same_account=True)
        self.assertEqual(got["reason"], "self_referral")
        self.assertEqual(got["kinds"], ["account"])

    def test_two_wallets_funded_from_one_source_are_one_person(self):
        existing = {"funding:" + sy.hash_("0xswap", SALT, kind="funding"): [
            {"referee": "u-first", "same_referrer": True}]}
        got = sy.decision([sig("funding", "0xswap")], [sig("ip", "203.0.113.1")], existing)
        self.assertEqual(got["state"], "refused")
        self.assertEqual(got["reason"], "duplicate_funding")
        self.assertIn("one person", got["sentence"])

    def test_the_same_funding_source_under_a_different_referrer_is_reviewed_not_refused(self):
        existing = {"funding:" + sy.hash_("0xswap", SALT, kind="funding"): [
            {"referee": "u-other-referrers-referee", "same_referrer": False}]}
        got = sy.decision([sig("funding", "0xswap")], [sig("ip", "203.0.113.9")], existing)
        self.assertEqual(got["state"], "attributed",
                         "a collision across referrers is a farm to look at, not a refusal a legitimate referrer "
                         "cannot argue with")

    def test_a_shared_device_or_ip_is_a_review_and_earns_nothing_until_it_is_cleared(self):
        existing = {"ip:" + sy.hash_("198.51.100.7", SALT, kind="ip"): [
            {"referee": "u-coworker", "same_referrer": True}]}
        got = sy.decision([sig("ip", "198.51.100.7")], [sig("funding", "0xfeed")], existing)
        self.assertEqual(got["state"], "review")
        self.assertEqual(got["reason"], "shared_device_or_ip")
        self.assertEqual(got["kinds"], ["ip"])

    def test_an_address_shared_with_nobody_is_noted_rather_than_refused(self):
        got = sy.decision([sig("ip", "198.51.100.7")], [sig("ip", "198.51.100.7")], {})
        self.assertEqual(got["state"], "attributed")
        self.assertEqual(got["reason"], "shared_ip_only")
        self.assertIn("household", got["sentence"])

    def test_a_clean_attribution_is_attributed(self):
        got = sy.decision([sig("device", "iphone-9"), sig("funding", "0x1")], [sig("funding", "0x2")], {})
        self.assertEqual((got["state"], got["reason"], got["review"]), ("attributed", "", False))

    def test_the_decision_order_is_the_published_order(self):
        # Precedence is data: a self-referral that is ALSO over velocity is still a self-referral, because the
        # consequence (a builder-code revocation ground) is different in kind from a rate limit.
        got = sy.decision([sig("funding", "0xabc")], [sig("funding", "0xabc")], {},
                          attributed_last_hour=99, attributed_last_day=999)
        self.assertEqual(got["reason"], "self_referral")
        self.assertEqual(sy.PRECEDENCE, ("self_referral", "duplicate_funding", "shared_device_or_ip", "velocity"))
        self.assertEqual(rt.TERMS["rules"][3].count("revoking the builder code"), 1)

    def test_velocity_is_a_rate_limit_with_a_sentence_and_a_limit(self):
        hour = sy.velocity(5, 5)
        self.assertTrue(hour["hit"])
        self.assertEqual(hour["scope"], "hour")
        self.assertIn("waits", hour["sentence"])
        day = sy.velocity(1, 25)
        self.assertEqual(day["scope"], "day")
        self.assertFalse(sy.velocity(4, 24)["hit"])
        got = sy.decision([sig("device", "d1")], [], {}, attributed_last_hour=5, attributed_last_day=5)
        self.assertEqual(got["state"], "review")
        self.assertEqual(got["reason"], "velocity")


class TestSignalsAreHashed(unittest.TestCase):
    def test_a_signal_is_never_stored_raw_and_needs_a_salt(self):
        digest = sy.hash_("203.0.113.7", SALT, kind="ip")
        self.assertNotIn("203.0.113.7", digest)
        self.assertTrue(digest.startswith("i_"))
        with self.assertRaises(ValueError):
            sy.hash_("203.0.113.7", "short")
        with self.assertRaises(ValueError):
            sy.hash_("x", SALT, kind="fingerprint")
        self.assertNotEqual(sy.hash_("203.0.113.7", SALT, kind="ip"), sy.hash_("203.0.113.7", "t" * 32, kind="ip"),
                            "two deployments' rows must not join")

    def test_the_same_input_hashes_the_same_way_and_case_does_not_matter(self):
        self.assertEqual(sy.hash_("0xABC", SALT, kind="funding"), sy.hash_(" 0xabc ", SALT, kind="funding"))


class TestClawback(unittest.TestCase):
    def test_unpaid_accruals_go_first_then_what_was_paid(self):
        got = sy.clawback(paid_micro=40_000_000, unpaid_micro=10_000_000, reason="signal_collision")
        self.assertEqual(got["unpaid_micro"], 10_000_000)
        self.assertEqual(got["paid_micro"], 40_000_000)
        self.assertTrue(got["requires_repayment"])
        got = sy.clawback(paid_micro=0, unpaid_micro=120_000_000, reason="self_referral")
        self.assertEqual((got["unpaid_micro"], got["paid_micro"], got["requires_repayment"]),
                         (120_000_000, 0, False))

    def test_under_the_floor_it_is_written_off_rather_than_invoiced(self):
        got = sy.clawback(paid_micro=1_000_000, unpaid_micro=3_000_000, reason="velocity")
        self.assertEqual(got["written_off_micro"], 4_000_000)
        self.assertEqual(got["requires_repayment"], False)
        self.assertIn("written off", got["sentence"])
        self.assertIn("under $5 is written off", " ".join(rt.PUBLISHED_RULES).lower())


class TestCodesAndLinks(unittest.TestCase):
    def test_a_short_code_is_normalised_so_a_lookalike_cannot_be_claimed(self):
        self.assertEqual(rc.normalise("Polymarket-Mike"), "polymarketmike")
        self.assertEqual(rc.validate_short_code("Polymarket-Mike")[0], "polymarketmike")
        self.assertEqual(rc.validate_short_code("m!ked")[0], "mked", "punctuation is stripped, not refused")
        self.assertEqual(rc.validate_short_code("abc")[1].count("4 to 16"), 1)
        self.assertEqual(rc.validate_short_code("x" * 17)[0], "")

    def test_reserved_and_all_digit_codes_are_refused_with_a_reason(self):
        for bad in ("polygm", "Support", "polymarket", "123456"):
            code, why = rc.validate_short_code(bad)
            self.assertEqual(code, "")
            self.assertTrue(why)
        self.assertIn("reserved", rc.validate_short_code("admin")[1])
        self.assertIn("all-digit", rc.validate_short_code("90210")[1])

    def test_a_link_token_is_high_entropy_and_a_code_link_is_human(self):
        token = rc.make_token()
        self.assertTrue(token.startswith("ref_"))
        self.assertEqual(len(token), len("ref_") + 22)
        self.assertNotEqual(token, rc.make_token())
        self.assertNotEqual(rc.make_token(seed="fixed"), rc.make_token(seed="fixed"),
                            "a seed mixes in; it is not the source of the entropy")
        self.assertEqual(rc.link_for(token), "https://polygm.app/r/" + token)
        self.assertEqual(rc.code_link("Polymarket-Mike"), "https://polygm.app/r/c/polymarketmike")
        self.assertEqual(rc.normalise("Poly-Mike"), "polymike")
        with self.assertRaises(ValueError):
            rc.link_for("mike")

    def test_a_click_belongs_to_the_link_it_followed(self):
        # The token wins when both arrive: a code that has changed hands must not steal an attribution from the
        # link the visitor actually clicked.
        self.assertEqual(rc.owner_of_click(token_owner="u-token", code_owner="u-code"), "u-token")
        self.assertEqual(rc.owner_of_click(code_owner="u-code"), "u-code")
        self.assertEqual(rc.owner_of_click(), "")


class TestTheTaxReality(unittest.TestCase):
    def test_the_us_form_and_threshold_are_stated_rather_than_ignored(self):
        under = rt.tax_requirement(100_000_000, country="US")
        self.assertTrue(under["required"])
        self.assertEqual(under["form"], "W-9")
        self.assertEqual(under["reportForm"], "1099-NEC")
        self.assertFalse(under["reportable"])
        self.assertTrue(rt.tax_requirement(700_000_000, country="US")["reportable"])

    def test_everywhere_else_needs_a_form_before_the_first_payout(self):
        got = rt.tax_requirement(700_000_000, country="IN")
        self.assertIn("W-8BEN", got["form"])
        self.assertTrue(got["reportable"])
        self.assertIn("their own return", got["note"])
        self.assertIn("[UNVERIFIED]", rt.TERMS["tax_note"])


if __name__ == "__main__":
    unittest.main()
