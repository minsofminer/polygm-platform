"""P07, executed: the auth surface served by the real app, not the modules in isolation.

Why this file exists separately from unit tests of `polygm_core.security`: every route here was written against
a signature it did not have, a status it did not return, or a table it did not own, and only a running app
notices. Three of the assertions below exist because a first draft was wrong in a way a unit test could not see
— `verify(at_ms=…)` called as `verify(at=…)`, a login throttle counted across *all* users so ten bad passwords
locked the product, and an unverified TOTP enrolment authorised a withdrawal.

Fixtures are built with the store, not with an endpoint, because there is no signup route yet — P09 owns
onboarding. That is the contract between the two phases: a `password_credentials` row plus a verified
`user_identities` row is all login needs.
"""
from __future__ import annotations
import base64
import contextlib
import io
import json
import os
import time
import unittest
import uuid

import subprocess
import sys
from pathlib import Path

from conftest import import_app, refresh_flags  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]

BOT_TOKEN = "7123456789:" + "Aa4" + "x" * 40  # lint-allow: shape only, never a real token
ADMIN = "adm_" + "k" * 44
DOC_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


def _security_env():
    """A KEK, a pepper and a service token, so the crypto paths run for real instead of being skipped.

    Set at import, before any class calls `import_app`: the app reads the KEK lazily, at first *use*, and a
    test that skipped this gets a 503 `SECURITY_ENV_MISSING` out of the envelope paths, which is the correct
    answer for an unconfigured pod and a confusing one for a test that meant to exercise the crypto.
    """
    os.environ["PGM_KEK_VERSION"] = "1"
    os.environ["PGM_KEK_v1"] = base64.b64encode(bytes(range(32, 64))).decode()
    os.environ["PGM_IP_PEPPER"] = "pepper-for-the-test-suite-0"
    os.environ["PGM_SERVICE_TOKEN"] = "svc-" + "s" * 40
    os.environ["PGM_IMAGE_PROXY_SECRET"] = "img-" + "i" * 40
    os.environ["PGM_TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
    os.environ["PGM_ADMIN_TOKEN"] = ADMIN
    os.environ.pop("PGM_REQUIRE_SECURITY_ENV", None)
    os.environ["PGM_TRUST_USER_HEADER"] = "1"


_security_env()


def init_data(user_id: int, *, at_s: int | None = None, token: str = BOT_TOKEN, tamper: str = "") -> str:
    from polygm_core.security import telegram as tg
    fields = {"auth_date": str(int(time.time()) if at_s is None else at_s),
              "query_id": "AAH" + uuid.uuid4().hex[:8],
              "user": json.dumps({"id": user_id, "first_name": "Test", "username": "tester"},
                                 separators=(",", ":"))}
    fields["hash"] = tg.sign(fields, token) + tamper
    return "&".join("%s=%s" % (k, v) for k, v in fields.items())


# Declared in `authz.LEVELS_TABLE` but not yet served, because a later phase builds them. The promise now lives
# in `authz.PLANNED` — P14 D1 found that a list written inside a test file cannot notice a route being *renamed*
# out from under the table, because both sides of that assertion were the test's own copies. This alias is kept
# so the assertions below read the same, and the list is read from the product.
#: The promise, read from the product: `authz.PLANNED` is declared in `authz.py` next to the table it describes,
#: and the P14 matrix cross-checks the same set against the running router. It used to be a literal list inside
#: this file, which could not notice a route being renamed out from under the table: both sides of the
#: comparison were the test's own copy, so the assertion could only ever agree with itself.
#:
#: P12 D6 served `withdraw` and the key export (renamed to `/v1/wallet/keys/export`, which is what the route, the
#: contract and the Mini App ledger call it), and they left this list in the same commit that shipped the
#: handlers. Nothing on it is provisional, and a name kept after the route exists would make it vacuous.
PLANNED_NOT_SERVED = sorted(__import__("polygm_core.security.authz", fromlist=["authz"]).PLANNED)


class RouteBase(unittest.TestCase):
    """One app per class, one user per test: a lockout in one test must not blind the next one."""

    app_name = "sec-route-base"
    app = client = None

    @classmethod
    def setUpClass(cls):
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
        # Entered as a context manager on purpose: `TestClient(app)` without it never runs the lifespan, and the
        # startup hook is where the authz table is mirrored into `route_auth_levels` and where `boot_check`
        # refuses to serve without a KEK. A test that skipped startup would grade a server that cannot boot.
        cls._stack = contextlib.ExitStack()
        cls.client = cls._stack.enter_context(TestClient(cls.app.app, raise_server_exceptions=False))
        missing = cls.app.SEC.ready()
        assert not missing, "0009 did not migrate, so nothing here tests what it claims: %s" % missing

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()

    def setUp(self):
        refresh_flags(self.app)
        self.con = self.app._db
        self.now = int(time.time() * 1000)
        self.pw = "correct horse battery staple 7!"
        # One Argon2id hash per test, reused for every fixture account: the point of the parameters is that
        # hashing is slow, and a suite that hashes 60 passwords is a suite that gets skipped on a laptop.
        self.phc = self.app._hasher().hash(self.pw)
        self.uid = self.new_user()
        self.email = "%s@example.test" % self.uid

    # ------------------------------------------------------------------ helpers
    @property
    def SEC(self):
        return self.app.SEC

    def new_user(self, *, pw=None, tg_id=None):
        """A fixture account. `tg_id` defaults to a random number because the identity table is UNIQUE on
        (kind,value) and two tests in one class must not collide on the same Telegram id."""
        uid = "u_" + uuid.uuid4().hex[:10]
        self.con.execute("INSERT INTO users (id, created_ms) VALUES (?,?)", (uid, self.now))
        self.SEC.link_identity(uid, "email", "%s@example.test" % uid, at=self.now)
        self.SEC.set_password(uid, self.app._hasher().hash(pw) if pw else self.phc, at=self.now)
        if tg_id is not None:
            self.SEC.link_identity(uid, "telegram", str(tg_id), at=self.now, proof_kind="initdata", verified=True)
        self._tg_ids = getattr(self, "_tg_ids", set()) | ({str(tg_id)} if tg_id is not None else set())
        return uid

    def login(self, ident=None, pw=None, **hdrs):
        h = {"Content-Type": "application/json"}
        h.update(hdrs)
        return self.client.post("/v1/auth/login", json={"identifier": ident or self.email,
                                                        "password": pw or self.pw}, headers=h)

    def bearer_for(self, uid):
        tok = self.client.post("/v1/auth/login", json={"identifier": uid, "password": self.pw},
                              ).json()["accessToken"]
        return {"Authorization": "Bearer %s" % tok, "Content-Type": "application/json"}

    def bearer(self, uid=None):
        tok = self.login(ident=uid if uid else None,
                         pw=self.pw).json().get("accessToken") if uid else self.login().json()["accessToken"]
        self.token = tok
        return {"Authorization": "Bearer %s" % tok, "Content-Type": "application/json"}

    def events(self, uid=None):
        return [r["kind"] for r in self.SEC.auth_events(uid or self.uid, since_ms=self.now - 60_000, limit=400)]

    def event_rows(self, kind, uid=None):
        sql = "SELECT detail_json FROM auth_events WHERE kind=?"
        args = [kind]
        if uid is not None:
            sql += " AND user_id=?"
            args.append(uid)
        return self.SEC.rows(sql + " ORDER BY at_ms DESC", tuple(args))

    def assert_error(self, resp, code, status=None):
        body = resp.json()
        self.assertEqual((body.get("error") or {}).get("code"), code,
                         "wanted %s, got %s (http %d)" % (code, json.dumps(body), resp.status_code))
        if status is not None:
            self.assertEqual(resp.status_code, status, body)
        return body

    def totp_enroll(self, headers):
        r = self.client.post("/v1/auth/totp/enroll", json={}, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.secret = r.json()["secret"]
        return r.json()

    def totp_code(self, *, at=None, steps=0):
        from polygm_core.security import totp
        return totp.code_for(self.secret, int(at if at is not None else time.time() * 1000)
                             + steps * totp.PERIOD_S * 1000)

    def totp_arm(self, headers):
        """Enrol, then prove the factor once. An unproven authenticator authorises nothing by design."""
        self.totp_enroll(headers)
        r = self.client.post("/v1/auth/totp/verify", json={"code": self.totp_code()}, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)


class TestLogin(RouteBase):
    app_name = "sec-login"

    def test_a_good_password_mints_an_access_and_a_refresh_token(self):
        r = self.login()
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        for k in ("accessToken", "refreshToken", "tokenType", "expiresInMs", "asOf", "staleAfter", "user"):
            self.assertIn(k, b)
        self.assertEqual(b["tokenType"], "Bearer")
        self.assertGreater(b["expiresInMs"], 0)
        self.assertEqual(b["user"]["id"], self.uid)
        self.assertNotIn(self.pw, json.dumps(b))
        self.assertIn("login_ok", self.events())

    def test_unknown_account_and_wrong_password_are_the_same_answer(self):
        a = self.login(pw="nope")
        b = self.login(ident="never.heard@example.test")
        self.assert_error(a, "LOGIN_FAILED", 401)
        self.assert_error(b, "LOGIN_FAILED", 401)
        self.assertEqual(sorted(a.json()), sorted(b.json()),
                         "the two bodies differ, which is a user-enumeration oracle on an email-keyed product")
        self.assertNotIn("never.heard", json.dumps(b.json()))

    def test_ten_bad_passwords_lock_the_attacked_account_and_nobody_else(self):
        victim = self.new_user()
        for _ in range(9):
            self.login(ident=victim, pw="guess")
        self.assertEqual(self.login(ident=victim, pw=self.pw).status_code, 200,
                         "nine tries must not lock: the limit is the documented ten")
        self.login(ident=victim, pw="guess")
        locked = self.login(ident=victim, pw=self.pw)               # the right password, refused
        self.assert_error(locked, "ACCOUNT_LOCKED", 429)
        self.assertGreater(int(locked.headers.get("retry-after") or 0), 60,
                           "Retry-After says 2s for a 15-minute lock, so the client hammers us")
        self.assertEqual(self.login().status_code, 200,
                         "the throttle is global again: one attacker, one product-wide outage")
        self.assertIn("login_locked", self.events(victim))
        self.assertEqual(json.loads(self.event_rows("login_locked", victim)[0]["detail_json"])["bucket"], "account",
                         "the lockout was recorded as an address lock, so support will chase the wrong cause")

    def test_the_hash_is_upgraded_while_we_still_hold_the_password(self):
        from polygm_core.security import passwords as pwd
        weak = None
        saved = dict(pwd.PARAMS)
        try:
            pwd.PARAMS.update({"memory_kib": pwd.MIN_MEMORY_KIB, "time_cost": pwd.MIN_TIME_COST,
                               "parallelism": pwd.MIN_PARALLELISM})
            weak = self.app._secb.Argon2id().hash(self.pw)
        finally:
            pwd.PARAMS.clear(); pwd.PARAMS.update(saved)
        self.SEC.set_password(self.uid, weak, at=self.now)
        self.assertEqual(self.SEC.credential(self.uid)["phc"], weak)
        self.assertEqual(self.login().status_code, 200)
        after = self.SEC.credential(self.uid)["phc"]
        self.assertNotEqual(after, weak, "a hash below the floor that is never upgraded is just a weak hash")
        self.assertTrue(pwd.is_acceptable(after), pwd.parse_phc(after))
        self.assertEqual(self.login().status_code, 200, "the upgraded hash must still verify")
        rows = self.event_rows("login_ok", self.uid)
        logged = [json.loads(r["detail_json"]) for r in rows]
        self.assertTrue(any(x.get("rehashed") for x in logged),
                        "the upgrade happened but no event recorded it: %s" % logged)

    def test_a_body_that_is_not_the_schema_never_reaches_the_verifier(self):
        for bad in ({}, {"identifier": "a@b.test"}, {"identifier": "a@b.test", "password": ""},
                    {"identifier": "a@b.test", "password": "x" * 5, "admin": True}):
            r = self.client.post("/v1/auth/login", json=bad)
            self.assertIn(r.status_code, (400, 422), "%s -> %d %s" % (bad, r.status_code, r.text))
        self.assertEqual(self.SEC.rows("SELECT 1 AS x FROM auth_events WHERE kind='login_bad_password'"), [],
                         "a rejected body was counted as an attempt, which is a self-inflicted lockout")

    def test_two_spellings_of_one_email_are_one_account(self):
        self.assertEqual(self.login(ident=self.email.upper()).status_code, 200)
        with self.assertRaises(ValueError) as ctx:
            self.SEC.link_identity(self.new_user(), "email", self.email.upper(), at=self.now)
        self.assertIn("IDENTITY_TAKEN", str(ctx.exception))
        self.assertEqual(self.SEC.identity_user("email", "  %s  " % self.email.upper()), self.uid)


class TestSessions(RouteBase):
    app_name = "sec-sessions"

    def test_a_refresh_token_can_be_spent_once(self):
        first = self.login().json()
        r = self.client.post("/v1/auth/refresh", json={"refreshToken": first["refreshToken"]})
        self.assertEqual(r.status_code, 200, r.text)
        second = r.json()
        self.assertNotEqual(second["refreshToken"], first["refreshToken"], "rotation returned the same token")
        again = self.client.post("/v1/auth/refresh", json={"refreshToken": first["refreshToken"]})
        self.assert_error(again, "REFRESH_REUSED", 401)
        after = self.client.post("/v1/auth/refresh", json={"refreshToken": second["refreshToken"]})
        self.assertIn(after.json()["error"]["code"], ("REFRESH_REUSED", "REFRESH_UNKNOWN"),
                      "the victim's new refresh survived the reuse alarm: %s" % after.text)
        self.assertIn("refresh_reuse", self.events())
        fam = self.SEC.one("SELECT family_id FROM auth_sessions WHERE user_id=?", (self.uid,))["family_id"]
        self.assertEqual(self.SEC.rows("SELECT 1 AS x FROM auth_sessions WHERE family_id=? AND revoked_ms IS NULL",
                                      (fam,)), [], "the family was not killed")

    def test_an_unknown_refresh_token_is_indistinguishable_from_a_spent_one(self):
        a = self.client.post("/v1/auth/refresh", json={"refreshToken": "r" * 40})
        self.assert_error(a, "REFRESH_UNKNOWN", 401)
        self.login()
        b = self.client.post("/v1/auth/refresh", json={"refreshToken": "s" * 40})
        self.assertEqual(sorted(a.json()), sorted(b.json()))

    def test_a_password_change_ends_every_existing_session(self):
        h = self.bearer()
        self.SEC.mint_session(self.uid, token_hash="hash_two", family_id="fam_two", at=self.now)
        self.assertEqual(self.client.get("/v1/auth/sessions", headers=h).status_code, 200)
        self.SEC.set_password(self.uid, self.app._hasher().hash("a brand new password 3!"), at=self.now + 1)
        self.assert_error(self.client.get("/v1/auth/sessions", headers=h), "UNAUTHENTICATED", 401)

    def test_the_session_list_carries_no_token_material(self):
        h = self.bearer()
        r = self.client.get("/v1/auth/sessions", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        blob = json.dumps(r.json())
        self.assertNotIn(self.token, blob)
        for needle in ("tokenHash", "refreshToken", "secret", "phc"):
            self.assertNotIn(needle, blob)
        items = r.json()["items"]
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(all(i["active"] for i in items), items)
        self.assertTrue(all(i["id"].startswith("ses_") for i in items), items)

    def test_user_a_cannot_log_user_b_out_of_their_own_session(self):
        ha = self.bearer()
        b_uid = self.new_user()
        hb = {"Authorization": "Bearer %s" % self.client.post("/v1/auth/login", json={
            "identifier": b_uid, "password": self.pw}).json()["accessToken"], "Content-Type": "application/json"}
        b_row = self.SEC.one("SELECT id FROM auth_sessions WHERE user_id=? AND revoked_ms IS NULL", (b_uid,))
        r = self.client.post("/v1/auth/sessions/revoke", json={"sessionId": b_row["id"]}, headers=ha)
        self.assert_error(r, "NO_SUCH_RESOURCE", 404)
        self.assertTrue(self.SEC.one("SELECT id FROM auth_sessions WHERE id=? AND revoked_ms IS NULL",
                                    (b_row["id"],)), "B's session died anyway")
        self.assertEqual(self.client.get("/v1/auth/sessions", headers=hb).status_code, 200)
        mine = self.SEC.one("SELECT id FROM auth_sessions WHERE user_id=? AND revoked_ms IS NULL", (self.uid,))
        self.assertEqual(self.client.post("/v1/auth/sessions/revoke", json={"sessionId": mine["id"]},
                                         headers=ha).json()["revoked"], 1)
        self.assert_error(self.client.get("/v1/auth/sessions", headers=ha), "UNAUTHENTICATED", 401)

    def test_logout_ends_one_session_and_everywhere_ends_all_of_them(self):
        ha = self.bearer()
        other = self.SEC.mint_session(self.uid, token_hash="hash_other", family_id="fam_other", at=self.now)
        self.assertEqual(self.client.post("/v1/auth/logout", headers=ha).json()["ended"], True)
        self.assertTrue(self.SEC.one("SELECT id FROM auth_sessions WHERE id=? AND revoked_ms IS NULL",
                                    (other["id"],)), "logout wiped the account instead of the device")
        self.client.get("/v1/auth/sessions", headers=self.bearer())
        everywhere = self.client.post("/v1/auth/sessions/revoke",
                                      json={"sessionId": "ignored", "everywhere": True},
                                      headers=self.bearer())
        self.assertGreaterEqual(everywhere.json()["sessions"], 1, everywhere.text)
        self.assertEqual(self.SEC.rows("SELECT 1 AS x FROM auth_sessions WHERE user_id=? AND revoked_ms IS NULL",
                                      (self.uid,)), [])

    def test_an_expired_access_token_is_refused_on_the_clock(self):
        h = self.bearer()
        self.con.execute("UPDATE auth_sessions SET expires_ms=? WHERE user_id=?", (self.now - 1, self.uid))
        self.assert_error(self.client.get("/v1/auth/sessions", headers=h), "UNAUTHENTICATED", 401)

    def test_the_dev_identity_header_is_off_when_the_security_env_is_required(self):
        self.assertEqual(self.client.get("/v1/auth/sessions", headers={"X-User-Id": self.uid}).status_code, 200,
                         "CI depends on the dev identity; if this flips, half the suite breaks confusingly")
        os.environ["PGM_REQUIRE_SECURITY_ENV"] = "1"
        try:
            self.assert_error(self.client.get("/v1/auth/sessions", headers={"X-User-Id": self.uid}),
                              "UNAUTHENTICATED", 401)
            self.assertEqual(self.client.get("/v1/auth/sessions", headers=self.bearer()).status_code, 200,
                             "a real token must still work with the flag on")
        finally:
            os.environ.pop("PGM_REQUIRE_SECURITY_ENV", None)

    def test_the_bearer_identity_beats_the_header_on_the_order_path(self):
        h = self.bearer()
        squatter = self.new_user()
        r = self.client.post("/v1/orders", headers={**h, "X-User-Id": squatter,
                                                    "Idempotency-Key": "sec-" + uuid.uuid4().hex},
                             json={"marketId": "0xM1", "tokenId": "0xT10", "side": "BUY", "price": "0.50",
                                   "size": "10"})
        # Refused rather than silently re-attributed: an SDK that sends both must not discover later that it
        # traded as somebody else. The security property is that the header never *wins*, and that is what the
        # two assertions below pin down.
        self.assert_error(r, "SESSION_MISMATCH", 403)
        self.assertEqual(self.SEC.rows("SELECT 1 AS x FROM order_intents WHERE user_id=?", (squatter,)), [],
                         "the squatter got an order anyway")
        ok = self.client.post("/v1/orders", headers={**h, "Idempotency-Key": "sec-" + uuid.uuid4().hex},
                              json={"marketId": "0xM1", "tokenId": "0xT10", "side": "BUY", "price": "0.50",
                                    "size": "10"})
        self.assertIn(ok.status_code, (200, 202), ok.text)
        row = self.SEC.one("SELECT user_id FROM order_intents ORDER BY rowid DESC LIMIT 1")
        self.assertTrue(row, "no intent was recorded, so the assertion above proves nothing")
        self.assertEqual(str(row["user_id"]), self.uid)


class TestTelegram(RouteBase):
    app_name = "sec-telegram"

    def test_a_signed_payload_from_a_linked_account_logs_in_exactly_once(self):
        self.tg = 4242 + (uuid.uuid4().int % 100000)
        q = init_data(self.tg)
        uid = self.new_user(tg_id=self.tg)
        r = self.client.post("/v1/auth/telegram", json={"initData": q})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("accessToken", r.json())
        self.assertEqual(r.json()["user"]["id"], uid)
        replay = self.client.post("/v1/auth/telegram", json={"initData": q})
        self.assert_error(replay, "TELEGRAM_REPLAY", 409)
        self.assertEqual(len(self.rows("SELECT 1 AS x FROM auth_sessions WHERE user_id=?", (uid,))), 1,
                         "the replay minted a second session")
        self.assertIn("login_telegram", self.events(uid))

    def rows(self, sql, params=()):
        return self.SEC.rows(sql, params)

    def test_a_forged_hash_is_refused(self):
        tg = 5000 + (uuid.uuid4().int % 100000)
        self.new_user(tg_id=tg)
        r = self.client.post("/v1/auth/telegram", json={"initData": init_data(tg, tamper="0")})
        self.assert_error(r, "TELEGRAM_INVALID", 401)

    def test_an_old_payload_is_refused_even_though_the_signature_is_valid(self):
        from polygm_core.security import telegram as tg
        mine = 6000 + (uuid.uuid4().int % 100000)
        self.new_user(tg_id=mine)
        stale = int(time.time()) - tg.LOGIN_MAX_AGE_S - 5
        r = self.client.post("/v1/auth/telegram", json={"initData": init_data(mine, at_s=stale)})
        body = self.assert_error(r, "TELEGRAM_INVALID", 401)
        self.assertNotIn("4242", json.dumps(body), "the refusal echoed the Telegram id")
        self.assertIn("telegram_too_old", [x["kind"] for x in
                                           self.SEC.rows("SELECT kind FROM auth_events WHERE kind LIKE 'telegram%'")],
                      "a stale payload was refused but nothing recorded why")

    def test_a_payload_for_an_unlinked_account_gets_no_session_and_no_explanation(self):
        r = self.client.post("/v1/auth/telegram", json={"initData": init_data(9999)})
        body = self.assert_error(r, "TELEGRAM_INVALID", 401)
        self.assertNotIn("9999", json.dumps(body))
        self.assertEqual(self.SEC.rows("SELECT 1 AS x FROM auth_sessions"), [], "a session was minted anyway")

    def test_garbage_in_the_field_is_a_401_or_422_never_a_500(self):
        for bad in ("x" * 24, "%%%%" * 6, "hash=zz&user={", "a=1&b=2&c=3&d=4&e=5&f=6&g=7&h=8&i=9&j=0"):
            r = self.client.post("/v1/auth/telegram", json={"initData": bad})
            self.assertIn(r.status_code, (401, 422), "%r -> %d %s" % (bad, r.status_code, r.text))

    def test_a_missing_bot_token_is_reported_as_a_misconfiguration(self):
        saved = os.environ.pop("PGM_TELEGRAM_BOT_TOKEN")
        try:
            self.assert_error(self.client.post("/v1/auth/telegram", json={"initData": init_data(1)}),
                              "SECURITY_ENV_MISSING", 503)
        finally:
            os.environ["PGM_TELEGRAM_BOT_TOKEN"] = saved

    def test_the_replay_row_survives_and_names_the_session_it_minted(self):
        from polygm_core.security import telegram as tg
        q = init_data(777)
        uid = self.new_user(tg_id=777)
        self.assertEqual(self.client.post("/v1/auth/telegram", json={"initData": q}).status_code, 200)
        row = self.SEC.telegram_state(tg.auth_hash(q))
        self.assertTrue(row, "nothing was recorded, so the next replay cannot be detected")
        self.assertEqual(str(row["user_id"]), uid)
        self.assertTrue(row["bound_session"], "the replay row does not say which session it minted")
        self.assertTrue(row["used_ms"], "the payload was never marked consumed")


class TestTotpAndAddresses(RouteBase):
    app_name = "sec-totp"

    def setUp(self):
        super().setUp()
        self.h = self.bearer()

    # A factor's code is single-use per window, so a fixture that spends one code per *authorised action* is
    # not a convenience - it is the rule under test. `arm` returns the mint-next-code callable for that account,
    # and `_advance_clock` moves the server into the next window when a test legitimately needs two money
    # actions (add then remove): the alternative is a 30-second sleep per test, or testing one of them through
    # the store and learning nothing about the route.
    def _advance_clock(self, windows: int = 1) -> None:
        if not hasattr(self, "_orig_now_ms"):
            self._orig_now_ms = self.app._now_ms
            self.addCleanup(setattr, self.app, "_now_ms", self._orig_now_ms)
            self._shift_ms = 0
        from polygm_core.security import totp
        self._shift_ms += int(windows) * totp.PERIOD_S * 1000
        base = self._orig_now_ms
        self.app._now_ms = lambda: base() + self._shift_ms

    @staticmethod
    def _code(secret, steps):
        from polygm_core.security import totp
        return totp.code_for(secret, int(time.time() * 1000) + steps * totp.PERIOD_S * 1000)

    def arm(self, headers, *, skip_proof=False):
        r = self.client.post("/v1/auth/totp/enroll", json={}, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["verified"], False, "enrolment may not claim to be verified")
        secret = r.json()["secret"]
        self.secret = secret                      # the unverified-state tests assert against this
        state = {"n": 0}

        def nxt():
            state["n"] += 1
            return self._code(secret, state["n"])

        if not skip_proof:
            v = self.client.post("/v1/auth/totp/verify", json={"code": self._code(secret, 0)}, headers=headers)
            self.assertEqual(v.status_code, 200, v.text)
        return nxt

    def test_an_unverified_authenticator_authorises_nothing(self):
        nxt = self.arm(self.h, skip_proof=True)
        r = self.client.post("/v1/wallet/withdrawal-addresses/add",
                             json={"address": "0x" + "ab" * 20, "code": nxt()}, headers=self.h)
        self.assert_error(r, "TOTP_REQUIRED", 403)
        self.assertEqual(self.SEC.rows("SELECT 1 AS x FROM withdrawal_addresses WHERE user_id=?", (self.uid,)),
                         [], "the refusal was cosmetic: the destination was written anyway")
        self.assertIn("totp_unverified_use", [x["kind"] for x in self.SEC.rows(
            "SELECT kind FROM auth_events WHERE kind=? AND user_id=?", ("totp_unverified_use", self.uid))],
            "somebody attached a factor and immediately tried to spend it - that is the incident, not a bug")

    def test_enrol_then_verify_then_use_is_the_only_happy_path(self):
        nxt = self.arm(self.h)
        st = self.SEC.totp_state(self.uid)
        self.assertTrue(st["verified_ms"], "the account is armed but the row says it never proved the factor")
        r = self.client.post("/v1/wallet/withdrawal-addresses/add",
                             json={"address": "0x" + "ab" * 20, "code": nxt(), "label": "ledger"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertGreaterEqual(b["usable_ms"] - self.now, self.app._authz.COOLDOWN_MS - 5000,
                                "the hold is shorter than 24 hours: %d ms" % (b["usable_ms"] - self.now))
        lst = self.client.get("/v1/wallet/withdrawal-addresses", headers=self.h).json()
        self.assertEqual(len(lst["items"]), 1)
        self.assertEqual(lst["cooldownMs"], self.app._authz.COOLDOWN_MS)
        self.assertNotIn("0x" + "ab" * 20, json.dumps(lst["items"]),
                         "the list echoes the whole destination; the UI needs a last-4 and nothing more")

    def test_a_spent_code_is_refused_inside_its_own_window(self):
        from polygm_core.security import totp
        self.arm(self.h)
        secret = self.secret
        again = self.client.post("/v1/auth/totp/verify", json={"code": self._code(secret, 1)}, headers=self.h)
        self.assertEqual(again.status_code, 200, again.text)
        repeat = self.client.post("/v1/auth/totp/verify", json={"code": self._code(secret, 1)}, headers=self.h)
        self.assert_error(repeat, "TOTP_INVALID", 403)
        self.assertIn("totp_reused", [x["kind"] for x in
                                      self.SEC.rows("SELECT kind FROM auth_events WHERE kind LIKE 'totp%'")],
                      "a replayed code is the loudest signal in the product and nothing logged it")
        self.assertTrue(totp.is_mandatory("withdraw") and totp.is_mandatory("address_add"))
        self.assertTrue(totp.is_mandatory("key_export"))
        self.assertFalse(totp.is_mandatory("read_positions"), "2FA on a read is a churn engine, not a control")

    def test_five_wrong_codes_lock_the_authenticator_not_the_account(self):
        from polygm_core.security import totp
        self.arm(self.h, skip_proof=True)
        for _ in range(totp.MAX_ATTEMPTS):
            self.client.post("/v1/auth/totp/verify", json={"code": "123456"}, headers=self.h)
        self.assert_error(self.client.post("/v1/auth/totp/verify", json={"code": self._code(self.secret, 0)},
                                           headers=self.h), "TOTP_LOCKED", 429)
        self.assertEqual(self.login().status_code, 200, "a locked second factor must not lock the password out")
        self.con.execute("UPDATE totp_enrollments SET locked_until_ms=? WHERE user_id=?", (self.now - 1, self.uid))
        self.assertEqual(self.client.post("/v1/auth/totp/verify", json={"code": self._code(self.secret, 0)},
                                          headers=self.h).status_code, 200, "the lock never expires")

    def test_a_new_destination_is_held_for_24_hours_and_cannot_be_deleted_while_held(self):
        nxt = self.arm(self.h)
        addr = "0x" + "cd" * 20
        added = self.client.post("/v1/wallet/withdrawal-addresses/add", json={"address": addr, "code": nxt()},
                                headers=self.h)
        self.assertEqual(added.status_code, 200, added.text)
        self._advance_clock()
        self.assert_error(self.client.post("/v1/wallet/withdrawal-addresses/remove",
                                           json={"addressId": added.json()["id"], "code": nxt()}, headers=self.h),
                          "REMOVE_DURING_COOLDOWN", 409)
        self.assertEqual(self.SEC.one("SELECT id FROM withdrawal_addresses WHERE id=?",
                                     (added.json()["id"],))["id"], added.json()["id"],
                         "a held destination was deleted, which is how a takeover cleans up behind itself")
        self.con.execute("UPDATE withdrawal_addresses SET added_ms=?, usable_ms=? WHERE id=?",
                         (self.now - 90_000, self.now - 1, added.json()["id"]))
        self._advance_clock()
        after = self.client.post("/v1/wallet/withdrawal-addresses/remove",
                                 json={"addressId": added.json()["id"], "code": nxt()}, headers=self.h)
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(self.client.get("/v1/wallet/withdrawal-addresses", headers=self.h).json()["items"], [])

    def test_removing_a_destination_also_needs_the_factor(self):
        """The gap this phase's own gate found: `totp.REQUIRED_FOR` names `address_remove`, and the route did not.

        A declaration in a module that no caller enforces is documentation, and the documentation was already in
        the contract as a 403 on this path. So the route now asks, and the check is that a session alone buys
        nothing: without a code the call is refused *before* the row is looked up.
        """
        self.arm(self.h, skip_proof=True)
        self.assert_error(self.client.post("/v1/wallet/withdrawal-addresses/remove",
                                          json={"addressId": "w_whatever"}, headers=self.h), "TOTP_REQUIRED", 403)
        nxt = self.arm(self.h)
        added = self.client.post("/v1/wallet/withdrawal-addresses/add",
                                 json={"address": "0x" + "ef" * 20, "code": nxt()}, headers=self.h)
        self.assertEqual(added.status_code, 200, added.text)
        # A wrong code is refused, and the row survives: the factor is a gate on this call, not a suggestion.
        self.assert_error(self.client.post("/v1/wallet/withdrawal-addresses/remove",
                                          json={"addressId": added.json()["id"], "code": "000000"}, headers=self.h),
                          "TOTP_INVALID", 403)
        self.assertIsNotNone(self.SEC.one("SELECT id FROM withdrawal_addresses WHERE id=?", (added.json()["id"],)))

    def test_the_destination_cap_holds_and_no_row_can_skip_the_cooldown(self):
        nxt = self.arm(self.h)
        n = self.app._authz.MAX_ADDRESSES_PER_USER
        # Filled through the store, which is the same function the route calls: a TOTP code is valid for one
        # window and is single-use inside it (see `totp.verify`), so ten route-level spends would need a five
        # minute fixture. The route is exercised once, at the boundary, which is where an off-by-one lives.
        for i in range(n):
            self.SEC.add_address(self.uid, "0x%040x" % (i + 1), at=self.now)
        self.assert_error(self.client.post("/v1/wallet/withdrawal-addresses/add",
                                           json={"address": "0x%040x" % (n + 1), "code": nxt()},
                                           headers=self.h), "ADDRESS_LIMIT", 422)
        lst = self.client.get("/v1/wallet/withdrawal-addresses", headers=self.h).json()["items"]
        self.assertEqual(len(lst), n)
        self.assertNotIn("address", lst[0], "the list still returns the whole destination")
        self.assertIn("\u2026", lst[0]["display"])
        with self.assertRaises(Exception):
            self.con.execute("INSERT INTO withdrawal_addresses (id, user_id, address, added_ms, usable_ms,"
                             " confirmed_ms, added_via, skip_cooldown) VALUES ('w_x',?,?,?,?,?,'user',1)",
                             (self.uid, self.now, self.now, self.now))
        with self.assertRaises(Exception):
            self.con.execute("UPDATE withdrawal_addresses SET skip_cooldown=1 WHERE user_id=?", (self.uid,))

    def test_two_accounts_naming_one_destination_are_linked_in_the_record(self):
        other = self.new_user()
        oh = self.bearer_for(other)
        nxt_me = self.arm(self.h)
        nxt_them = self.arm(oh)
        shared = "0x" + "ee" * 20
        theirs = self.client.post("/v1/wallet/withdrawal-addresses/add", json={"address": shared,
                                                                              "code": nxt_them()}, headers=oh)
        self.assertEqual(theirs.status_code, 200, theirs.text)
        self.assertEqual(theirs.json()["shared_with"], [], "a first claim is not an overlap")
        mine = self.client.post("/v1/wallet/withdrawal-addresses/add", json={"address": shared, "code": nxt_me()},
                                headers=self.h)
        self.assertEqual(mine.status_code, 200, mine.text)
        self.assertEqual(mine.json()["shared_with"], [other], "the overlap is invisible to support")
        self.assertIn("address_shared", [x["kind"] for x in self.SEC.rows(
            "SELECT kind FROM auth_events WHERE kind='address_shared'")])
        self._advance_clock()
        self.assert_error(self.client.post("/v1/wallet/withdrawal-addresses/remove",
                                           json={"addressId": mine.json()["id"], "code": nxt_them()}, headers=oh),
                          "NO_SUCH_RESOURCE", 404)
        self.assertTrue(self.SEC.one("SELECT id FROM withdrawal_addresses WHERE id=? AND removed_ms IS NULL",
                                     (mine.json()["id"],)), "the other account deleted my destination")

    def test_an_unknown_destination_id_is_a_404_not_a_403(self):
        nxt = self.arm(self.h)
        self.assert_error(self.client.post("/v1/wallet/withdrawal-addresses/remove",
                                           json={"addressId": "waddr_nope", "code": nxt()}, headers=self.h),
                          "NO_SUCH_RESOURCE", 404)


class TestAdmin(RouteBase):
    app_name = "sec-admin"

    def test_break_glass_needs_a_token_two_approvers_and_a_real_reason(self):
        payload = {"reason": "incident 4311: session hijack suspected, revoking all sessions",
                   "approvers": ["ops-ani", "ops-ben"], "scope": "all"}
        self.assert_error(self.client.post("/v1/admin/revoke-sessions", json=payload), "SIGNER_UNAVAILABLE", 503)
        self.assert_error(self.client.post("/v1/admin/revoke-sessions", json=payload,
                                           headers={"x-admin-token": "wrong"}), "ADMIN_REQUIRED", 403)
        h = {"x-admin-token": ADMIN, "Content-Type": "application/json"}
        # One approver is a shape error the schema can see, so it is a 422 from validation; the case
        # `break_glass_ok` exists for is the one the schema cannot see: two names, one human.
        self.assert_error(self.client.post("/v1/admin/revoke-sessions", headers=h,
                                           json={**payload, "approvers": ["ops-ani"]}), "VALIDATION", 422)
        self.assert_error(self.client.post("/v1/admin/revoke-sessions", headers=h,
                                           json={**payload, "approvers": ["ops-ani", "ops-ani"]}),
                          "BREAK_GLASS_DENIED", 422)
        self.assert_error(self.client.post("/v1/admin/revoke-sessions", headers=h,
                                           json={**payload, "reason": "do it"}), "BREAK_GLASS_DENIED", 422)
        mine = self.bearer()
        self.client.get("/v1/auth/sessions", headers=mine)
        good = self.client.post("/v1/admin/revoke-sessions", headers=h, json=payload)
        self.assertEqual(good.status_code, 200, good.text)
        self.assertGreaterEqual(good.json()["revoked"]["sessions"], 1)
        self.assert_error(self.client.get("/v1/auth/sessions", headers=mine), "UNAUTHENTICATED", 401)
        self.assertIn("admin_revoke_all", [x["kind"] for x in self.SEC.rows(
            "SELECT kind FROM auth_events WHERE kind='admin_revoke_all'")])

    def test_a_scoped_revocation_names_its_target(self):
        h = {"x-admin-token": ADMIN, "Content-Type": "application/json"}
        other = self.new_user()
        payload = {"reason": "incident 4311: one account compromised, scoping the revocation",
                   "approvers": ["ops-ani", "ops-ben"], "scope": "user", "userId": other}
        r = self.client.post("/v1/admin/revoke-sessions", headers=h, json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.SEC.rows("SELECT 1 AS x FROM auth_sessions WHERE user_id=? AND revoked_ms IS NULL",
                                      (other,)), [])
        self.assert_error(self.client.post("/v1/admin/revoke-sessions", headers=h,
                                          json={**payload, "userId": ""}), "VALIDATION", 422)

    def test_a_service_route_is_not_reachable_with_a_user_token(self):
        h = self.bearer()
        self.assertNotEqual(self.client.post("/v1/admin/revoke-sessions", headers={**h, "x-admin-token": "x"},
                                             json={"reason": "x" * 30, "approvers": ["a", "b"]}).status_code, 200)


class TestHeadersAndRedaction(RouteBase):
    app_name = "sec-headers"

    def test_security_headers_on_every_response(self):
        for path in ("/healthz", "/v1/markets", "/v1/auth/sessions"):
            if path.endswith("sessions"):
                pass
            r = self.client.get(path, headers={"X-User-Id": self.uid} if "sessions" in path else {})
            csp = r.headers.get("content-security-policy", "")
            self.assertIn("frame-ancestors", csp, "%s: no frame-ancestors, so any page can frame us" % path)
            self.assertIn("default-src 'self'", csp)
            self.assertNotIn("x-frame-options", {k.lower() for k in r.headers},
                             "XFO cannot express an allowlist; on the API it must be absent, not 'DENY'")
            self.assertIn("nosniff", r.headers.get("x-content-type-options", ""))
            self.assertIn("same-origin", r.headers.get("cross-origin-opener-policy", "").lower())

    def test_an_identity_response_is_never_cacheable_and_a_market_list_still_is(self):
        auth = self.client.get("/v1/auth/sessions", headers={"X-User-Id": self.uid})
        self.assertIn("no-store", auth.headers.get("cache-control", "").lower(),
                      "a response that says who you are, sitting in a shared cache")
        self.assertIn("no-store", self.client.get("/v1/markets/does-not-exist").headers.get("cache-control", ""),
                      "an error page echoed into a cache is a stale lie")
        public = self.client.get("/v1/markets")
        self.assertNotIn("no-store", public.headers.get("cache-control", "").lower(),
                         "P05's `cache.ttlMs` promises a cacheable public read; no-store contradicts it")

    def test_the_mini_app_allowlist_is_not_the_api_policy(self):
        api = self.client.get("/v1/markets").headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", api)
        framed = self.client.get("/v1/markets", headers={"x-openout-frame": "miniapp"})
        csp = framed.headers["content-security-policy"]
        self.assertIn("https://web.telegram.org", csp, "the Mini App cannot frame us, which is the whole point")
        self.assertNotIn("frame-ancestors 'none'", csp)
        self.assertNotIn("x-frame-options", {k.lower() for k in framed.headers},
                         "XFO came back and would have broken the webview it is meant to allow")
        self.assertNotIn("evil.com", csp)

    def test_a_password_that_looks_like_a_key_never_reaches_a_log_line(self):
        private = "0x" + "1234abcd" * 5
        words = "abandon ability able about above absent absorb abstract absurd abuse access accident"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.client.post("/v1/auth/login", json={"identifier": self.email, "password": private})
            self.client.post("/v1/auth/login", json={"identifier": self.email, "password": words})
            self.client.post("/v1/wallet/withdrawal-addresses/add", json={"address": private[2:]})
        out = buf.getvalue()
        self.assertGreaterEqual(out.count("\n"), 3, "the access log printed nothing, so this test proved nothing")
        for line in out.splitlines():
            if not line.strip():
                continue
            json.loads(line)                                      # still one JSON object per line
            self.assertNotIn(private, line)
            self.assertNotIn("abandon ability", line)
            self.assertNotIn(self.pw, line)
            self.assertNotIn("password", line.lower())
        blob = json.dumps(self.SEC.rows("SELECT detail_json FROM auth_events WHERE user_id=?", (self.uid,)))
        self.assertNotIn(private, blob)
        self.assertNotIn(words, blob)

    def test_the_redactor_covers_the_shapes_we_actually_emit(self):
        from polygm_core.security import redact
        for secret in ("0x" + "ab" * 32,
                       "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",  # lint-allow: fixture
                       "user someone@example.com failed to unlock",
                       "wallet seed: " + "abandon " * 12 + "zoo",
                       "secret=ghp_" + "T" * 36):
            out = redact.redact_text(secret)
            self.assertNotEqual(out, secret, "unredacted: %s" % secret[:40])
            self.assertTrue(redact.REDACTED in out or "[pem-key]" in out or "[" in out,
                            "no redaction marker in %r - the rule did not fire" % out[:60])
            self.assertNotIn("abandon abandon abandon", out)
        addr = "the destination 0x" + "ab" * 20 + " was rejected"
        short = redact.redact_text(addr)
        self.assertIn("0x", short)
        self.assertNotIn("ab" * 20, short)
        self.assertLess(len(short), len(addr))


class TestDrillRecords(RouteBase):
    app_name = "sec-drills"     # its own plane: two classes on one name share one module and fight over its DB
    """`record_drill` refuses a failure with no step named, and the table refuses an edit afterwards.

    Both halves are pinned because the mutation harness killed neither at first: the guard is two lines of `if`
    in the store, and a drill record that can be softened after the fact is a drill record that will be.
    """

    def test_a_failed_drill_must_name_the_step_that_failed(self):
        with self.assertRaises(ValueError):
            self.SEC.record_drill(kind="key_compromise", started_ms=self.now, finished_ms=self.now + 1,
                                 verdict="fail", measured_ms=5)
        ok = self.SEC.record_drill(kind="key_compromise", started_ms=self.now, finished_ms=self.now + 1,
                                  verdict="fail", measured_ms=5, failed_step="provider rate limit hit at 60%")
        self.assertEqual(ok["verdict"], "fail")
        self.assertEqual(self.SEC.latest_drill("key_compromise")["failed_step"],
                         "provider rate limit hit at 60%")

    def test_the_record_cannot_be_softened_after_the_fact(self):
        self.SEC.record_drill(kind="key_compromise", started_ms=self.now, finished_ms=self.now + 1,
                             verdict="pass", measured_ms=5)
        with self.assertRaises(Exception):
            self.con.execute("UPDATE drill_records SET notes='it went fine' WHERE id=1")
        with self.assertRaises(Exception):
            self.con.execute("DELETE FROM drill_records")


class TestOperationIsTheTemplate(RouteBase):
    """The bug the P07 gate found: an authorised user, a parameterised route, a 500.

    `_principal` derived the operation from the request URL, so `GET /v1/orders/intents/0xabc` was looked up in a
    table keyed by `/v1/orders/intents/{intent_id}`, missed, and answered 500 AUTHZ_UNDECLARED. Every served route
    whose path carries an identifier was broken for any caller holding a real session. The dev-header path most of
    this suite exercises never reached that code, which is how ~600 green tests missed it: the honest lesson is
    that a fixture which bypasses authentication cannot catch an authorisation bug, and the P06 tests were
    written before P07 put a gate in that path.
    """

    def _intent(self, uid: str, intent_id: str) -> None:
        self.con.execute(
            "INSERT INTO order_intents (id, user_id, market_id, token_id, side, price_micro, size_micro, "
            "notional_micro, state, idempotency_key, created_ms, updated_ms) "
            "VALUES (?,?,'0xmarket','1234','BUY',500000,2000000,1000000,'submitted',?,?,?)",
            (intent_id, uid, "k-" + intent_id, self.now, self.now))

    def test_authenticated_call_to_a_parameterised_route_is_authorised_not_undeclared(self):
        r = self.client.get("/v1/orders/intents/does-not-exist", headers=self.bearer())
        self.assertNotEqual(r.status_code, 500, r.text)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual((r.json().get("error") or {}).get("code"), "NOT_FOUND")

    def test_a_second_user_with_a_valid_session_cannot_read_the_first_users_intent(self):
        other = self.new_user()
        self._intent(self.uid, "0xintent1")
        mine = self.client.get("/v1/orders/intents/0xintent1", headers=self.bearer())
        self.assertEqual(mine.status_code, 200, mine.text)
        theirs = self.client.get("/v1/orders/intents/0xintent1", headers=self.bearer_for(other))
        self.assertEqual((theirs.json().get("error") or {}).get("code"), "NOT_FOUND")
        self.assertNotIn("0xintent1", theirs.text)

class TestAuthzTable(RouteBase):
    app_name = "sec-authz"

    def test_every_served_route_declares_a_level_and_the_mirror_agrees(self):
        from polygm_core.security import authz
        served = sorted({"%s %s" % (verb, r.path) for r in self.app.app.routes for verb in sorted(r.methods or ())
                         if getattr(r, "methods", None) and verb in ("GET", "POST", "PUT", "DELETE")
                         and r.path not in DOC_PATHS and not r.path.startswith("/docs")})
        rep = authz.coverage(served)
        self.assertEqual(rep["undeclared"], [], "routes with no auth decision: %s" % rep["undeclared"])
        # The registry is deliberately ahead of the code (P08-P12 land routes it already classifies), so "stale"
        # is not a bug list - it is a promise about what is still to come. Naming it in full here is the point:
        # when `POST /v1/wallet/withdraw` ships, this assertion is where someone remembers to look.
        # `GET /openapi.json` is excluded for a reason of its own: it is in the table and FastAPI only adds it to
        # the router when the schema is first generated, so whether it is "served" depends on when you look. It is
        # no longer served at all where it matters (`PGM_REQUIRE_SECURITY_ENV=1` turns the interactive surface off,
        # which P14's matrix asserts as its own check), so it is not part of this promise list either.
        stale = sorted(set(rep["stale"]) - {"GET /openapi.json"})
        self.assertEqual(stale, sorted(PLANNED_NOT_SERVED),
                         "the table and the router disagree in a way nobody decided: %s"
                         % sorted(set(stale) ^ set(PLANNED_NOT_SERVED)))
        self.assertEqual(rep["bad_level"], {})
        mirrored = self.SEC.route_levels()
        for op, (level, _why) in authz.LEVELS_TABLE.items():
            self.assertEqual(mirrored.get(op), level, "%s: the table says %s, the database says %s"
                             % (op, level, mirrored.get(op)))

    def test_object_level_checks_are_404s_not_403s(self):
        from polygm_core.security import authz
        self.assertFalse(authz.assert_owns("u_a", "u_b"))
        self.assertTrue(authz.assert_owns("u_a", "u_a"))
        self.assertFalse(authz.assert_owns(None, "u_b"), "an anonymous caller owns nothing")

    def test_admins_are_denied_the_two_operations_that_matter(self):
        from polygm_core.security import authz
        for op in ("skip_withdrawal_cooldown", "un-enroll_totp"):
            forbidden, why = authz.admin_may_not(op)
            self.assertTrue(forbidden, "%s must be off-limits to admin tooling" % op)
            self.assertIn("second approver", why, "a refusal without a reason becomes a workaround")
        self.assertFalse(authz.admin_may_not("read_positions")[0], "an admin cannot even read? that is not the rule")


    def test_a_service_token_is_compared_without_leaking_length(self):
        from polygm_core.security import authz
        right = "s" * 40
        self.assertTrue(authz.check_service_token(right, right))
        self.assertFalse(authz.check_service_token("w" * 40, right))
        self.assertFalse(authz.check_service_token("", ""), "an unconfigured secret must not match a blank header")
        self.assertFalse(authz.check_service_token(right, ""), "and must not match when the server has none")
        self.assertFalse(authz.check_service_token("s" * 8, "s" * 8),
                         "a short token is not a secret: the length floor is the point")


if __name__ == "__main__":
    unittest.main()


class TestOptimisedBuildIsNotADifferentProduct(unittest.TestCase):
    """P14 D3: the three guards SAST found as `assert` must survive `python -O`.

    Why this is not a style note. `assert` is stripped by `python -O`, and each of these three is a *runtime*
    control:

      * `risk.limits` refuses a deny code whose severity is not one of the known set — the severity decides how a
        refusal is displayed and whether it is retryable, so an unknown one would be rendered by whichever client
        felt like it;
      * `risk.limits.evaluate_extra`'s inner `deny()` refuses a code that is not in the table at all;
      * `copy.engine` refuses a skip reason that is not documented, and a silently-counted skip is a copy that
        stopped without saying why.

    So the test does not grep for the word `raise` — that would pass on a file that raises in a comment. It runs
    a *child interpreter with -O* and asserts the guards still fire there. The child is a subprocess because
    optimisation is fixed at interpreter start: there is no way to un-assert a running module.
    """

    def test_the_deny_table_invariant_fires_under_optimisation(self) -> None:
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from polygm_core.risk import limits\n"
            "from dataclasses import replace\n"
            "bad = dict(limits.DENY_CODES)\n"
            "bad['P14_FAKE'] = replace(list(limits.DENY_CODES.values())[0], severity='not-a-severity')\n"
            "limits.DENY_CODES.clear(); limits.DENY_CODES.update(bad)\n"
            "try:\n"
            "    limits.validate_deny_table()\n"
            "except ValueError as e:\n"
            "    print('RAISED', e); sys.exit(0)\n"
            "print('SILENT'); sys.exit(3)\n"
        ) % str(ROOT / "packages")
        r = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         "a bad severity passed validation under -O: %s" % (r.stdout + r.stderr)[-300:])
        self.assertIn("RAISED", r.stdout)

    def test_the_unknown_deny_code_guard_fires_under_optimisation(self) -> None:
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from polygm_core.risk import limits\n"
            "print('deny_code_known:', limits.deny_code_known('P14_NOT_A_CODE'))\n"
            "assert not limits.deny_code_known('P14_NOT_A_CODE')\n"   # deliberately an assert: inside -O it is a
            "print('OK')\n"                                          # no-op, so the printed value is the evidence
        ) % str(ROOT / "packages")
        r = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-300:])
        self.assertIn("deny_code_known: False", r.stdout,
                      "the table lookup itself is the control, and it must not depend on assertion machinery")

    def test_the_copy_skip_reason_guard_fires_under_optimisation(self) -> None:
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from polygm_core.copy.engine import EngineStats\n"
            "try:\n"
            "    EngineStats().skip('not_a_documented_reason: because')\n"
            "except ValueError as e:\n"
            "    print('RAISED', e); sys.exit(0)\n"
            "print('SILENT'); sys.exit(3)\n"
        ) % str(ROOT / "packages")
        r = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         "an undocumented skip reason was accepted under -O: %s" % (r.stdout + r.stderr)[-300:])
        self.assertIn("RAISED", r.stdout)

    def test_a_documented_skip_reason_still_counts(self) -> None:
        """The canary for over-raising: a guard that refuses everything is a copy engine that stops working."""
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from polygm_core.copy.engine import EngineStats, SKIP_REASONS\n"
            "s = EngineStats(); s.skip(sorted(SKIP_REASONS)[0] + ': ok')\n"
            "print('COUNTED', sorted(s.skipped))\n"
        ) % str(ROOT / "packages")
        r = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-300:])
        self.assertIn("COUNTED", r.stdout)
