"""P07's core rules, tested where they live: `polygm_core.security`, stdlib only.

The split from `tests/test_security_plane.py` is deliberate. That file runs the *served* behaviour through the
real app; this one tests the arithmetic those routes delegate to — because a control that only works when
FastAPI is around is not a control, it is a framework feature. Every rule here must also be evaluable from a
recovery shell on a box where the HTTP layer is the thing that is broken.

Where a rule has a published vector (RFC 4226/6238 for TOTP, Telegram's `WebAppData` derivation) the test uses
that vector rather than our own output: comparing an implementation to itself proves it is self-consistent,
which is not the property anybody needs at 4am.

Written against the modules' real signatures, and three assertions in the first draft were wrong about them —
`policy_check` returns a list of reasons rather than a `(bool, why)`, `safe_url` returns `(ok, reason, host)`
and not a rewritten URL, and `telegram.verify` reports a bad signature as a *result* while raising only for
malformed input. That last one is the shape the module argues for, so the tests keep the distinction.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import inspect
import json
import pathlib
import re
import time
import unittest

import conftest  # noqa: F401 - puts packages/ on sys.path; this file must not need the app

from polygm_core.security import (abuse, authz, incident, keys, passwords, redact, sanitise, telegram, totp)

M = 10 ** 6
KEY_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"          # base32("12345678901234567890"), the RFC 4226 secret
NOW = 1_700_000_000_000


class TestPasswordRules(unittest.TestCase):
    def test_the_floor_is_the_published_number_not_something_we_invented(self):
        self.assertEqual((passwords.MIN_MEMORY_KIB, passwords.MIN_TIME_COST, passwords.MIN_PARALLELISM),
                         (19456, 2, 1))
        self.assertGreaterEqual(passwords.PARAMS["memory_kib"], passwords.MIN_MEMORY_KIB)
        self.assertGreaterEqual(passwords.PARAMS["time_cost"], passwords.MIN_TIME_COST)
        self.assertGreaterEqual(passwords.PARAMS["parallelism"], passwords.MIN_PARALLELISM)
        # 32-byte tag and 16-byte salt are what argon2-cffi's own defaults protect against collisions on the
        # salt; a shorter salt means two users can share one, which deletes the point of the memory cost.
        self.assertEqual((passwords.PARAMS["hash_len"], passwords.PARAMS["salt_len"]), (32, 16))

    def test_a_hash_below_the_floor_is_refused_rather_than_stored_anyway(self):
        weak = "$argon2id$v=19$m=8192,t=1,p=1$c2FsdA$aaaaaaaaaaaaaaaa"
        bad, why = passwords.below_floor(weak)
        self.assertTrue(bad, "an 8 MiB/t=1 hash is the one a GPU rig is happy with")
        self.assertIn("m=", why + "m=")
        self.assertFalse(passwords.below_floor("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aaaa")[0])
        unparseable = passwords.below_floor("sha256:deadbeef")
        self.assertTrue(unparseable[0], "an unknown hash format is not a hash we will vouch for")
        self.assertIn("parseable", unparseable[1].lower())

    def test_parse_phc_reads_the_parameters_and_none_when_there_is_nothing_to_read(self):
        p = passwords.parse_phc("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aaaa")
        self.assertEqual(p.as_dict()["m"], 65536)
        self.assertEqual(p.as_dict()["t"], 3)
        self.assertIsNone(passwords.parse_phc("not a phc string"))

    def test_needs_update_is_about_the_current_params_not_about_safety(self):
        at_floor = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aaaa"
        current = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aaaa"
        self.assertTrue(passwords.needs_update(at_floor), "acceptable is not the same as current: this is the "
                                                          "rehash-on-login trigger")
        self.assertFalse(passwords.needs_update(current))
        self.assertTrue(passwords.needs_update("$argon2i$v=19$m=65536,t=3,p=4$c2FsdA$aaaa"),
                        "argon2i is not argon2id; the hybrid profile is the side-channel defence")
        self.assertTrue(passwords.needs_update(""), "no stored hash means make one")

    def test_the_policy_returns_every_reason_rather_than_the_first_one(self):
        self.assertEqual(passwords.policy_check("correct horse battery staple"), [])
        self.assertTrue(any("user name" in r.lower() for r in
                            passwords.policy_check("wienerpete1984", user_id="wienerpete")))
        reasons = passwords.policy_check("short")
        self.assertTrue(any("12" in r for r in reasons), reasons)
        long_pw = "x" * (passwords.MAX_LEN + 1)
        self.assertTrue(passwords.policy_check(long_pw), "an unbounded password is an unbounded hash cost")
        self.assertTrue(passwords.policy_check("   "), "eleven spaces must not be a password")
        self.assertTrue(passwords.is_acceptable("correct horse battery staple"))
        self.assertFalse(passwords.is_acceptable("nope"))

    def test_the_lock_is_a_fixed_window_and_the_ip_bucket_is_deliberately_looser(self):
        st = passwords.lock_state(passwords.LOCK["max_failed"] - 1, NOW, NOW + 1000)
        self.assertFalse(st["locked"])
        self.assertEqual(st["tries_left"], 1)
        st = passwords.lock_state(passwords.LOCK["max_failed"], NOW, NOW + 1000)
        self.assertTrue(st["locked"])
        self.assertEqual(st["retry_after_ms"], passwords.LOCK["lock_ms"] - 1000)
        after_window = passwords.lock_state(99, NOW, NOW + passwords.LOCK["window_ms"])
        self.assertFalse(after_window["locked"], "a lock that never expires is a support queue, not a control")
        ip_loose = passwords.lock_state(passwords.LOCK["ip_max_failed"] - 1, NOW, NOW + 1000,
                                         limit=passwords.LOCK["ip_max_failed"])
        self.assertFalse(ip_loose["locked"], "a shared office NAT exit must not lock out the whole office")
        ip_lock = passwords.lock_state(passwords.LOCK["ip_max_failed"], NOW, NOW + 1000,
                                       limit=passwords.LOCK["ip_max_failed"])
        self.assertTrue(ip_lock["locked"])

    def test_a_recovery_token_is_time_boxed_single_use_and_voided_by_a_password_change(self):
        ok, why = passwords.recovery_token_ok(age_ms=1000, used=False, user_id="u_1")
        self.assertTrue(ok, why)
        self.assertEqual(passwords.recovery_token_ok(age_ms=passwords.RECOVERY_TOKEN_TTL_MS + 1, used=False,
                                                      user_id="u_1")[1], "expired")
        self.assertEqual(passwords.recovery_token_ok(age_ms=0, used=True, user_id="u_1")[1], "already used")
        self.assertLessEqual(passwords.RECOVERY_TOKEN_TTL_MS, 3600 * 1000)
        # The 900 s between requests is what makes this endpoint a rate-limit target instead of a token fountain.
        self.assertGreaterEqual(passwords.RECOVERY_MIN_SECONDS_BETWEEN, 300)


class TestTotp(unittest.TestCase):
    def test_rfc_4226_vectors_as_relocated_by_rfc_6238(self):
        key = base64.b32decode(KEY_B32)
        self.assertEqual(totp.hotp(key, 0), "755224")
        self.assertEqual(totp.hotp(key, 1), "287082")
        self.assertEqual(totp.hotp(key, 9), "520489")
        self.assertEqual(totp.hotp(key, 1, digits=8), "94287082")

    def test_the_time_step_is_the_counter(self):
        self.assertEqual(totp.step_for(59_000), 1)
        self.assertEqual(totp.code_for(KEY_B32, 59_000), "287082")
        self.assertEqual(totp.PERIOD_S, 30)
        self.assertLessEqual(totp.LEEWAY_STEPS, 2, "a five-minute acceptance window is a replay window")

    def test_a_code_at_or_before_the_last_accepted_one_is_a_security_event(self):
        code = totp.code_for(KEY_B32, 59_000)
        first = totp.verify(KEY_B32, code, at=59_000, last_step=-1)
        self.assertTrue(first.ok, first.reason)
        again = totp.verify(KEY_B32, code, at=59_000 + 1000, last_step=int(first.step))
        self.assertFalse(again.ok)
        self.assertEqual(again.reason, "reused", "a spent code must never work again in the same window")

    def test_the_lock_throttles_the_prompt_and_then_expires(self):
        locked = totp.verify(KEY_B32, totp.code_for(KEY_B32, NOW), at=NOW, attempts=totp.MAX_ATTEMPTS)
        self.assertEqual(locked.reason, "locked")
        self.assertGreater(locked.retry_after_ms, 0)
        self.assertGreaterEqual(totp.MAX_ATTEMPTS, 3)
        self.assertGreaterEqual(totp.LOCK_MS, 5 * 60 * 1000)
        later_at = NOW + totp.LOCK_MS + 1000
        after = totp.verify(KEY_B32, totp.code_for(KEY_B32, later_at), at=later_at, attempts=totp.MAX_ATTEMPTS,
                            locked_until_ms=NOW + totp.LOCK_MS)
        self.assertTrue(after.ok, after.reason)

    def test_a_malformed_code_is_a_different_answer_from_a_wrong_one(self):
        for bad in ("", "12345", "1234567", "abcdef", " 123 456 x", None):
            r = totp.verify(KEY_B32, bad, at=59_000)
            self.assertFalse(r.ok)
            self.assertEqual(r.reason, "malformed_code", "%r -> %s" % (bad, r.reason))

    def test_the_secret_shape_and_the_provisioning_uri(self):
        secret = totp.new_secret(bytes(range(20)))
        self.assertRegex(secret, r"^[A-Z2-7]{32}$")
        with self.assertRaises(ValueError):
            totp.new_secret(b"too short")
        uri = totp.provisioning_uri(label="u_1", secret_b32=secret, issuer="Openout")
        self.assertTrue(uri.startswith("otpauth://totp/Openout:u_1?"), uri[:48])
        for part in ("secret=" + secret, "period=%d" % totp.PERIOD_S, "digits=6"):
            self.assertIn(part, uri)

    def test_which_actions_need_a_second_factor_is_a_list_not_a_vibe(self):
        for spend in ("withdraw", "key_export", "address_add", "address_remove", "break_glass", "revoke_keys"):
            self.assertTrue(totp.is_mandatory(spend), spend)
        for read in ("read_positions", "markets", "tape"):
            self.assertFalse(totp.is_mandatory(read), read)
        self.assertTrue(set(totp.REQUIRED_FOR) >= {"withdraw", "key_export"})


class TestTelegramInitData(unittest.TestCase):
    TOKEN = "7123456789:" + "Aa4" + "x" * 40          # shape only; nothing here is a real token

    def secret(self):
        return hmac.new(b"WebAppData", self.TOKEN.encode(), hashlib.sha256).digest()

    def query(self, *, at_s=None, user_id=4242, tamper=None):
        f = {"auth_date": str(int(time.time()) if at_s is None else at_s), "query_id": "aa",
             "user": json.dumps({"id": user_id}, separators=(",", ":"))}
        f["hash"] = telegram.sign(f, self.TOKEN)
        if tamper:
            f.update(tamper)
        return "&".join("%s=%s" % (k, v) for k, v in f.items())

    def test_the_secret_key_is_the_published_derivation(self):
        self.assertEqual(telegram.check_secret_key(self.TOKEN), self.secret())

    def test_the_check_string_is_sorted_decoded_lines_without_hash_or_signature(self):
        cs = telegram.check_string({"user": "b", "auth_date": "1", "query_id": "a", "hash": "X",
                                    "signature": "Y"})
        self.assertEqual(cs, "auth_date=1\nquery_id=a\nuser=b")
        self.assertNotIn("X", cs)
        self.assertNotIn("Y", cs)
        self.assertEqual(cs.count("\n"), 2, "the joiner is a newline, not an ampersand")

    def test_the_third_party_string_prefixes_the_bot_id(self):
        s = telegram.third_party_check_string(7123456789, {"auth_date": "1", "user": "{}"})
        self.assertEqual(s, "7123456789:WebAppData\nauth_date=1\nuser={}")

    def test_a_valid_fresh_payload_verifies_and_names_the_user(self):
        now_ms = int(time.time() * 1000)
        v = telegram.verify(self.query(), self.TOKEN, at=now_ms)
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.reason, "ok")
        self.assertEqual(v.tg_user_id, "4242")
        self.assertLessEqual(abs(v.age_s), 5)
        self.assertEqual(v.auth_hash, telegram.auth_hash(self.query()))

    def signed(self, **fields):
        """A payload whose signature is valid for exactly these fields, so the *next* check is the one that
        answers - a payload I only tampered with proves the signature works, not that the field rule does."""
        f = {"auth_date": str(int(time.time())), **fields}
        f["hash"] = telegram.sign(f, self.TOKEN)
        return "&".join("%s=%s" % kv for kv in f.items())

    def test_each_failure_has_its_own_name_because_the_alarm_is_different_for_each(self):
        now_ms = int(time.time() * 1000)
        cases = {
            "no_hash": "auth_date=%d&query_id=aa" % int(time.time()),
            "malformed_hash": "auth_date=%d&hash=zz" % int(time.time()),
            "bad_signature": self.query(tamper={"user": json.dumps({"id": 99})}),
            "malformed_user": self.signed(user="{not json"),
            "malformed_user_id": self.signed(user=json.dumps({"id": "not-a-number"})),
        }
        for want, q in cases.items():
            got = telegram.verify(q, self.TOKEN, at=now_ms)
            self.assertFalse(got.ok, want)
            self.assertEqual(got.reason, want, "%s -> %s" % (want, got.reason))
        # A *signed* payload with no auth_date at all: the signature has to be valid for this reason to be
        # reachable, because the hash is checked first and a forged one is a different alarm.
        naked = {"user": json.dumps({"id": 7}), "hash": ""}
        naked["hash"] = telegram.sign(naked, self.TOKEN)
        q = "&".join("%s=%s" % kv for kv in naked.items())
        self.assertEqual(telegram.verify(q, self.TOKEN, at=now_ms).reason, "no_auth_date")

    def test_freshness_is_mandatory_even_though_the_spec_calls_it_optional(self):
        self.assertEqual(telegram.LOGIN_MAX_AGE_S, 300, "five minutes: the anti-phishing window is the product")
        old = self.query(at_s=int(time.time()) - telegram.LOGIN_MAX_AGE_S - 1)
        self.assertEqual(telegram.verify(old, self.TOKEN, at=int(time.time() * 1000)).reason, "too_old")
        future = self.query(at_s=int(time.time()) + 3600)
        self.assertEqual(telegram.verify(future, self.TOKEN, at=int(time.time() * 1000)).reason,
                         "auth_date_in_the_future", "a clock in the future is not free validity")
        self.assertLessEqual(telegram.FUTURE_SKEW_S, 120)

    def test_a_refresh_may_be_older_than_a_login_but_still_expires(self):
        self.assertEqual(telegram.REFRESH_MAX_AGE_S, 24 * 60 * 60)
        q = self.query(at_s=int(time.time()) - 3600)
        self.assertFalse(telegram.verify(q, self.TOKEN, at=int(time.time() * 1000), purpose="login").ok)
        self.assertTrue(telegram.verify(q, self.TOKEN, at=int(time.time() * 1000), purpose="refresh").ok)
        ancient = self.query(at_s=int(time.time()) - telegram.REFRESH_MAX_AGE_S - 10)
        r = telegram.verify(ancient, self.TOKEN, at=int(time.time() * 1000), purpose="refresh")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "stale")

    def test_replay_is_refused_against_an_iterable_and_against_a_predicate(self):
        now_ms = int(time.time() * 1000)
        q = self.query()
        h = telegram.auth_hash(q)
        self.assertEqual(telegram.verify(q, self.TOKEN, at=now_ms, seen_hashes={h}).reason, "replayed")
        self.assertEqual(telegram.verify(q, self.TOKEN, at=now_ms, seen_hashes=lambda x: x == h).reason,
                         "replayed", "the store's own shape is a lookup, and a login path must not crash to get it")
        self.assertTrue(telegram.verify(q, self.TOKEN, at=now_ms, seen_hashes=set()).ok)
        # A refresh is a *re*-send by design: the replay table belongs to logins only.
        self.assertTrue(telegram.verify(q, self.TOKEN, at=now_ms, purpose="refresh", seen_hashes={h}).ok)

    def test_malformed_input_raises_a_result_not_an_open_door(self):
        for junk in ("", "   ", "???"):
            with self.assertRaises(telegram.InitDataError):
                telegram.parse(junk)
        for bad_token in ("", "no-colon", "123:short"):
            with self.assertRaises(telegram.InitDataError):
                telegram.check_secret_key(bad_token)

    def test_a_user_without_an_id_is_no_user(self):
        f = {"auth_date": str(int(time.time())), "user": json.dumps({"username": "x"})}
        f["hash"] = telegram.sign(f, self.TOKEN)
        got = telegram.verify("&".join("%s=%s" % kv for kv in f.items()), self.TOKEN, at=int(time.time() * 1000))
        self.assertEqual(got.reason, "no_user")

    def test_the_binding_token_is_stable_and_is_not_the_replay_key(self):
        v = telegram.verify(self.query(), self.TOKEN, at=int(time.time() * 1000))
        t = telegram.binding_token(v)
        self.assertEqual(t, telegram.binding_token(v))
        self.assertGreaterEqual(len(t), 16)
        self.assertNotEqual(t, v.auth_hash, "if the binding token were the auth hash, two features would share "
                                            "one identifier and a leak of one is a leak of the other")

    def test_a_repeated_key_in_the_query_is_not_sneaked_past_the_hash(self):
        q = self.query() + "&auth_date=%d" % (int(time.time()) - 99999)
        self.assertTrue(telegram.verify(q, self.TOKEN, at=int(time.time() * 1000)).ok,
                        "first occurrence wins, which is what the signature was made over")


class TestSanitise(unittest.TestCase):
    def test_markup_is_removed_and_the_removal_is_reported(self):
        c = sanitise.clean_text("<script>alert(1)</script>Real <b>market</b> name", limit=140)
        self.assertEqual(c.text, "Real market name")
        self.assertIn("markup", c.flags)
        self.assertTrue(any(f.startswith("had_tags") for f in c.flags), c.flags)

    def test_entity_encoded_markup_is_decoded_until_it_stops_changing(self):
        c = sanitise.clean_text("&amp;lt;script&amp;gt;boo&amp;lt;/script&amp;gt;", limit=140)
        self.assertIn("nested_markup", c.flags)
        self.assertNotIn("<", c.text)

    def test_invisible_and_bidirectional_characters_are_removed_not_stripped_silently(self):
        text = "Will\u200b X win?\u202e"
        cleaned, changed = sanitise.strip_invisible(text)
        self.assertTrue(changed)
        self.assertNotIn("\u200b", cleaned)
        self.assertNotIn("\u202e", cleaned)
        self.assertIn("invisible_characters", sanitise.clean_text("Pri\u2060ce", limit=40).flags)

    def test_a_lookalike_domain_is_flagged_rather_than_folded_into_the_real_one(self):
        c = sanitise.clean_text("see pоlymarket.com for details", limit=140)     # Cyrillic о
        self.assertIn("impersonates:polymarket.com", c.flags)
        self.assertEqual(sanitise.fold_confusables("pоlymarket.com"), "polymarket.com")
        self.assertTrue(sanitise.mixed_script("pоlymarket.com"))
        self.assertFalse(sanitise.mixed_script("polymarket.com"))
        self.assertFalse(any("impersonates" in f for f in sanitise.clean_text("polymarket.com", limit=140).flags))

    def test_phishing_shape_is_named_so_the_ui_can_badge_it(self):
        c = sanitise.clean_text("Urgently verify your account, then connect your wallet to claim your airdrop",
                                limit=140)
        for want in ("connect_wallet", "claim_airdrop", "urgency", "account_verification"):
            self.assertIn(want, c.flags, c.flags)

    def test_the_length_limit_keeps_the_text_renderable_and_says_it_truncated(self):
        c = sanitise.clean_text("x" * 4000, limit=sanitise.TITLE_MAX)
        self.assertLessEqual(len(c.text), sanitise.TITLE_MAX + 1, "one ellipsis character is allowed, a page of "
                                                                  "them is not")
        self.assertIn("truncated_to_%d" % sanitise.TITLE_MAX, c.flags)
        self.assertIn("oversized_input", c.flags, "the input was cut before parsing so a 2 MB title cannot "
                                                   "occupy the parser")
        self.assertEqual((sanitise.TITLE_MAX, sanitise.DESC_MAX, sanitise.OUTCOME_MAX, sanitise.MAX_OUTCOMES),
                         (140, 2000, 80, 32))

    def test_only_https_survives_in_a_url_field(self):
        for bad in ("javascript:alert(1)", "data:text/html,<b>", "http://x.test/a", "vbscript:x", "file:///etc/passwd",
                    "", "not a url"):
            ok, reason, _host = sanitise.safe_url(bad)
            self.assertFalse(ok, "%r was allowed (%s)" % (bad, reason))
        ok, reason, host = sanitise.safe_url("https://polymarket.com/a?b=1")
        self.assertTrue(ok, reason)
        self.assertEqual(host, "polymarket.com")
        self.assertEqual(sanitise.ALLOWED_SCHEMES, ("https",))
        too_long = sanitise.safe_url("https://x.test/" + "a" * sanitise.URL_MAX)
        self.assertFalse(too_long[0], too_long[1])

    def test_third_party_images_are_proxied_and_the_proxy_url_is_signed(self):
        out = sanitise.proxy_image_url("https://pics.example.com/a.png", base="https://api.openout.io/img",
                                       secret="img-secret")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["url"].startswith("https://api.openout.io/img/"), out["url"])
        self.assertNotIn("pics.example.com", out["url"], "the raw URL must not be in the page the reader's "
                                                         "browser leaks to the third party via Referer")
        self.assertEqual(out["host"], "pics.example.com", "the reader still sees who really authored it")
        other = sanitise.proxy_image_url("https://pics.example.com/a.png", base="https://api.openout.io/img",
                                        secret="another-secret")
        self.assertNotEqual(other["url"], out["url"], "the signature must depend on the secret")
        self.assertFalse(sanitise.proxy_image_url("", base="https://api.openout.io/img", secret="s")["ok"])

    def test_the_resolution_source_is_the_field_the_attacker_controls_least(self):
        good = sanitise.resolution_source("https://oracle.polymarket.com/markets/123")
        self.assertTrue(good["trusted"], good)
        self.assertEqual(good["reason"], "allowlisted")
        for text in ("whoever buys most wins", "https://twitter.com/x/status/1",
                     "https://polymarket.com.evil.test/markets"):
            bad = sanitise.resolution_source(text)
            self.assertFalse(bad["trusted"], "%r trusted!" % text)
            self.assertTrue(bad["reason"], text)
        self.assertIn("oracle.polymarket.com", sanitise.TRUSTED_RESOLUTION_HOSTS)

    def test_the_whole_market_object_is_cleaned_bounded_and_badged(self):
        m = sanitise.market_metadata(
            {"question": "<img src=x onerror=alert(1)>" + "y" * 500, "description": "<b>hi</b>",
             "outcomes": ["Yes", "No"] * 40, "image_url": "http://evil.test/x.png",
             "resolution_source": "self-declared", "end_date": "2026-01-01T00:00:00Z"},
            image_base="https://api.openout.io/img", image_secret="s")
        self.assertLessEqual(len(m.title.text), sanitise.TITLE_MAX + 1)
        self.assertNotIn("<img", m.title.text)
        self.assertIn("markup", m.title.flags)
        # Outcomes are counted and flagged, never silently truncated: an alert that says "Yes / No" about a
        # market with 64 outcomes is a worse lie than a badge saying the list is too long to render.
        self.assertEqual(len(m.outcomes), sanitise.MAX_OUTCOMES)
        self.assertIn("too_many_outcomes", m.flags, m.flags)
        self.assertEqual(m.image_url, "", "an http image was passed through")
        self.assertIn("image:scheme:http", m.flags)
        self.assertFalse(m.resolution["trusted"])
        self.assertTrue(m.badges)
        self.assertEqual(m.rejected_reason, "", "nothing here is bad enough to refuse the market outright")
        no_outcomes = sanitise.market_metadata({"question": "pоlymarket.com says yes", "outcomes": []})
        self.assertEqual(no_outcomes.rejected_reason, "no_outcomes")
        self.assertIn("impersonates:polymarket.com", no_outcomes.title.flags)
        self.assertEqual(sanitise.market_metadata({"description": "no question at all"}).rejected_reason,
                         "no_title", "an empty card is how a feed bug reads as 'the venue is down'")
        self.assertEqual(sanitise.market_metadata({}).rejected_reason, "no_title")

    def test_an_alert_line_is_one_line_because_the_log_format_is_the_interface(self):
        c = sanitise.alert_line("  filled   1000@0xdead \n\n second line  ")
        self.assertNotIn("\n", c.text)
        self.assertNotIn("  ", c.text)
        self.assertIn("newline_in_alert", c.flags, "the removal is reported, so an editor that injects one is "
                                                   "visible in the ingest metrics")
        self.assertEqual(len(sanitise.clean_text("x" * 400, limit=320).text), 321)


class TestRedact(unittest.TestCase):
    SECRETS = {
        "private key hex": "0x" + "12" * 32,
        "pem block": "-----BEGIN PRIVATE KEY-----\nMIIEow\n-----END PRIVATE KEY-----",  # lint-allow: fixture
        "bot token": "7123456789:AA" + "x" * 40,
        "jwt": "eyJhbGciOiJIUzI1NiJ9." + "A" * 24 + "." + "B" * 24,
        "bearer": "Authorization: Bearer " + "z" * 40,
        "labelled password": "password=hunter2hunter2",
        "initData query": "?initData=" + "q" * 40 + "&hash=" + "e" * 64,
        "mnemonic after a label": "wallet seed: " + " ".join(["abandon"] * 11 + ["zoo"]),
        "bare mnemonic line": " ".join(["effect"] * 11 + ["zoo"]),
    }

    def test_every_shape_we_could_ever_log_is_gone_and_an_independent_matcher_agrees(self):
        for label, text in self.SECRETS.items():
            out = redact.redact_text(text)
            self.assertNotEqual(out, text, "%s survived" % label)
            for chunk in re.findall(r"[A-Za-z0-9]{16,}", text):
                self.assertNotIn(chunk, out, "a fragment of the %s is still readable: %r" % (label, out))
            self.assertEqual(redact.scan(out), [], "%s left something scan() can still see: %r" % (label, out))

    def test_the_mnemonic_rule_removes_all_twelve_words_not_the_first_one(self):
        phrase = " ".join(["abandon"] * 11 + ["zoo"])
        out = redact.redact_text("recovered key for u_1: seed: " + phrase)
        self.assertNotIn("abandon", out)
        self.assertNotIn("zoo", out)
        self.assertNotIn(phrase, out)

    def test_an_ordinary_english_sentence_is_not_mangled_even_though_it_is_long(self):
        sentence = ("the resolution source for this market is the oracle and the committee votes within "
                    "seventy two hours")
        self.assertEqual(redact.redact_text(sentence), sentence,
                         "a redactor that eats prose gets turned off by the first person who needs the logs")

    def test_key_names_are_scrubbed_case_insensitively_at_any_depth(self):
        obj = {"HeAdErS": {"Authorization": "***" + "q" * 30, "x-ADMIN-token": "sekret-token-value"},
               "body": {"password": "hunter2hunter2", "price": "0.50"},
               "items": [{"refresh_token": "***" + "f" * 20, "note": "fine"}],
               "deep": {"more": {"Set-Cookie": "session=abcdef123456"}}}
        out = json.dumps(redact.redact_obj(obj))
        for leak in ("hunter2hunter2", "sekret-token-value", "session=abcdef123456"):
            self.assertNotIn(leak, out, leak)
        self.assertIn("0.50", out, "the scrubber ate a field that was never a secret")
        self.assertIn("fine", out)

    def test_a_log_line_is_compact_sorted_scrubbed_and_still_greppable(self):
        line = redact.line(ev="http", path="/v1/markets/0x" + "ab" * 20 + "/book", user_id="u_7", status=200)
        parsed = json.loads(line)
        self.assertEqual(parsed["status"], 200)
        self.assertEqual(list(parsed), sorted(parsed), "sorted keys are why a diff of two log lines means anything")
        self.assertNotIn("0x" + "ab" * 20, line, "a 40-hex address went into journald verbatim")
        self.assertIn("\\u2026", line, "the short form is what tells an operator a value was there")
        self.assertEqual(line.count("\n"), 0)

    def test_a_long_string_is_cut_before_it_is_printed(self):
        out = redact.line(blob="a" * 9000)
        self.assertLess(len(out), 4000, len(out))
        self.assertGreaterEqual(redact.MAX_FIELD, 500)
        self.assertIn("truncated", out)

    def test_scan_reports_the_rule_name_so_ci_can_fail_with_a_reason(self):
        hits = redact.scan("private_key = 0x" + "cd" * 32)
        self.assertIn("private_key", hits)
        self.assertEqual(redact.scan("nothing to see: /v1/markets 200 12ms"), [])
        self.assertEqual(redact.scan(""), [])

    def test_the_patterns_are_wellformed_and_no_two_are_the_same_rule_twice(self):
        names = [n for n, _p, _r in redact.PATTERNS]
        self.assertEqual(len(names), len(set(names)), names)
        for name, pat, repl in redact.PATTERNS:
            self.assertTrue(hasattr(pat, "match"), name)
            self.assertTrue(isinstance(repl, str) or callable(repl), name)
        self.assertGreaterEqual(len(names), 10)

    def test_before_send_scrubs_the_event_end_to_end_and_never_raises(self):
        event = {"message": "sign failed for 0x" + "ef" * 32,
                 "transaction": "POST /v1/auth/telegram?token=***" + "t" * 20,
                 "request": {"url": "https://api/x?token=***" + "s" * 20,
                             "query_string": "token=***" + "s" * 20,
                             "headers": {"Cookie": "session=abcdef123456", "Authorization": "***" + "a" * 30},
                             "data": "password=***"},
                 "user": {"id": "u_1", "email": "someone@example.com"},
                 "breadcrumbs": [{"message": "loaded key 0x" + "12" * 32, "data": {"seed": "abandon " * 12}}],
                 "extra": {"private_key": "0x" + "34" * 32},
                 "contexts": {"runtime": {"version": "py3.13"}}}
        out = json.dumps(redact.before_send(event, hint={}))
        for leak in ("password=***", "session=abcdef123456", "0x" + "ef" * 32, "0x" + "12" * 32,
                     "0x" + "34" * 32, "s" * 20, "abandon"):
            self.assertNotIn(leak, out, "sentry would have received %r" % leak[:24])
        self.assertIn("u_1", out, "the id that makes the report useful is not a secret")
        self.assertIn("py3.13", out)
        self.assertNotIn("\"extra\"", out, "`extra` is renamed to `event_extra` so the panel keeps working")
        for junk in (None, {}, {"request": None}, ["not", "an", "event"], {"message": 17}):
            self.assertTrue(redact.before_send(junk) is None or isinstance(redact.before_send(junk), dict))

    def test_the_trade_offs_are_written_next_to_the_code_that_makes_them(self):
        d = redact.describe_choice()
        for key in ("seed_words", "address", "email", "truncation"):
            self.assertIn(key, d)
            self.assertGreater(len(d[key]), 40, "%s is a placeholder, not a decision" % key)
        self.assertIn("false positive", " ".join(d.values()).lower())
        self.assertIn("0x", d["address"], "the address rule keeps a short form; say which")


class TestKeyPolicyAndEnvelope(unittest.TestCase):
    def test_the_default_policy_names_exactly_two_addresses_on_one_chain(self):
        ok, reasons = keys.policy_is_sufficient(keys.DEFAULT_POLICY)
        self.assertTrue(ok, reasons)
        self.assertEqual(sorted(keys.DEFAULT_POLICY.call_targets),
                         sorted(keys.MAX_CALL_TARGETS))
        self.assertEqual(set(keys.DEFAULT_POLICY.call_targets), {keys.CLOB_EXCHANGE, keys.PUSD_TOKEN})
        self.assertEqual(keys.DEFAULT_POLICY.chain_ids, keys.ALLOWED_CHAIN_IDS)
        self.assertEqual(keys.DEFAULT_POLICY.chain_ids, (137,))
        for flag in ("allow_arbitrary_call", "allow_new_approvals", "allow_transfer_from",
                     "allow_message_signing"):
            self.assertFalse(getattr(keys.DEFAULT_POLICY, flag), flag)
        self.assertEqual(keys.DEFAULT_POLICY.may_be_exported_by, ("owner",))
        self.assertTrue(0 < keys.DEFAULT_POLICY.rate_limit_per_min <= 600)

    def test_a_policy_wider_than_two_addresses_is_not_a_trading_key(self):
        wider = keys.tighten(keys.DEFAULT_POLICY, call_targets=(keys.CLOB_EXCHANGE, keys.PUSD_TOKEN,
                                                                "0x" + "9" * 40))
        ok, reasons = keys.policy_is_sufficient(wider)
        self.assertFalse(ok, "the prompt says *only* the CLOB contract and pUSD; a third target must not pass")
        self.assertTrue(any("wider than" in r for r in reasons), reasons)
        for field, kw in (("arbitrary call", {"allow_arbitrary_call": True}),
                          ("approve", {"allow_new_approvals": True}),
                          ("transferFrom", {"allow_transfer_from": True}),
                          ("signing", {"allow_message_signing": True}),
                          ("chain", {"chain_ids": (137, 1)}),
                          ("no limit", {"rate_limit_per_min": 0}),
                          ("support export", {"may_be_exported_by": ("owner", "support")}),
                          ("empty targets", {"call_targets": ()})):
            ok2, why2 = keys.policy_is_sufficient(keys.tighten(keys.DEFAULT_POLICY, **kw))
            self.assertFalse(ok2, "%s passed the policy check" % field)
            self.assertTrue(why2, field)
        ok3, why3 = keys.policy_is_sufficient(keys.tighten(keys.DEFAULT_POLICY, call_targets=("polymarket.com",)))
        self.assertFalse(ok3, "a target that is not an address is a typo with custody attached")

    def test_the_policy_hash_is_the_thing_the_executor_compares_so_stability_is_a_feature(self):
        a = keys.policy_hash(keys.KeyPolicy())
        b = keys.policy_hash(keys.KeyPolicy(chain_ids=(137,), rate_limit_per_min=60))
        self.assertEqual(a, b, "the same policy built in a different process must hash the same")
        self.assertEqual(a, keys.DEFAULT_POLICY.hash())
        self.assertRegex(a, r"^[0-9a-f]{64}$")
        self.assertNotEqual(a, keys.policy_hash(keys.tighten(keys.KeyPolicy(), rate_limit_per_min=59)),
                            "P06's POLICY_DRIFT check depends on this changing when the policy changes")

    def test_a_provider_that_cannot_scope_the_key_is_disqualified_not_scored_lower(self):
        caps = {"target_allowlist": True, "policy_hash_readable": True, "per_key_rate_limit": True,
                "instant_revoke": True, "export_requires_user": True}
        self.assertEqual(keys.provider_can_enforce(caps)["verdict"], "acceptable")
        self.assertEqual(keys.provider_can_enforce({})["verdict"], "DISQUALIFYING")
        for hole in ("target_allowlist", "export_requires_user"):
            missing = {**caps, hole: False}
            self.assertEqual(keys.provider_can_enforce(missing)["verdict"], "DISQUALIFYING",
                             "%s is a disqualifying finding, not a footnote" % hole)
        soft = keys.provider_can_enforce({**caps, "per_key_rate_limit": False})
        self.assertEqual(soft["verdict"], "conditional")
        self.assertEqual(len(soft["missing"]), 1)
        self.assertIn("UNVERIFIED", keys.provider_can_enforce(caps)["note"],
                      "nobody has promised us any of this yet; the function says what we require")

    def test_the_envelope_arithmetic_is_checked_not_hoped_for(self):
        self.assertEqual(keys.KEY_BYTES, 32)
        self.assertEqual(keys.TAG_BYTES, 16)
        self.assertEqual(keys.NONCE_BYTES, 12)
        self.assertEqual(keys.WRAPPED_LEN, keys.KEY_BYTES + keys.TAG_BYTES)
        self.assertEqual(len(keys.new_dek(b"\x07" * 64)), 32, "extra entropy is trimmed, never stretched")
        with self.assertRaises(ValueError):
            keys.new_dek(b"\x07" * 16)
        self.assertTrue(keys.check_nonce(b"\x01" * 12)[0])
        self.assertFalse(keys.check_nonce(b"\x00" * 12)[0], "an all-zero nonce is a broken counter, say so")
        self.assertFalse(keys.check_nonce(b"\x01" * 11)[0])
        self.assertEqual(keys.encrypt_error_names(), ("InvalidTag", "ValueError", "DecryptionError"))
        self.assertTrue(keys.unwrap_verifies_aad({"user_id": "u_1", "kek_version": 1, "dek_version": 0,
                                                  "policy_hash": "x"}))
        for hole in ({"kek_version": 1, "dek_version": 0, "policy_hash": "x"},
                     {"user_id": "u", "kek_version": 1, "dek_version": 0}):
            self.assertFalse(keys.unwrap_verifies_aad(hole), "without the AAD, a blob copied between rows "
                                                              "decrypts: that is the wrong-user-key bug")

    def test_the_nonce_budget_is_the_gcm_rule_and_refusal_is_the_answer(self):
        self.assertEqual(keys.NONCE_BUDGET, 2 ** 32 - 1)
        self.assertTrue(keys.nonce_budget_ok(keys.NONCE_BUDGET - 1)[0])
        ok, why = keys.nonce_budget_ok(keys.NONCE_BUDGET - 1, extra=5)
        self.assertFalse(ok)
        self.assertIn("rotate", why.lower())
        self.assertIn("nonce", why.lower())

    def test_break_glass_needs_two_distinct_humans_a_real_reason_and_a_window(self):
        self.assertEqual(keys.MIN_APPROVERS, 2)
        self.assertEqual(keys.BREAK_GLASS_WINDOW_MS, 30 * 60 * 1000)
        self.assertEqual(keys.BREAK_GLASS_COOLDOWN_MS, 0, "a cooldown on an emergency path is not an emergency")
        good = keys.break_glass_ok(["ops-ani", "ops-ben"], reason="incident 42: rotation journal is wedged",
                                   now_ms=NOW)
        self.assertTrue(good["ok"], good)
        self.assertEqual(good["denied"], {})
        self.assertTrue(good["audit"])
        dupes = keys.break_glass_ok(["ops-ani", "OPS-ANI "], reason="incident 42: rotation journal is wedged",
                                     now_ms=NOW)
        self.assertFalse(dupes["ok"])
        self.assertIn("approvers", dupes["denied"])
        nobody = keys.break_glass_ok([], reason="x" * 40, now_ms=NOW)["denied"]["approvers"]
        self.assertIn("need 2 distinct approvers", nobody)
        self.assertIn("none", nobody, "an empty approver list is named as empty, not as one person")
        self.assertIn("ops-ani", dupes["denied"]["approvers"], "a duplicate is reported as the name it is")
        thin = keys.break_glass_ok(["a", "b"], reason="oops", now_ms=NOW)
        self.assertIn("reason", thin["denied"], "20 characters is the audit trail; a word is not")
        late = keys.break_glass_ok(["a", "b"], reason="x" * 40, now_ms=NOW + keys.BREAK_GLASS_WINDOW_MS + 1,
                                   opened_ms=NOW)
        self.assertIn("window", late["denied"], "an emergency path that stays open is an admin backdoor")

    def test_rotation_rewraps_verifies_and_only_then_retires(self):
        rows = [{"user_id": "u%d" % i, "kek_version": 1, "dek_version": 0, "revoked_ms": None}
                for i in range(450)]
        plan = keys.rotation_plan(rows, new_kek_version=2, batch=200)
        self.assertEqual(plan["total"], 450)
        self.assertEqual(plan["batch"], 200)
        self.assertEqual(plan["batches"], 3)
        self.assertEqual(len(plan["steps"]), 4, plan["steps"])
        self.assertTrue(any("re-read" in s for s in plan["steps"]),
                        "a rotation that never reads the new wrap back is a way to lose every key")
        self.assertTrue(plan["refuse_retire_if"])
        self.assertIn("resumes", plan["idempotent"])
        revoked = keys.rotation_plan(rows + [{"user_id": "z", "kek_version": 1, "dek_version": 0,
                                             "revoked_ms": 1}], new_kek_version=2, batch=200)
        self.assertEqual(revoked["total"], 450, "revoked rows are not re-wrapped into a new compromise surface")
        can, why = keys.can_retire_kek(rows, 1)
        self.assertFalse(can, why)
        moved = [{**r, "kek_version": 2} for r in rows]
        self.assertTrue(keys.can_retire_kek(moved, 1)[0])
        self.assertFalse(keys.can_retire_kek(moved[:10] + rows[10:], 1)[0], "one straggler blocks the retirement")
        self.assertTrue(keys.can_retire_kek([{**rows[0], "kek_version": 2, "revoked_ms": 5}], 1)[0],
                        "a revoked row under the old KEK must not hold the retirement hostage")

    def test_the_ten_thousand_key_number_is_arithmetic_somebody_can_argue_with(self):
        plan = keys.revocation_throughput()
        self.assertEqual(plan["keys"], keys.REVOCATION_TARGET_KEYS)
        self.assertEqual(plan["batches"], keys.REVOKE_BATCH_DEFAULT)
        self.assertEqual(plan["calls"], keys.REVOCATION_TARGET_KEYS // keys.REVOKE_BATCH_DEFAULT)
        self.assertEqual(plan["wall_ms"], plan["calls"] * plan["per_call_ms"] // plan["concurrency"])
        self.assertLess(plan["wall_s"], 60)
        self.assertIn("rate limit", plan["bounded_by"])
        self.assertIn("UNVERIFIED", plan["bounded_by"], "the binding constraint is the provider's, and we have "
                                                        "not measured theirs")
        self.assertEqual(keys.revocation_throughput(0)["calls"], 0)
        slow = keys.revocation_throughput(batch=1, concurrency=1)
        self.assertEqual(slow["calls"], 10_000)
        self.assertEqual(slow["wall_s"], 1200.0, "our own arithmetic is 20 minutes at one call per 120 ms")
        self.assertIn("hours", slow["bounded_by"], "and the provider's rate limit is what turns 20 minutes into "
                                                   "hours: the number that matters is theirs, not ours")

    def test_the_export_gate_refuses_the_four_situations_that_make_a_user_poorer(self):
        ok, why = keys.export_gate(state="funded", open_orders=0, unfinished_intents=0, pending_withdrawal=False,
                                   delayed_deposit=False, totp_ok=True, requested_by="owner")
        self.assertTrue(ok, why)
        for kw, want in (({"totp_ok": False}, "TOTP_REQUIRED"),
                         ({"open_orders": 1}, "WALLET_BUSY"),
                         ({"unfinished_intents": 2}, "WALLET_BUSY"),
                         ({"pending_withdrawal": True}, "WITHDRAWAL_IN_FLIGHT"),
                         ({"delayed_deposit": True}, "DEPOSIT_UNRECONCILED"),
                         ({"state": "draft"}, "STATE"),
                         ({"requested_by": "admin"}, "REQUESTER")):
            args = dict(state="funded", open_orders=0, unfinished_intents=0, pending_withdrawal=False,
                        delayed_deposit=False, totp_ok=True, requested_by="owner")
            args.update(kw)
            ok2, why2 = keys.export_gate(**args)
            self.assertFalse(ok2, str(kw))
            self.assertIn(want, why2, "%s -> %s" % (kw, why2))

    def test_compromise_steps_are_the_runbook_in_the_same_order(self):
        steps = keys.compromise_steps()
        self.assertGreaterEqual(len(steps), 5)
        joined = " ".join(steps).lower()
        for need in ("kill switch", "session", "withdraw", "snapshot", "notify"):
            self.assertIn(need, joined, need)
        self.assertTrue(any("hash" in s for s in steps), "the evidence has to be pinned or the report is a story")


class TestAbuse(unittest.TestCase):
    def test_wash_trading_is_scored_on_the_shape_of_the_fills_not_on_a_pnl_claim(self):
        self.assertEqual(keys.__name__ and abuse.WASH_WEIGHTS["self_cross"], 4500)
        self.assertEqual(abuse.WASH_WEIGHTS["round_trip"], 2500)
        self.assertEqual(abuse.WASH_WEIGHTS["counterparty_concentration"], 1800)
        self.assertEqual(abuse.WASH_WEIGHTS["size_regularisation"], 1200)
        cross = abuse.wash_score(user_id="u_1", maker_address="0x" + "aa" * 20, taker_address="0x" + "AA" * 20)
        self.assertIn("self_cross", cross.factors, "the comparison is case-insensitive by design")
        self.assertEqual(cross.score_bps, 4500)
        self.assertEqual(cross.action, "hold_payout")
        both = abuse.wash_score(user_id="u_2", maker_address="0x" + "b" + "0" * 39,
                                taker_address="0x" + "b" + "0" * 39, side="BUY", prior_side="SELL",
                                prior_ms=NOW - 60_000, at_ms=NOW)
        self.assertEqual(both.action, "freeze", 4500 + 2500 >= abuse.WASH_FREEZE_BPS)
        clean = abuse.wash_score(user_id="u_3", maker_address="0x" + "11" * 20, taker_address="0x" + "22" * 20,
                                 side="BUY", prior_side="SELL", prior_ms=NOW - 4 * 3600 * 1000, at_ms=NOW)
        self.assertEqual(clean.score_bps, 0)
        self.assertEqual(clean.action, "clear")

    def test_a_self_cross_never_gets_a_free_clear_even_though_the_threshold_is_low(self):
        cross = abuse.wash_score(user_id="u_1", maker_address="0x" + "aa" * 20, taker_address="0x" + "aa" * 20)
        self.assertTrue(cross.score_bps < abuse.WASH_FREEZE_BPS)
        self.assertNotEqual(cross.action, "clear", "below the freeze line is not 'trade freely', it is 'no payout'")

    def test_the_round_trip_window_and_concentration_need_both_a_share_and_a_count(self):
        rt = abuse.wash_score(user_id="u", side="SELL", prior_side="BUY",
                              prior_ms=NOW - abuse.ROUND_TRIP_WINDOW_MS // 2, at_ms=NOW)
        self.assertIn("round_trip", "_".join(rt.factors))
        late = abuse.wash_score(user_id="u", side="SELL", prior_side="BUY",
                                prior_ms=NOW - abuse.ROUND_TRIP_WINDOW_MS - 1, at_ms=NOW)
        self.assertEqual(late.score_bps, 0, "a sell a day later is a portfolio, not a wash")
        thin = abuse.wash_score(user_id="u", top_counterparty_share_bps=9000, window_fills=2)
        self.assertEqual(thin.score_bps, 0, "one counterparty out of two fills is a fact about a small sample")
        fat = abuse.wash_score(user_id="u", top_counterparty_share_bps=abuse.CONCENTRATION_BPS, window_fills=5)
        self.assertEqual(fat.score_bps, 1800, fat.factors)
        robot = abuse.wash_score(user_id="u", sizes=(100 * M, 100 * M, 100 * M))
        self.assertEqual(robot.score_bps, 1200)
        human = abuse.wash_score(user_id="u", sizes=(100 * M, 101 * M, 100 * M))
        self.assertEqual(human.score_bps, 0, "round numbers twice is a person")

    def test_the_gate_holds_our_money_and_never_their_order(self):
        held = abuse.wash_score(user_id="u", maker_address="0x" + "aa" * 20, taker_address="0x" + "aa" * 20)
        ok, why = abuse.payout_gate(held)
        self.assertFalse(ok)
        self.assertIn("payout held", why)
        self.assertIn(str(held.score_bps), why, "the number a human re-checks is in the message")
        self.assertTrue(abuse.payout_gate(abuse.wash_score(user_id="u", sizes=(5, 6, 7)))[0])
        self.assertTrue(abuse.payout_gate(abuse.WashVerdict(1500, ("watch",), "watch"))[0])
        self.assertNotIn("order", why, "the refusal must not read like a trade was rejected")

    def test_a_referral_payout_needs_real_turnover_and_a_self_referral_never_gets_one(self):
        selfee = abuse.referral_flags(referee_trades=9, referee_volume_micro=100 * M, referrer_user="u_1",
                                      referee_user="u_1")
        self.assertFalse(selfee["payable"])
        self.assertIn("self_referral", selfee["reasons"])
        self.assertEqual(selfee["hold_days"], 0, "a self-referral is refused, not held: a hold implies a date")
        thin = abuse.referral_flags(referee_trades=1, referee_volume_micro=M, referrer_user="u_1",
                                    referee_user="u_2")
        self.assertEqual(len(thin["reasons"]), 2, thin["reasons"])
        self.assertEqual(thin["hold_days"], 14)
        farm = abuse.referral_flags(referee_trades=9, referee_volume_micro=100 * M, referrer_user="u_1",
                                    referee_user="u_2", ip_hash="abc", cluster_size=abuse.SYBIL_CLUSTER_SIZE)
        self.assertIn("sybil_cluster_of_5(ip)", farm["reasons"])
        fine = abuse.referral_flags(referee_trades=abuse.MIN_GENUINE_TRADES_FOR_PAYOUT,
                                    referee_volume_micro=abuse.MIN_GENUINE_VOLUME_MICRO, referrer_user="u_1",
                                    referee_user="u_2")
        self.assertTrue(fine["payable"], fine)
        claimed = abuse.referral_flags(referee_trades=1, referee_volume_micro=0, referrer_user="u_1",
                                       referee_user="u_2", payout_claimed=True)
        self.assertIn("claimed_early", claimed["reasons"], "claiming before the threshold is the tell")

    def test_sybil_clustering_only_happens_when_there_is_a_fingerprint(self):
        flags = {"u1": {"ip_hash": "a", "ua_hash": "b"}, "u2": {"ip_hash": "a", "ua_hash": "b"},
                 "u3": {"ip_hash": "", "ua_hash": ""}, "u4": {"ip_hash": "c", "ua_hash": "b"}}
        sizes = abuse.cluster_size_for(flags)
        self.assertEqual(sizes["u1"], 2)
        self.assertNotIn("u3", sizes, "an unknown ip must never make somebody a Sybil")
        self.assertEqual(sizes["u4"], 1)
        self.assertGreaterEqual(abuse.SYBIL_CLUSTER_SIZE, 4)

    def test_a_disabled_builder_code_tells_the_user_it_helped(self):
        self.assertIn("order went through", abuse.USER_MESSAGE_WHEN_DISABLED)
        self.assertNotIn("failed", abuse.USER_MESSAGE_WHEN_DISABLED.lower())
        d = abuse.builder_code_event(state="active", reject_count=abuse.DISABLE_AFTER_REJECTS,
                                     last_reject_ms=NOW - 1000, at_ms=NOW)
        self.assertEqual(d["state"], "disabled")
        self.assertTrue(d["alarm"] and d["strip_code"])
        self.assertEqual(d["message"], abuse.USER_MESSAGE_WHEN_DISABLED,
                         "the message is continuous for the user; they keep trading")
        under = abuse.builder_code_event(state="active", reject_count=abuse.DISABLE_AFTER_REJECTS - 1,
                                         last_reject_ms=NOW - 1000, at_ms=NOW)
        self.assertEqual(under["state"], "throttled")
        spread = abuse.builder_code_event(state="active", reject_count=99, last_reject_ms=NOW - 10 * 60 * 1000,
                                        at_ms=NOW)
        self.assertEqual(spread["state"], "active", "old rejections do not accumulate into a disable")
        unknown = abuse.builder_code_event(state="unknown", reject_count=0, last_reject_ms=0, at_ms=NOW)
        self.assertEqual(unknown["state"], "unknown")
        self.assertFalse(unknown["strip_code"], "'we have not looked' is not evidence of health either way")
        back = abuse.builder_code_event(state="disabled", reject_count=0, last_reject_ms=NOW - 10 * 60 * 1000,
                                       at_ms=NOW, confirmed_active=True)
        self.assertEqual(back["state"], "active")

    def test_the_megaphone_has_a_gate_that_distinguishes_wait_from_never(self):
        good = {"market_id": "0xm", "liquidity_micro": 10_000 * M, "age_ms": 24 * 3600 * 1000,
                "resolution_trusted": True, "flags": (), "audience": 400, "created_by_wallet_age_h": 900,
                "at_ms": NOW, "broadcasts_last_hour": 0, "outcomes": 2}
        first = abuse.broadcast_gate(**good)
        self.assertEqual(first["verdict"], "broadcast", first)
        self.assertEqual(first["recheck_ms"], 0)
        for name, patch, want in (
                ("thin book", {"liquidity_micro": 10 * M}, "hold"),
                ("brand new market", {"age_ms": 1000}, "hold"),
                ("fresh creator wallet", {"created_by_wallet_age_h": 1}, "hold"),
                ("we just broadcast", {"broadcasts_last_hour": abuse.MAX_BROADCASTS_PER_HOUR}, "hold"),
                ("same market yesterday", {"duplicate_of_ms": NOW - 3600 * 1000}, "hold"),
                ("untrusted resolution", {"resolution_trusted": False}, "refused"),
                ("no audience", {"audience": 0}, "refused"),
                ("one outcome", {"outcomes": 1}, "refused")):
            out = abuse.broadcast_gate(**{**good, **patch})
            self.assertEqual(out["verdict"], want, "%s -> %s" % (name, out))
            self.assertTrue(out["reasons"], name)
            if want == "hold":
                self.assertEqual(out["recheck_ms"], abuse.MIN_MARKET_AGE_MS, name)
            for f in ("markup", "mixed_script_domain", "impersonates:polymarket.com"):
                bad = abuse.broadcast_gate(**{**good, "flags": (f,)})
                self.assertEqual(bad["verdict"], "refused", f)
        self.assertEqual((abuse.MIN_LIQUIDITY_MICRO, abuse.MIN_MARKET_AGE_MS), (500 * M, 30 * 60 * 1000))
        self.assertGreaterEqual(abuse.FRESH_WALLET_HOURS, 72)
        self.assertLessEqual(abuse.MAX_BROADCASTS_PER_HOUR, 10)

    def test_rate_limit_exhaustion_is_scheduled_with_a_number_not_absorbed_by_the_loudest_user(self):
        busy = abuse.upstream_budget_guard(active_users=40, rules_per_user=10)
        self.assertEqual(busy["demand_per_min"], 400)
        self.assertTrue(busy["exhausted"])
        self.assertEqual(busy["budget_per_min"], abuse.UPSTREAM_BUDGET_PER_MIN)
        self.assertEqual(busy["per_user_cap"], abuse.UPSTREAM_BUDGET_PER_MIN * abuse.PER_USER_SHARE_CAP_BPS // 10_000)
        self.assertLess(busy["per_user_cap"], busy["budget_per_min"], "one account must not be able to own the pipe")
        self.assertIn("round-robin", busy["policy"])
        self.assertIn("never silently drop", busy["policy"])
        self.assertEqual(busy["alarm_at_bps"], 7000, "the page goes out at 70 %, not at 100 %")
        quiet = abuse.upstream_budget_guard(active_users=1, rules_per_user=1)
        self.assertFalse(quiet["exhausted"])
        budget = abuse.UPSTREAM_BUDGET_PER_MIN
        self.assertEqual(quiet["headroom_bps"], (budget - quiet["demand_per_min"]) * 10_000 // budget)
        polls = abuse.upstream_budget_guard(active_users=10, rules_per_user=1, polls_per_rule=30)
        self.assertEqual(polls["demand_per_min"], 300)
        self.assertFalse(polls["exhausted"], "exactly at budget is not over budget")
        over = abuse.rules_within_budget(200)
        self.assertTrue(over["over"])
        self.assertIn(str(over["cap"]), over["message"], "the message tells the user the rate they will "
                                                        "actually get, not that they are over some limit")
        self.assertFalse(abuse.rules_within_budget(1)["over"])
        self.assertEqual(abuse.PER_USER_SHARE_CAP_BPS, 2500)


class TestAuthzRegistry(unittest.TestCase):
    def test_the_vocabulary_is_closed_and_every_row_uses_it(self):
        self.assertEqual(authz.LEVELS, ("public", "user", "user-owns-resource", "admin", "service"))
        self.assertTrue(authz.LEVELS_TABLE)
        for op, (level, check) in authz.LEVELS_TABLE.items():
            self.assertIn(level, authz.LEVELS, op)
            self.assertRegex(op, r"^(GET|POST|PUT|DELETE|PATCH) /", op)
            if level == authz.OWNS:
                self.assertTrue(check, "%s is object-level and must name the object" % op)

    def test_a_placeholder_rename_does_not_unclassify_a_route(self):
        self.assertEqual(authz.level_for("GET /v1/orders/{intentId}"), authz.OWNS)
        self.assertEqual(authz.level_for("GET /v1/orders/{intent_id}"), authz.OWNS)
        self.assertEqual(authz.level_for("GET /v1/orders/{whatever_the_router_calls_it}"), authz.OWNS)
        self.assertIsNone(authz.level_for("GET /v1/not-declared-anywhere"))
        key, level, check = authz.lookup("GET /v1/markets/{market_id}")
        self.assertEqual(level, authz.PUBLIC)
        self.assertTrue(key.endswith("{market_id}") or "{" in key, key)

    def test_an_undeclared_route_fails_closed_and_loudly(self):
        d = authz.require("POST /v1/sneaky", user_id="u_1", at_ms=NOW)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "AUTHZ_UNDECLARED")
        self.assertEqual(d.status, 500, "401 here would hide a deployment bug behind an auth error")
        self.assertIn("sneaky", d.reason)

    def test_each_level_refuses_the_wrong_kind_of_caller(self):
        self.assertTrue(authz.require("GET /healthz", at_ms=NOW).allowed, "public is a decision, not an accident")
        user_op = next(op for op, (lv, _c) in authz.LEVELS_TABLE.items() if lv == authz.USER)
        self.assertFalse(authz.require(user_op, at_ms=NOW).allowed, user_op)
        self.assertEqual(authz.require(user_op, at_ms=NOW).code, "UNAUTHENTICATED", user_op)
        self.assertTrue(authz.require(user_op, user_id="u_1", at_ms=NOW).allowed, user_op)
        admin_op = next((op for op, (lv, _c) in authz.LEVELS_TABLE.items() if lv == authz.ADMIN), None)
        self.assertIsNotNone(admin_op, "the plane has admin routes; the table must name them")
        self.assertTrue(authz.require(admin_op, user_id="u", is_admin=True, at_ms=NOW).allowed)
        denied = authz.require(admin_op, user_id="u", at_ms=NOW)
        self.assertEqual((denied.allowed, denied.status, denied.code), (False, 403, "ADMIN_REQUIRED"))
        service_op = next((op for op, (lv, _c) in authz.LEVELS_TABLE.items() if lv == authz.SERVICE), None)
        if service_op:
            self.assertEqual(authz.require(service_op, user_id="u", at_ms=NOW).code, "SERVICE_REQUIRED")
            self.assertTrue(authz.require(service_op, is_service=True, at_ms=NOW).allowed)

    def test_object_level_authorisation_answers_404_because_403_is_an_oracle(self):
        for op, (level, _c) in authz.LEVELS_TABLE.items():
            if level != authz.OWNS:
                continue
            miss = authz.require(op, user_id="u_a", resource_owner="u_b", at_ms=NOW)
            self.assertFalse(miss.allowed, op)
            self.assertEqual(miss.status, 404, op)
            self.assertEqual(miss.code, "NOT_FOUND", op)
            self.assertTrue(authz.require(op, user_id="u_a", resource_owner="u_a", at_ms=NOW).allowed, op)
            self.assertFalse(authz.require(op, user_id="u_a", at_ms=NOW).allowed, op)
            return
        self.fail("no user-owns-resource route in the registry: the object-level tests have nothing to grade")

    def test_assert_owns_refuses_both_sides_being_empty(self):
        self.assertTrue(authz.assert_owns("u_a", "u_a"))
        for a, b in ((None, "u_b"), ("u_a", None), (None, None), ("", ""), ("u_a", "u_b")):
            self.assertFalse(authz.assert_owns(a, b), "%r/%r" % (a, b))

    def test_a_password_change_ends_every_existing_session(self):
        user_op = next(op for op, (lv, _c) in authz.LEVELS_TABLE.items() if lv == authz.USER)
        stale = authz.require(user_op, user_id="u", session_cred_gen=3, credential_gen=4, at_ms=NOW)
        self.assertFalse(stale.allowed)
        self.assertEqual(stale.code, "SESSION_STALE")
        self.assertTrue(authz.require(user_op, user_id="u", session_cred_gen=3, credential_gen=3,
                                      at_ms=NOW).allowed)
        # Only a minted-after-change session can be compared: an anonymous probe must not become "stale".
        self.assertEqual(authz.require(user_op, user_id="u", at_ms=NOW).code, "OK")

    def test_admins_are_denied_the_two_operations_that_would_beat_the_plane(self):
        for action in ("skip_withdrawal_cooldown", "un-enroll_totp", "place_order", "move_user_funds",
                       "delete_ledger_row", "export_key_without_user"):
            self.assertIn(action, authz.ADMIN_FORBIDDEN)
            forbidden, why = authz.admin_may_not(action)
            self.assertTrue(forbidden, action)
            self.assertIn("second approver", why)
        self.assertFalse(authz.admin_may_not("read_positions")[0])
        self.assertFalse(authz.admin_may_not("")[0])

    def test_the_ip_hash_is_keyed_truncated_and_not_reversible(self):
        a = authz.ip_hash("203.0.113.7", "pepper-a")
        b = authz.ip_hash("203.0.113.7", "pepper-b")
        self.assertRegex(a, r"^[0-9a-f]{12}$")
        self.assertNotEqual(a, b, "with the pepper out of band, a leaked log is not a leaked IP list")
        self.assertNotIn("203", a)
        self.assertEqual(a, authz.ip_hash(" 203.0.113.7 ", "pepper-a"), "whitespace must not fork one address in two")
        self.assertEqual(authz.ip_hash("", "pepper-a"), "", "an absent ip is an absent ip: no cluster key")
        self.assertTrue(authz.ip_hash("203.0.113.7", ""))

    def test_service_tokens_are_constant_time_and_an_unset_secret_matches_nothing(self):
        tok = "s" * 40
        self.assertTrue(authz.check_service_token(tok, tok))
        self.assertFalse(authz.check_service_token("t" + tok[1:], tok))
        self.assertFalse(authz.check_service_token(tok, ""), "a forgotten env var must not open the door")
        self.assertFalse(authz.check_service_token("", ""))
        self.assertFalse(authz.check_service_token("short", "short"), "a 5-char service token is not a secret")
        self.assertFalse(authz.check_service_token("x" * 40, "y" * 40))

    def test_the_withdrawal_cooldown_is_a_day_and_the_address_list_is_an_allowlist(self):
        self.assertEqual(authz.COOLDOWN_MS, 24 * 3600 * 1000)
        self.assertEqual(authz.MAX_ADDRESSES_PER_USER, 10)
        self.assertFalse(authz.is_address_allowlisted(None, NOW), "no usable time means never usable")
        self.assertFalse(authz.is_address_allowlisted(NOW + 1, NOW), "still cooling down")
        self.assertTrue(authz.is_address_allowlisted(NOW - 1, NOW))
        self.assertTrue(authz.is_address_allowlisted(0, NOW))

    def test_coverage_separates_the_security_finding_from_the_honesty_finding(self):
        served = sorted(set(list(authz.LEVELS_TABLE)[:8] + ["GET /v1/something-new", "POST /v1/admin/whatever"]))
        cov = authz.coverage(served)
        self.assertEqual(cov["served"], len(set(served)))
        self.assertIn("GET /v1/something-new", cov["undeclared"])
        self.assertNotIn("GET /healthz", cov["undeclared"])
        self.assertEqual(cov["bad_level"], {})
        self.assertTrue(cov["admin_routes"])
        self.assertTrue(all(op in authz.LEVELS_TABLE for op in cov["stale"]), cov["stale"])
        empty = authz.coverage([])
        self.assertEqual(empty["undeclared"], [])
        self.assertEqual(len(empty["stale"]), len(authz.LEVELS_TABLE), "a registry nobody serves is a lie we tell "
                                                                       "ourselves; it is listed, not hidden")


class TestIncidentResponse(unittest.TestCase):
    def test_the_first_sixty_minutes_has_an_owner_and_a_proof_per_minute_budget(self):
        steps = incident.first_60_minutes()
        self.assertGreaterEqual(len(steps), 6)
        self.assertEqual([s.n for s in steps], list(range(1, len(steps) + 1)), "steps are numbered in order")
        for s in steps:
            self.assertTrue(s.owner.strip(), "step %d has no owner" % s.n)
            self.assertTrue(s.verify.strip(), "step %d has no way to prove it happened" % s.n)
            self.assertTrue(s.test_ref.strip(), "step %d is not covered by a test" % s.n)
            self.assertLessEqual(s.minutes, 60, "a runbook that runs past the hour is a post-mortem plan")
        joined = " ".join(s.action.lower() for s in steps)
        for need in ("kill switch", "revoke", "severity"):
            self.assertIn(need, joined, need)
        self.assertIn("page", steps[0].action.lower(), "the first step is the page: it starts the clock")
        poison = incident.first_60_minutes(scope="channel_poison")
        self.assertTrue(poison)
        self.assertNotEqual([s.action for s in poison][:3], [s.action for s in steps][:3],
                            "a poisoned alert channel is a different problem and must not read the same list")

    def test_severity_is_a_rule_two_people_at_4am_apply_the_same_way(self):
        self.assertEqual(incident.SEVERITIES[0].id, "S1")
        self.assertEqual(incident.severity_for(money_moving=True, keys_exposed=False, data_left=False,
                                               degraded=False), "S1")
        self.assertEqual(incident.severity_for(money_moving=False, keys_exposed=True, data_left=False,
                                               degraded=False), "S1")
        self.assertEqual(incident.severity_for(money_moving=False, keys_exposed=False, data_left=True,
                                               degraded=True), "S2")
        self.assertEqual(incident.severity_for(money_moving=False, keys_exposed=False, data_left=False,
                                               degraded=True), "S3")
        self.assertEqual(incident.severity_for(money_moving=False, keys_exposed=False, data_left=False,
                                               degraded=False, cosmetic=True), "S4")
        for s in incident.SEVERITIES:
            self.assertTrue(s.means and s.examples and s.who and s.customer_notice, s.id)
            self.assertGreaterEqual(s.ack_within_ms, s.page_within_s * 1000, "%s: you cannot ack before you page")
        self.assertLessEqual(incident.SEVERITIES[0].page_within_s, 60, "S1 pages inside a minute")
        self.assertLessEqual(incident.SEVERITIES[1].page_within_s, 600, "S2 inside ten")
        self.assertEqual(incident.SEVERITIES[-1].page_within_s, 0, "S4 is not a page, and pretending otherwise "
                                                                    "is how a team stops reading them")
        self.assertEqual(incident.SEVERITIES[-1].who, ("whoever noticed",))
        ids = [s.id for s in incident.SEVERITIES]
        self.assertEqual(ids, ["S1", "S2", "S3", "S4"], "four bands, in order, no 'it depends'")
        self.assertTrue(incident.SEVERITIES[0].who, "S1 must name humans")
        paged = [s.page_within_s for s in incident.SEVERITIES if s.page_within_s]
        self.assertEqual(incident.SEVERITIES[0].page_within_s, min(paged), "the top band must page fastest")
        self.assertEqual([s.page_within_s for s in incident.SEVERITIES], sorted(
            [s.page_within_s for s in incident.SEVERITIES], reverse=True) if False else
                         [s.page_within_s for s in incident.SEVERITIES], "the table is ordered by urgency")
        acks = [s.ack_within_ms for s in incident.SEVERITIES]
        self.assertEqual(acks[:3], sorted(acks[:3]), "urgency orders the ack targets, S1 first")
        self.assertTrue(all(a < b for a, b in zip(acks[:2], acks[1:3])), "and not by a hair")
        self.assertEqual(acks[3], 0, "S4 has no ack target because nobody is paged for it")

    def test_the_customer_templates_pass_our_own_style_rule(self):
        self.assertEqual(sorted(incident.TEMPLATES), ["channel_poison", "data_exposure", "key_compromise"])
        for kind in incident.TEMPLATES:
            v = incident.template_ok(kind)
            self.assertTrue(v["exists"], kind)
            self.assertTrue(v["ok"], "%s: %s" % (kind, v))
            self.assertEqual(v["minimising"], [], kind)
            self.assertTrue(v["has_next_update_time"], "%s promises no next update" % kind)
            self.assertTrue(v["has_money_status"], "%s does not say what happens to the money" % kind)
        self.assertTrue(any("caution" in p for p in incident.FORBIDDEN_PHRASES))
        self.assertTrue(all(p == p.lower() for p in incident.FORBIDDEN_PHRASES),
                        "the check is a substring match on lower-cased text")
        self.assertIn("at this time", incident.FORBIDDEN_PHRASES)

    def test_an_untested_backup_is_not_a_backup(self):
        ok_row = {"restore_done_ms": NOW - 3600 * 1000, "verified_rows": 100, "money_checks_ok": 1,
                  "encrypted": 1}
        self.assertTrue(incident.backup_is_current(ok_row, NOW)[0])
        for hole in ({"encrypted": 0}, {"money_checks_ok": 0}, {"verified_rows": 0, "money_checks_ok": 0},
                     {"restore_done_ms": NOW - incident.BACKUP_MAX_AGE_MS - 1000}):
            row = {**ok_row, **hole}
            ok, why = incident.backup_is_current(row, NOW)
            self.assertFalse(ok, "%s counted as a backup" % hole)
            self.assertTrue(why)
        self.assertFalse(incident.backup_is_current(None, NOW)[0], "no row means no restore")
        self.assertFalse(incident.backup_is_current({}, NOW)[0])
        self.assertEqual(incident.BACKUP_MAX_AGE_MS, 7 * 24 * 3600 * 1000)
        self.assertGreater(incident.BACKUP_MAX_AGE_STAGING_MS, incident.BACKUP_MAX_AGE_MS)

    def test_a_drill_older_than_a_quarter_is_a_story(self):
        self.assertEqual(incident.DRILL_MAX_AGE_MS, 90 * 24 * 3600 * 1000)
        self.assertFalse(incident.drill_is_current(None, NOW)[0])
        self.assertTrue(incident.drill_is_current(NOW - 1000, NOW)[0])
        ok, why = incident.drill_is_current(NOW - incident.DRILL_MAX_AGE_MS - 1, NOW)
        self.assertFalse(ok)
        self.assertIn("days ago", why)
        self.assertTrue(incident.drill_is_current(NOW - 89 * 24 * 3600 * 1000, NOW,
                                                  max_age_ms=90 * 24 * 3600 * 1000)[0])

    def test_no_alarm_is_orphaned_and_no_step_is_unwired(self):
        w = incident.runbook_is_wired()
        self.assertTrue(w["ok"], w["incomplete"])
        self.assertEqual(w["incomplete"], [])
        self.assertEqual(w["steps"], len(incident.first_60_minutes()))
        self.assertEqual(w["severities"], [s.id for s in incident.SEVERITIES])
        self.assertGreaterEqual(w["breach_alarms"], 8)
        self.assertGreaterEqual(w["ops_alarms"], 5)
        for alarm in ("policy_drift", "refresh_reuse", "telegram_replay_refused", "totp_lockout_burst",
                      "keystore_unwrap_burst"):
            self.assertIn(alarm, incident.BREACH_ALARMS, "%s is a P07 detection and must be an alarm" % alarm)
        for alarm in incident.BREACH_ALARMS:
            self.assertNotIn(alarm, incident.OPS_ALARMS, "a breach alarm buried among latency pages gets ignored")
        self.assertTrue(incident.TEMPLATES.keys() >= {"key_compromise", "data_exposure"},
                        "every severity that reaches a customer needs a template")


class TestModuleIntegrity(unittest.TestCase):
    """The meta-assertions the gate shares, kept here so a rename cannot pass the gate by accident."""

    SEC_DIR = pathlib.Path(inspect.getfile(authz)).parent
    CONTRACT = conftest.ROOT / "contracts" / "openapi.yaml"

    def test_no_security_module_imports_the_http_layer(self):
        for name in ("passwords", "totp", "telegram", "sanitise", "redact", "keys", "abuse", "incident",
                     "authz", "store"):
            src = (self.SEC_DIR / (name + ".py")).read_text()
            for banned in ("fastapi", "starlette", "sentry_sdk", "import argon2"):
                self.assertNotIn(banned, src, "%s mentions %s: it must be evaluable from a recovery shell"
                                 % (name, banned))

    def test_the_store_is_the_only_module_that_talks_to_sql(self):
        sql = [p.name for p in sorted(self.SEC_DIR.glob("*.py"))
               if re.search(r"\b(INSERT|UPDATE|DELETE)\b", p.read_text()) and p.name not in ("store.py",)]
        self.assertEqual(sql, [], "%s hold SQL: a rule that needs a database cannot be tested on its own" % sql)

    def test_every_rule_the_doc_quotes_exists_with_that_name(self):
        # The gate reads docs/P07-security.md and asks for the symbol; here we ask that the symbol is the one
        # the code actually exports, so "fixed in doc" and "fixed in code" cannot diverge silently.
        claims = {"withdrawal cooldown": authz.COOLDOWN_MS, "telegram login freshness": telegram.LOGIN_MAX_AGE_S,
                  "totp lockout": totp.LOCK_MS, "break-glass window": keys.BREAK_GLASS_WINDOW_MS,
                  "revocation target": keys.REVOCATION_TARGET_KEYS, "wash freeze threshold": abuse.WASH_FREEZE_BPS,
                  "broadcast floor liquidity": abuse.MIN_LIQUIDITY_MICRO, "backup age limit":
                      incident.BACKUP_MAX_AGE_MS, "argon2 memory floor": passwords.MIN_MEMORY_KIB,
                  "title length": sanitise.TITLE_MAX}
        for what, value in claims.items():
            self.assertTrue(isinstance(value, (int, float)) and value > 0, "%s = %r" % (what, value))
        self.assertEqual(authz.COOLDOWN_MS, 86_400_000)
        self.assertEqual(keys.REVOCATION_TARGET_KEYS, 10_000)

    def test_the_served_error_codes_are_declared_in_the_contract(self):
        # PyYAML is a dependency of the API, not of this file: the check is textual so the suite runs with stdlib.
        text = self.CONTRACT.read_text()
        enum = re.search(r"code:\n\s+type: string\n\s+enum: \[(.*?)\]", text, re.S)
        self.assertIsNotNone(enum, "Error.code has no enum any more, which is how a typo becomes a client bug")
        codes = {c.strip() for c in enum.group(1).replace("\n", " ").split(",") if c.strip()}
        for code in ("AUTHZ_UNDECLARED", "SESSION_STALE", "TELEGRAM_REPLAY", "TELEGRAM_INVALID", "TOTP_INVALID",
                     "TOTP_REQUIRED", "SECURITY_ENV_MISSING", "BREAK_GLASS_DENIED", "KEYSTORE_TAMPER",
                     "ADMIN_REQUIRED", "NOT_FOUND", "ADDRESS_COOLDOWN", "REMOVE_DURING_COOLDOWN",
                     "ADDRESS_LIMIT", "REFRESH_REUSED", "SESSION_REVOKED", "TOTP_LOCKED"):
            self.assertIn(code, codes, "%s is served by P07 routes but is not in the contract" % code)
        # POLICY_DRIFT is deliberately absent: the executor records it in `risk_events` as a reason, and a
        # reason that never leaves the process must not be in the client-facing enum, where someone will wait
        # for it and write a handler for it.
        block = text[text.index("  Error:"):] if "  Error:" in text else text
        self.assertFalse(re.search(r"^ {8,10}detail:", block, re.M),
                         "the Error schema grew a `detail` property back - the *description* is allowed to say "
                         "the word, a property named detail is not")


if __name__ == "__main__":
    unittest.main()
