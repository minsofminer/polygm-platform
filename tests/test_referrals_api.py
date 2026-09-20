"""P11 · D5's API, walked as the phase's own referral criterion.

The gate class at the bottom is the kit's second acceptance sentence: **a referral attempt from a second wallet
funded by the first, and the attempt being caught.** It runs over the real routes, on real rows, with the
evidence the admin queue would see.

What the tests around it pin, in the order somebody meets them:

  1. **A signup earns nothing, and neither does a deposit.** A fresh attribution is `pending`, its referrer's
     earnings are zero, and the accrual run over a day with an order that was never filled writes no row at all.
     This is the model's whole anti-farm argument, and it is asserted rather than described.
  2. **The accrual is a share of a fee we were PAID.** The run reads `fee_micro_observed`; an order whose fee is
     still only *expected* earns the referrer nothing, which is the difference between a funded budget and a
     promise against a projection.
  3. **The three collisions get three different answers** — refused (self), refused (same funding source),
     reviewed (shared device) — and only the reviewed one pauses without cancelling.
  4. **Self-referral is a builder-code revocation ground**, and that is checked on `builder_code_status`, the same
     table the venue's own rejections write to.
  5. **The dashboard's numbers are its own.** The funnel is monotone, the settlement buckets sum to the accruals,
     and the tax form requirement is stated rather than implied.
  6. **One referee, one referral, for ever** — a second application is answered from the record, and a refused
     referral is appealed rather than re-applied.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import unittest

from conftest import import_app, refresh_flags  # noqa: F401

MICRO = 10 ** 6


def _tm_int(cur) -> int:
    """`lastrowid` across both drivers: SQLite sets it on the cursor, so this is the one place that names it."""
    return int(getattr(cur, "lastrowid", 0) or 0)
DAY = 86_400_000
ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")


class ReferralBase(unittest.TestCase):
    app_name = "api-referrals"

    @classmethod
    def setUpClass(cls):
        # The operator token and the signal salt are configuration, so the suite configures them: `_admin` reads
        # the environment at call time, and `_ref_salt` refuses a salt under 16 characters rather than silently
        # hashing signals without one.
        os.environ.setdefault("PGM_ADMIN_TOKEN", "adm_" + "k" * 44)
        os.environ.setdefault("PGM_REFERRAL_SALT", "test-referral-salt-0123456789")

    def setUp(self):
        # A DATABASE PER TEST, which `conftest.import_app` is built to give (it keys the file on the name it is
        # asked for). Two reasons, and neither is tidiness:
        #
        #   * `referral_accruals` is append-only on both engines, so a suite that tidied up after itself would be
        #     blocked by the very trigger that makes the table worth having; and
        #   * two of the D5 routes read the WHOLE table — the day's accrual run and the operator's review queue —
        #     so on a class-wide database `out["accruals"] == 1` would depend on which test ran first.
        self.app = import_app("%s-%s" % (self.app_name, re.sub(r"[^a-z0-9]", "-", self._testMethodName.lower())))
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app.app, raise_server_exceptions=False)
        refresh_flags(self.app)
        self.db = self.app._db
        now = self.app._now_ms()
        self.REF, self.SUB, self.SUB2, self.SUB3, self.PLAIN = "u-ref", "u-sub", "u-sub2", "u-sub3", "u-plain"
        for uid in (self.REF, self.SUB, self.SUB2, self.SUB3, self.PLAIN):
            self.db.execute("INSERT OR IGNORE INTO users (id, stonks_address, created_ms, tier,"
                            " entitlement_until_ms) VALUES (?,NULL,?,'free',0)", (uid, now))
        self.db.commit()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def day_for(ts_ms: int) -> str:
        return datetime.datetime.fromtimestamp(ts_ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")

    def key(self, name):
        return {"Idempotency-Key": "ref-key-%s" % name}

    def admin(self):
        return {"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"]}

    def call(self, method, url, uid=None, body=None, key=None, expect=200, extra=None, params=None):
        headers = dict({"X-User-Id": uid} if uid else {}, **(self.key(key) if key else {}),
                       **(extra or {}))
        kwargs = {"headers": headers}
        if params:
            kwargs["params"] = params
        if method == "post":
            kwargs["json"] = body
        r = getattr(self.client, method)(url, **kwargs)
        self.assertEqual(r.status_code, expect, "%s %s -> %d %s" % (method, url, r.status_code, r.text[:400]))
        return r.json()

    def me(self, uid=None):
        uid = uid or self.REF
        return self.call("get", "/v1/referrals/me", uid=uid)

    def link(self, uid=None):
        uid = uid or self.REF
        return self.me(uid)["link"]

    def apply(self, uid, code, key, **extra):
        body = {"code": code, **extra}
        headers = {"X-User-Id": uid, **self.key(key)}
        r = self.client.post("/v1/referrals/apply", headers=headers, json=body)
        self.assertIn(r.status_code, (200, 409, 422), r.text[:300])
        return r

    def attribute(self, uid, code="", key=None, **extra):
        """A clean attribution, through the route, and a helper that fails loudly if it is not clean."""
        got = self.apply(uid, code or self.link()["token"], key or ("apply-%s" % uid), **extra)
        self.assertEqual(got.status_code, 200, got.text[:300])
        return got.json()

    def qualify(self, referee, days_ago=1, notional=40 * MICRO):
        """Give a referee the qualifying order, as the database would hold it after a matched fill."""
        row = self.db.execute("SELECT signed_up_ms, state FROM referral_attributions WHERE referee=?",
                              (referee,)).fetchone()
        # Never before the signup: 0016 CHECKs `qualify_ms = 0 OR qualify_ms >= signed_up_ms`, and an order that
        # predates the account that placed it is not a scenario worth testing — it is a bug in the fixture.
        self.assertIsNotNone(row, "the referee must be attributed before they can qualify")
        at = max(self.app._now_ms() - days_ago * DAY, row[0] + 1000)
        # The qualifying order goes in; the STATE stays as the route left it. A referee under review who places a
        # matched order is the exact case the review exists for, so flipping them to `qualified` here would test
        # the fixture instead of the rule.
        state = "qualified" if str(row[1]) == "pending" else str(row[1])
        self.db.execute("UPDATE referral_attributions SET state=?, qualify_order=?, qualify_ms=?,"
                        " notional_micro=?, term_ends_ms=? WHERE referee=?", (state, "0xorder-%s" % referee, at,
                                                                             notional,
                                                                             at + 365 * DAY, referee))
        self.db.commit()
        return at

    def fee(self, referee, at, observed_micro, *, expected_micro=None):
        """One attributable order: `fee_micro_observed` is what the venue paid us."""
        self.db.execute("INSERT INTO builder_attribution (order_id, intent_id, user_id, builder_code,"
                        " fee_bps_expected, fee_micro_expected, fee_micro_observed, market_id, token_id,"
                        " placed_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        ("0xorder-%s" % referee, "0xintent-%s" % referee, referee, "polygm-referral", 25,
                         expected_micro if expected_micro is not None else observed_micro, observed_micro,
                         "0xm", "0xt", at))
        self.db.commit()

    def open_review(self, kind, referee, findings=("the two wallets share a funding source",)):
        """The queue item a person has to decide, planted the way the accrual job plants one.

        Opened directly rather than through a route, because every route that opens a queue item ALSO changes the
        referee's state — a shared device holds the accrual until a person clears it — and this test is about the
        DECISION's arithmetic (what a clawback cancels and what it asks for) rather than about how the item got
        there. `TestTheRewardOnlyExistsIfTheFeeDid` covers the route-opened review.
        """
        at = self.app._now_ms()
        cur = self.db.execute("INSERT INTO referral_reviews (kind, subject, referee, state, findings_json,"
                              " decision, actor, opened_ms, decided_ms) VALUES (?,?,?,'open',?,'','',?,0)",
                              (str(kind), self.REF, str(referee), json.dumps(list(findings)), at))
        self.db.commit()
        return _tm_int(cur)

    def accrue(self, day, key=None):
        return self.call("post", "/v1/referrals/accrue", body={"day": day}, key=key or ("accrue-%s" % day),
                         extra=self.admin())


class TestTheModelAndTheTerms(ReferralBase):
    def test_the_published_terms_are_the_engine_s_constants(self):
        out = self.call("get", "/v1/referrals/terms")
        terms = out["terms"]
        import polygm_core.referrals.terms as rt
        self.assertEqual(terms["shareBps"], rt.SHARE_BPS)
        self.assertEqual(terms["qualifyNotionalMicro"], rt.QUALIFY_NOTIONAL_MICRO)
        self.assertEqual(terms["termDays"], rt.TERM_DAYS)
        self.assertEqual(terms["payoutMinMicro"], rt.PAYOUT_MIN_MICRO)
        self.assertEqual(terms["model"], "builder-fee share")
        self.assertIn("observed", terms["paidFrom"])
        # The rejected alternatives are served WITH their reasons, because "why not a bounty" is the first
        # question a referrer asks and the answer is the argument the model rests on.
        self.assertIn("flat bounty on a first funded trade", terms["rejectedModels"])
        self.assertIn("deposit-size reward", terms["rejectedModels"])
        self.assertIn("Pro credit", terms["rejectedModels"])

    def test_the_rules_are_published_including_the_clawback(self):
        rules = " ".join(self.call("get", "/v1/referrals/terms")["terms"]["rules"])
        self.assertIn("claw", rules.lower())
        self.assertIn("no second level", rules)
        self.assertIn("funded from the same source", rules)
        self.assertIn("revoking the builder code", rules)
        self.assertIn("$20", rules)

    def test_there_is_no_public_referrer_leaderboard_and_the_position_is_served(self):
        terms = self.call("get", "/v1/referrals/terms")["terms"]
        self.assertIn("spam contest", terms["noReferrerLeaderboard"])


class TestTheFunnelAndTheLink(ReferralBase):
    def test_the_link_is_minted_on_first_read_and_is_stable_after(self):
        first = self.link()
        self.assertTrue(first["token"].startswith("ref_"))
        self.assertTrue(first["url"].endswith(first["token"]))
        self.assertTrue(first["created"])
        second = self.link()
        self.assertEqual(second["token"], first["token"])
        self.assertFalse(second["created"], "the second read must not mint a second link")
        self.assertEqual(second["code"], "", "a short code is chosen, never issued")

    def test_a_short_code_is_chosen_validated_and_collision_refused(self):
        got = self.call("post", "/v1/referrals/code", uid=self.REF, body={"code": "Poly-Market-Mike"}, key="code-1")
        self.assertEqual(got["code"], "polymarketmike")
        self.assertTrue(got["shortUrl"].endswith("/c/polymarketmike"))
        self.assertEqual(self.link()["code"], "polymarketmike")
        # A reserved word, a too-short code and an all-digit code are refused with a sentence.
        # Each refusal is a 422 with a code of its own and the ENGINE's sentence, which says which rule broke.
        # The sentence never echoes what was typed — asserted here, because "invalid (code)" is what a user gets
        # when somebody decides echoing input into a response body is easier than writing the rule down.
        for bad, needle in (("admin", "reserved"), ("abc", "4 to 16"), ("90210", "all-digit")):
            body = self.call("post", "/v1/referrals/code", uid=self.REF, body={"code": bad}, key="bad-%s" % bad,
                             expect=422)
            self.assertEqual(body["error"]["code"], "CODE_INVALID", bad)
            self.assertIn(needle, body["error"]["message"])
            self.assertNotIn(bad, body["error"]["message"], "the refusal must not repeat what was typed")
        # Another account cannot take it.
        body = self.call("post", "/v1/referrals/code", uid=self.SUB, body={"code": "POLY-MARKET-MIKE"}, key="code-2",
                         expect=409)
        self.assertEqual(body["error"]["code"], "CODE_TAKEN")
        # Rotating retires the old one: exactly one active short code per account.
        got = self.call("post", "/v1/referrals/code", uid=self.REF, body={"code": "mike2"}, key="code-3")
        self.assertEqual(got["previous"], "polymarketmike")
        active = self.db.execute("SELECT code FROM referral_links WHERE user_id=? AND kind='short'"
                                 " AND state='active'", (self.REF,)).fetchall()
        self.assertEqual([r[0] for r in active], ["mike2"])

    def test_the_funnel_is_monotone_and_counts_the_money_steps_separately(self):
        self.attribute(self.SUB)
        self.attribute(self.SUB2)
        self.qualify(self.SUB)
        out = self.me()
        self.assertEqual(out["funnel"]["signups"], 2)
        self.assertEqual(out["funnel"]["funded"], 1)
        self.assertEqual(out["funnel"]["trading"], 0)
        self.assertEqual(out["funnel"]["earned"], 0)
        self.assertEqual(out["funnelFindings"], [])
        self.assertIn("matched order", out["funnelMeaning"]["funded"])
        # A click count below the signup count is NOT a finding: a code shouted on a stream produces signups
        # with no click, and an invariant that fired on that would be switched off rather than fixed.
        self.assertEqual(out["funnel"]["clicks"], 0)
        self.assertEqual(out["funnelFindings"], [])

    def test_the_dashboard_counts_a_referee_once_and_keeps_the_reason(self):
        self.attribute(self.SUB)
        out = self.me()
        row = out["referrals"][0]
        self.assertEqual(row["state"], "pending")
        self.assertIn("has not placed a matched order", row["stateText"])
        self.assertEqual(row["earnedMicro"], 0)
        self.assertNotRegex(str(out), ADDRESS.pattern, "no address may appear anywhere in a dashboard")
        self.assertIn("no wallet linked yet", row["referee"], "a referee without a wallet is named honestly")


class TestTheRewardOnlyExistsIfTheFeeDid(ReferralBase):
    def test_a_pending_referee_accrues_nothing_even_when_the_order_stands(self):
        self.attribute(self.SUB)
        self.fee(self.SUB, self.app._now_ms() - 1000, 10 * MICRO)
        out = self.accrue(self.day_for(self.app._now_ms() - 1000))
        self.assertEqual((out["accruals"], out["shareMicro"]), (0, 0))
        self.assertIn("a referral under review accrues nothing", out["note"])

    def test_the_accrual_is_a_share_of_the_fee_we_were_paid(self):
        self.attribute(self.SUB)
        at = self.qualify(self.SUB, days_ago=1)
        self.fee(self.SUB, at + 3600_000, 10 * MICRO, expected_micro=40 * MICRO)
        day = self.day_for(at + 3600_000)
        out = self.accrue(day)
        self.assertEqual(out["accruals"], 1)
        self.assertEqual(out["observedMicro"], 10 * MICRO)
        self.assertEqual(out["shareMicro"], int(10 * MICRO * 0.25))
        row = self.db.execute("SELECT fee_observed_micro, share_bps, share_micro FROM referral_accruals").fetchone()
        self.assertEqual(tuple(row), (10 * MICRO, 2500, 2_500_000))
        # …and it is the OBSERVED fee, not the expected one: the row proves which column it read.
        self.assertNotEqual(row[0], 40 * MICRO)

    def test_an_order_whose_fee_has_not_been_paid_yet_earns_nothing(self):
        self.attribute(self.SUB)
        at = self.qualify(self.SUB, days_ago=1)
        self.db.execute("INSERT INTO builder_attribution (order_id, intent_id, user_id, builder_code,"
                        " fee_bps_expected, fee_micro_expected, fee_micro_observed, market_id, token_id,"
                        " placed_ms) VALUES ('0xunpaid','0xi',?,'polygm-referral',25,?,NULL,'0xm','0xt',?)",
                        (self.SUB, 9 * MICRO, at + 1000))
        self.db.commit()
        out = self.accrue(self.day_for(at + 1000))
        self.assertEqual(out["accruals"], 0)

    def test_a_re_run_for_the_same_day_pays_nothing_twice(self):
        self.attribute(self.SUB)
        at = self.qualify(self.SUB, days_ago=1)
        self.fee(self.SUB, at + 1000, 8 * MICRO)
        day = self.day_for(at + 1000)
        first = self.accrue(day, key="accrue-a")
        second = self.accrue(day, key="accrue-b")
        self.assertEqual(first["accruals"], 1)
        self.assertEqual(second["accruals"], 0)
        self.assertEqual(second["skipped"], 1)
        total = self.db.execute("SELECT COUNT(*), COALESCE(SUM(share_micro),0) FROM referral_accruals").fetchone()
        self.assertEqual((total[0], total[1]), (1, 2_000_000))

    def test_the_earnings_buckets_sum_to_the_accruals_and_the_hold_is_real(self):
        self.attribute(self.SUB)
        at = self.qualify(self.SUB, days_ago=1)
        self.fee(self.SUB, at + 1000, 40 * MICRO)
        self.accrue(self.day_for(at + 1000))
        out = self.me()
        e = out["earnings"]
        self.assertEqual(e["accruedMicro"], 10 * MICRO)
        self.assertEqual(e["settledMicro"] + e["holdingMicro"], e["accruedMicro"])
        self.assertEqual(e["payableMicro"], 0, "inside the settlement hold and below the $20 minimum")
        # $20 still to come, not $10: nothing has left the hold yet, so `to_minimum` measures the gap between the
        # PAYABLE balance and the minimum rather than between the minimum and everything ever accrued.
        self.assertEqual(e["toMinimumMicro"], 20 * MICRO)
        self.assertIn("settlement hold", e["note"])

    def test_the_payout_page_states_the_schedule_the_minimum_and_the_tax_form(self):
        out = self.me()
        p = out["payout"]
        self.assertEqual(p["minimumMicro"], 20 * MICRO)
        self.assertIn("monthly", p["schedule"])
        self.assertTrue(p["tax"]["required"])
        self.assertTrue(p["tax"]["form"])
        self.assertIn("1099-NEC", p["tax"]["reportForm"])
        self.assertGreater(p["nextAtMs"], self.app._now_ms() - 40 * DAY)
        self.assertIn("[UNVERIFIED]", out["terms"]["taxNote"])

    def test_a_referral_under_review_accrues_nothing_and_loses_nothing(self):
        self.attribute(self.SUB, device="iphone-1")
        held = self.attribute(self.SUB2, device="iphone-1")
        self.assertEqual(held["state"], "review")
        self.qualify(self.SUB2, days_ago=1)
        at = self.app._now_ms() - DAY
        self.fee(self.SUB2, at + 1000, 40 * MICRO)
        day = self.day_for(at + 1000)
        self.assertEqual(self.accrue(day)["accruals"], 0)
        self.assertEqual(self.me()["review"]["open"], 1)
        # Clearing it puts the referral back to qualified, and the next run accrues the day it missed.
        item = self.call("get", "/v1/referrals/review", extra=self.admin())
        got = self.call("post", "/v1/referrals/review", key="rev-clear", extra=self.admin(),
                        body={"id": item["items"][0]["id"], "decision": "clear", "reason": "coworkers, confirmed"})
        self.assertEqual(got["state"], "cleared")
        self.assertEqual(self.accrue(day, key="accrue-after-clear")["accruals"], 1)
        self.assertEqual(self.me()["earnings"]["accruedMicro"], 10 * MICRO)


class TestTheThreeCollisionsOverTheApi(ReferralBase):
    def test_the_gate_pair_a_second_wallet_funded_by_the_first_is_caught(self):
        # The kit's sentence, over the routes: u-sub refers from a funding source, then u-sub2 arrives from the
        # SAME source. Same source means one person, so the second attempt is refused and the referrer's own
        # dashboard still shows exactly one attribution.
        self.attribute(self.SUB, funding="0xfundshared")
        second = self.apply(self.SUB2, self.link()["token"], "gate-2", funding="0xfundshared")
        self.assertEqual(second.status_code, 409, second.text[:200])
        body = second.json()
        self.assertEqual(body["error"]["code"], "REFUSED")
        self.assertIn("one person", body["error"]["message"])
        self.assertEqual(len(self.me()["referrals"]), 1)
        self.assertEqual(self.me()["review"]["open"], 1, "the collision is on the queue, not silently dropped")
        queued = self.call("get", "/v1/referrals/review",
                           extra=self.admin())
        self.assertEqual(queued["items"][0]["kind"], "duplicate_funding")

    def test_a_shared_device_is_reviewed_rather_than_refused(self):
        self.attribute(self.SUB, device="macbook-7")
        got = self.attribute(self.SUB2, device="macbook-7")
        self.assertEqual(got["state"], "review")
        self.assertEqual(got["reason"], "shared_device_or_ip")
        self.assertIn("person looks at this", got["sentence"])

    def test_self_referral_is_refused_and_revokes_the_builder_code(self):
        body = self.apply(self.REF, self.link()["token"], "self-1").json()
        self.assertEqual(body["error"]["code"], "SELF_REFERRAL")
        self.assertIn("cannot refer itself", body["error"]["message"])
        row = self.db.execute("SELECT state, source, note FROM builder_code_status WHERE code='polygm-referral'"
                              ).fetchone()
        self.assertEqual(row[0], "disabled")
        self.assertEqual(row[1], "manual")
        self.assertIn("self-referral", row[2])
        # The hard block is the schema's too: no attribution row points a referee at themselves.
        self.assertIsNone(self.db.execute("SELECT referee FROM referral_attributions WHERE referee=?",
                                          (self.REF,)).fetchone())
        # …and the queue has it, so an operator sees the revocation ground rather than a mystery disable.
        queued = self.call("get", "/v1/referrals/review",
                           extra=self.admin())
        self.assertEqual(queued["items"][0]["kind"], "self_referral")
        self.assertIn("revocation", queued["note"])

    def test_two_accounts_linked_to_one_wallet_are_a_self_referral(self):
        import sqlite3
        at = self.app._now_ms()
        # One address belongs to one account: `user_identities` is UNIQUE (kind, value), so the identity route
        # into two accounts is closed by the schema and asserted here rather than assumed.
        self.db.execute("INSERT INTO user_identities (kind, value, user_id, state, claimed_ms, verified_ms,"
                        " proof_kind, revoked_ms) VALUES ('wallet','0xshared',?,'claimed',?,NULL,'',NULL)",
                        (self.REF, at))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO user_identities (kind, value, user_id, state, claimed_ms, verified_ms,"
                            " proof_kind, revoked_ms) VALUES ('wallet','0xshared',?,'claimed',?,NULL,'',NULL)",
                            (self.SUB, at))
        # …and the deposit address, which carries no UNIQUE constraint, is the second look: two accounts pointed
        # at one proxy wallet are one person, whatever the identity rows say.
        self.db.execute("UPDATE users SET stonks_address='0xSharedWallet' WHERE id IN (?,?)", (self.REF, self.SUB))
        self.db.commit()
        body = self.apply(self.SUB, self.link()["token"], "linked-1").json()
        self.assertEqual(body["error"]["code"], "SELF_REFERRAL")
        self.assertIn("account", body["error"]["message"])
        self.assertIsNone(self.db.execute("SELECT referee FROM referral_attributions WHERE referee=?",
                                          (self.SUB,)).fetchone(), "the schema is the hard block")

    def test_velocity_is_a_rate_limit_on_the_referrer(self):
        import polygm_core.referrals.sybil as sy
        self.attribute(self.SUB)
        now = self.app._now_ms()
        for i in range(sy.MAX_PER_HOUR):
            filler = "u-filler-%d" % i
            self.db.execute("INSERT OR IGNORE INTO users (id, stonks_address, created_ms, tier,"
                            " entitlement_until_ms) VALUES (?,NULL,?,'free',0)", (filler, now))
            self.db.execute("INSERT INTO referral_attributions (referee, referrer, code, token, state, reason,"
                            " signed_up_ms, qualify_order, qualify_ms, notional_micro, term_ends_ms,"
                            " builder_code, decided_ms) VALUES (?,?,?,'','pending','',?,'',0,0,0,?,0)",
                            (filler, self.REF, "", now - 600_000, "polygm-referral"))
        self.db.commit()
        got = self.attribute(self.SUB3)
        self.assertEqual(got["state"], "review")
        self.assertEqual(got["reason"], "velocity")
        self.assertIn("waits", got["sentence"])

    def test_one_referee_is_referred_once_and_a_refusal_is_appealed_not_re_applied(self):
        self.attribute(self.SUB, funding="0xfundshared")
        self.apply(self.SUB2, self.link()["token"], "gate-2", funding="0xfundshared")
        again = self.apply(self.SUB2, self.link()["token"], "gate-3")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["error"]["code"], "ALREADY_REFERRED")
        self.assertIn("appealed", again.json()["error"]["message"])
        # And a wallet that applied cleanly cannot be re-attributed to a different referrer either.
        self.call("post", "/v1/referrals/code", uid=self.PLAIN, body={"code": "othercode"}, key="code-other")
        code = self.link(self.PLAIN)["code"]
        other = self.apply(self.SUB, code, "steal-1")
        self.assertEqual(other.status_code, 409)
        self.assertEqual(other.json()["error"]["code"], "ALREADY_REFERRED")


class TestTheReviewQueueAndTheClawback(ReferralBase):
    def test_the_queue_is_operator_only_and_the_decision_is_on_the_record(self):
        unauth = self.client.get("/v1/referrals/review", headers={"X-Admin-Token": "nope"})
        self.assertEqual(unauth.status_code, 403)
        no_token = self.client.get("/v1/referrals/review")
        self.assertEqual(no_token.status_code, 503, "an unconfigured operator token is a misconfiguration, not an attack")
        self.attribute(self.SUB, device="d1")
        self.attribute(self.SUB2, device="d1")
        item = self.call("get", "/v1/referrals/review", extra=self.admin())["items"][0]
        self.assertTrue(item["findings"], "a queue row with no sentence is a row nobody can act on")
        got = self.call("post", "/v1/referrals/review", key="ex-1", extra=self.admin(),
                        body={"id": item["id"], "decision": "exclude", "reason": "same person, confirmed"})
        self.assertEqual(got["state"], "actioned")
        left = self.call("get", "/v1/referrals/review", extra=self.admin())
        self.assertEqual(left["count"], 0)
        done = self.call("get", "/v1/referrals/review", params={"state": "actioned"}, extra=self.admin())
        self.assertEqual(done["items"][0]["decision"], "exclude")
        self.assertEqual(done["items"][0]["actor"], "operator")

    def test_a_clawback_cancels_unpaid_accruals_first_and_publishes_the_rule(self):
        self.attribute(self.SUB)
        self.attribute(self.SUB2)
        for uid in (self.SUB, self.SUB2):
            self.qualify(uid, days_ago=60)
        at = self.app._now_ms() - 60 * DAY
        self.fee(self.SUB, at + 1000, 40 * MICRO)
        self.fee(self.SUB2, at + 2000, 40 * MICRO)
        self.accrue(self.day_for(at + 1000))
        self.assertEqual(self.me()["earnings"]["accruedMicro"], 20 * MICRO)
        review_id = self.open_review("duplicate_funding", self.SUB2)
        got = self.call("post", "/v1/referrals/review", key="claw-1", extra=self.admin(),
                        body={"id": review_id, "decision": "claw_back", "reason": "the two wallets are one person"})
        self.assertEqual(got["clawback"]["unpaidMicro"], 10 * MICRO)
        self.assertFalse(got["clawback"]["requiresRepayment"], "nothing was paid yet")
        # …and the code survives: this is a duplicate-funding clawback, not a self-referral, and `polygm-referral`
        # is the code every referrer is attributed under.
        code_row = self.db.execute("SELECT state FROM builder_code_status WHERE code=?",
                                   (self.app._REF_BUILDER_CODE,)).fetchone()
        self.assertNotEqual(str(code_row[0]) if code_row else "", "disabled")
        # The accrual row survives (the table is append-only) and the attribution carries the state: a clawback
        # that deleted its own evidence would be unauditable.
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM referral_accruals").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT state FROM referral_attributions WHERE referee=?",
                                         (self.SUB2,)).fetchone()[0], "clawed_back")
        out = self.me()
        self.assertEqual(out["earnings"]["clawedBackMicro"], 10 * MICRO)
        self.assertEqual(out["funnel"]["earned"], 1, "the clawed-back referee is no longer counted as earning")
        self.assertEqual(out["funnelFindings"], [])

    def test_an_unknown_item_and_a_decision_without_a_reason_are_refused(self):
        self.assertEqual(self.client.post("/v1/referrals/review", headers={**self.admin(), **self.key("x1")},
                                          json={"id": 999, "decision": "clear", "reason": "nope"}).status_code, 404)
        bad = self.client.post("/v1/referrals/review", headers={**self.admin(), **self.key("x2")},
                               json={"id": 1, "decision": "clear", "reason": "no"})
        self.assertEqual(bad.status_code, 422)
        bad2 = self.client.post("/v1/referrals/review", headers={**self.admin(), **self.key("x3")},
                                json={"id": 1, "decision": "vibes", "reason": "because"})
        self.assertEqual(bad2.status_code, 422)

    def test_the_accrual_run_is_operator_only_and_refuses_a_future_day(self):
        no_token = self.client.post("/v1/referrals/accrue", headers=self.key("a1"), json={"day": "2026-01-01"})
        self.assertIn(no_token.status_code, (403, 503))
        future = self.client.post("/v1/referrals/accrue", headers={**self.admin(), **self.key("a2")},
                                  json={"day": "2099-01-01"})
        self.assertEqual(future.status_code, 422)
        # Operator-facing, so the sentence stays in the log against the request id and the body names the field:
        # there is no browser to help here, and the on-call has the log line.
        self.assertIn("(day)", future.json()["error"]["message"])

    def test_idempotency_and_session_enforcement_on_every_write(self):
        # Missing key: 400, and the shape is the API's own contract.
        for url, body in (("/v1/referrals/code", {"code": "mike"}), ("/v1/referrals/apply", {"code": "x"})):
            r = self.client.post(url, headers={"X-User-Id": self.REF}, json=body)
            self.assertEqual(r.status_code, 400, url)
            self.assertEqual(r.json()["error"]["code"], "IDEM_KEY_REQUIRED")
        # No session: 401 for the dashboard and both writes.
        for url in ("/v1/referrals/me",):
            self.assertEqual(self.client.get(url).status_code, 401)
        for url, body in (("/v1/referrals/code", {"code": "mike2"}), ("/v1/referrals/apply", {"code": "ref_x"})):
            self.assertEqual(self.client.post(url, headers=self.key("n"), json=body).status_code, 401, url)
        # A replay returns the first answer; a different body under the same key conflicts.
        first = self.call("post", "/v1/referrals/code", uid=self.REF, body={"code": "mike2"}, key="replay-1")
        again = self.call("post", "/v1/referrals/code", uid=self.REF, body={"code": "mike2"}, key="replay-1")
        self.assertEqual(again["code"], first["code"])
        conflict = self.call("post", "/v1/referrals/code", uid=self.REF, body={"code": "mikethree"}, key="replay-1",
                             expect=409)
        self.assertEqual(conflict["error"]["code"], "IDEM_CONFLICT")

    def test_a_code_that_resolves_to_nobody_is_a_422_naming_the_field(self):
        got = self.apply(self.SUB, "nobodyhere", "unknown-1")
        self.assertEqual(got.status_code, 422)
        self.assertIn("(code)", got.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
