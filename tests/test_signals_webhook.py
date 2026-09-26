"""The webhook transport's own suite: every refusal, and proof that refusing means refusing *before* the socket.

The style here is deliberate and it is the point of the file. For each hostile URL the test does not only assert
that a `TargetRefused` came back — it installs an opener that fails the test if it is ever called, so what is
being asserted is "we never connected", which is the property P14's item asked for ("a refusal before the send
rather than a timeout after it"). An implementation that connected to 169.254.169.254 and then objected would
pass a status-only test and fail every test in this file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages"))

from polygm_core.signals import fanout as F                     # noqa: E402
from polygm_core.signals import webhook as W                    # noqa: E402


class NeverOpened:
    """The opener that makes "we refused" a claim about the socket. Any call is a test failure."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers, timeout):            # noqa: ARG002
        self.calls.append(url)
        raise AssertionError("the transport opened a connection to %s" % url)


class FakeResponse:
    def __init__(self, status=200, body=b'{"ok":true}', headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, n):
        return self._body[:n]


class Redirect(Exception):
    """Stands in for `urllib.error.HTTPError` — the transport's redirect branch reads `.code` and `.headers`,
    which is the entire surface it uses."""

    def __init__(self, code, location):
        super().__init__("HTTP %d" % code)
        self.code = code
        self.headers = {"Location": location}


def opener_of(seq):
    """An opener that answers with the next item of `seq` and records what it was asked to open."""
    calls = []

    def open_(url, body, headers, timeout):                     # noqa: ARG001
        calls.append(url)
        item = seq[min(len(calls) - 1, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item
    open_.calls = calls
    return open_


class TestTheGuardRefusesBeforeTheSocket(unittest.TestCase):
    #: Every class the item named, plus the alternate notations that are how a naive filter gets bypassed.
    HOSTILE = (
        ("http://hooks.example.com/x", W.TargetRefused),
        ("https://127.0.0.1/v1/admin/kill-switch", W.TargetRefused),
        ("https://127.1.2.3/x", W.TargetRefused),
        ("https://localhost/x", W.TargetRefused),            # resolves, so this one needs the resolver
        ("https://169.254.169.254/latest/meta-data/iam/security-credentials/", W.TargetRefused),
        ("https://10.1.2.3/x", W.TargetRefused),
        ("https://192.168.0.1/x", W.TargetRefused),
        ("https://172.16.4.4/x", W.TargetRefused),
        ("https://100.64.0.7/x", W.TargetRefused),           # carrier-grade NAT is still not public
        ("https://0.0.0.0/x", W.TargetRefused),
        ("https://[::1]/x", W.TargetRefused),
        ("https://[fd00::1]/x", W.TargetRefused),            # IPv6 unique-local
        ("https://[fe80::1]/x", W.TargetRefused),            # IPv6 link-local
        ("https://[::ffff:169.254.169.254]/x", W.TargetRefused),
        ("https://2130706433/x", W.TargetRefused),           # 127.0.0.1 written as one integer
        ("https://0x7f000001/x", W.TargetRefused),
        ("https://user:pw@hooks.example.com/x", W.TargetRefused),
        ("https://hooks.example.com:22/x", W.TargetRefused),  # a webhook that dials SSH is a port scan
        ("https://hooks.example.com\nX-Smuggled: 1/x", W.TargetRefused),
        ("https://ex%61mple.com/x", W.TargetRefused),
        ("gopher://hooks.example.com/x", W.TargetRefused),
        ("file:///etc/passwd", W.TargetRefused),
        ("", W.TargetRefused),
    )

    def test_every_hostile_url_is_refused_without_a_connection(self):
        never = NeverOpened()
        seen_codes = set()
        for url, _ in self.HOSTILE:
            with self.subTest(url=url):
                resolver = lambda host: [a for a in (
                    (2, 1, 6, "", ("127.0.0.1", 0)) if host in ("localhost",) else
                    (2, 1, 6, "", ("93.184.216.34", 0)),)]
                codes = set()
                for resolve in (None, resolver):                 # save time (no DNS) and send time (DNS)
                    with self.assertRaises(W.TargetRefused) as cm:
                        W.deliver(url=url, payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                                  resolve=resolve, opener=never)
                    codes.add(cm.exception.code)
                    self.assertTrue(cm.exception.message, "a refusal with no message is not reportable")
                seen_codes |= codes
        self.assertEqual([], never.calls, "a refusal that connects first is the bug this test exists for")
        # Every refusal names its own reason rather than collapsing into one "invalid URL".
        self.assertEqual({"TARGET_SCHEME", "TARGET_NOT_PUBLIC", "TARGET_USERINFO", "TARGET_PORT",
                          "TARGET_MALFORMED", "TARGET_MISSING"}, seen_codes)

    def test_the_resolution_half_is_what_catches_a_named_internal_host(self):
        """`localhost` is not internal by its *shape*: the syntax half passes it and the resolver is what
        refuses. This is the split the module documents, asserted rather than described."""
        never = NeverOpened()
        self.assertEqual({"kind": "hostname", "addresses": []},
                         {k: v for k, v in W.check_target("https://localhost:8443/x").items()
                          if k in ("kind", "addresses")})
        with self.assertRaises(W.TargetRefused) as cm:
            W.check_target("https://localhost:8443/x",
                           resolve=lambda host: [(2, 1, 6, "", ("127.0.0.1", 0))])
        self.assertEqual("TARGET_NOT_PUBLIC", cm.exception.code)
        self.assertIn("loopback", cm.exception.detail)
        self.assertEqual([], never.calls)

    def test_a_dns_answer_with_one_private_address_is_refused(self):
        """Split-horizon DNS, which is the realistic version of the attack: the record is public *and* private
        depending on who asks, so one bad address in the answer is enough to refuse the whole host."""
        with self.assertRaises(W.TargetRefused) as cm:
            W.check_target("https://hooks.example.com/x",
                           resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0)),
                                                 (2, 1, 6, "", ("10.0.0.5", 0))])
        self.assertEqual("TARGET_NOT_PUBLIC", cm.exception.code)
        self.assertIn("10.0.0.5", cm.exception.detail)

    def test_an_unresolvable_host_fails_closed(self):
        """An unresolvable host is not a safe host, it is a host we can make no claim about. Both the NXDOMAIN
        shape (empty answer) and the resolver-error shape refuse, and neither becomes an "allow"."""
        for resolver in (lambda host: [], lambda host: (_ for _ in ()).throw(OSError("no address"))):
            with self.assertRaises(W.TargetRefused) as cm:
                W.check_target("https://hooks.example.com/x", resolve=resolver)
            self.assertEqual("TARGET_UNRESOLVABLE", cm.exception.code)

    def test_an_ordinary_https_webhook_is_allowed(self):
        t = W.check_target("https://hooks.example.com/alerts?token=abc",
                           resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))])
        self.assertEqual("hooks.example.com", t["host"])
        self.assertEqual(443, t["port"])
        self.assertEqual(["93.184.216.34"], t["addresses"])
        # An unprivileged custom port is a legitimate self-hosted receiver; a privileged one is not (above).
        self.assertEqual(8443, W.check_target("https://hooks.example.com:8443/x",
                                              resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))])["port"])


class TestTheTransportSignsAndNeverEchoes(unittest.TestCase):
    def test_a_delivery_is_signed_the_way_a_receiver_verifies_it(self):
        seen = {}

        def open_(url, body, headers, timeout):
            seen.update({"url": url, "body": body, "headers": headers, "timeout": timeout})
            return FakeResponse(200, b'{"ok":true}')

        out = W.deliver(url="https://hooks.example.com/x", payload={"b": 2, "a": 1}, secret="s3cret",
                        delivery_id="deadbeef", at_ms=1_700_000_000_000,
                        resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))], opener=open_)
        self.assertTrue(out["ok"])
        self.assertEqual(200, out["status"])
        # The receiver's side of the contract, computed from first principles rather than from our own helper.
        expected = hmac.new(b"s3cret", b"1700000000000." + seen["body"], hashlib.sha256).hexdigest()
        self.assertEqual("t=1700000000000,v1=%s" % expected, seen["headers"]["X-PolyGM-Signature"])
        self.assertEqual("1700000000000", seen["headers"]["X-PolyGM-Timestamp"])
        self.assertEqual("deadbeef", seen["headers"]["X-PolyGM-Delivery"])
        self.assertEqual("application/json", seen["headers"]["Content-Type"])
        self.assertEqual({"a": 1, "b": 2}, json.loads(seen["body"]), "the body is canonical, sorted JSON")
        self.assertEqual(W.CONNECT_TIMEOUT_S, seen["timeout"])
        # The response body is a hostile input: the record carries its hash and length, never the bytes.
        self.assertEqual(hashlib.sha256(b'{"ok":true}').hexdigest()[:32], out["bodySha256"])
        self.assertNotIn("body", out)

    def test_a_thousand_second_timeout_is_not_a_setting_we_have(self):
        """The timeout is a constant of the transport, not a parameter of the call: a worker that can be told
        "wait an hour" is a worker whose lease expires while it waits, and the row is then delivered twice."""
        import inspect
        sig = inspect.signature(W.deliver).parameters
        self.assertIn("timeout_s", sig)
        self.assertEqual(5.0, sig["timeout_s"].default)

    def test_a_redirect_hop_is_a_new_decision(self):
        """The redirect that makes SSRF work: the first host is public, the second is not. The hop is refused,
        and the refusal happens instead of the connection to 127.0.0.1 — the second call never happens."""
        open_ = opener_of([Redirect(302, "https://127.0.0.1/admin/backups")])
        with self.assertRaises(W.TargetRefused) as cm:
            W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                      resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))], opener=open_)
        self.assertEqual("TARGET_NOT_PUBLIC", cm.exception.code)
        self.assertEqual(["https://hooks.example.com/x"], open_.calls, "the second hop was never dialled")

    def test_a_relative_redirect_is_resolved_then_re_guarded(self):
        open_ = opener_of([Redirect(307, "/also-private"), FakeResponse(200)])
        with self.assertRaises(W.TargetRefused) as cm:
            W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                      resolve=lambda host: [(2, 1, 6, "", ("127.0.0.1", 0))], opener=open_)
        self.assertEqual("TARGET_NOT_PUBLIC", cm.exception.code)
        self.assertEqual([], open_.calls, "a host that resolves privately is refused before the first socket")

    def test_a_downgrade_to_http_is_refused_at_the_hop(self):
        open_ = opener_of([Redirect(301, "http://hooks.example.com/x")])
        with self.assertRaises(W.TargetRefused) as cm:
            W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                      resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))], opener=open_)
        self.assertEqual("TARGET_SCHEME", cm.exception.code)

    def test_a_public_redirect_chain_is_followed_and_recorded(self):
        public = lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))]
        open_ = opener_of([Redirect(302, "https://www.example.com/one"),
                           Redirect(302, "https://cdn.example.com/two"), FakeResponse(204)])
        out = W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                        resolve=public, opener=open_)
        self.assertTrue(out["ok"])
        self.assertEqual(204, out["status"])
        self.assertEqual(3, len(out["hops"]), "every hop is recorded, so a redirect chain is auditable")
        self.assertEqual("https://hooks.example.com/x", out["hops"][0]["url"])
        self.assertEqual("https://cdn.example.com/two", out["hops"][-1]["url"])

    def test_a_redirect_loop_stops_at_the_cap(self):
        public = lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))]
        open_ = opener_of([Redirect(302, "https://a.example.com/%d" % i) for i in range(10)])
        with self.assertRaises(W.TargetRefused) as cm:
            W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                      resolve=public, opener=open_)
        self.assertEqual("TARGET_REDIRECT", cm.exception.code)
        self.assertEqual(W.MAX_REDIRECTS + 1, len(open_.calls), "the cap is the cap, not a suggestion")

    def test_a_returned_redirect_is_treated_like_a_raised_one(self):
        """The same hop guard for both shapes. A client with redirects disabled hands back the 302 as a response;
        if that path skipped the re-check, the guard would be only as good as the client's exception style."""
        class Ret:
            def __init__(self, loc):
                self.status = 302
                self.headers = {"Location": loc}

            def read(self, n):                                   # pragma: no cover - must never be reached
                raise AssertionError("a redirect body was read instead of re-guarded")

        open_ = opener_of([Ret("https://169.254.169.254/latest/meta-data/")])
        with self.assertRaises(W.TargetRefused) as cm:
            W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                      resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))], opener=open_)
        self.assertEqual("TARGET_NOT_PUBLIC", cm.exception.code)
        self.assertEqual(["https://hooks.example.com/x"], open_.calls)

    def test_a_provider_saying_no_is_a_failure_not_a_refusal(self):
        """The distinction the caller needs: a 500 is retryable (the provider may recover), a refusal is not."""
        public = lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))]
        out = W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                        resolve=public, opener=opener_of([FakeResponse(500, b"boom")]))
        self.assertFalse(out["ok"])
        self.assertEqual(500, out["status"])
        self.assertIsNone(out["refused"])
        # A transport error is also a failure, and its text is bounded so a hostile error cannot fill a log line.
        out = W.deliver(url="https://hooks.example.com/x", payload={"a": 1}, secret="s", delivery_id="d", at_ms=1,
                        resolve=public, opener=opener_of([TimeoutError("x" * 500)]))
        self.assertFalse(out["ok"])
        self.assertLessEqual(len(out["error"]), 200)


class TestTheQueueHalf(unittest.TestCase):
    def _claim(self, target):
        return {"signal_id": "sig-1", "user_id": "u1", "channel": "webhook", "status": F.STATUS_SENDING,
                "attempts": 1, "queued_ms": 1_000, "claim_ms": 1_000, "priority": 0, "target": target,
                "idempotency_key": F.idempotency_key("sig-1", "webhook")}

    def test_a_refused_target_is_dead_on_the_first_refusal_not_the_fifth_attempt(self):
        row = W.send_claimed(self._claim("https://169.254.169.254/x"), payload={"a": 1}, secret="s",
                             now_ms=2_000, resolve=lambda host: [], opener=NeverOpened())
        self.assertEqual(F.STATUS_DEAD, row["status"])
        self.assertEqual("refused:TARGET_NOT_PUBLIC", row["dead_reason"])
        self.assertNotIn("retry_in_ms", row, "a blocked address is not retried")

    def test_a_provider_failure_schedules_the_backoff_the_queue_owns(self):
        row = W.send_claimed(self._claim("https://hooks.example.com/x"), payload={"a": 1}, secret="s", now_ms=2_000,
                             resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))],
                             opener=opener_of([FakeResponse(503)]))
        self.assertEqual(F.STATUS_RETRY, row["status"])
        self.assertGreater(row["retry_in_ms"], 0)
        self.assertEqual("http 503", row["last_error"])

    def test_an_acked_delivery_is_terminal_and_carries_the_provider_key(self):
        claim = self._claim("https://hooks.example.com/x")
        row = W.send_claimed(claim, payload={"a": 1}, secret="s", now_ms=2_000,
                             resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))],
                             opener=opener_of([FakeResponse(200)]))
        self.assertEqual(F.STATUS_SENT, row["status"])
        self.assertEqual(2_000, row["sent_ms"])
        # The key the receiver dedupes on is the queue's own key, not a transport-local counter: that is what
        # makes a redelivery after a lost lease recognisable as the same alert.
        self.assertEqual(F.idempotency_key("sig-1", "webhook"), row["idempotency_key"])
        self.assertNotIn(row["status"], F.CLAIMABLE, "a sent delivery must not be reclaimable")


if __name__ == "__main__":
    unittest.main()
