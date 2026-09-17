"""The executor, including the case P04 names explicitly: sign → POST → process dies before the response.

This is the only module in P04 where a wrong answer costs real money, so the tests are written as
properties of the *system*, not of the function:

  * after an unconfirmed POST the intent is UNCERTAIN and the venue is asked, not the user;
  * recovery NEVER produces a second POST;
  * the client order hash is reproducible in a brand-new process, because that is what makes recovery
    possible at all;
  * when the lookup is exhausted, the answer stays uncertain — a guess here is a duplicate order.

Half of these run across a real socket (the mock is served by ThreadingHTTPServer on 127.0.0.1) because an
in-process fake cannot distinguish "the venue was slow" from "the test was slow".
"""
from __future__ import annotations
import importlib.util
import json
import threading
import unittest
from contextlib import contextmanager

from conftest import ROOT  # noqa: F401
from polygm_core.executor.executor import (HOPS, OrderIntent, Submission, build_signed_payload, reconcile,
                                           submit)
from polygm_core.risk.gate import Decision
from polygm_core.ledger.ledger import IntentState, venue_status_to_state


def _load(mod_name, rel_path):
    spec = importlib.util.spec_from_file_location(mod_name, str(ROOT / rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mk = _load("mock_clob", "services/executor-mock/mock_clob.py")
tp = _load("mock_transport", "services/executor-mock/transport.py")

ALLOW = Decision(allowed=True, code="OK", message="ok")
PUB = "0x" + "ab" * 20


def intent(**kw) -> OrderIntent:
    base = dict(id="0x" + "11" * 16, user_id="0xuser", token_id="71321045679252219", side="BUY",
                price_micro=500_000, size_shares_micro=10_000_000, idempotency_key="key-0001",
                condition_id="0xc", tick_size="0.001")
    base.update(kw)
    return OrderIntent(**base)


class TestPayload(unittest.TestCase):
    def test_version_is_v2_and_money_crosses_as_an_asserted_float(self):
        p = build_signed_payload(intent())
        self.assertEqual(p["version"], "v2")
        self.assertIsInstance(p["price"], float)
        self.assertEqual(p["price"], 0.5)
        self.assertEqual(p["size"], 10.0)
        self.assertEqual(p["fee_rate_bps"], 0)

    def test_the_hash_is_reproducible_from_the_intent_alone(self):
        i = intent()
        self.assertEqual(i.client_order_hash(PUB), i.client_order_hash(PUB))
        self.assertEqual(len(i.client_order_hash(PUB)), 66)

    def test_the_hash_changes_when_anything_that_matters_changes(self):
        base = intent().client_order_hash(PUB)
        for kw in ({"price_micro": 499_000}, {"size_shares_micro": 9_000_000}, {"side": "SELL"},
                   {"idempotency_key": "key-0002"}, {"expiration": 12}):
            self.assertNotEqual(base, intent(**kw).client_order_hash(PUB), kw)
        self.assertNotEqual(base, intent().client_order_hash("0xother"))

    def test_hops_never_retry_an_unconfirmed_post(self):
        self.assertEqual(HOPS["post_order"]["retries"], 0)
        self.assertIn("UNCERTAIN", HOPS["post_order"]["on_exhaustion"].upper())
        # cancels are safe to retry, posts are not - the asymmetry must be visible in the table
        self.assertGreater(HOPS["cancel_all"]["retries"], 0)


class TestCrashRecovery(unittest.TestCase):
    """D5's last bullet, end to end."""

    def test_sign_post_die_then_recover_in_a_new_process(self):
        mock = mk.MockClob()
        mock.set_scenario("timeout_after_accept")
        i = intent()
        h = i.client_order_hash(PUB)
        # process A: signs and POSTs, gets nothing back
        t1 = mk.ScenarioTransport(mock, signer_pubkey=PUB)
        sub_a = submit(i, t1, ALLOW, signer_pubkey=PUB)
        self.assertEqual(sub_a.state, "uncertain")
        self.assertIsNone(sub_a.venue_order_id)
        self.assertEqual(t1.post_calls, 1)
        self.assertTrue(sub_a.notes and "reconcile" in sub_a.notes[0])

        # process B: knows only the intent and the public key, and MUST derive the same hash
        sub_b = Submission(i, h, "uncertain", notes=["rebuilt after restart"])
        t2 = mk.ScenarioTransport(mock, signer_pubkey=PUB)
        rec = reconcile(sub_b, t2, delays_ms=(0, 0))
        self.assertEqual(rec.state, "submitted", "recovery must find the order the dead process posted")
        self.assertEqual(t2.post_calls, 0, "recovery must never re-POST")
        self.assertIn(rec.venue_order_id, mock.orders)
        # and the intent's terminal state is one the user can be told about truthfully
        self.assertIn(venue_status_to_state(mock.orders[rec.venue_order_id]["status"]),
                      {s.value for s in IntentState} | {"live", "filled"})


class TestUncertainSafety(unittest.TestCase):
    def setUp(self):
        self.mock = mk.MockClob()

    def test_exhausted_lookup_stays_uncertain_and_blocks(self):
        i = intent()
        h = i.client_order_hash(PUB)
        sub = Submission(i, h, "uncertain", notes=["x"])
        lonely = mk.MockClob()                       # the venue knows nothing (still unreachable)
        rec = reconcile(sub, mk.ScenarioTransport(lonely), delays_ms=(0, 0, 0))
        self.assertEqual(rec.state, "uncertain")
        self.assertIn("blocked", rec.notes[-1])
        self.assertIn("alert", rec.notes[-1].lower())

    def test_unmapped_venue_status_does_not_become_a_second_post(self):
        # A venue that invents a status is an incident, not a reason to guess "not found".
        i = intent()
        h = i.client_order_hash(PUB)
        sub = Submission(i, h, "uncertain")

        class Weird:
            def post_order(self, s, *, timeout_ms):
                raise AssertionError("must not re-post")

            def find_order(self, hh, *, timeout_ms):
                return {"orderID": "0xdead", "status": "frobnicated"}

            def cancel_all(self, *, timeout_ms):
                return {}

        rec = reconcile(sub, Weird(), delays_ms=(0,))
        self.assertEqual(rec.state, "uncertain")
        self.assertIn("unmapped", rec.notes[-1])

    def test_a_rejected_order_carries_the_venue_code(self):
        self.mock.set_scenario("reject")
        t = mk.ScenarioTransport(self.mock, signer_pubkey=PUB)
        s = submit(intent(), t, ALLOW, signer_pubkey=PUB)
        self.assertEqual(s.state, "rejected")
        self.assertEqual(s.raw_error_code, "not_enough_balance_or_allowance")
        self.assertIsNone(s.venue_order_id)

    def test_risk_denial_never_reaches_the_venue(self):
        t = mk.ScenarioTransport(self.mock, signer_pubkey=PUB)
        s = submit(intent(), t, Decision(allowed=False, code="OFF_TICK", message="no"),
                   signer_pubkey=PUB)
        self.assertEqual(s.state, "rejected")
        self.assertEqual(s.raw_error_code, "OFF_TICK")
        self.assertEqual(t.post_calls, 0)


class TestHashIsOnTheWire(unittest.TestCase):
    """The hash the executor reconciles by must be the hash the venue indexed. Two transports, one answer."""

    def test_both_transports_send_the_identical_dedupe_key(self):
        i = intent()
        h = i.client_order_hash(PUB)
        payload = build_signed_payload(i, client_order_hash=h)
        self.assertEqual(payload["client_order_hash"], h)
        mock = mk.MockClob()
        t = mk.ScenarioTransport(mock, signer_pubkey=PUB)
        sub = submit(i, t, ALLOW, signer_pubkey=PUB)
        self.assertEqual(sub.order_hash, h)
        self.assertIn(h, mock.by_hash, "the venue indexed a different key than the executor will look up")

    def test_the_mock_refuses_a_payload_with_no_hash_at_all(self):
        # ScenarioTransport asserts the field exists; a real adapter that forgot it would produce an order
        # that can never be reconciled, i.e. exactly the money bug, so it must fail loudly.
        with self.assertRaises(AssertionError):
            mk.ScenarioTransport(mk.MockClob()).post_order({"nope": 1}, timeout_ms=10)


class TestVenueValidation(unittest.TestCase):
    def test_v1_payload_is_rejected_by_the_mock(self):
        m = mk.MockClob()
        p = dict(build_signed_payload(intent()), version="v1")
        self.assertEqual(m.post_order(p)["code"], "order_version_mismatch")

    def test_off_tick_and_small_orders_are_rejected_by_the_mock(self):
        m = mk.MockClob()
        for patch, code in (({"price": 0.5005}, "invalid_tick_size"),
                            ({"size": 1.0}, "invalid_size")):
            with self.subTest(code=code):
                self.assertEqual(m.post_order(dict(build_signed_payload(intent()),
                                                   tick_size="0.01", **patch))["code"], code)

    def test_a_price_that_cannot_round_trip_is_rejected(self):
        # The mock checks the float the way a venue would: if the client shipped a float that cannot come
        # back as the integer the ledger recorded, the order is refused rather than silently resized.
        m = mk.MockClob()
        p = dict(build_signed_payload(intent()), price=float.fromhex("1.fffffffffffffp-2"))
        out = m.post_order(p)
        self.assertIn(out.get("code"), ("rounding_boundary", "invalid_price"))


@contextmanager
def serving():
    from http.server import ThreadingHTTPServer
    mock = mk.MockClob()
    mk.Handler.mock = mock
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mk.Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield mock, "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)


class TestOverARealSocket(unittest.TestCase):
    """`make dev` runs this server; the timeout branch must be real HTTP, not an exception in a method."""

    def test_timeout_round_trips_as_504_and_becomes_uncertain(self):
        with serving() as (mock, base):
            i = intent()
            h = i.client_order_hash(PUB)
            t = tp.HttpTransport(base)
            self.assertEqual(t.set_scenario("accept")["ok"], True)
            ok = submit(i, t, ALLOW, signer_pubkey=PUB)
            self.assertEqual(ok.state, "submitted")
            self.assertEqual(mock.snapshot()["requests"], ["v2"])

            t.set_scenario("timeout_after_accept")
            i2 = intent(id="0x" + "22" * 16, idempotency_key="key-0002")
            lost = submit(i2, t, ALLOW, signer_pubkey=PUB)
            self.assertEqual(lost.state, "uncertain", "the 504 must not be read as a rejection")
            self.assertEqual(t.last_status, 504)
            self.assertEqual(t.post_calls, 2)

            self.assertNotEqual(lost.order_hash, ok.order_hash,
                                "two intents must not share a dedupe key or recovery cannot tell them apart")
            rec = reconcile(lost, t, delays_ms=(0, 0))
            self.assertEqual(rec.state, "submitted")
            self.assertEqual(t.post_calls, 2, "recovery must not add a POST over the wire either")
            self.assertEqual(t.lookup_calls >= 1, True)

    def test_unreachable_venue_raises_connectionerror_not_a_fake_answer(self):
        with serving() as (mock, base):
            t = tp.HttpTransport(base)
            t.set_scenario("unreachable")
            with self.assertRaises(ConnectionError):
                t.post_order(build_signed_payload(intent()), timeout_ms=1500)

    def test_partial_fill_is_visible_through_the_trades_endpoint(self):
        with serving() as (mock, base):
            t = tp.HttpTransport(base)
            s = submit(intent(), t, ALLOW, signer_pubkey=PUB)
            self.assertEqual(s.state, "submitted")
            t.fill(s.venue_order_id, price=0.5, size=4.0)
            trades = t._call("GET", "/v1/trades?limit=10", None, 2000)
            self.assertEqual(len(trades["trades"]), 1)
            self.assertEqual(trades["trades"][0]["size"], 4.0)
            found = t.find_order(intent().client_order_hash(PUB), timeout_ms=2000)
            self.assertEqual(found["size_matched"], 4_000_000)


if __name__ == "__main__":
    unittest.main()
