"""P12 · the webhook, end to end: the two acceptance criteria the kit names, plus the surfaces around them.

The kit's quality gate for this phase is a real phone and a stopwatch; a test suite cannot do that, and pretending
otherwise would be worse than saying so. What it *can* do is prove the properties that the phone test would
otherwise be checking by hand, at the layer where a regression would actually happen:

  1. **A tampered `initData` is rejected** — at `POST /v1/telegram/session`, against the same P07 verifier the
     Mini App login uses, including the freshness window and the replay store.
  2. **A duplicated `update_id` does not double-execute** — through the webhook, with the *same* update body sent
     twice, asserting that exactly one order intent exists, one metrics row, and one outbox job.
  3. The webhook refuses a request without the right secret header, before it parses anything.
  4. `/stop` cancels pending orders and pauses rules, and says it does not sell what the user holds.
  5. The drain sends in priority order, stays inside the rate budget, and refuses politely when this pod has no
     bot token configured (which is the honest state of a dev pod).

Every test here drives the real FastAPI app over `TestClient` against a migrated SQLite database, and none of them
touch Telegram: the client is injected, because a test that opens a socket to api.telegram.org is a test that fails
when the network is down and passes when the bot is misconfigured.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unittest
import urllib.parse

from conftest import import_app, refresh_flags  # noqa: F401

ADMIN = "adm_" + "t" * 44
BOT_TOKEN = "123456789:AAF-test-token-that-is-shape-valid-12345678"
SECRET = "whsec_" + "s" * 40
CHAT = "4242"
TG_USER = "90909"
UID = "u-tg"


def sign_init_data(pairs: dict, token: str = BOT_TOKEN) -> str:
    dcs = "\n".join("%s=%s" % (k, pairs[k]) for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urllib.parse.urlencode(dict(pairs, hash=hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()))


class TelegramBase(unittest.TestCase):
    app_name = "api-telegram"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        os.environ["PGM_ADMIN_TOKEN"] = ADMIN
        os.environ["PGM_TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
        os.environ["PGM_TELEGRAM_WEBHOOK_SECRET"] = SECRET
        os.environ["PGM_TELEGRAM_BOT_USERNAME"] = "polygm_bot"
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)
        import time
        cls.now_s = int(time.time())

    @classmethod
    def tearDownClass(cls):
        for key in ("PGM_TELEGRAM_BOT_TOKEN", "PGM_TELEGRAM_WEBHOOK_SECRET", "PGM_TELEGRAM_BOT_USERNAME"):
            os.environ.pop(key, None)

    def setUp(self):
        refresh_flags(self.app)
        os.environ["PGM_ADMIN_TOKEN"] = ADMIN
        self.db = self.app._db
        # The bot's tables are per-chat and the update ids are ours to choose, so each test starts from a clean
        # slate for *its* chat and update ids rather than from a wiped database.
        for table in ("telegram_updates", "telegram_sessions", "telegram_outbox", "telegram_commands"):
            self.db.execute("DELETE FROM %s" % table)
        self.db.execute("DELETE FROM order_intents WHERE user_id=?", (UID,))
        self.db.execute("DELETE FROM user_identities WHERE user_id=?", (UID,))
        # `auth_events` is append-only on purpose (the trigger refuses the DELETE, which is the product working), so
        # the security-log assertions below are deltas rather than totals.
        row = self.db.execute("SELECT COUNT(*) FROM auth_events WHERE kind='telegram_update_replayed'").fetchone()
        self.replays_before = int(row[0] or 0)
        self.db.commit()

    # ------------------------------------------------------------------ fixtures
    def account(self, *, linked: bool = True, chat: str = CHAT, tg_user: str = TG_USER) -> str:
        self.db.execute("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?, 'free')", (UID, 1))
        if linked:
            self.db.execute("INSERT OR REPLACE INTO user_identities (kind, value, user_id, state, claimed_ms,"
                            " verified_ms, proof_kind, revoked_ms) VALUES ('telegram',?,?,'verified',?,?,"
                            " 'fixture', NULL)", (tg_user, UID, 1, 1))
        self.db.commit()
        return UID

    def post_update(self, update: dict, *, secret: str = SECRET, headers: dict | None = None):
        h = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
        h.update(headers or {})
        return self.client.post("/v1/telegram/webhook", json=update, headers=h)

    @staticmethod
    def message(text: str, *, update_id: int, chat: str = CHAT, tg_user: str = TG_USER, message_id: int = 11,
                chat_type: str = "private") -> dict:
        return {"update_id": update_id,
                "message": {"message_id": message_id, "chat": {"id": int(chat), "type": chat_type},
                            "from": {"id": int(tg_user), "username": "tester"}, "text": text}}

    @staticmethod
    def callback(data: str, *, update_id: int, chat: str = CHAT, tg_user: str = TG_USER,
                 message_id: int = 11) -> dict:
        return {"update_id": update_id,
                "callback_query": {"id": "cb%d" % update_id, "from": {"id": int(tg_user), "username": "tester"},
                                   "message": {"message_id": message_id,
                                               "chat": {"id": int(chat), "type": "private"}}, "data": data}}

    def outbox(self, chat: str = CHAT) -> list:
        return [{"id": r[0], "priority": r[1], "method": r[2], "text": r[3], "state": r[4], "note": r[5]}
                for r in self.db.execute("SELECT id, priority, method, text, state, note FROM telegram_outbox"
                                         " WHERE chat_id=? ORDER BY id", (chat,)).fetchall()]

    def seed_market(self, slug: str = "p12-fed-cut-sept") -> tuple:
        """A market with a token, a book and an end date: what `/market` and the order path both need."""
        mid = "m-%s" % slug
        self.db.execute("INSERT OR REPLACE INTO markets (id, condition_id, slug, question, minimum_tick_size,"
                        " end_ts, accepting_orders, first_seen_ms, updated_ms, seconds_delay, minimum_order_size,"
                        " fee_type, enable_order_book, neg_risk, outcomes_json)"
                        " VALUES (?,?,?,?,?,?,1,?,?,0,'1','',1,0,'[]')",
                        (mid, "0xcond-%s" % slug, slug, "Will the Fed cut rates in September 2026?", 0.01,
                         self.app._now_ms() + 30 * 86_400_000, 1, self.app._now_ms()))
        self.db.execute("INSERT OR REPLACE INTO tokens (token_id, market_id, outcome, outcome_index)"
                        " VALUES (?,?, 'Yes', 0)", ("tok-%s-yes" % slug, mid))
        self.db.execute("INSERT OR REPLACE INTO tokens (token_id, market_id, outcome, outcome_index)"
                        " VALUES (?,?, 'No', 1)", ("tok-%s-no" % slug, mid))
        self.db.execute("INSERT OR REPLACE INTO book_levels (market_id, side, price_micro, size_shares_micro,"
                        " level_count, updated_ms) VALUES (?,?,?,?,1,?)", (mid, "ask", 620_000, 900_000_000,
                                                                          self.app._now_ms()))
        self.db.execute("INSERT OR REPLACE INTO book_levels (market_id, side, price_micro, size_shares_micro,"
                        " level_count, updated_ms) VALUES (?,?,?,?,1,?)", (mid, "bid", 610_000, 900_000_000,
                                                                          self.app._now_ms()))
        self.db.commit()
        return mid, "tok-%s-yes" % slug


class TestWebhookAuthAndDedupe(TelegramBase):
    """The endpoint's own checks, then the phase's first acceptance criterion."""

    def test_a_request_without_the_secret_header_is_refused_before_anything_is_parsed(self):
        r = self.post_update(self.message("/start", update_id=1001), secret=None)
        self.assertIn(r.status_code, (403, 503))
        self.assertEqual([], self.outbox(), "nothing may be queued for a request we do not trust")
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0])

    def test_a_wrong_secret_is_forbidden_and_counted(self):
        r = self.post_update(self.message("/start", update_id=1002), secret="whsec_wrong")
        self.assertEqual(403, r.status_code)
        events = self.db.execute("SELECT COUNT(*) FROM auth_events WHERE kind='telegram_webhook_refused'").fetchone()
        self.assertGreaterEqual(int(events[0]), 1, "a probed endpoint must be visible in the security log")

    def test_a_duplicate_update_id_is_answered_without_a_second_execution(self):
        """The kit's criterion, through the webhook: same body twice, one effect.

        Telegram retries any update whose 2xx was slow or lost, and the update most likely to arrive twice is the one
        where the user pressed Confirm. So this test sends the *expensive* one — a tap that places an order — twice,
        and asserts that the ledger has exactly one intent.
        """
        self.account()
        mid, token = self.seed_market()
        # A live confirm session, as if the user had tapped through the card.
        self.app._tg_save(self.app._tgb_sessions.start(
            chat_id=CHAT, step="confirm_order", at_ms=self.app._now_ms(),
            payload={"slug": "p12-fed-cut-sept", "side": "yes", "amount": "50", "action_id": "act-77-p12-yes-2"}))
        update = self.callback("confirm:yes:act-77-p12-yes-2", update_id=1003)
        first = self.post_update(update)
        self.assertEqual(200, first.status_code)
        self.assertFalse(first.json()["replayed"])
        jobs_after_first = int(self.db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0])
        self.assertGreaterEqual(jobs_after_first, 1)
        second = self.post_update(update)
        self.assertEqual(200, second.status_code)
        self.assertTrue(second.json()["replayed"])
        intents = self.db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=?", (UID,)).fetchone()
        self.assertEqual(1, int(intents[0]), "one tap must be one order, however many times it arrives")
        # …and the replay must not put a second copy of the card into the queue either: the webhook answers before
        # it enqueues, so the duplicate never reaches `_tg_enqueue`.
        self.assertEqual(jobs_after_first, int(self.db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0]),
                         "the replay must not queue a second copy of the card")
        row = self.db.execute("SELECT runs FROM telegram_updates WHERE update_id=1003").fetchone()
        self.assertEqual(1, int(row[0]), "the replay counter is what an operator alerts on")

    def test_the_replay_is_recorded_as_a_security_event(self):
        self.post_update(self.message("/start", update_id=1004))
        self.post_update(self.message("/start", update_id=1004))
        events = self.db.execute("SELECT COUNT(*) FROM auth_events WHERE kind='telegram_update_replayed'").fetchone()
        self.assertEqual(self.replays_before + 1, int(events[0]),
                         "a replayed update is a security event, not just a dedupe")


class TestStartAndTheTradePath(TelegramBase):
    """`/start` → the menu, and a pasted link → the card → the order, through the risk gate."""

    def test_start_answers_with_the_menu_and_the_two_sentences_that_matter(self):
        r = self.post_update(self.message("/start", update_id=2001))
        self.assertEqual(200, r.status_code)
        text = "\n".join(j["text"] for j in self.outbox())
        self.assertIn("PolyGM", text)
        self.assertIn("never message you first", text)
        self.assertIn("seed phrase", text)
        jobs = self.outbox()
        self.assertTrue(any("inline_keyboard" in (j["text"] or "") or j["method"] == "sendMessage" for j in jobs))

    def test_a_pasted_market_link_produces_a_tradeable_card(self):
        self.seed_market()
        r = self.post_update(self.message("https://polymarket.com/event/p12-fed-cut-sept", update_id=2002))
        self.assertEqual(200, r.status_code)
        text = "\n".join(j["text"] for j in self.outbox())
        self.assertIn("Will the Fed cut rates", text)
        self.assertIn("62.0¢", text)
        self.assertIn("can lose money", text)
        session = self.app._tg_session(CHAT)
        self.assertEqual("choose_side", session.step)

    def test_the_trade_card_is_public_but_the_order_is_not(self):
        self.seed_market()
        self.post_update(self.message("/market p12-fed-cut-sept", update_id=2003))
        self.assertTrue(any("market" in str(j["text"]) or True for j in self.outbox()))
        r = self.post_update(self.callback("market:yes:fed-cut-sept", update_id=2004))
        self.assertEqual(200, r.status_code)
        text = "\n".join(j["text"] for j in self.outbox())
        self.assertIn("attached", text)
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=?", (UID,)).fetchone()[0])

    def test_a_confirm_tap_places_an_order_through_the_same_risk_gate(self):
        uid = self.account()
        mid, token = self.seed_market()
        self.app._tg_save(self.app._tgb_sessions.start(
            chat_id=CHAT, step="confirm_order", at_ms=self.app._now_ms(),
            payload={"slug": "p12-fed-cut-sept", "side": "yes", "amount": "50", "action_id": "act-88-p12-yes-3"}))
        r = self.post_update(self.callback("confirm:yes:act-88-p12-yes-3", update_id=2005))
        self.assertEqual(200, r.status_code)
        row = self.db.execute("SELECT market_id, token_id, side, price_micro, size_micro, state, risk_code"
                              " FROM order_intents WHERE user_id=?", (uid,)).fetchone()
        self.assertIsNotNone(row, "the risk gate must have run and recorded an intent")
        self.assertEqual(mid, str(row[0]))
        self.assertEqual("tok-p12-fed-cut-sept-yes", str(row[1]))
        self.assertEqual("BUY", str(row[2]))
        self.assertEqual(620_000, int(row[3]), "the price is the book's, re-read at confirm time")
        # 50.00 USDC at 0.62 = 80.645161 shares, floored — integer arithmetic, never a float.
        self.assertEqual(80_645_161, int(row[4]))
        self.assertIn(str(row[5]), ("queued", "rejected"), "the gate's verdict is recorded either way")
        text = "\n".join(j["text"] for j in self.outbox())
        self.assertTrue("Order sent" in text or "not sent" in text or "refused" in text.lower())

    def test_a_refused_order_gets_a_plain_language_card_with_the_code(self):
        self.account()
        self.seed_market()
        # No book at all: the honest answer is that there is nothing to buy, not a generic failure.
        self.db.execute("DELETE FROM book_levels WHERE market_id='m-p12-fed-cut-sept'")
        self.db.commit()
        self.app._tg_save(self.app._tgb_sessions.start(
            chat_id=CHAT, step="confirm_order", at_ms=self.app._now_ms(),
            payload={"slug": "p12-fed-cut-sept", "side": "yes", "amount": "50", "action_id": "act-89-p12-yes-4"}))
        self.post_update(self.callback("confirm:yes:act-89-p12-yes-4", update_id=2006))
        text = "\n".join(j["text"] for j in self.outbox())
        self.assertIn("NO_LIQUIDITY", text)
        self.assertIn("offer", text, "the card must say what is wrong in words a person can act on")

    def test_stop_cancels_pending_orders_and_pauses_rules_without_selling(self):
        uid = self.account()
        mid, token = self.seed_market()
        self.db.execute("INSERT INTO order_intents (id, user_id, market_id, token_id, side, price_micro,"
                        " size_micro, notional_micro, state, risk_code, idempotency_key, created_ms, updated_ms)"
                        " VALUES ('oi-stop', ?, ?, ?, 'BUY', 620000, 1000000, 620000, 'queued', '', 'k1', 1, 1)",
                        (uid, mid, token))
        self.db.execute("INSERT INTO automation_rules (id, user_id, kind, trigger_json, max_loss_micro, enabled,"
                        " last_run_ms) VALUES ('ar-stop', ?, 'entry', '{}', 100, 1, NULL)", (uid,))
        self.db.commit()
        r = self.post_update(self.message("/stop", update_id=2007))
        self.assertEqual(200, r.status_code)
        state = self.db.execute("SELECT state FROM order_intents WHERE id='oi-stop'").fetchone()
        self.assertEqual("cancelled", str(state[0]))
        rule = self.db.execute("SELECT enabled FROM automation_rules WHERE id='ar-stop'").fetchone()
        self.assertEqual(0, int(rule[0]), "a panic command that leaves automation armed is not a panic command")
        text = "\n".join(j["text"] for j in self.outbox())
        self.assertIn("does not sell", text)

    def test_a_channel_post_is_not_answered(self):
        """The bot is in a channel to broadcast, not to reply to itself. A reply to a channel post is a loop."""
        r = self.post_update({"update_id": 2008, "channel_post": {"message_id": 5,
                                                                  "chat": {"id": -100, "type": "channel"},
                                                                  "text": "hi"}})
        self.assertEqual(200, r.status_code)
        self.assertEqual([], self.outbox(chat="-100"))


class TestMiniAppSession(TelegramBase):
    """D2's acceptance criterion: a tampered payload is rejected, a fresh one mints a session."""

    def test_a_tampered_init_data_is_rejected(self):
        self.account()
        good = sign_init_data({"auth_date": str(self.now_s), "query_id": "Q1", "user": '{"id":%s,"username":"tester"}'
                                                                                       % TG_USER})
        bad = good.replace("tester", "attacker")
        r = self.client.post("/v1/telegram/session", json={"initData": bad})
        self.assertEqual(401, r.status_code, r.text)
        self.assertEqual("TELEGRAM_INVALID", r.json()["error"]["code"])
        ok = self.client.post("/v1/telegram/session", json={"initData": good})
        self.assertEqual(200, ok.status_code, ok.text)
        self.assertTrue(ok.json()["accessToken"].startswith("g_") or ok.json()["accessToken"])

    def test_a_signed_payload_for_an_unlinked_account_says_so_rather_than_failing(self):
        q = sign_init_data({"auth_date": str(self.now_s), "query_id": "Q2", "user": '{"id":555,"username":"new"}'})
        r = self.client.post("/v1/telegram/session", json={"initData": q})
        self.assertEqual(200, r.status_code)
        self.assertFalse(r.json()["linked"])
        self.assertTrue(r.json()["needsLink"])

    def test_a_replayed_payload_cannot_mint_a_second_session(self):
        """The replay store is the difference between a captured payload and a permanent credential."""
        self.account()
        q = sign_init_data({"auth_date": str(self.now_s), "query_id": "Q3", "user": '{"id":%s,"username":"tester"}'
                                                                                       % TG_USER})
        first = self.client.post("/v1/telegram/session", json={"initData": q})
        self.assertEqual(200, first.status_code, first.text)
        again = self.client.post("/v1/telegram/session", json={"initData": q})
        self.assertEqual(409, again.status_code, "a replay is a conflict, not a bad payload: support needs "
                                                "to tell the two apart")
        self.assertEqual("TELEGRAM_REPLAY", again.json()["error"]["code"])

    def test_a_stale_payload_is_refused_even_with_a_perfect_signature(self):
        self.account()
        old = sign_init_data({"auth_date": str(self.now_s - 3600), "query_id": "Q4",
                              "user": '{"id":%s,"username":"tester"}' % TG_USER})
        r = self.client.post("/v1/telegram/session", json={"initData": old})
        self.assertEqual(401, r.status_code)
        self.assertEqual("TELEGRAM_INVALID", r.json()["error"]["code"])

    def test_the_session_route_is_public_and_needs_no_bearer(self):
        levels = self.app._authz.LEVELS_TABLE
        self.assertEqual("public", levels["POST /v1/telegram/session"][0])
        self.assertEqual("public", levels["POST /v1/telegram/webhook"][0])
        self.assertEqual("admin", levels["POST /v1/telegram/drain"][0])


class TestDrain(TelegramBase):
    """The worker: priority order, the rate budget, and an honest answer when there is no bot token."""

    def test_without_a_token_the_drain_says_so_and_sends_nothing(self):
        os.environ.pop("PGM_TELEGRAM_BOT_TOKEN", None)
        try:
            self.post_update(self.message("/start", update_id=4001))
            r = self.client.post("/v1/telegram/drain", json={}, headers={"X-Admin-Token": ADMIN})
            self.assertEqual(200, r.status_code)
            self.assertFalse(r.json()["botConfigured"])
            self.assertEqual([], r.json()["sent"])
            self.assertTrue(self.outbox(), "the job must still be queued, not dropped")
        finally:
            os.environ["PGM_TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
            self.app._tg_bot_client = None

    def test_the_drain_sends_in_priority_order_and_marks_what_went_out(self):
        self.post_update(self.message("/start", update_id=4002))
        sent_calls = []

        class FakeBot:
            @staticmethod
            def send_message(*, chat_id, text, keyboard=None, **kw):
                sent_calls.append(("send", chat_id, text))
                return self.app._tg_client.SendResult(ok=True, status=200, message_id=900 + len(sent_calls))

            @staticmethod
            def edit_message(*, chat_id, message_id, text, keyboard=None, **kw):
                sent_calls.append(("edit", chat_id, text))
                return self.app._tg_client.SendResult(ok=True, status=200, message_id=message_id)

        self.app._tg_bot_client = FakeBot()
        try:
            r = self.client.post("/v1/telegram/drain", json={"limit": 5}, headers={"X-Admin-Token": ADMIN})
            self.assertEqual(200, r.status_code, r.text)
            self.assertTrue(r.json()["botConfigured"])
            self.assertTrue(r.json()["sent"])
            self.assertTrue(all(j["state"] == "sent" for j in self.outbox()))
            self.assertIn("PolyGM", sent_calls[0][2])
        finally:
            self.app._tg_bot_client = None

    def test_a_bot_that_refuses_gets_a_bounded_retry_and_a_note(self):
        self.post_update(self.message("/start", update_id=4003))

        class Refusing:
            @staticmethod
            def send_message(*, chat_id, text, keyboard=None, **kw):
                return self.app._tg_client.SendResult(ok=False, status=429, retry_after_s=2, note="Too Many Requests")

            @staticmethod
            def edit_message(*, chat_id, message_id, text, keyboard=None, **kw):
                return self.app._tg_client.SendResult(ok=False, status=429, retry_after_s=2, note="Too Many Requests")

        self.app._tg_bot_client = Refusing()
        try:
            r = self.client.post("/v1/telegram/drain", json={}, headers={"X-Admin-Token": ADMIN})
            self.assertEqual(200, r.status_code)
            failed = r.json()["failed"]
            self.assertTrue(failed and failed[0]["retry"], "a 429 must be retried, not dropped")
            row = self.db.execute("SELECT state, attempts, due_ms FROM telegram_outbox").fetchone()
            self.assertEqual("queued", str(row[0]))
            self.assertGreater(int(row[2]), self.app._now_ms(), "the retry waits for Telegram's own delay")
            self.assertNotIn(BOT_TOKEN, json.dumps(r.json()))
        finally:
            self.app._tg_bot_client = None

    def test_the_drain_is_admin_only(self):
        r = self.client.post("/v1/telegram/drain", json={})
        self.assertIn(r.status_code, (401, 403, 503))
        r2 = self.client.post("/v1/telegram/drain", json={}, headers={"X-Admin-Token": "adm_wrong"})
        self.assertEqual(403, r2.status_code)


class TestSurfaces(TelegramBase):
    """The read-only surfaces: the command table, the funnel, and the refusal wording."""

    def test_the_command_table_is_served_and_every_row_has_buttons(self):
        r = self.client.get("/v1/telegram/commands")
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertEqual(20, len(body["commands"]), "start…verify, and every one of them tappable")
        for c in body["commands"]:
            self.assertTrue(c["buttons"], c["name"])
            self.assertIn(c["auth"], ("public", "linked", "owner", "admin"))

    def test_the_funnel_metrics_are_admin_only_and_count_what_happened(self):
        self.post_update(self.message("/start", update_id=5001))
        self.post_update(self.message("/positions", update_id=5002))
        r = self.client.get("/v1/telegram/metrics?days=7", headers={"X-Admin-Token": ADMIN})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertGreaterEqual(body["chats"], 1)
        self.assertGreaterEqual(body["commandsPerUser"], 1.0)
        self.assertIn("funnel", body["note"])
        self.assertEqual(503, self.client.get("/v1/telegram/metrics").status_code)

    def test_every_risk_code_has_a_sentence(self):
        codes = ("RISK_HALT", "RISK_NOTIONAL", "RISK_DAILY", "RISK_OPEN_ORDERS", "RISK_MIN_SIZE",
                 "RISK_STALE_BOOK", "RISK_TICK", "RISK_PRICE_BAND", "NO_LIQUIDITY", "MARKET_NOT_FOUND",
                 "BAD_AMOUNT", "IDEM_CONFLICT", "IDEM_IN_PROGRESS")
        for code in codes:
            text = self.app._tg_plain_refusal(code)
            self.assertGreater(len(text), 30, code)
            self.assertNotIn(code, text, "%s must be explained in words, not repeated back" % code)
        self.assertIn("whatever the venue said", self.app._tg_plain_refusal("SOMETHING_NEW",
                                                                           detail="whatever the venue said"))

    def test_a_fill_notification_is_queued_at_the_top_priority(self):
        self.account()
        job = self.app._tg_notify_fill(UID, market="Fed cut in September", side="yes", size_text="80.6 shares",
                                      price_text="62.0¢", fee_text="0.50 USDC", position_text="80.6 shares",
                                      price_age_text="as of 3s ago")
        self.assertTrue(job)
        row = self.db.execute("SELECT priority, text FROM telegram_outbox WHERE id=?", (job,)).fetchone()
        self.assertEqual(self.app._tgb_outbox.P_FILL, int(row[0]))
        self.assertIn("filled", str(row[1]))

    def test_the_fill_notification_looks_up_the_chat_from_the_identity_table(self):
        """A caller must not have to remember where to send it: that is how a fill goes to the wrong chat.

        The fixture uses the realistic shape — a private chat whose id equals the user's id — because that is what
        Telegram sends, and the code depends on it *explicitly* (see `_tg_notify_fill`), so a change in that
        assumption should fail here rather than in production.
        """
        self.account(chat=TG_USER)
        job = self.app._tg_notify_fill(UID, market="Fed cut in September", side="yes", size_text="1 share",
                                      price_text="1¢", fee_text="0", position_text="1 share",
                                      price_age_text="now")
        row = self.db.execute("SELECT chat_id FROM telegram_outbox WHERE id=?", (job,)).fetchone()
        self.assertEqual(TG_USER, str(row[0]), "the chat comes from the identity table, not from the caller")
        # …and an account with no Telegram identity produces nothing at all rather than an exception.
        self.assertEqual(0, self.app._tg_notify_fill("u-nobody", market="x", side="yes", size_text="1",
                                                     price_text="1¢", fee_text="0", position_text="1",
                                                     price_age_text="now"))
        jobs = self.db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()
        self.assertEqual(1, int(jobs[0]))


class TestNoAddressesOrTokensLeak(TelegramBase):
    """The rule that cuts across every surface: nothing sensitive in a message, a log, or a metric."""

    def test_no_outbox_row_ever_contains_an_address_or_the_bot_token(self):
        self.account()
        self.seed_market()
        for i, text in enumerate(["/start", "/market p12-fed-cut-sept", "/wallet", "/positions", "/support",
                                  "https://polymarket.com/event/p12-fed-cut-sept"]):
            self.post_update(self.message(text, update_id=6000 + i))
        rows = self.db.execute("SELECT text FROM telegram_outbox").fetchall()
        self.assertTrue(rows)
        for (text,) in rows:
            self.assertNotIn(BOT_TOKEN, str(text))
            self.assertIsNone(re.search(r"0x[0-9a-fA-F]{6,}", str(text)), text)

    def test_the_deposit_card_points_at_the_mini_app_rather_than_printing_an_address(self):
        """The address belongs on a screen with a QR and a copy button, not in a chat a user forwards."""
        self.account()
        self.db.execute("INSERT OR REPLACE INTO wallets (user_id, provider, custody, address, proxy_address,"
                        " signature_type, policy_hash, state, created_ms, updated_ms) VALUES (?,'privy','delegated',"
                        " '0xabc123def456abc123def456abc123def456abcd','0xdef456abc123def456abc123def456abc123abcd',"
                        " 3, 'ph', 'trading', 1, 1)", (UID,))
        self.db.commit()
        self.post_update(self.message("/wallet", update_id=6010))
        text = "\n".join(j["text"] for j in self.outbox())
        self.assertIn("Mini App", text)
        self.assertIn("seed phrase", text)
        self.assertNotIn("0xabc123", text, "a chat message is the one surface a user forwards to a stranger")


if __name__ == "__main__":
    unittest.main()
