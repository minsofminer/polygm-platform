"""P12 · D6, executed: the wallet over HTTP, against the real app.

The Mini App's six wallet routes are the HTTP half of ceremonies the bot has run since P07, which is exactly why
they need their own tests rather than a happy-path smoke: the *order* of the locks is the feature. A withdrawal
screen that asks for a password before it checks the allowlist is a screen that trains a user to type their
password into the wrong prompt, and the only way to see that is to assert which refusal arrives first.

Three bug classes were caught here, all of them invisible to reading the code:

* `cash_ledger` has `amount_micro`/`created_ms`, not `delta_micro`/`at_ms`. The chat cards and the first draft of
  these routes read the second pair — names that read better and do not exist, so every wallet read was an
  exception at tap time. (`tests/test_wallet_cards` below pins the cards too; they are the same read.)
* `_tm.usdc(...)` was called in those cards and does not exist anywhere in the codebase; the formatter is
  `fmt_usdc` from `polygm_core.money.cents`, imported at the top of `app.py`.
* `/pnl` summed `kind='realised'`, a ledger kind the CHECK constraint has never allowed, and read
  `position_snapshots.balance_micro`, a column that does not exist. Both would have answered "0.00" forever, which
  is the failure mode a test cannot see unless it seeds money first — so it does.

No money moves in this file. `POST /v1/wallet/withdraw` records a *request* (the custody plane signs from P14), and
the last assertion of the happy path is precisely that the row is not a payment.
"""
from __future__ import annotations
import base64
import contextlib
import json
import os
import time
import unittest
import uuid

from conftest import import_app, refresh_flags  # noqa: F401

BOT_TOKEN = "7123456789:" + "Aa4" + "x" * 40  # lint-allow: shape only, never a real token
DOC_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


def _security_env():
    """A KEK, a pepper and a service token, so the crypto paths run for real instead of being skipped."""
    os.environ["PGM_KEK_VERSION"] = "1"
    os.environ["PGM_KEK_v1"] = base64.b64encode(bytes(range(32, 64))).decode()
    os.environ["PGM_IP_PEPPER"] = "pepper-for-the-test-suite-0"
    os.environ["PGM_SERVICE_TOKEN"] = "svc-" + "s" * 40
    os.environ["PGM_IMAGE_PROXY_SECRET"] = "img-" + "i" * 40
    os.environ["PGM_TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
    os.environ["PGM_ADMIN_TOKEN"] = "adm_" + "k" * 44
    os.environ.pop("PGM_REQUIRE_SECURITY_ENV", None)
    os.environ["PGM_TRUST_USER_HEADER"] = "1"


_security_env()

USDC = 10 ** 6
CHAINS = {"ethereum": 12, "base": 5, "polygon": 128, "arbitrum": 8}


class WalletBase(unittest.TestCase):
    """One app per class, one account per test.

    Identity is the trust header (`X-User-Id`) rather than a bearer token on purpose: most of what this file
    asserts is what happens to a user who has *not* finished the ceremony — no password, no authenticator, a
    watch-only wallet — and every one of those states has to be reachable, so signing in with a password cannot be
    the price of admission. `TestWalletSession` covers the bearer path separately, because that is the one the
    Mini App actually uses.
    """

    app_name = "wallet-api"
    app = client = None

    @classmethod
    def setUpClass(cls):
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
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
        # One Argon2id hash per test, shared by every fixture account in it: the cost is the point, and a suite
        # that hashes a password per account is a suite somebody turns off.
        self.phc = self.app._hasher().hash(self.pw)
        self.seq = 0
        self.uid = self.user()

    # ------------------------------------------------------------------ helpers
    @property
    def SEC(self):
        return self.app.SEC

    def h(self, uid=None):
        """The trust-header identity. `Content-Type` always set: every mutating route here takes a JSON body."""
        return {"X-User-Id": uid or self.uid, "Content-Type": "application/json"}

    def user(self, *, pw=True, email_identity=True):
        uid = "u_w_" + uuid.uuid4().hex[:10]
        self.con.execute("INSERT INTO users (id, created_ms) VALUES (?,?)", (uid, self.now))
        if email_identity:
            self.SEC.link_identity(uid, "email", "%s@example.test" % uid, at=self.now)
        if pw:
            self.SEC.set_password(uid, self.phc, at=self.now)
        return uid

    def wallet(self, uid=None, *, custody="delegated", signature_type=3, state="funded",
               address="0x1111111111111111111111111111111111111111"):
        uid = uid or self.uid
        self.con.execute("INSERT INTO wallets (user_id, provider, custody, address, proxy_address, signature_type,"
                         " policy_hash, state, created_ms, updated_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (uid, "turnkey", custody, address, "0x" + "2" * 40, int(signature_type), "ph-do-not-trade",
                          state, self.now, self.now))
        return address

    def cash(self, micro, *, kind="deposit", reason="test credit", uid=None, at=None):
        """A ledger row. `ref_id` is unique per row because the table is `UNIQUE (ref_table, ref_id, kind, user_id)`
        — a fixture that reused a key would be silently swallowed and the test would then assert about one row."""
        uid = uid or self.uid
        self.seq += 1
        self.con.execute("INSERT INTO cash_ledger (user_id, kind, amount_micro, ref_table, ref_id, created_ms,"
                         " reason) VALUES (?,?,?,?,?,?,?)",
                         (uid, kind, int(micro), "test", "t%d" % self.seq, int(at if at is not None else self.now),
                          reason))
        self.con.commit()
        return int(micro)

    def address(self, uid=None, *, addr=None, usable=True):
        """An allowlisted destination. `usable=` moves it out of its 24-hour hold the way time would: the hold is a
        real rule and two tests below assert it *first* rather than walking around it."""
        uid = uid or self.uid
        addr = addr or ("0x" + uuid.uuid4().hex + "ab" * 4)[:42]
        out = self.SEC.add_address(uid, addr, at=self.now, label="cold")
        if usable:
            self.con.execute("UPDATE withdrawal_addresses SET usable_ms=? WHERE id=?", (self.now - 1, out["id"]))
            self.con.commit()
        return out["id"], addr

    def arm_totp(self, uid=None):
        """Enrol and verify, through the routes: the secret is only readable in the enrol response, and computing a
        code against a fixture-invented secret would test this file instead of the product.

        The arming code is deliberately for the PREVIOUS window. `verify` accepts one step either side and then
        records the step it accepted, so arming with "now" leaves a spend at "now" indistinguishable from a replay
        whenever the test happens to cross a 30-second boundary. Arming one window back makes the spend strictly
        newer than the accepted step, every run, without weakening what is being tested: the replay guard itself is
        asserted below with the arming code."""
        from polygm_core.security import totp
        h = self.h(uid)
        r = self.client.post("/v1/auth/totp/enroll", json={}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        secret = r.json()["secret"]
        code = totp.code_for(secret, self.now - totp.PERIOD_S * 1000)
        v = self.client.post("/v1/auth/totp/verify", json={"code": code}, headers=h)
        self.assertEqual(v.status_code, 200, v.text)
        self.armed_code_at = self.now - totp.PERIOD_S * 1000
        return secret

    def code(self, secret, *, steps=0):
        """A code for the spend window: `steps=0` is now, `steps=-1` is the code that armed the factor."""
        from polygm_core.security import totp
        return totp.code_for(secret, self.now + steps * totp.PERIOD_S * 1000)

    def key_material(self, uid=None):
        uid = uid or self.uid
        # `key_wraps.kek_version` is a real foreign key to `kek_versions`: a wrap that names a ceremony nobody
        # recorded is refused by the database, which is the point of the column. The fixture records version 1.
        if not self.con.execute("SELECT 1 FROM kek_versions WHERE version=1").fetchone():
            self.SEC.new_kek(at=self.now, provider="env", key_id="test-ceremony")
        self.SEC.wrap_key(uid, ciphertext=base64.b64encode(b"wrapped" + os.urandom(8)).decode(),
                          nonce=base64.b64encode(os.urandom(12)).decode(),
                          tag=base64.b64encode(os.urandom(16)).decode(), kek_version=1,
                          policy_hash="ph-do-not-trade", at=self.now)
        row = self.con.execute("SELECT dek_version FROM key_wraps WHERE user_id=?", (uid,)).fetchone()
        return int(row[0])

    def key(self, name: str) -> str:
        """An Idempotency-Key that is unique to this test *and* this run.

        `withdrawals.idempotency_key` is UNIQUE on the column, not per user, and `import_app` gives the whole module
        one database file that survives between runs — so a literal key is a collision with the previous run's row
        and a 500 out of a route that has nothing wrong with it. The key is still a plausible one: 8-128 chars of
        `[A-Za-z0-9_-]`, which is what `_idem_shape` enforces.
        """
        return "k-%s-%s" % (str(self.uid).replace("_", ""), name)

    def count(self, sql, *args):
        return int(self.con.execute(sql, args).fetchone()[0])

    def err(self, resp, code, status=None):
        body = resp.json()
        self.assertEqual((body.get("error") or {}).get("code"), code,
                         "wanted %s, got %s (http %d)" % (code, json.dumps(body)[:300], resp.status_code))
        if status is not None:
            self.assertEqual(resp.status_code, status, body)
        return body

    def ok(self, resp, status=200):
        self.assertEqual(resp.status_code, status, resp.text[:400])
        return resp.json()


class TestWalletBalance(WalletBase):
    """The wallet card: what you hold, what is reserved, and which locks are already armed."""

    def test_the_ledger_is_the_balance_and_the_intents_are_the_reserve(self):
        self.wallet()
        self.cash(120 * USDC)
        self.cash(-20 * USDC, kind="buy", reason="BUY 200 sh at 0.1")
        self.con.execute("INSERT INTO order_intents (id, user_id, market_id, token_id, side, price_micro,"
                         " size_micro, notional_micro, state, idempotency_key, created_ms, updated_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("oi-wallet-1", self.uid, "0xM1", "0xT1", "BUY", 500_000, 30 * USDC, 15 * USDC,
                          "submitted", "k-wallet-1", self.now, self.now))
        self.con.commit()
        out = self.ok(self.client.get("/v1/wallet/balance", headers=self.h()))
        self.assertEqual(out["cashMicro"], str(100 * USDC))
        self.assertEqual(out["reservedMicro"], str(15 * USDC))
        self.assertEqual(out["wallet"]["custody"], "delegated")
        self.assertEqual(out["wallet"]["address"], "0x1111111111111111111111111111111111111111")
        self.assertEqual({c["chain"]: c["confirmations"] for c in out["chains"]}, CHAINS)
        self.assertTrue(out["locks"]["password"])
        self.assertFalse(out["locks"]["totp"], "the card must not claim a lock the user has not armed")
        self.assertTrue(out.get("cacheKey"), "the Mini App's cache is keyed per user; an empty key is a shared one")

    def test_no_wallet_is_a_null_wallet_and_a_sentence_not_a_404(self):
        out = self.ok(self.client.get("/v1/wallet/balance", headers=self.h()))
        self.assertIsNone(out["wallet"])
        self.assertEqual(out["cashMicro"], "0")
        self.assertIn("/wallet", out["note"], "the screen has to say where a wallet comes from")

    def test_a_suspended_wallet_is_not_a_deposit_destination(self):
        self.wallet(state="suspended")
        out = self.ok(self.client.get("/v1/wallet/balance", headers=self.h()))
        self.assertIsNone(out["wallet"], "suspended and closing are not places money may be sent")

    def test_an_anonymous_caller_gets_a_401_and_no_numbers(self):
        r = self.client.get("/v1/wallet/balance", headers={"X-User-Id": ""})
        self.err(r, "UNAUTHENTICATED", 401)
        self.assertNotIn("wallet", r.json())


class TestWalletTransactions(WalletBase):
    """The statement. `cash_ledger` is append-only and every row is money that moved."""

    def test_entries_are_newest_first_and_the_cursor_is_the_last_row(self):
        self.cash(10 * USDC, reason="first")
        self.cash(-3 * USDC, kind="buy", reason="second", at=self.now + 1_000)
        self.cash(2 * USDC, kind="refund", reason="third", at=self.now + 2_000)
        out = self.ok(self.client.get("/v1/wallet/transactions?limit=2", headers=self.h()))
        self.assertEqual([e["reason"] for e in out["entries"]], ["third", "second"])
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["nextBeforeMs"], self.now + 1_000, "the cursor is the age of the oldest row shown")
        page2 = self.ok(self.client.get("/v1/wallet/transactions?limit=2&beforeMs=%d" % out["nextBeforeMs"],
                                        headers=self.h()))
        self.assertEqual([e["reason"] for e in page2["entries"]], ["first"])
        self.assertEqual(page2["nextBeforeMs"], 0, "a short page is the end of the statement")

    def test_the_amount_and_the_reason_travel_verbatim(self):
        self.cash(1_500_000, reason="BUY 3 sh at 0.500")
        out = self.ok(self.client.get("/v1/wallet/transactions", headers=self.h()))
        self.assertEqual(out["entries"][0]["deltaMicro"], "1500000")
        self.assertEqual(out["entries"][0]["reason"], "BUY 3 sh at 0.500",
                         "\"adjust\" with no sentence is the row a support ticket is made of")
        self.assertEqual(out["entries"][0]["ref"], "test:t1")

    def test_the_limit_is_bounded_at_both_ends(self):
        for i in range(3):
            self.cash((i + 1) * USDC, at=self.now + i)
        self.assertEqual(len(self.ok(self.client.get("/v1/wallet/transactions?limit=1",
                                                     headers=self.h()))["entries"]), 1)
        self.assertEqual(self.client.get("/v1/wallet/transactions?limit=500", headers=self.h()).status_code, 422)
        self.assertEqual(self.client.get("/v1/wallet/transactions?limit=0", headers=self.h()).status_code, 422)

    def test_another_accounts_money_is_not_in_this_statement(self):
        other = self.user()
        self.cash(99 * USDC, uid=other, reason="not yours")
        self.cash(1 * USDC, reason="yours")
        out = self.ok(self.client.get("/v1/wallet/transactions", headers=self.h()))
        self.assertEqual([e["reason"] for e in out["entries"]], ["yours"])

    def test_an_anonymous_caller_gets_nothing(self):
        r = self.client.get("/v1/wallet/transactions", headers={"X-User-Id": ""})
        self.err(r, "UNAUTHENTICATED", 401)


class TestDepositQuote(WalletBase):
    """A deposit intent: the address to send to, and the four legs the app will then report on."""

    def body(self, **over):
        out = {"chain": "polygon", "amountUsdc": "50"}
        out.update(over)
        return out

    def test_a_quote_names_the_address_the_wait_and_the_row(self):
        self.wallet()
        out = self.ok(self.client.post("/v1/wallet/deposit/quote", json=self.body(),
                                       headers=dict(self.h(), **{"Idempotency-Key": self.key("dep")})), 202)
        self.assertEqual(out["status"], "detecting")
        self.assertEqual(out["chain"], "polygon")
        self.assertEqual(out["amountMicro"], str(50 * USDC))
        self.assertEqual(out["address"], "0x1111111111111111111111111111111111111111")
        self.assertEqual(out["minConfirmations"], 128, "polygon waits 128 blocks and the screen must say so")
        self.assertEqual(out["confirmations"], 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM deposits WHERE user_id=?", self.uid), 1)

    def test_a_retry_with_the_same_key_is_the_same_deposit_not_a_second_one(self):
        self.wallet()
        k = {"Idempotency-Key": self.key("dep-retry")}
        first = self.ok(self.client.post("/v1/wallet/deposit/quote", json=self.body(), headers=dict(self.h(), **k)),
                        202)
        again = self.ok(self.client.post("/v1/wallet/deposit/quote", json=self.body(), headers=dict(self.h(), **k)))
        self.assertEqual(again["depositId"], first["depositId"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM deposits WHERE user_id=?", self.uid), 1,
                         "a client that taps twice is watching one deposit")

    def test_the_same_key_with_a_different_body_is_a_conflict(self):
        self.wallet()
        k = {"Idempotency-Key": self.key("dep-conflict")}
        self.client.post("/v1/wallet/deposit/quote", json=self.body(), headers=dict(self.h(), **k))
        r = self.client.post("/v1/wallet/deposit/quote", json=self.body(amountUsdc="51"), headers=dict(self.h(), **k))
        self.err(r, "IDEM_CONFLICT", 409)

    def test_a_missing_key_is_refused(self):
        self.wallet()
        self.err(self.client.post("/v1/wallet/deposit/quote", json=self.body(), headers=self.h()),
                 "IDEM_KEY_REQUIRED", 400)

    def test_a_chain_with_no_bridge_behind_it_is_refused(self):
        self.wallet()
        r = self.client.post("/v1/wallet/deposit/quote", json=self.body(chain="solana"),
                             headers=dict(self.h(), **{"Idempotency-Key": self.key("dep-chain")}))
        body = self.err(r, "BAD_FIELD")
        self.assertIn("chain", json.dumps(body), "the refusal names the field that is wrong")
        # The four chains the quote accepts are the four the wallet card lists, which is why the card carries them.
        listed = [c["chain"] for c in self.ok(self.client.get("/v1/wallet/balance", headers=self.h()))["chains"]]
        self.assertIn("polygon", listed)

    def test_a_malformed_amount_never_reaches_an_insert(self):
        self.wallet()
        for raw, code in (("0", "ZERO_SIZE"), ("abc", "BAD_AMOUNT"), ("-5", "BAD_AMOUNT"), ("", "BAD_AMOUNT")):
            with self.subTest(raw=raw):
                r = self.client.post("/v1/wallet/deposit/quote", json=self.body(amountUsdc=raw),
                                     headers=dict(self.h(), **{"Idempotency-Key": self.key("dep-bad-" + (raw or "empty"))}))
                self.err(r, code)
        self.assertEqual(self.count("SELECT COUNT(*) FROM deposits WHERE user_id=?", self.uid), 0)

    def test_a_deposit_without_a_wallet_says_where_a_wallet_comes_from(self):
        r = self.client.post("/v1/wallet/deposit/quote", json=self.body(),
                             headers=dict(self.h(), **{"Idempotency-Key": self.key("dep-nw")}))
        self.err(r, "NOT_FOUND", 404)

    def test_an_anonymous_caller_cannot_create_a_deposit_intent(self):
        self.wallet()
        r = self.client.post("/v1/wallet/deposit/quote", json=self.body(),
                             headers={"X-User-Id": "", "Content-Type": "application/json",
                                      "Idempotency-Key": self.key("dep-anon")})
        self.err(r, "UNAUTHENTICATED", 401)
        self.assertEqual(self.count("SELECT COUNT(*) FROM deposits WHERE user_id=?", self.uid), 0)


class TestDepositProgress(WalletBase):
    """The four legs. A progress screen that cannot say "stuck" is a spinner that never ends."""

    def deposit(self, uid=None, *, status="detecting", chain="base", confirmations=0, error=None, resolved=None):
        uid = uid or self.uid
        row = self.con.execute("INSERT INTO deposits (user_id, asset, chain, amount_micro, credit_key, status,"
                               " confirmations, last_error, first_seen_ms, resolved_ms, tx_hash)"
                               " VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                               (uid, "USDC", chain, 20 * USDC, "dep:%s:%d" % (uid, self.seq + 1), status,
                                int(confirmations), error, self.now, resolved, "0xtx")).fetchone()
        self.seq += 1
        self.con.commit()
        return int(row[0])

    def test_the_legs_are_states_and_exactly_one_is_active(self):
        self.wallet()
        did = self.deposit(status="confirming", chain="base", confirmations=2)
        out = self.ok(self.client.get("/v1/wallet/deposit/%d" % did, headers=self.h()))
        self.assertEqual([s["key"] for s in out["steps"]],
                         ["detecting", "confirming", "bridging", "crediting", "credited"])
        self.assertEqual([s["state"] for s in out["steps"]],
                         ["done", "active", "pending", "pending", "pending"])
        self.assertIn("5", out["steps"][1]["label"], "the wait is a number of blocks, said out loud")
        self.assertEqual(out["minConfirmations"], 5)
        self.assertEqual(out["problem"], "")

    def test_credited_marks_every_leg_done_and_stamps_the_time(self):
        self.wallet()
        did = self.deposit(status="credited", chain="ethereum", confirmations=12, resolved=self.now + 500)
        out = self.ok(self.client.get("/v1/wallet/deposit/%d" % did, headers=self.h()))
        self.assertEqual([s["state"] for s in out["steps"]], ["done"] * 5)
        self.assertEqual(out["resolvedMs"], self.now + 500)
        self.assertEqual(out["steps"][-1]["doneMs"], self.now + 500)

    def test_a_stuck_deposit_carries_the_reason_instead_of_a_spinner(self):
        self.wallet()
        did = self.deposit(status="stuck", error="the bridge returned an unparseable receipt")
        out = self.ok(self.client.get("/v1/wallet/deposit/%d" % did, headers=self.h()))
        self.assertEqual(out["problem"], "the bridge returned an unparseable receipt")
        self.assertEqual(out["steps"][-1]["state"], "stopped",
                         "a stopped deposit must not paint five green ticks")
        self.assertIn("human", out["steps"][-1]["label"])

    def test_someone_elses_deposit_is_not_found_and_that_is_the_same_answer_as_missing(self):
        other = self.user()
        did = self.deposit(uid=other)
        self.err(self.client.get("/v1/wallet/deposit/%d" % did, headers=self.h()), "NOT_FOUND", 404)
        self.err(self.client.get("/v1/wallet/deposit/999999", headers=self.h()), "NOT_FOUND", 404)

    def test_an_anonymous_caller_cannot_read_a_deposit(self):
        self.wallet()
        did = self.deposit()
        self.err(self.client.get("/v1/wallet/deposit/%d" % did, headers={"X-User-Id": ""}),
                 "UNAUTHENTICATED", 401)


class TestWithdrawCeremony(WalletBase):
    """The order of the locks is the test.

    Allowlist → cooldown → balance → typed amount → typed address → password → authenticator, and every refusal is
    its own code. The tests below are written as a *ladder*: each one breaks exactly one rung and asserts the rung
    above it was reached, so a handler that validated in a different order would fail here rather than in a
    support ticket.
    """

    def ready(self, *, cash=100 * USDC, custody="delegated", signature_type=3, pw=True, usable_address=True):
        uid = self.user(pw=pw)
        self.uid = uid
        self.wallet(uid, custody=custody, signature_type=signature_type)
        if cash:
            self.cash(cash)
        aid, addr = self.address(uid, usable=usable_address)
        secret = self.arm_totp(uid)
        self.secret = secret
        return uid, aid, addr

    def body(self, aid, addr, **over):
        out = {"amountUsdc": "50", "addressId": aid, "typedAmount": "50", "typedAddress": addr,
               "password": self.pw, "code": self.code(self.secret)}
        out.update(over)
        return out

    def post(self, uid, payload, key=None):
        return self.client.post("/v1/wallet/withdraw", json=payload,
                                headers=dict(self.h(uid), **{"Idempotency-Key": key or self.key("wd")}))

    def test_a_destination_nobody_allowlisted_is_refused_before_the_password_is_read(self):
        uid, _aid, addr = self.ready()
        r = self.post(uid, self.body("waddr_nope", addr, password="definitely wrong"))
        self.err(r, "NOT_FOUND", 404)
        self.assertEqual(self.count("SELECT COUNT(*) FROM withdrawals WHERE user_id=?", uid), 0)

    def test_a_fresh_address_is_still_inside_its_hold(self):
        uid, aid, addr = self.ready(usable_address=False)
        body = self.err(self.post(uid, self.body(aid, addr, password="definitely wrong")), "ADDRESS_COOLDOWN")
        self.assertIn("message", body.get("error") or {} or {}, "the hold has to be explainable, not just refused")
        self.assertEqual(self.count("SELECT COUNT(*) FROM withdrawals WHERE user_id=?", uid), 0)

    def test_another_accounts_destination_is_not_yours(self):
        _uid, _aid, addr = self.ready()
        stranger = self.user()
        stranger_aid, _ = self.address(stranger)
        r = self.post(self.uid, self.body(stranger_aid, addr))
        self.err(r, "NOT_FOUND", 404)

    def test_more_than_the_ledger_holds_is_insufficient_balance(self):
        uid, aid, addr = self.ready(cash=10 * USDC)
        body = self.err(self.post(uid, self.body(aid, addr, amountUsdc="50", typedAmount="50")),
                        "INSUFFICIENT_BALANCE")
        self.assertIn("10", json.dumps(body), "the refusal says what is actually available")

    def test_the_typed_amount_must_be_the_amount_shown(self):
        uid, aid, addr = self.ready()
        r = self.post(uid, self.body(aid, addr, typedAmount="49.99"))
        body = self.err(r, "BAD_FIELD", 422)
        self.assertIn("typedAmount", json.dumps(body))
        self.assertEqual(self.count("SELECT COUNT(*) FROM withdrawals WHERE user_id=?", uid), 0)

    def test_the_typed_address_must_be_the_allowlisted_one(self):
        uid, aid, addr = self.ready()
        r = self.post(uid, self.body(aid, addr, typedAddress="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"))
        body = self.err(r, "BAD_FIELD", 422)
        self.assertIn("typedAddress", json.dumps(body))

    def test_a_raw_destination_in_the_body_is_refused_by_name(self):
        uid, aid, addr = self.ready()
        r = self.post(uid, self.body(aid, addr, destAddress="0xfeedfacefeedfacefeedfacefeedfacefeedface"))
        self.err(r, "ADDRESS_NOT_ALLOWED")

    def test_without_a_password_the_answer_says_so_instead_of_failing_the_login(self):
        uid, aid, addr = self.ready(pw=False)
        self.err(self.post(uid, self.body(aid, addr)), "PASSWORD_REQUIRED")

    def test_a_wrong_password_is_a_wrong_password(self):
        uid, aid, addr = self.ready()
        self.err(self.post(uid, self.body(aid, addr, password="not the password")), "PASSWORD_WRONG")

    def test_without_an_authenticator_the_answer_is_enrol_one(self):
        uid, aid, addr = self.ready()
        self.con.execute("DELETE FROM totp_enrollments WHERE user_id=?", (uid,))
        self.con.commit()
        self.err(self.post(uid, self.body(aid, addr)), "TOTP_REQUIRED")

    def test_a_bad_code_is_a_bad_code(self):
        uid, aid, addr = self.ready()
        self.err(self.post(uid, self.body(aid, addr, code="000000")), "TOTP_INVALID")
        self.assertEqual(self.count("SELECT COUNT(*) FROM withdrawals WHERE user_id=?", uid), 0)

    def test_the_code_that_armed_the_factor_cannot_pay_anything(self):
        uid, aid, addr = self.ready()
        self.err(self.post(uid, self.body(aid, addr, code=self.code(self.secret, steps=-1))), "TOTP_INVALID")

    def test_the_happy_path_records_a_request_and_says_it_is_not_a_payment(self):
        uid, aid, addr = self.ready()
        out = self.ok(self.post(uid, self.body(aid, addr), key=self.key("wd-happy")), 202)
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["amountMicro"], str(50 * USDC))
        self.assertEqual(out["destination"], addr)
        self.assertEqual(out["notified"], {"email": False, "telegram": False})
        self.assertIn("not a payment", out["note"])
        row = self.con.execute("SELECT amount_micro, dest_address, typed_amount, typed_address, allowlist_hit,"
                               " cooldown_ok, password_verified, status, idempotency_key, notified_email,"
                               " notified_telegram FROM withdrawals WHERE id=?", (out["withdrawalId"],)).fetchone()
        self.assertEqual(tuple(row[:2]), (50 * USDC, addr))
        self.assertEqual(tuple(row[2:4]), ("50", addr), "the typed strings are stored as typed, not as micros")
        self.assertEqual(tuple(row[4:8]), (1, 1, 1, "queued"))
        self.assertEqual(row[8], self.key("wd-happy"))
        self.assertEqual(tuple(row[9:11]), (0, 0), "nothing has been sent, so nothing claims it was")
        kinds = [r[0] for r in self.con.execute("SELECT kind FROM auth_events WHERE user_id=?", (uid,)).fetchall()]
        self.assertIn("withdraw_requested", kinds, "the request is in the account's own audit trail")

    def test_a_retry_of_a_successful_withdrawal_is_answered_before_the_locks_run(self):
        """The bug this pins: the ceremony used to run *before* the idempotency check, so the retry of a
        withdrawal whose response was lost came back `TOTP_INVALID` — a completed withdrawal reported as a failed
        one, because `_totp_gate` consumes the step. The retry must be answered from the stored body, and the
        authenticator's own bookkeeping must show that nothing verified twice."""
        uid, aid, addr = self.ready()
        before = self.con.execute("SELECT last_step FROM totp_enrollments WHERE user_id=?", (uid,)).fetchone()[0]
        first = self.ok(self.post(uid, self.body(aid, addr), key=self.key("wd-retry")), 202)
        after_first = self.con.execute("SELECT last_step FROM totp_enrollments WHERE user_id=?",
                                       (uid,)).fetchone()[0]
        again = self.ok(self.post(uid, self.body(aid, addr), key=self.key("wd-retry")))
        self.assertEqual(again["withdrawalId"], first["withdrawalId"])
        self.assertEqual(again["status"], "queued")
        self.assertEqual(self.count("SELECT COUNT(*) FROM withdrawals WHERE user_id=?", uid), 1)
        last = self.con.execute("SELECT last_step FROM totp_enrollments WHERE user_id=?", (uid,)).fetchone()[0]
        self.assertEqual(last, after_first, "the replay verified nothing")
        self.assertGreater(after_first, before, "the first call did verify a fresh step")

    def test_a_watch_only_wallet_cannot_sign_anything_out(self):
        uid, aid, addr = self.ready(custody="read_only", signature_type=0)
        self.err(self.post(uid, self.body(aid, addr)), "SIGNER_UNAVAILABLE")

    def test_an_anonymous_caller_cannot_start_a_withdrawal(self):
        uid, aid, addr = self.ready()
        r = self.client.post("/v1/wallet/withdraw", json=self.body(aid, addr),
                             headers={"X-User-Id": "", "Content-Type": "application/json",
                                      "Idempotency-Key": self.key("wd-anon")})
        self.err(r, "UNAUTHENTICATED", 401)
        self.assertEqual(self.count("SELECT COUNT(*) FROM withdrawals WHERE user_id=?", uid), 0)


class TestKeyExport(WalletBase):
    """Export is a ceremony, not a getter: password, authenticator, and the word EXPORT typed out."""

    def ready(self):
        uid = self.user()
        self.uid = uid
        self.wallet(uid)
        self.dek = self.key_material(uid)
        self.secret = self.arm_totp(uid)
        return uid

    def body(self, **over):
        out = {"password": self.pw, "code": self.code(self.secret), "typedConfirm": "EXPORT"}
        out.update(over)
        return out

    def test_the_word_must_be_typed(self):
        uid = self.ready()
        r = self.client.post("/v1/wallet/keys/export", json=self.body(typedConfirm="export"),
                             headers=self.h(uid))
        body = self.err(r, "BAD_FIELD", 422)
        self.assertIn("typedConfirm", json.dumps(body))

    def test_a_wrong_password_stops_it(self):
        uid = self.ready()
        self.err(self.client.post("/v1/wallet/keys/export", json=self.body(password="nope"), headers=self.h(uid)),
                 "PASSWORD_WRONG")

    def test_without_an_authenticator_it_says_so(self):
        uid = self.ready()
        self.con.execute("DELETE FROM totp_enrollments WHERE user_id=?", (uid,))
        self.con.commit()
        self.err(self.client.post("/v1/wallet/keys/export", json=self.body(), headers=self.h(uid)),
                 "TOTP_REQUIRED")

    def test_no_key_material_yet_is_a_404(self):
        uid = self.user()
        self.uid = uid
        self.secret = self.arm_totp(uid)
        self.err(self.client.post("/v1/wallet/keys/export", json=self.body(), headers=self.h(uid)),
                 "NOT_FOUND", 404)

    def test_the_export_hands_over_wrapped_material_once_and_counts_it(self):
        uid = self.ready()
        r = self.client.post("/v1/wallet/keys/export", json=self.body(), headers=self.h(uid))
        out = self.ok(r)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertEqual(out["dekVersion"], self.dek)
        self.assertTrue(out["wrappedKey"] and out["nonce"] and out["tag"])
        self.assertEqual(out["wrapsUsed"], 1, "an export that runs the nonce counter out is a wallet that cannot sign")
        self.assertIn("P14", out["note"], "the screen has to say the ceremony is not go-live")
        self.assertIn("wrapped", out["note"])
        row = self.con.execute("SELECT messages_wrapped FROM key_wraps WHERE user_id=?", (uid,)).fetchone()
        self.assertEqual(int(row[0]), 1)
        kinds = [r[0] for r in self.con.execute("SELECT kind FROM auth_events WHERE user_id=?", (uid,)).fetchall()]
        self.assertIn("key_export", kinds, "the phase demands the export be obvious and logged")

    def test_a_key_that_has_run_out_of_nonces_refuses_to_export_and_says_why(self):
        uid = self.ready()
        self.con.execute("UPDATE key_wraps SET messages_wrapped=? WHERE user_id=?", (2 ** 32 - 1, uid))
        self.con.commit()
        r = self.client.post("/v1/wallet/keys/export", json=self.body(), headers=self.h(uid))
        self.err(r, "SIGNER_UNAVAILABLE")
        kinds = [r[0] for r in self.con.execute("SELECT kind FROM auth_events WHERE user_id=?", (uid,)).fetchall()]
        self.assertIn("key_export_blocked", kinds)

    def test_an_anonymous_caller_cannot_export_anything(self):
        self.ready()
        r = self.client.post("/v1/wallet/keys/export", json=self.body(), headers={"X-User-Id": "",
                                                                                  "Content-Type": "application/json"})
        self.err(r, "UNAUTHENTICATED", 401)


class TestWalletSession(WalletBase):
    """The one path the Mini App actually walks: a session, not the trust header."""

    def login(self, uid):
        r = self.client.post("/v1/auth/login", json={"identifier": uid, "password": self.pw},
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": "Bearer %s" % r.json()["accessToken"], "Content-Type": "application/json"}

    def test_a_session_reads_the_same_wallet_the_webview_will_show(self):
        uid = self.user()
        self.uid = uid
        self.wallet(uid)
        self.cash(7 * USDC)
        h = self.login(uid)
        out = self.ok(self.client.get("/v1/wallet/balance", headers=h))
        self.assertEqual(out["cashMicro"], str(7 * USDC))
        self.assertEqual(out["wallet"]["userId"], uid)

    def test_a_revoked_session_reads_nothing(self):
        uid = self.user()
        self.uid = uid
        self.wallet(uid)
        h = self.login(uid)
        sid = self.ok(self.client.get("/v1/auth/sessions", headers=h))["items"][0]["id"]
        self.ok(self.client.post("/v1/auth/sessions/revoke", json={"sessionId": sid}, headers=h))
        self.err(self.client.get("/v1/wallet/balance", headers=h), "UNAUTHENTICATED", 401)
        # The list is gone too, not just the wallet: revocation must not leave a read surface behind.
        self.err(self.client.get("/v1/wallet/transactions", headers=h), "UNAUTHENTICATED", 401)


class TestWalletCards(WalletBase):
    """The chat's own money cards.

    These are the same ledger reads behind `_tg_fetch`, and they are here because they were broken in a way no
    existing test could see: `delta_micro`/`at_ms` are not columns, `_tm.usdc` is not a function, `kind='realised'`
    is not a ledger kind, and `position_snapshots.balance_micro` is not a column. A card is only exercised when a
    *user taps it*, so `pytest` was green while `/balance`, `/history` and `/pnl` raised on every tap. Calling the
    card builders directly, with money in the tables, is the smallest thing that pins them.
    """

    def cards(self, name, uid=None):
        return self.app._tg_fetch(name, {"account_id": uid or self.uid})

    def test_the_balance_card_prints_the_ledger_sum_in_usdc(self):
        uid = self.user()
        self.uid = uid
        self.cash(42_500_000)
        out = self.cards("balance", uid)
        self.assertIn("42.5", out["text"])
        self.assertIn("USDC", out["text"], "no raw number without its unit")

    def test_the_history_card_lists_movements_newest_first(self):
        uid = self.user()
        self.uid = uid
        self.cash(1_000_000, reason="older", at=self.now)
        self.cash(-500_000, kind="buy", reason="newer", at=self.now + 1_000)
        out = self.cards("history", uid)
        self.assertLess(out["text"].index("newer"), out["text"].index("older"))

    def test_the_pnl_card_nets_exits_against_the_basis_they_released(self):
        """100 USDC in, a 200-share buy at 0.40 (basis 80), a sell of 100 shares for 50: half the basis is
        released, so realised is 50 − 40 = 10 USDC. If the card kept summing `kind='realised'` it would print 0."""
        uid = self.user()
        self.uid = uid
        self.cash(100 * USDC, reason="deposit")
        self.cash(-(80 * USDC), kind="buy", reason="BUY 200 sh at 0.40")
        self.cash(50 * USDC, kind="sell_fill", reason="SELL 100 sh at 0.50")
        self.con.execute("INSERT INTO position_lots (user_id, token_id, market_id, shares_open_micro, basis_micro,"
                         " opened_ms, source) VALUES (?,?,?,?,?,?,?)",
                         (uid, "0xT1", "0xM1", 100 * USDC, 40 * USDC, self.now, "fill"))
        self.con.commit()
        out = self.cards("pnl", uid)
        self.assertIn("10", out["text"], out["text"])
        self.assertIn("drawdown", out["text"], "a PnL with no drawdown beside it is the rule the phase broke")
        self.assertIn("Peak cash", out["text"], "this build has no equity curve, so the card says cash, not equity")

    def test_a_card_with_no_money_says_zero_rather_than_raising(self):
        uid = self.user()
        self.uid = uid
        for name in ("balance", "history", "pnl"):
            with self.subTest(card=name):
                self.cards(name, uid)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
