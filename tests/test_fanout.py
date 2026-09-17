"""Delivery scheduling: priority that does not starve, at-least-once that does not double-page, and backoff that ends.

These are the four behaviours P05 owes the user experience and that no amount of live venue testing can show,
because they only appear under load or mid-failure. Everything here is a pure function over rows, so the tests
are a table in and a table out.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from polygm_core.signals import fanout as F                                     # noqa: E402


def row(i=0, *, priority=2, status="queued", attempts=0, user="u1", channel="push", queued=1_000,
        signal=None, claim=None):
    return {"signal_id": signal or "sig%d" % i, "user_id": user, "channel": channel, "priority": priority,
            "queued_ms": queued, "status": status, "attempts": attempts,
            **({"claim_ms": claim} if claim is not None else {})}


class TestPlan(unittest.TestCase):
    def test_priority_order_is_paid_first_within_the_same_arrival(self):
        out = F.plan([row(1, priority=2, queued=1, user="a"), row(2, priority=0, queued=1, user="b"),
                      row(3, priority=1, queued=1, user="c")], now_ms=10 ** 9, workers=4)
        self.assertEqual([c["priority"] for c in out["claims"]], [0, 1, 2],
                         "the tier is a queue position, which is all `entitlements.plan` is allowed to buy")

    def test_an_overdue_free_alert_outranks_a_fresh_paid_one(self):
        """Priority is the second sort key, deliberately.

        Strict priority is a starvation machine: while a pro user's rules keep firing, a free user's alert never
        moves, and "your alerts are late" turns into "your alerts are gone". Age is first, so a class can be late
        by a cycle but not indefinitely — which is the same argument the DB's UNIQUE dedupe makes about alerts.
        """
        out = F.plan([row(0, priority=2, queued=1_000), row(1, priority=0, queued=999_000)],
                     now_ms=1_000_000, workers=1)
        self.assertEqual(len(out["claims"]), 1)
        self.assertEqual(out["claims"][0]["priority"], 2, "the 16-minute-old alert is claimed first")
        self.assertEqual(out["waiting"][0]["reason"], "no-worker")

    def test_one_user_cannot_take_the_whole_cycle(self):
        burst = [row(i, priority=0, queued=1, user="u-fat") for i in range(6)] + [row(99, priority=2, queued=2,
                                                                                        user="u-thin")]
        out = F.plan(burst, now_ms=10 ** 9, workers=4, per_user_per_cycle=2)
        by_user = {}
        for c in out["claims"]:
            by_user[c["user_id"]] = by_user.get(c["user_id"], 0) + 1
        self.assertEqual(by_user.get("u-fat"), 2, "the cap is per user per cycle, not per cycle")
        self.assertEqual(by_user.get("u-thin"), 1, "and it is what lets the single alert through at all")
        self.assertEqual({w["reason"] for w in out["waiting"]}, {"per-user-cap"})

    def test_a_paid_user_bursting_does_not_change_another_users_cap(self):
        rows = [row(i, priority=0, user="u%d" % (i % 3), queued=1) for i in range(9)]
        out = F.plan(rows, now_ms=10 ** 9, workers=9, per_user_per_cycle=2)
        self.assertEqual(len(out["claims"]), 6, "three users x two each")

    def test_a_channel_is_a_separate_lane(self):
        """A user with a push and a telegram alert should get both in one cycle: the cap stops one channel's
        burst from delaying another's, and both are wanted."""
        out = F.plan([row(0, user="u1", channel="push"), row(1, user="u1", channel="telegram")],
                     now_ms=10 ** 9, workers=4, per_user_per_cycle=1)
        self.assertEqual(len(out["claims"]), 2)


class TestLeases(unittest.TestCase):
    def test_an_unacked_claim_becomes_claimable_again_and_costs_an_attempt(self):
        """At-least-once, stated honestly: the delivery may happen twice, so the key must be stable.

        A lost lease is not returned to `queued` unchanged — it consumed an attempt. A worker wedged in a send
        would otherwise be reclaimed forever and the queue would look busy while making no progress, which is
        the failure mode an invisible retry counter hides.
        """
        stuck = row(0, status="sending", claim=1_000)
        soon = F.plan([stuck], now_ms=1_000 + 29_000)
        self.assertEqual((soon["claims"], soon["expired_claims"], soon["waiting"][0]["reason"]),
                         ([], [], "leased"))
        late = F.plan([stuck], now_ms=1_000 + 31_000, max_attempts=5)
        self.assertEqual(len(late["expired_claims"]), 1)
        self.assertEqual(late["expired_claims"][0]["status"], "retry")
        self.assertEqual(late["expired_claims"][0]["attempts"], 1)
        self.assertEqual(len(late["claims"]), 1, "and it is immediately available to another worker")

    def test_a_sent_row_is_never_reclaimed(self):
        out = F.plan([row(0, status="sent", claim=1_000)], now_ms=10 ** 12)
        self.assertEqual((out["claims"], out["dead"]), ([], []))
        self.assertEqual(out["queued_total"], 0, "an acked delivery must not be counted as backlog")

    def test_the_idempotency_key_is_stable_across_the_reclaim(self):
        first = F.plan([row(7, status="sending", claim=0)], now_ms=10 ** 9)["expired_claims"]
        claimed = F.plan([row(7)], now_ms=10 ** 9)["claims"]
        self.assertEqual(F.idempotency_key("sig7", "push"), claimed[0]["idempotency_key"])
        self.assertEqual(len(first), 1)

    def test_two_channels_of_one_signal_get_two_keys(self):
        self.assertNotEqual(F.idempotency_key("sig1", "push"), F.idempotency_key("sig1", "telegram"),
                            "one signal may page the same user twice on purpose; a single key would drop one")
        self.assertNotEqual(F.idempotency_key("a", "push"), F.idempotency_key("a\\x1fpush", "push"),
                            "the separator has to be one the data cannot contain")


class TestRetry(unittest.TestCase):
    def test_backoff_grows_doubles_and_stops_at_the_ceiling(self):
        vals = [F.backoff_ms(a, key="k", base_ms=1000, ceiling_ms=8000) for a in range(6)]
        self.assertTrue(all(vals[i] <= vals[i + 1] for i in range(5)), vals)
        self.assertLess(vals[0], 2000)
        self.assertEqual(vals[-1], 8000, "an unbounded backoff is an alert that never arrives, eventually")

    def test_jitter_is_deterministic_per_delivery(self):
        self.assertEqual([F.backoff_ms(3, key="sig-a") for _ in range(3)], [F.backoff_ms(3, key="sig-a")] * 3)
        self.assertNotEqual(F.backoff_ms(3, key="sig-a"), F.backoff_ms(3, key="sig-b"),
                            "two workers recomputing the same retry must agree, and `hash()` would not survive"
                            " a restart (PYTHONHASHSEED)")

    def test_fail_schedules_then_dead_letters_once(self):
        r = row(0)
        steps = []
        for _ in range(5):
            r = F.fail(r, now_ms=0, max_attempts=3, base_ms=100)
            steps.append((r["status"], r["attempts"]))
        self.assertEqual(steps, [("retry", 1), ("retry", 2), ("dead", 3), ("dead", 3), ("dead", 3)],
                         "a dead row must stay dead: re-deriving status from attempts alone would revive it")

    def test_a_failed_row_is_not_due_before_its_backoff(self):
        r = F.fail(row(0), now_ms=100_000, max_attempts=5, base_ms=60_000)
        out = F.plan([dict(r, queued_ms=100_000, backoff_base_ms=60_000)], now_ms=110_000)
        self.assertEqual(out["claims"], [])
        self.assertEqual(out["waiting"][0]["reason"], "backoff")
        later = F.plan([dict(r, queued_ms=100_000, backoff_base_ms=60_000)], now_ms=170_000)
        self.assertEqual(len(later["claims"]), 1, "due at 100_000 + ~60_000 with sub-minute jitter")

    def test_max_attempts_exhaustion_is_reported_as_dead_not_dropped(self):
        out = F.plan([row(0, attempts=5)], now_ms=10 ** 9, max_attempts=5)
        self.assertEqual(out["claims"], [])
        self.assertEqual(out["dead"][0]["dead_reason"], "max_attempts",
                         "the support answer to 'my alerts stopped' is a row, not a log line")


class TestLag(unittest.TestCase):
    def test_lag_is_reported_per_class_because_the_two_meanings_differ(self):
        rows = [row(0, priority=0, queued=900_000), row(1, priority=2, queued=100_000),
                row(2, priority=2, status="sent", queued=1)]
        out = F.lag_ms(rows, now_ms=1_000_000)
        self.assertEqual(out["by_priority"][2], 900_000)
        self.assertEqual(out["by_priority"][0], 100_000)
        self.assertEqual(out["worst_ms"], 900_000)
        self.assertEqual(out["by_priority"][1], 0, "a class with no backlog reads as zero, not as absent"
                                                    " — the ops page must not have to guess which is which")

    def test_no_backlog_is_zero_rather_than_absent(self):
        self.assertEqual(F.lag_ms([], now_ms=10 ** 9), {"worst_ms": 0, "by_priority": {0: 0, 1: 0, 2: 0}})


if __name__ == "__main__":
    unittest.main()
