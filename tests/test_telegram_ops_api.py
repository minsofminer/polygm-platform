"""P12 · D5/D8 over HTTP: the broadcast, the kill switch, the ops surface, and the Mini App's order route.

Why these four together, and why a test file of their own:

  * **The broadcast's candidate query.** The first version of `_tg_broadcast_candidates` named columns `tape_trades`
    does not have (`ts_ms`, `size_micro`, `notional_micro`, `outcome`) and filled the quality gate's inputs with
    literals — `liquidity_micro: 0`, `age_ms: 3_600_000`, `audience: 1`, `resolution_trusted: True`. Nothing caught
    it: `channel.py` and `ops.py` were unit-tested against their own inputs, and the route was not called at all.
    These tests call the route, so the SQL has to parse and run, and they assert that what the gate receives comes
    from the tables — a market with no resolution source must be *refused*, not waved through by a constant.
  * **The kill switch is a table, not a variable.** Its state is the newest row of an append-only table, because the
    question after an incident is not "is it off now" but "who turned it off, when, and why".
  * **The Mini App's order route** is the webview's half of the same order path the bot uses, so it is tested through
    a real minted session: 401 without one, 422 for a malformed amount, 404 for a market we do not have, 409 for a
    reused key — and an accepted order that lands as exactly one intent, priced by the server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unittest
import urllib.parse
from pathlib import Path

from conftest import import_app, refresh_flags  # noqa: F401

ADMIN = "adm_" + "t" * 44
BOT_TOKEN = "123456789:AAF-test-token-that-is-shape-valid-12345678"
SECRET = "whsec_" + "s" * 40
CHAT = "4343"
TG_USER = "90910"
UID = "u-tg-ops"
CHANNEL = "-1001234567890"


def sign_init_data(pairs: dict, token: str = BOT_TOKEN) -> str:
    dcs = "\n".join("%s=%s" % (k, pairs[k]) for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urllib.parse.urlencode(dict(pairs, hash=hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()))


class OpsBase(unittest.TestCase):
    app_name = "api-telegram-ops"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        os.environ["PGM_ADMIN_TOKEN"] = ADMIN
        os.environ["PGM_TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
        os.environ["PGM_TELEGRAM_WEBHOOK_SECRET"] = SECRET
        os.environ["PGM_TELEGRAM_BOT_USERNAME"] = "polygm_bot"
        os.environ["PGM_TELEGRAM_CHANNEL"] = CHANNEL
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)
        import time
        cls.now_s = int(time.time())

    @classmethod
    def tearDownClass(cls):
        for key in ("PGM_TELEGRAM_BOT_TOKEN", "PGM_TELEGRAM_WEBHOOK_SECRET", "PGM_TELEGRAM_BOT_USERNAME",
                    "PGM_TELEGRAM_CHANNEL"):
            os.environ.pop(key, None)

    def setUp(self):
        refresh_flags(self.app)
        os.environ["PGM_ADMIN_TOKEN"] = ADMIN
        os.environ["PGM_TELEGRAM_CHANNEL"] = CHANNEL
        self.db = self.app._db
        # Only this test's rows: the bot's tables are shared by the file, and a broadcast's cadence history is
        # exactly the kind of state a later test would otherwise inherit and be confused by.
        # Only the tables a product is allowed to prune. `telegram_broadcasts` (the cadence history) and
        # `telegram_kill_state` (the audit trail) are append-only — their triggers refuse a DELETE, which is the
        # design — so tests that need a clean history get one from a fresh database (`app_name` per class) instead of
        # from a delete the database would not permit in production either.
        for table in ("telegram_outbox", "telegram_commands", "telegram_sessions", "telegram_updates"):
            self.db.execute("DELETE FROM %s" % table)
        # `telegram_kill_state` is append-only — the trigger refuses the DELETE, which is the product working — so a
        # case starts from a known state by *appending* a disengaged row rather than erasing history. The audit trail
        # is the point of that table, and a test that wipes it would be testing a switch that cannot be audited.
        self.db.execute("INSERT INTO telegram_kill_state (engaged, scope, reason, changed_by, at_ms)"
                        " VALUES (0, 'all', 'test setup: known state before the case', 'test', ?)",
                        (self.app._now_ms(),))
        self.kill_rows_before = int(self.db.execute("SELECT COUNT(*) FROM telegram_kill_state").fetchone()[0])
        self.db.commit()
        self.db.execute("DELETE FROM tape_fills WHERE condition_id LIKE '0xcond-p12-%'")
        self.db.execute("DELETE FROM market_stats WHERE condition_id LIKE '0xcond-p12-%'")
        self.db.execute("DELETE FROM market_meta WHERE market_id LIKE 'm-p12-%'")
        self.db.execute("DELETE FROM order_intents WHERE user_id=?", (UID,))
        self.db.execute("DELETE FROM user_identities WHERE user_id=?", (UID,))
        self.db.commit()
        self.account()

    # ------------------------------------------------------------------ fixtures
    def account(self) -> str:
        self.db.execute("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?, 'free')", (UID, 1))
        self.db.execute("INSERT OR REPLACE INTO user_identities (kind, value, user_id, state, claimed_ms,"
                        " verified_ms, proof_kind, revoked_ms) VALUES ('telegram',?,?,'verified',?,?,'fixture',"
                        " NULL)", (TG_USER, UID, 1, 1))
        self.db.commit()
        return UID

    _mints = 0

    def bearer(self) -> str:
        # A fresh `query_id` per mint, from a counter rather than `id(self)`: the payload's replay store is doing its
        # job, and a helper that mints the same signed payload twice is refused by it (409 `TELEGRAM_REPLAY`), which
        # looks like a bug in the route under test.
        type(self)._mints += 1
        init = sign_init_data({"auth_date": str(self.now_s), "query_id": "Qops%d" % self._mints,
                               "user": '{"id":%s,"username":"tester"}' % TG_USER})
        r = self.client.post("/v1/telegram/session", json={"initData": init})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["linked"], r.text)
        return r.json()["accessToken"]

    def auth(self, token: str | None = None) -> dict:
        return {"Authorization": "Bearer %s" % (token or self.bearer())}

    def seed_market(self, slug="p12-fed-cut-sept", *, liquidity_micro=50_000_000_000, resolution_source="https://x.test/s",
                    first_seen_ms=None, category="Politics"):
        """A market that *could* be broadcast, with every gate input set explicitly so a test can move one of them."""
        mid = "m-%s" % slug
        cond = "0xcond-%s" % slug
        at = self.app._now_ms()
        first = at - 48 * 3_600_000 if first_seen_ms is None else first_seen_ms
        self.db.execute("INSERT OR REPLACE INTO markets (id, condition_id, slug, question, minimum_tick_size,"
                        " end_ts, accepting_orders, first_seen_ms, updated_ms, seconds_delay, minimum_order_size,"
                        " fee_type, enable_order_book, neg_risk, outcomes_json)"
                        " VALUES (?,?,?,?,?,?,1,?,?,0,'1','',1,0,'[]')",
                        (mid, cond, slug, "Will the Fed cut rates in September 2026?", 0.01,
                         at + 30 * 86_400_000, first, at))
        self.db.execute("INSERT OR REPLACE INTO tokens (token_id, market_id, outcome, outcome_index)"
                        " VALUES (?,?, 'Yes', 0)", ("tok-%s-yes" % slug, mid))
        self.db.execute("INSERT OR REPLACE INTO tokens (token_id, market_id, outcome, outcome_index)"
                        " VALUES (?,?, 'No', 1)", ("tok-%s-no" % slug, mid))
        self.db.execute("INSERT OR REPLACE INTO book_levels (market_id, side, price_micro, size_shares_micro,"
                        " level_count, updated_ms) VALUES (?,?,?,?,1,?)", (mid, "ask", 620_000, 900_000_000, at))
        self.db.execute("INSERT OR REPLACE INTO market_meta (market_id, category, resolution_source,"
                        " resolution_criteria, updated_ms) VALUES (?,?,?,?,?)",
                        (mid, category, resolution_source, "Per the published CPI print.", at))
        self.db.execute("INSERT OR REPLACE INTO market_stats (condition_id, volume_24h_micro, liquidity_micro,"
                        " fill_count, last_fill_ms, last_book_change_ms, resolved_seen_ms, meta_json, updated_ms)"
                        " VALUES (?,?,?,1,?,?,0,'{}',?)", (cond, 5_000_000_000, liquidity_micro, at, at, at))
        self.db.commit()
        return mid, cond, "tok-%s-yes" % slug

    def seed_fill(self, cond: str, *, notional_micro=250_000_000_000, shares_micro=403_225_806_451, into_ms=None):
        # Deliberately enormous: the candidate list is the top five fills by notional, and the shared fixture database
        # already holds whale fills from other phases. A test that sorts below them is a test about the fixture."""
        at = self.app._now_ms() if into_ms is None else into_ms
        self.db.execute("INSERT INTO tape_fills (dedupe_key, condition_id, token_id, outcome, outcome_index, wallet,"
                        " side, price_micro, size_micro, usd_notional_micro, ts_ms, ingest_ms, source, fee_rate_bps)"
                        " VALUES (?,?,?,?,0,'0xwhale', 'BUY', 620000, ?, ?, ?, ?, 'rest', 0)",
                        ("dk-%s-%d" % (cond, at), cond, "tok-%s-yes" % cond.replace("0xcond-", ""), "Yes",
                         shares_micro, notional_micro, at, at))
        self.db.commit()

    def audience(self, n: int = 12):
        """`n` chats that have talked to the bot, which is what the reachability count reads."""
        for i in range(n):
            self.db.execute("INSERT INTO telegram_commands (at_ms, chat_id, chat_type, user_id, command, action,"
                            " ok, dur_ms, update_id) VALUES (?,?, 'private', '', '/market', '', 1, 5, ?)",
                            (self.app._now_ms(), "900%d" % i, 500_000 + i))
        self.db.commit()

    def broadcast(self, **body):
        return self.client.post("/v1/telegram/broadcast", json=body, headers={"X-Admin-Token": ADMIN})

    @staticmethod
    def mine(body: dict, slug: str = "p12-fed-cut-sept") -> list:
        """This test's own verdicts. The database is shared across the suite and other phases seeded their own
        markets and whale fills, so an assertion about "the candidate" has to name which market it means."""
        return [v for v in body["verdicts"] if v["slug"] == slug]

    def kill(self, engaged: bool, reason: str, scope: str = "all"):
        return self.client.post("/v1/telegram/kill", json={"engaged": engaged, "reason": reason, "scope": scope},
                                headers={"X-Admin-Token": ADMIN})


class TestBroadcastRehearsal(OpsBase):
    """The megaphone's dry run: the SQL runs, and every gate input comes from a table rather than a constant.

    Its own database (`app_name`), because a real send in the same class would leave a broadcast row behind and the
    cadence cap — six minutes between any two channel posts — would then hold these candidates and make a *correct*
    pipeline look broken. Separate databases, separate questions.
    """
    app_name = "api-tg-ops-rehearsal"

    def test_the_candidate_query_runs_and_the_default_is_a_dry_run(self):
        # The regression: this call used to raise `no such column: f.ts_ms` — the route was never exercised, so a
        # broken SQL string sat behind a green unit-test suite. The assertion is deliberately blunt: it runs.
        _, cond, _ = self.seed_market()
        self.seed_fill(cond)
        self.audience()
        r = self.broadcast()
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["dryRun"])
        self.assertEqual(0, int(self.db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0]),
                         "a dry run must not queue anything")
        mine = self.mine(body)
        self.assertEqual(1, len(mine), body)
        item = [i for i in body["preview"]["items"] if i["slug"] == "p12-fed-cut-sept"][0]
        self.assertIn("Large fill", item["text"])
        self.assertTrue(item["has_deep_link"],
                        "every channel post carries the Mini App link, or the acquisition loop has no next step")
        # The rehearsal answers the whole question: would it go out, and if not, why not.
        self.assertTrue(mine[0]["wouldSend"], mine)
        self.assertEqual(1, body["wouldSend"], body)

    def test_the_gate_reads_the_tables_not_a_constant(self):
        """Move one input at a time and watch the verdict change, which is the only way to know it is an input."""
        _, cond, _ = self.seed_market(liquidity_micro=0)
        self.seed_fill(cond)
        self.audience()
        held = self.mine(self.broadcast().json())[0]
        self.assertEqual("hold", held["verdict"], held)
        self.assertIn("liquidity", " ".join(held["reasons"]).lower(), held)
        self.assertFalse(held["wouldSend"])

        self.db.execute("UPDATE market_stats SET liquidity_micro=? WHERE condition_id=?", (50_000_000_000, cond))
        self.db.commit()
        self.assertTrue(self.mine(self.broadcast().json())[0]["wouldSend"], "a funded market is no longer held")

    def test_a_market_with_no_resolution_source_is_refused_not_delayed(self):
        # `resolution_trusted` is a *refusal* in P07's gate, because no amount of waiting fixes it — and the old
        # candidate dict hard-coded it to True, so this market would have been broadcast.
        _, cond, _ = self.seed_market(resolution_source="")
        self.seed_fill(cond)
        self.audience()
        v = self.mine(self.broadcast().json())[0]
        self.assertEqual("refused", v["verdict"], v)
        self.assertIn("resolution", " ".join(v["reasons"]).lower(), v)
        self.assertGreater(v["recheckMs"], -1)

    def test_a_market_younger_than_the_floor_is_held_with_a_recheck_time(self):
        _, cond, _ = self.seed_market(first_seen_ms=self.app._now_ms() - 60_000)
        self.seed_fill(cond)
        self.audience()
        v = self.mine(self.broadcast().json())[0]
        self.assertEqual("hold", v["verdict"], v)
        self.assertGreater(v["recheckMs"], 0, "a hold has to say when to look again, or it is a drop")

    def test_with_no_reachable_audience_the_megaphone_refuses_to_point_at_anyone(self):
        # The honest first-release state: no channel members we can count, nobody has talked to the bot. `no_audience`
        # is a refusal in P07's gate and it is the correct one — better than posting to an unknown N.
        _, cond, _ = self.seed_market()
        self.seed_fill(cond)
        body = self.broadcast().json()
        v = self.mine(body)[0]
        self.assertEqual("refused", v["verdict"], v)
        self.assertIn("no_audience", v["reasons"])
        self.assertEqual(0, body["reach"]["audience"])
        # The count's provenance travels with the count: an operator reading `audience: 12` has to know it is a floor
        # from our own tables and not the channel's member count, which we cannot see from a webhook.
        self.assertIn("telegram_commands", body["reach"]["source"])
        self.assertIn("uncounted", body["reach"]["source"])

class TestBroadcastSend(OpsBase):
    """A real send: one job, one recorded broadcast, the cadence cap, and the kill switch's own scope.

    Its own database (`app_name`), because the append-only cadence history is exactly what these three tests
    reason about: sharing a database with the rehearsal class would make each one depend on whether a previous
    test had spoken, and "the test that runs second fails" is a fixture bug wearing a product bug's clothes.
    """
    app_name = "api-tg-ops-send"

    def test_a_real_send_queues_one_job_records_the_broadcast_and_rechecks_the_gate(self):
        mid, cond, _ = self.seed_market()
        self.seed_fill(cond)
        self.audience()
        r = self.broadcast(dryRun=False)
        self.assertEqual(200, r.status_code, r.text)
        self.assertIn("p12-fed-cut-sept", r.json()["sent"], r.text)
        jobs = self.db.execute("SELECT chat_id, priority, state FROM telegram_outbox").fetchall()
        self.assertEqual(1, len(jobs))
        self.assertEqual(CHANNEL, str(jobs[0][0]), "the channel post goes to the configured channel")
        gate = self.db.execute("SELECT verdict FROM broadcast_gates WHERE market_id=? ORDER BY id DESC LIMIT 1",
                               (mid,)).fetchone()
        self.assertEqual("broadcast", str(gate[0]), "every verdict is recorded, including the allowed ones")
        row = self.db.execute("SELECT kind, market_id FROM telegram_broadcasts WHERE market_id=?", (mid,)).fetchall()
        self.assertEqual(1, len(row))
        self.assertEqual("large_fill", str(row[0][0]))

    def test_b_second_send_inside_the_six_minute_gap_is_skipped_by_cadence(self):
        # Cadence is the product rule that a channel which speaks constantly is a channel nobody reads, and the gap
        # lives in the append-only broadcast log rather than in this process's memory.
        mid, cond, _ = self.seed_market()
        self.seed_fill(cond)
        self.audience()
        # Two attempts back to back, and the second one must be held by the floor. The first attempt is not asserted
        # to *succeed*: this class shares one database with the test above it, and a test whose meaning depends on
        # what ran before it is a test that will one day fail because somebody added a case in the middle.
        self.broadcast(dryRun=False)
        again = self.broadcast(dryRun=False).json()
        self.assertNotIn("p12-fed-cut-sept", again["sent"], again)
        # The cadence refusal's own words: "the last broadcast was 0s ago; the floor is 360s". Asserting on a word the
        # code does not use is how a test passes for the wrong reason — or, as here, fails for one.
        self.assertTrue(any("floor" in str(s.get("why", "")) for s in again["skipped"]), again)

    def test_c_the_kill_switch_stops_the_megaphone_but_not_a_personal_alert(self):
        mid, cond, _ = self.seed_market()
        self.seed_fill(cond)
        self.audience()
        self.kill(True, "impersonation wave in progress", scope="channel")
        r = self.broadcast(dryRun=False)
        # 503 with the registered `RISK_HALT` code and `retryable: true`: a halt is a *pause* with a documented
        # reason, and reusing P06's code means a client's existing mapping of halts already covers the megaphone.
        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual("RISK_HALT", r.json()["error"]["code"])
        self.assertEqual(0, int(self.db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0]))
        # An engaged channel scope is not an engaged product: a personal fill notification still goes out, which is
        # the entire reason the scopes exist rather than one boolean.
        plan = self.app._tgb_channel.personal_copy(
            {"kind": "large_fill", "slug": "p12-fed-cut-sept", "question": "Will the Fed cut?",
             "side": "yes", "size_text": "500 shares", "price_text": "62.0¢", "amount_text": "310 USDC"},
            bot_username="polygm_bot", price_text="62.0¢", age_text="just now", why="your order filled")
        self.app._tg_enqueue(chat_id=CHAT, chat_type="private", plan=plan,
                             priority=self.app._tgb_outbox.P_ALERT_PERSONAL)
        self.db.commit()
        class FakeBot:
            @staticmethod
            def send_message(*, chat_id, text, keyboard=None, **kw):
                return self.app._tg_client.SendResult(ok=True, status=200, message_id=901)

            @staticmethod
            def edit_message(*, chat_id, message_id, text, keyboard=None, **kw):
                return self.app._tg_client.SendResult(ok=True, status=200, message_id=message_id)

        self.app._tg_bot_client = FakeBot()
        try:
            drain = self.client.post("/v1/telegram/drain", json={}, headers={"X-Admin-Token": ADMIN}).json()
        finally:
            self.app._tg_bot_client = None
        # `sent` is the list of job ids the drain actually pushed, so a personal alert arriving during a channel-scope
        # halt is visible as a non-empty list.
        self.assertGreaterEqual(len(drain["sent"]), 1, drain)


class TestKillSwitch(OpsBase):
    """D8's switch: a table, a reason, a scope, and an audit trail."""
    app_name = "api-tg-ops-kill"

    def test_a_reason_is_required_and_it_has_to_be_a_sentence(self):
        self.assertEqual(422, self.kill(True, "oops").status_code)
        self.assertEqual(200, self.kill(True, "support impersonation on Telegram").status_code)

    def test_the_state_is_the_newest_row_and_the_history_stays(self):
        self.kill(True, "channel ban risk from a spam wave", scope="channel")
        self.kill(False, "telegram support confirmed the reports were closed", scope="channel")
        r = self.client.get("/v1/telegram/ops", headers={"X-Admin-Token": ADMIN})
        self.assertEqual(200, r.status_code, r.text)
        state = r.json()["kill"]
        self.assertFalse(state["engaged"], r.text)
        self.assertEqual("channel", state["scope"])
        # A delta, not a total: the table is append-only across the whole suite (each case appends its known-state
        # row), and that is the product working. The same lesson P10 recorded for `auth_events`.
        rows = self.db.execute("SELECT COUNT(*) FROM telegram_kill_state").fetchone()
        self.assertEqual(self.kill_rows_before + 2, int(rows[0]),
                         "the audit trail is the point: an off switch with no history is a rumour")

    def test_ops_reports_the_operational_questions_a_human_actually_asks(self):
        body = self.client.get("/v1/telegram/ops", headers={"X-Admin-Token": ADMIN}).json()
        for key in ("queueDepth", "stuckClaims", "botConfigured", "username", "recovery", "findings"):
            self.assertIn(key, body, body)
        self.assertTrue(body["botConfigured"])
        self.assertEqual("@polygm_bot", body["username"]["bot"])
        self.assertEqual(("trade", "markets", "wallet"), tuple(body["username"]["mini_app_short_names"]))

    def test_ops_is_admin_only(self):
        # No header at all is 503 `SIGNER_UNAVAILABLE` and a wrong header is 403 `ADMIN_REQUIRED` — the distinction
        # P06 chose so that "this pod has no admin token" never reads as "you are not an admin".
        self.assertEqual(503, self.client.get("/v1/telegram/ops").status_code)
        self.assertEqual(403, self.client.get("/v1/telegram/ops", headers={"X-Admin-Token": "wrong"}).status_code)


class TestRefusalVocabulary(OpsBase):
    """Every code the product can show a user must be a code the API can actually emit.

    This is the test the `MARKET_NOT_FOUND` bug needed and did not have: the card's mapping returned a name that is
    not in `CODES`, so the Mini App's route turned a perfectly ordinary "no such market" into a 500, and the chat's
    plain-language table had a row that no code path could ever reach. Both halves now answer to the registered
    vocabulary, and this asserts it rather than trusting it.
    """
    app_name = "api-tg-ops-vocab"

    def test_the_cards_unknown_market_uses_the_registered_code(self):
        res = self.app._tg_order_from_card("u-nobody", {"slug": "does-not-exist", "side": "yes", "amount": "50"},
                                           "tma-vocab-0001")
        self.assertFalse(res["ok"])
        self.assertIn(res["code"], self.app.CODES, res)

    def test_every_plain_language_sentence_is_about_a_code_that_exists(self):
        """Walk the table itself rather than a list of names written here — a list would go stale exactly as the table
        did. `RISK_HALT` is spot-checked for wording so a table of empty strings cannot pass."""
        self.assertIn("paused", self.app._tg_plain_refusal("RISK_HALT").lower())
        import inspect
        src = inspect.getsource(self.app._tg_plain_refusal)
        keys = re.findall(r'^\s{8}"([A-Z_]{4,})":', src, re.M)
        self.assertGreater(len(keys), 15, "the table shrank — every sentence removed is a support ticket created")
        for code in keys:
            self.assertIn(code, self.app.CODES, "%s is in no vocabulary the API can emit" % code)

    def test_the_chat_and_the_webview_speak_the_same_vocabulary(self):
        """The Python table and the TypeScript twin, compared by key. The P12 gate checks the same thing; this
        catches it at the unit level, where the fix is a one-line diff."""
        import inspect
        py = inspect.getsource(self.app._tg_plain_refusal)
        keys = set(re.findall(r'^\s{8}"([A-Z_]{4,})":', py, re.M))
        ts = (Path(__file__).resolve().parents[1] / "web" / "src" / "tma" / "trade.ts").read_text()
        block = ts[ts.index("export function plainRefusal"):]
        ts_keys = set(re.findall(r'^\s{4}([A-Z_]{4,}):', block, re.M))
        self.assertEqual(keys, ts_keys,
                         "the chat and the Mini App must not offer different explanations for the same refusal")


class TestMiniAppOrder(OpsBase):
    """The webview's half of the order path: session-authed, server-priced, one intent per key."""
    app_name = "api-tg-ops-order"

    def post(self, body, *, key="tma-abcdef0123456789", token=None):
        headers = {"Idempotency-Key": key}
        if token is not False:
            headers.update(self.auth(token if isinstance(token, str) else None))
        return self.client.post("/v1/telegram/order", json=body, headers=headers)

    def test_without_a_session_nothing_can_be_placed(self):
        self.seed_market()
        r = self.post({"slug": "p12-fed-cut-sept", "side": "yes", "amountUsdc": "50"}, token=False)
        self.assertEqual(401, r.status_code, r.text)

    def test_without_an_idempotency_key_it_refuses_rather_than_guessing(self):
        # 400, not 422: `IDEM_KEY_REQUIRED` is its own registered code and 400 is the status it carries everywhere
        # else in this API. The distinction is deliberate — a missing header is a request to fix, a malformed body is
        # a payload to fix — and it is P04's vocabulary rather than this route's opinion.
        self.seed_market()
        r = self.client.post("/v1/telegram/order", json={"slug": "p12-fed-cut-sept", "side": "yes",
                                                         "amountUsdc": "50"}, headers=self.auth())
        self.assertEqual(400, r.status_code, r.text)
        self.assertEqual("IDEM_KEY_REQUIRED", r.json()["error"]["code"])

    def test_an_accepted_order_is_one_intent_priced_by_the_server(self):
        self.seed_market()
        r = self.post({"slug": "p12-fed-cut-sept", "side": "yes", "amountUsdc": "50"})
        self.assertEqual(202, r.status_code, r.text)
        body = r.json()
        # 50 USDC at the seeded 0.62 ask, floored like every other money path in this product.
        self.assertEqual("80645161", body["sharesMicro"], body)
        self.assertEqual("620000", body["priceMicro"], body)
        n = self.db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=?", (UID,)).fetchone()
        self.assertEqual(1, int(n[0]))

    def test_a_malformed_amount_is_a_validation_failure_not_a_gate_refusal(self):
        self.seed_market()
        r = self.post({"slug": "p12-fed-cut-sept", "side": "yes", "amountUsdc": "0"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("BAD_AMOUNT", r.json()["error"]["code"])

    def test_a_market_we_do_not_have_is_a_404(self):
        r = self.post({"slug": "p12-not-a-market", "side": "yes", "amountUsdc": "50"})
        self.assertEqual(404, r.status_code, r.text)
        self.assertEqual("NOT_FOUND", r.json()["error"]["code"])

    def test_reusing_the_key_for_a_different_order_is_a_conflict(self):
        self.seed_market()
        first = self.post({"slug": "p12-fed-cut-sept", "side": "yes", "amountUsdc": "50"}, key="tma-key-reuse-1")
        self.assertEqual(202, first.status_code, first.text)
        second = self.post({"slug": "p12-fed-cut-sept", "side": "yes", "amountUsdc": "75"}, key="tma-key-reuse-1")
        self.assertIn(second.status_code, (403, 409), second.text)
        n = self.db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=?", (UID,)).fetchone()
        self.assertEqual(1, int(n[0]), "a reused key must not place a second order")


if __name__ == "__main__":
    unittest.main()
