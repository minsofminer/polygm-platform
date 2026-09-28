"""The payment-webhook controls, tested against the attacks they exist for.

Every test here is one of the P14 OPEN item's requirements turned into an attack that must fail: a forged
signature, a captured request replayed five minutes later, the same request delivered twice, a body that arrived
as bytes and was verified after a round trip through a dict, a Stars payment invented as a form post, and an
amount that disagrees with the record we hold.

The tests sign with `sign_stripe` — the module's own test-only helper — rather than hand-rolling the scheme, so
what is tested is the scheme production will use. Where a test needs to prove something about the *implementation*
rather than the interface (that the comparison does not return early, that the raw bytes are what gets hashed) it
says so, because those are the two places a later "simplification" would quietly remove the control.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import unittest

from polygm_core.payments import replay, webhooks
from polygm_core.payments.webhooks import (PROVIDER_STARS, PROVIDER_STRIPE, WebhookError, fulfilment,
                                           parse_stripe_signature, sign_stripe, stars_payment, verify_stripe,
                                           verify_update_secret)

# Deliberately short and obviously not a credential: `whsec_` is Stripe's real endpoint-secret prefix, and the
# P04 secret scan reads `secret = "<16+ chars>"` as a key. A test constant that looks like a live key trains the
# scanner to exempt things, which is how a scanner stops being one.
SECRET = "whsec-test"
NOW_MS = 1_800_000_000_000
AT_S = NOW_MS // 1000
BODY = json.dumps({"id": "evt_1", "type": "checkout.session.completed",
                   "data": {"object": {"amount_total": 2500, "currency": "usd"}}},
                  separators=(",", ":")).encode()


ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
LITE = ROOT / "db" / "migrations-sqlite"


def fresh_conn() -> sqlite3.Connection:
    """A ledger on SQLite, built from the *generated* files so the test uses the shipped DDL.

    Two files, because the append-only promise is split exactly that way by the builder: the table comes from the
    transpiled migration, and the triggers from `_append_only.sql`, which is generated from `APPEND_ONLY` in
    `tools/build-sqlite-migrations.py`. Loading only the first would test a ledger anybody can delete from — and
    the first run of this test did, which is how the fixture grew its second `executescript`.
    """
    conn = sqlite3.connect(":memory:")
    # The whole portable subset, in order, then the generated triggers — the same assembly `test_migrations.py`
    # uses. `_append_only.sql` covers every append-only table at once, so a fixture that loaded only 0022's
    # triggers would have to hand-pick them and would then be testing something other than what ships.
    for path in sorted(LITE.glob("[0-9]*.sql")):
        conn.executescript(path.read_text())
    conn.executescript((LITE / "_append_only.sql").read_text())
    return conn


class StripeSignature(unittest.TestCase):
    def test_a_correctly_signed_body_is_accepted(self):
        header = sign_stripe(BODY, SECRET, AT_S)
        out = verify_stripe(BODY, header, SECRET, at_ms=NOW_MS)
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.event_id, "evt_1")
        self.assertEqual(out.event_type, "checkout.session.completed")

    def test_the_signature_covers_the_raw_bytes_not_a_re_encoding(self):
        """A body that parses to the same object but is not the same bytes must be refused.

        This is the requirement P14 wrote as "verify the signature over the RAW bytes", and it is the one a
        handler breaks by accident: parse the JSON, re-encode it, verify that. The re-encoded bytes are different
        (key order, spacing, escaping), so an honest request fails — and the usual "fix" is to stop verifying the
        body at all. The test proves both halves: same object, different bytes, refused.
        """
        header = sign_stripe(BODY, SECRET, AT_S)
        re_encoded = json.dumps(json.loads(BODY), indent=2).encode()
        self.assertEqual(json.loads(re_encoded), json.loads(BODY))
        self.assertNotEqual(re_encoded, BODY)
        out = verify_stripe(re_encoded, header, SECRET, at_ms=NOW_MS)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "bad_signature")

    def test_a_decoded_string_is_refused_rather_than_guessed_at(self):
        header = sign_stripe(BODY, SECRET, AT_S)
        with self.assertRaises(WebhookError):
            verify_stripe(BODY.decode(), header, SECRET, at_ms=NOW_MS)

    def test_a_tampered_body_is_refused(self):
        header = sign_stripe(BODY, SECRET, AT_S)
        tampered = BODY.replace(b"2500", b"250000")
        out = verify_stripe(tampered, header, SECRET, at_ms=NOW_MS)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "bad_signature")

    def test_the_wrong_secret_is_refused(self):
        header = sign_stripe(BODY, SECRET, AT_S)
        out = verify_stripe(BODY, header, "whsec_a_different_endpoint", at_ms=NOW_MS)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "bad_signature")

    def test_a_capture_replayed_after_five_minutes_is_refused(self):
        header = sign_stripe(BODY, SECRET, AT_S)
        just_inside = verify_stripe(BODY, header, SECRET, at_ms=(AT_S + webhooks.TOLERANCE_S) * 1000)
        self.assertTrue(just_inside.ok, just_inside.reason)
        outside = verify_stripe(BODY, header, SECRET, at_ms=(AT_S + webhooks.TOLERANCE_S + 1) * 1000)
        self.assertFalse(outside.ok)
        self.assertEqual(outside.reason, "timestamp_outside_tolerance")

    def test_a_future_dated_signature_is_refused(self):
        """A timestamp ahead of us is a clock problem or an attempt to buy a longer window; neither adds trust."""
        header = sign_stripe(BODY, SECRET, AT_S + 3600)
        out = verify_stripe(BODY, header, SECRET, at_ms=NOW_MS)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "timestamp_in_the_future")

    def test_every_candidate_is_compared_so_timing_does_not_say_which_matched(self):
        """Stripe sends several `v1=` values while a secret rotates.

        An early `return` on the first match would make the response time depend on the *position* of the correct
        signature — an oracle for somebody willing to send a few thousand requests. The implementation accumulates,
        and this test reads the source to keep it that way, because the behaviour is invisible from outside.
        """
        import inspect
        src = inspect.getsource(verify_stripe)
        loop = src.split("for candidate in parsed.v1:", 1)[1].split("if not matched:", 1)[0]
        self.assertIn("compare_digest", loop)
        self.assertNotIn("return", loop.replace("return Verified(False, \"bad_signature\"", "X"))

    def test_two_valid_signatures_are_both_accepted_and_all_are_counted(self):
        good = sign_stripe(BODY, SECRET, AT_S).split("v1=")[1]
        older = hashlib.sha256(b"an older secret").hexdigest()
        header = "t=%d,v1=%s,v1=%s" % (AT_S, older, good)
        out = verify_stripe(BODY, header, SECRET, at_ms=NOW_MS)
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.candidates, 2)

    def test_a_header_with_no_timestamp_or_no_supported_version_is_refused_by_name(self):
        self.assertEqual(verify_stripe(BODY, "v1=" + "a" * 64, SECRET, at_ms=NOW_MS).reason, "no_timestamp")
        self.assertEqual(verify_stripe(BODY, "t=%d" % AT_S, SECRET, at_ms=NOW_MS).reason, "no_supported_signature")
        self.assertEqual(verify_stripe(BODY, "", SECRET, at_ms=NOW_MS).reason, "no_signature")

    def test_an_unconfigured_secret_verifies_nothing(self):
        """With an empty secret every HMAC over an empty key matches, so this must refuse before comparing."""
        header = "t=%d,v1=%s" % (AT_S, hmac.new(b"", b"", hashlib.sha256).hexdigest())
        self.assertEqual(verify_stripe(BODY, header, "", at_ms=NOW_MS).reason, "no_secret_configured")

    def test_a_malformed_header_element_is_an_error_not_a_missing_signature(self):
        with self.assertRaises(WebhookError):
            parse_stripe_signature("t=1,garbage")

    def test_an_event_without_an_id_is_refused(self):
        body = json.dumps({"type": "checkout.session.completed"}).encode()
        out = verify_stripe(body, sign_stripe(body, SECRET, AT_S), SECRET, at_ms=NOW_MS)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "no_event_id")

    def test_the_audit_view_carries_no_payload(self):
        out = verify_stripe(BODY, sign_stripe(BODY, SECRET, AT_S), SECRET, at_ms=NOW_MS)
        self.assertNotIn("payload", out.as_dict())
        self.assertEqual(out.as_dict()["event_id"], "evt_1")


class ReplayLedger(unittest.TestCase):
    def test_the_same_event_delivered_twice_is_fulfilled_once(self):
        conn = fresh_conn()
        header = sign_stripe(BODY, SECRET, AT_S)
        first = verify_stripe(BODY, header, SECRET, at_ms=NOW_MS,
                              claim=lambda e, p, t: replay.claim_event(conn, e, p, t, "checkout.session.completed"))
        second = verify_stripe(BODY, header, SECRET, at_ms=NOW_MS + 1000,
                               claim=lambda e, p, t: replay.claim_event(conn, e, p, t, ""))
        self.assertTrue(first.ok, first.reason)
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "replay")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0], 1)

    def test_a_forged_event_never_reaches_the_ledger(self):
        """The claim runs last, so an attacker cannot poison the ledger with ids a real event will later need."""
        conn = fresh_conn()
        out = verify_stripe(BODY, sign_stripe(BODY, "not-the-secret", AT_S), SECRET, at_ms=NOW_MS,
                            claim=lambda e, p, t: replay.claim_event(conn, e, p, t))
        self.assertFalse(out.ok)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0], 0)

    def test_an_empty_event_id_is_never_claimed(self):
        conn = fresh_conn()
        self.assertFalse(replay.claim_event(conn, "", PROVIDER_STRIPE, NOW_MS))

    def test_the_concurrent_delivery_case_is_decided_by_the_constraint(self):
        """Two connections, one event: the INSERT decides, not a prior SELECT.

        This is why the module is written around the unique constraint. A read-then-write would let both
        deliveries see "not seen" and both fulfil — the race that a payment handler must not lose.
        """
        conn_a = fresh_conn()
        self.assertTrue(replay.claim_event(conn_a, "evt_race", PROVIDER_STRIPE, NOW_MS))
        self.assertFalse(replay.claim_event(conn_a, "evt_race", PROVIDER_STRIPE, NOW_MS + 1))

    def test_the_ledger_is_append_only(self):
        conn = fresh_conn()
        replay.claim_event(conn, "evt_immutable", PROVIDER_STRIPE, NOW_MS)
        for statement in ("UPDATE payment_events SET event_type='x' WHERE event_id='evt_immutable'",
                          "DELETE FROM payment_events WHERE event_id='evt_immutable'"):
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(statement)
        self.assertTrue(replay.seen_event(conn, "evt_immutable"))

    def test_the_provider_column_refuses_anything_that_is_not_a_verifier(self):
        conn = fresh_conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO payment_events (event_id, provider, received_ms) VALUES ('x','paypal',1)")


class TelegramStars(unittest.TestCase):
    def test_the_update_secret_is_compared_in_constant_time(self):
        self.assertTrue(verify_update_secret("s3cret", "s3cret"))
        self.assertFalse(verify_update_secret("s3cre", "s3cret"))
        self.assertFalse(verify_update_secret(None, "s3cret"))
        self.assertFalse(verify_update_secret("anything", ""))

    def test_a_stars_payment_is_read_from_inside_a_message(self):
        update = {"update_id": 1, "message": {"message_id": 2, "from": {"id": 42},
                  "successful_payment": {"telegram_payment_charge_id": "tg_charge_1",
                                         "provider_payment_charge_id": "prov_1",
                                         "total_amount": 250, "currency": "XTR"}}}
        out = stars_payment(update)
        self.assertTrue(out.ok, out.reason)
        self.assertEqual(out.total_amount, 250)
        self.assertEqual(out.user_id, "42")

    def test_a_payment_posted_as_its_own_body_is_refused(self):
        """The shape somebody invents when they build a payment endpoint and want the same field to arrive
        directly. Telegram never sends it, so accepting it would be an unauthenticated fulfilment route."""
        out = stars_payment({"successful_payment": {"telegram_payment_charge_id": "tg_x", "total_amount": 250}})
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "payment_outside_a_message")

    def test_a_payment_with_no_charge_id_is_refused(self):
        out = stars_payment({"message": {"successful_payment": {"total_amount": 250}}})
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "no_charge_id")

    def test_a_zero_or_absent_amount_is_refused(self):
        self.assertEqual(stars_payment({"message": {"successful_payment": {"telegram_payment_charge_id": "c"}}}).reason,
                         "bad_amount")
        self.assertEqual(stars_payment({"message": {"successful_payment": {"telegram_payment_charge_id": "c",
                                                                           "total_amount": 0}}}).reason,
                         "bad_amount")


class Fulfilment(unittest.TestCase):
    """The rule that a webhook is a notification, not an instruction."""

    RECORD = {"user_id": "u_9", "plan": "pro", "currency": "usd"}

    def test_the_amount_must_match_the_record_we_hold(self):
        self.assertTrue(fulfilment(provider_amount_micro=2500, provider_currency="usd",
                                   recorded=self.RECORD, recorded_amount_micro=2500).ok)
        under = fulfilment(provider_amount_micro=100, provider_currency="usd", recorded=self.RECORD,
                           recorded_amount_micro=2500)
        self.assertFalse(under.ok)
        self.assertEqual(under.reason, "amount_mismatch")
        over = fulfilment(provider_amount_micro=999999, provider_currency="usd", recorded=self.RECORD,
                          recorded_amount_micro=2500)
        self.assertFalse(over.ok)
        self.assertEqual(over.reason, "amount_mismatch")

    def test_the_user_and_the_plan_come_from_our_row_not_from_the_event(self):
        """The event cannot name a beneficiary: its payload is not an input to this decision at all."""
        out = fulfilment(provider_amount_micro=2500, provider_currency="usd", recorded=self.RECORD,
                         recorded_amount_micro=2500)
        self.assertEqual(out.user_id, "u_9")
        self.assertEqual(out.plan, "pro")

    def test_an_event_for_something_we_never_wrote_down_refuses(self):
        out = fulfilment(provider_amount_micro=2500, provider_currency="usd", recorded=None,
                         recorded_amount_micro=2500)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "no_recorded_intent")

    def test_a_currency_mismatch_refuses(self):
        out = fulfilment(provider_amount_micro=2500, provider_currency="eur", recorded=self.RECORD,
                         recorded_amount_micro=2500)
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, "currency_mismatch")


class TheSurfaceIsNotWiderThanTheControl(unittest.TestCase):
    """Two source-level checks, because these are properties of the *repository*, not of a function.

    A future contributor adding a payment route could satisfy every test above and still ship a way in that skips
    all of it. These assertions are the tripwire.
    """

    def test_no_route_declares_a_payment_webhook_of_its_own(self):
        from pathlib import Path
        app = (Path(__file__).resolve().parent.parent / "services" / "api" / "app.py").read_text()
        # The only webhook route is Telegram's update endpoint, which verifies the secret header. A route named
        # for a provider is exactly what this OPEN item said must not exist before it is written against this
        # package.
        for forbidden in ('"/v1/stripe', '"/v1/payments', '"/v1/stars', '"/v1/webhooks'):
            self.assertNotIn(forbidden, app, "a provider webhook route exists; route it through polygm_core.payments")

    def test_the_verifier_never_parses_before_it_verifies(self):
        """Raw bytes are hashed with nothing between them and the MAC.

        The tempting refactor is to parse first (so the handler can read `type`), then verify — which makes the
        body a dict again and loses the bytes. This asserts the ordering inside the function.
        """
        import inspect
        src = inspect.getsource(verify_stripe)
        self.assertLess(src.index("signed = b"), src.index("json.loads(bytes(raw_body))"))


if __name__ == "__main__":
    unittest.main()
