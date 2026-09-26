"""The fee ramp's rules, tested against the venue mechanics they exist to obey.

The phase's most consequential product decision is a number other people pay: `builder_bps`. The kit sequences it
(launch at zero, raise only after retention holds, 10-25 bps, one change per seven days, three days of notice, one
pending change at a time, publicly queryable), and every one of those constraints is a refusal this module has to
be able to make. A test file that only checked "0 bps at launch" would leave six of them untested.
"""
from __future__ import annotations
import json
import pathlib
import unittest

from polygm_core.revenue import schedule as sched

ROOT = pathlib.Path(__file__).resolve().parents[1]
DAY = sched.DAY_MS
LAUNCH = 1_800_000_000_000        # an arbitrary fixed launch instant; nothing here depends on the real clock


def steps():
    return sched.load_steps()


def change(bps, *, effective_in_days=10, announced_days_ago=0, now=None):
    now = LAUNCH + 30 * DAY if now is None else now
    return sched.Change(bps=bps, effective_ms=now + effective_in_days * DAY,
                        announced_ms=now - announced_days_ago * DAY)


def history(*entries):
    """Applied changes, oldest first. Each entry is (bps, days_after_launch_when_effective)."""
    return [sched.Change(bps=bps, effective_ms=LAUNCH + d * DAY, announced_ms=LAUNCH + (d - 10) * DAY)
            for bps, d in entries]


class TestTheRampItself(unittest.TestCase):
    def test_launch_is_zero_bps(self):
        steps_ = steps()
        self.assertEqual(steps_[0].bps, 0, "the kit's sequence: launch at 0 bps, buy volume share not margin")
        self.assertEqual(sched.current_bps(steps_, LAUNCH + 5 * DAY, LAUNCH), 0)

    def test_the_ceiling_is_the_kits_ceiling(self):
        self.assertEqual(sched.MAX_BPS, 2500)
        self.assertLessEqual(max(s.bps for s in steps()), sched.MAX_BPS)

    def test_every_step_after_launch_states_its_condition(self):
        for step in steps()[1:]:
            self.assertTrue(step.requires.strip(), "%s raises the rate with no condition" % step.id)

    def test_steps_are_at_least_seven_days_apart(self):
        steps_ = steps()
        for a, b in zip(steps_, steps_[1:]):
            self.assertGreaterEqual(b.at_day - a.at_day, 7)

    def test_the_rate_before_launch_is_the_launch_rate(self):
        """Before launch there is no live rate; answering '0' is the honest answer, and it must not read as a
        schedule that applied early."""
        self.assertEqual(sched.current_bps(steps(), LAUNCH - DAY, LAUNCH), 0)

    def test_the_ramp_arrives_step_by_step(self):
        steps_ = steps()
        self.assertEqual(sched.current_bps(steps_, LAUNCH + 89 * DAY, LAUNCH), 0)
        self.assertEqual(sched.current_bps(steps_, LAUNCH + 90 * DAY, LAUNCH), 1000)
        self.assertEqual(sched.current_bps(steps_, LAUNCH + 180 * DAY, LAUNCH), 2500)

    def test_validate_flags_an_implausible_ramp(self):
        bad = [sched.Step("launch", 0, 500, ""), sched.Step("jump", 3, 2500, ""),
               sched.Step("over", 60, 4000, "gate met")]
        problems = sched.validate_steps(bad)
        joined = " | ".join(problems)
        self.assertIn("launch at 0", joined, "a ramp that does not start at zero must be refused")
        self.assertIn("one change per 7 days", joined)
        self.assertIn("above the 2500 bps ceiling", joined)

    def test_validate_passes_the_committed_ramp(self):
        self.assertEqual(sched.validate_steps(steps()), [])


class TestTheVenueMechanics(unittest.TestCase):
    def test_seven_day_spacing_is_enforced(self):
        """The change is built AT the clock the review is given. The first version of this test built it at the
        helper's default day (30) and reviewed it at day 12, so the spacing was 23 days - legal - and the test
        failed while the rule it was testing worked. Stating one clock in two places is the bug; `now=` fixes it."""
        now = LAUNCH + 12 * DAY
        reasons = sched.review(change(1000, effective_in_days=3, now=now), history=history((0, 10)),
                               retention_ok=True, now_ms=now)
        self.assertTrue(any("one change per 7 days" in r for r in reasons), reasons)

    def test_three_days_notice_is_enforced(self):
        reasons = sched.review(change(1000, effective_in_days=1), history=(), retention_ok=True)
        self.assertTrue(any("advance notice" in r for r in reasons), reasons)

    def test_one_pending_change_at_a_time(self):
        future = sched.Change(bps=1000, effective_ms=LAUNCH + 40 * DAY, announced_ms=LAUNCH + 30 * DAY)
        reasons = sched.review(change(1500, effective_in_days=20), history=[future], retention_ok=True,
                               now_ms=LAUNCH + 31 * DAY)
        self.assertTrue(any("pending change at a time" in r for r in reasons), reasons)

    def test_an_increase_without_retention_is_refused(self):
        reasons = sched.review(change(1000), history=(), retention_ok=False)
        self.assertTrue(any("requires retention to hold" in r for r in reasons), reasons)

    def test_a_cut_is_allowed_without_retention(self):
        """The asymmetry is deliberate: the one rate change that is never against the user is a cut."""
        reasons = sched.review(change(0), history=history((1000, 10)), retention_ok=False, now_ms=LAUNCH + 60 * DAY)
        self.assertEqual(reasons, [], "a fee cut must not need a retention gate")

    def test_a_cut_still_obeys_the_venue(self):
        reasons = sched.review(change(0, effective_in_days=1), history=history((1000, 10)), retention_ok=True,
                               now_ms=LAUNCH + 12 * DAY)
        self.assertTrue(any("advance notice" in r for r in reasons),
                        "the venue's clock is not ours to skip, even when we are lowering a fee")

    def test_a_legal_change_passes_cleanly(self):
        reasons = sched.review(change(1000, effective_in_days=10), history=(), retention_ok=True)
        self.assertEqual(reasons, [])

    def test_negative_and_over_ceiling_rates_are_refused(self):
        self.assertTrue(sched.review(change(-1), history=(), retention_ok=True))
        self.assertTrue(any("ceiling" in r for r in sched.review(change(9999), history=(), retention_ok=True)))

    def test_each_refusal_names_the_rule_and_the_numbers(self):
        """A refusal that says 'invalid' teaches nobody anything; each message must carry the constraint and the
        value that broke it."""
        for forced in (change(1000, effective_in_days=3), change(1000, effective_in_days=1),
                       change(9999, effective_in_days=10)):
            for reason in sched.review(forced, history=history((0, 10)) if forced.bps == 1000 else [],
                                       retention_ok=True, now_ms=LAUNCH + 12 * DAY):
                self.assertTrue(any(ch.isdigit() for ch in reason), reason)


class TestTheConfigIsTheSingleSource(unittest.TestCase):
    def test_the_ramp_comes_from_config_gtm_json(self):
        raw = json.loads((ROOT / "config" / "gtm.json").read_text())
        self.assertEqual([s.id for s in steps()],
                         [s["id"] for s in raw["monetisation"]["steps"]],
                         "load_steps must not have its own copy of the ramp")

    def test_the_mechanics_match_the_kits_numbers(self):
        m = sched.load_mechanics()
        self.assertEqual(m["min_days_between_changes"], 7)
        self.assertEqual(m["advance_notice_days"], 3)
        self.assertTrue(m["one_pending_change_at_a_time"])
        self.assertTrue(m["publicly_queryable"], "the venue publishes rates; our plan says so out loud")

    def test_describe_reads_like_a_sentence(self):
        line = sched.describe(steps(), LAUNCH, LAUNCH + 100 * DAY)
        self.assertIn("1000 bps", line)
        self.assertIn("2500 bps", line, "the next step and its condition must be in the line")


if __name__ == "__main__":
    unittest.main()
