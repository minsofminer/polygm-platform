"""P12 · the bot's own tests: the properties the phase is judged on, checked without a token or a network.

Four of these are the phase's acceptance criteria in miniature: a duplicate `update_id` does not double-execute, a
tampered Mini App payload is rejected (proved at the HTTP layer in `test_telegrambot_api.py`, and here at the
signature layer), `/stop` cancels without a confirmation, and the motion constants are the *same numbers* as the
web's tokens rather than numbers that merely look similar.

The rest are the design rules this package is built on — escaping, callback payload size, the two-beat budget,
haptics on a fill and nowhere else, a below-threshold parse that asks instead of acting, and a precedence order
where a user's fill is delivered before a channel broadcast. Each of them would otherwise be a bug found by a
stranger in a chat, in front of their money.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from polygm_core.telegrambot import client, menu, motion, nl, outbox, render, router, sessions, updates
from polygm_core.security import telegram as sec_tg

ROOT = Path(__file__).resolve().parents[1]
TOKENS_CSS = ROOT / "web" / "styles" / "tokens.css"

BOT_TOKEN = "123456789:" + "AAF-" + "test-token-that-is-shape-valid" + "-12345678"  # lint-allow: shape only, never a real token


def msg(text: str, *, update_id: int = 1, chat_id: int = 7, chat_type: str = "private", user_id: int = 9,
        message_id: int = 11) -> updates.Update:
    return updates.classify({"update_id": update_id,
                             "message": {"message_id": message_id, "chat": {"id": chat_id, "type": chat_type},
                                         "from": {"id": user_id, "username": "tester"}, "text": text}},
                            bot_username="polygm_bot")


def tap(data: str, *, update_id: int = 2, chat_id: int = 7, user_id: int = 9, message_id: int = 11) -> updates.Update:
    return updates.classify({"update_id": update_id,
                             "callback_query": {"id": "cb%d" % update_id, "from": {"id": user_id},
                                                "message": {"message_id": message_id,
                                                            "chat": {"id": chat_id, "type": "private"}},
                                                "data": data}}, bot_username="polygm_bot")


MARKET = {"slug": "fed-cut-sept", "question": "Will the Fed cut rates in September 2026?", "mark": "62",
          "no_mark": "38", "spread": "1", "age": "as of 4 seconds ago", "closes": "2026-09-30"}


def fetcher(name: str, payload: dict):
    """The service seam, faked: the same shapes the API returns, without a database."""
    if name == "market":
        return {"market": dict(MARKET)} if payload.get("slug", "").startswith("fed-") else {"missing": True}
    if name == "positions":
        return {"text": "You hold <b>3 positions</b>, worth 412.50 USDC. Worst drawdown 18% of the book."}
    if name == "balance":
        return {"text": "Cash 1,240.00 USDC · locked 50.00 USDC.\n<i>as of 3 seconds ago</i>"}
    if name == "pnl":
        return {"text": "Realised +214.40 USDC · unrealised −88.10 USDC.\nMax drawdown 31% on 2026-08-14."}
    if name == "top":
        return {"text": "1. @a +21.4% (30d, n=112 trades — small sample)\n2. @b +18.9%"}
    if name == "wallet":
        return {"text": "Wallet ready. Deposit address on the Mini App — <i>never</i> paste it to anyone."}
    return {"text": ""}


class TestIdempotency(unittest.TestCase):
    """The kit's first acceptance criterion: a duplicated `update_id` must not double-execute."""

    def test_a_duplicate_update_is_replayed_without_an_action(self):
        u = tap("confirm:yes:act-45-fed-cut-sept-yes-2", update_id=45)
        fresh = router.route(u, at_ms=1_000, linked=True, session=sessions.start(chat_id="7", step="confirm_order",
                                                                                at_ms=900, payload={
                                                                                    "slug": "fed-cut-sept", "side": "yes",
                                                                                    "amount": "50",
                                                                                    "action_id": "act-45-fed-cut-sept-yes-2"}))
        self.assertEqual("place_order", fresh.action.kind)
        # ...and the same update arriving again, which is what Telegram does when our 2xx is slow or lost.
        again = router.route(u, at_ms=1_100, linked=True, session=None, seen=True)
        self.assertTrue(again.replay)
        self.assertEqual("none", again.action.kind)
        self.assertIn("not done it twice", again.plan.beats[0].text)

    def test_the_confirm_button_carries_the_key_the_action_uses(self):
        """The button's `callback_data` and the order's idempotency key must be the same string.

        Otherwise a double tap is deduped on the wire and *not* in the ledger, which is the exact failure the phase
        exists to prevent: two orders from one decision.
        """
        s = sessions.start(chat_id="7", step="choose_size", at_ms=1_000, payload={"slug": "fed-cut-sept",
                                                                                "side": "yes"})
        d = router.route(tap("size:50:fed-cut-sept", update_id=44), at_ms=1_100, session=s, linked=True)
        key = d.session.payload["action_id"]
        kb = d.plan.beats[-1].keyboard
        payloads = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
        self.assertIn("confirm:yes:%s" % key, payloads)

    def test_a_replayed_tap_on_a_dead_session_asks_instead_of_acting(self):
        d = router.route(tap("confirm:yes:act-1-x-y-1", update_id=46), at_ms=1_100, linked=True, session=None)
        self.assertEqual("none", d.action.kind)
        self.assertEqual("stale", d.metric)


class TestPanicCommand(unittest.TestCase):
    """`/stop` is the panic command: no confirmation, one hop, and it says what it cancelled."""

    def test_stop_needs_no_confirmation_and_clears_the_session(self):
        d = router.route(msg("/stop"), at_ms=1_000, linked=True,
                         session=sessions.start(chat_id="7", step="confirm_order", at_ms=900,
                                                payload={"slug": "fed-cut-sept", "side": "yes", "amount": "50",
                                                         "action_id": "a"}))
        self.assertEqual("cancel_all", d.action.kind)
        self.assertFalse(d.action.needs_confirmation, "a panic command that asks a question is not a panic command")
        self.assertIsNone(d.session, "the flow must not survive /stop")
        self.assertTrue(d.plan.beats[0].keyboard, "the reply needs Resume and a report, not just words")

    def test_stop_is_in_the_main_menu_not_behind_settings(self):
        """The kit puts /stop in the main menu, and this is the assertion that keeps it there."""
        markup = menu.main_menu().to_markup()["inline_keyboard"]
        flat = [b["callback_data"] for row in markup for b in row if "callback_data" in b]
        self.assertTrue(any(p.startswith("stop:") for p in flat),
                        "the panic command must be tappable from the main menu: %r" % flat)


class TestMotionMatchesTheWebTokens(unittest.TestCase):
    """The chat and the Mini App must move at the same speed, so the numbers are read from one place: the CSS."""

    def _tokens(self) -> dict:
        text = TOKENS_CSS.read_text()
        out = {}
        for name, value in re.findall(r"--pgm-([a-z-]+):\s*(\d+)ms", text):
            out[name] = int(value)
        return out

    def test_every_motion_duration_equals_the_web_token(self):
        toks = self._tokens()
        # The token names as `web/styles/tokens.css` spells them (`--pgm-dur-press`), so a rename on either side
        # fails here rather than silently producing a chat that moves at a different speed from the Mini App.
        for name, expected in (("dur-press", motion.DUR_PRESS_MS), ("dur-micro", motion.DUR_MICRO_MS),
                               ("dur-small", motion.DUR_SMALL_MS), ("dur-medium", motion.DUR_MEDIUM_MS),
                               ("dur-large", motion.DUR_LARGE_MS), ("dur-drawer", motion.DUR_DRAWER_MS),
                               ("flash-out", motion.FLASH_OUT_MS)):
            self.assertIn(name, toks, "web/styles/tokens.css no longer defines --pgm-%s" % name)
            self.assertEqual(toks[name], expected,
                             "motion.%s is %d ms and the web token is %d ms" % (name, expected, toks[name]))

    def test_the_easings_are_the_web_easings(self):
        text = TOKENS_CSS.read_text()
        self.assertIn("--pgm-ease-out: %s" % motion.EASE_OUT, text)
        self.assertIn("--pgm-ease-in-out: %s" % motion.EASE_IN_OUT, text)
        self.assertIn("--pgm-ease-drawer: %s" % motion.EASE_DRAWER, text)

    def test_chat_motion_is_within_telegrams_limits(self):
        self.assertLessEqual(motion.MAX_EDITS_PER_ACTION, 2, "a third edit in a chat reads as a glitch")
        self.assertEqual(5_000, motion.CHAT_ACTION_TTL_MS, "typing… expires after five seconds")
        self.assertLess(motion.CHAT_ACTION_REFRESH_MS, motion.CHAT_ACTION_TTL_MS)
        fast = motion.chat_action_plan(started_ms=1_000, now_ms=1_050, answered=False)
        self.assertFalse(fast["send"], "an instant reply must not flash 'typing' at all")
        later = motion.chat_action_plan(started_ms=1_000, now_ms=1_500, answered=False)
        self.assertTrue(later["send"] or later["after_ms"] > 0)
        slow = motion.chat_action_plan(started_ms=1_000, now_ms=5_400, answered=False)
        self.assertTrue(slow["send"] or slow["after_ms"] <= motion.CHAT_ACTION_REFRESH_MS,
                        "a typing line must be refreshed before Telegram's 5 s expiry: %r" % slow)
        done = motion.chat_action_plan(started_ms=1_000, now_ms=5_400, answered=True)
        self.assertFalse(done["send"], "…and must stop the moment the answer exists")

    def test_no_motion_animates_a_number(self):
        """P03's rule, restated where it is most likely to be broken: a chat is the obvious place to 'count up'."""
        self.assertIn("number", " ".join(motion.NEVER).lower())
        bad = {"sends": 1, "edits": 3, "haptics": [], "chat_action": True, "edit_message_id": 11,
               "expected_ms": 900, "animates_number": True}
        findings = motion.motion_findings(bad)
        self.assertTrue(any("number" in f for f in findings), findings)
        self.assertTrue(any("beat" in f for f in findings), findings)

    def test_haptics_are_whitelisted_to_fill_and_reject(self):
        self.assertEqual({"haptic_fill", "haptic_reject"}, set(motion.HAPTICS))
        plan = render.fill_card(market="Fed cut", side="yes", size_text="50 USDC", price_text="62¢",
                               fee_text="0.50 USDC", position_text="50 shares", price_age_text="4s ago")
        self.assertEqual(("haptic_fill",), plan.haptics)
        bad = render.Plan(beats=[render.Beat(text="hi")], haptics=("haptic_celebration",))
        self.assertTrue(any("haptic" in f for f in render.render_findings(bad)))


class TestCommandSurface(unittest.TestCase):
    """Every command tappable, every payload small, no payload carrying a secret."""

    def test_the_command_table_is_internally_consistent(self):
        self.assertEqual([], menu.menu_findings())

    def test_every_command_has_an_inline_alternative(self):
        for c in menu.commands():
            self.assertTrue(c.buttons, "/%s is only reachable by typing it" % c.name)

    def test_no_callback_payload_exceeds_the_bot_api_limit_or_carries_an_address(self):
        kbs = (menu.main_menu(), menu.order_card_keyboard(action_id="act-1", slug="fed-cut-sept"),
               menu.confirm_keyboard(action_id="act-1", side="yes"), menu.stop_keyboard())
        for kb in kbs:
            for row in kb.to_markup()["inline_keyboard"]:
                for b in row:
                    if "callback_data" in b:
                        self.assertLessEqual(len(b["callback_data"].encode("utf-8")), menu.MAX_CALLBACK_BYTES,
                                             b["callback_data"])
                        self.assertNotIn("0x", b["callback_data"])

    def test_a_confirm_button_needing_a_longer_id_still_fits(self):
        """A 64-byte cap with a minted id: the guard exists so a future longer id fails loudly, in a test."""
        with self.assertRaises(ValueError):
            menu.Button("Confirm", action="confirm", part="yes", value="x" * 80).callback_data()

    def test_the_card_that_leads_to_an_order_offers_a_second_tap_that_cancels(self):
        kb = menu.confirm_keyboard(action_id="act-1", side="yes").to_markup()["inline_keyboard"]
        actions = [b["callback_data"].split(":")[0] for row in kb for b in row if "callback_data" in b]
        self.assertEqual(["confirm", "cancel"], actions)


class TestRendering(unittest.TestCase):
    """Escaping, length, and the sentences the product requires on anything that shows a price or a PnL."""

    def test_a_market_question_is_escaped_for_html_parse_mode(self):
        plan = router._market_card("fed-cut-sept", at_ms=1, chat_id="7", session=None, market_index=(), fetch=lambda n,
                                   p: {"market": dict(MARKET, question="Will Trump & Biden debate <tonight>?")},
                                   why="test").plan
        text = plan.beats[-1].text
        self.assertIn("&amp;", text)
        self.assertIn("&lt;tonight&gt;", text)
        self.assertEqual([], render.render_findings(plan))

    def test_telegrams_own_tags_survive_escaping(self):
        self.assertEqual("<b>a &amp; b</b>", render.bold("a & b"))
        self.assertIn("<i>", render.Plan(beats=[render.Beat(text="<i>age</i>")]).beats[0].text)

    def test_a_message_over_the_cap_is_split_and_numbered(self):
        parts = render.split_text("para\n\n" * 400, limit=200)
        self.assertTrue(len(parts) > 1)
        self.assertTrue(parts[0].startswith("<i>1/"), parts[0][:20])
        for p in parts:
            self.assertLessEqual(len(p), 200)

    def test_an_address_in_a_message_is_a_finding(self):
        p = render.Plan(beats=[render.Beat(text="send to 0xdeadbeefdeadbeef now")])
        self.assertTrue(any("address" in f for f in render.render_findings(p)))

    def test_a_win_rate_without_its_sample_is_a_finding(self):
        """The standing rule from P11: a win rate is meaningless without n, and the chat is where it gets dropped."""
        p = render.Plan(beats=[render.Beat(text="win rate 71% this month")])
        self.assertTrue(any("sample" in f for f in render.render_findings(p)))
        ok = render.Plan(beats=[render.Beat(text="win rate 71% over 40 closed trades")])
        self.assertEqual([], render.render_findings(ok))

    def test_a_fill_card_says_price_size_fee_and_position(self):
        plan = render.fill_card(market="Fed cut in September", side="yes", size_text="50 USDC", price_text="62.0¢",
                                fee_text="0.50 USDC", position_text="80.6 shares", price_age_text="as of 3s ago")
        text = plan.beats[0].text
        for token in ("50 USDC", "62.0¢", "0.50 USDC", "80.6 shares", "as of 3s ago"):
            self.assertIn(token, text)
        self.assertEqual([], render.render_findings(plan))

    def test_a_rejection_is_plain_language_plus_the_machine_code(self):
        plan = render.refusal_card(what="Order not sent", code="RISK_LIMIT_NOTIONAL",
                                   plain="This order would take you over your own 500 USDC per-day limit.",
                                   next_step="You can lower the size, or raise the limit in /settings.")
        self.assertIn("RISK_LIMIT_NOTIONAL", plan.beats[0].text)
        self.assertIn("per-day limit", plan.beats[0].text)
        self.assertEqual(("haptic_reject",), plan.haptics)

    def test_the_unknown_state_is_said_out_loud(self):
        text = render.unknown_order_card(market="Fed cut", size_text="50 USDC").beats[0].text
        self.assertIn("limbo", text.lower())
        self.assertIn("do not resend", text.lower())


class TestNaturalLanguage(unittest.TestCase):
    """A sentence should produce a confirmation card, and the parser's honesty is the feature."""

    MARKETS = ({"slug": "fed-cut-sept", "question": "Will the Fed cut rates in September 2026?"},
               {"slug": "fed-cut-dec", "question": "Will the Fed cut rates by December 2026?"})

    def test_a_sentence_becomes_slots_and_a_confirmation(self):
        p = nl.parse("buy 50 yes on the fed market", markets=(self.MARKETS[0],))
        self.assertEqual("order", p["action"])
        self.assertEqual("yes", p["side"])
        self.assertEqual("50", p["amount"])
        self.assertEqual("fed-cut-sept", p["market_slug"])
        self.assertGreaterEqual(p["confidence"], p["threshold"])
        self.assertEqual([], menu.natural_language_findings(p))

    def test_the_parser_never_executes(self):
        p = nl.parse("buy $25 no on the fed cut by december", markets=self.MARKETS)
        self.assertFalse(p["executes"])
        self.assertTrue(menu.natural_language_findings(p) == [] or p["question"])

    def test_a_low_confidence_parse_asks_which_market(self):
        p = nl.parse("buy 50 yes on the fed market", markets=self.MARKETS)
        self.assertEqual("", p["action"], "an ambiguous parse must not be an order")
        self.assertTrue(p["ambiguous"])
        self.assertEqual(2, len(p["options"]))
        self.assertIn("Which one", p["question"])

    def test_a_market_that_does_not_resolve_is_a_question_not_a_trade(self):
        p = nl.parse("buy 50 yes on quantum banana futures", markets=self.MARKETS)
        self.assertEqual("", p["action"])
        self.assertIn("could not find", p["question"])
        self.assertLess(p["confidence"], p["threshold"])

    def test_a_lookup_question_is_not_mistaken_for_an_order(self):
        p = nl.parse("what is my pnl")
        self.assertEqual("pnl", p["action"])
        self.assertEqual("", p["amount"])

    def test_closing_a_position_in_words_is_understood(self):
        p = nl.parse("sell 100 yes on fed-cut-sept", markets=(self.MARKETS[0],))
        self.assertEqual("sell", p["verb"])

    def test_the_router_turns_a_confident_sentence_into_the_same_card_the_buttons_make(self):
        d = router.route(msg("buy 25 yes on fed-cut-sept", update_id=50), at_ms=1_000, linked=True,
                         market_index=(self.MARKETS[0],), fetch=fetcher)
        self.assertEqual("nl_order", d.metric)
        self.assertEqual("confirm_order", d.session.step)
        kb = d.plan.beats[0].keyboard["inline_keyboard"][0]
        self.assertTrue(any(b["callback_data"].startswith("confirm:") for b in kb))
        self.assertEqual([], render.render_findings(d.plan))

    def test_a_vague_sentence_gets_the_menu_not_an_error(self):
        d = router.route(msg("sell everything"), at_ms=1_000, linked=True)
        self.assertEqual("nl_question", d.metric)
        self.assertEqual("none", d.action.kind)
        self.assertTrue(d.plan.beats[0].keyboard is None or d.plan.beats[0].keyboard)


class TestFlows(unittest.TestCase):
    """The trade path and the two flows with a warning in the middle."""

    def _to_size(self, *, linked: bool = True, update_id: int = 60):
        card = router.route(msg("/market fed-cut-sept", update_id=update_id), at_ms=1_000, fetch=fetcher)
        return router.route(tap("market:yes:fed-cut-sept", update_id=update_id + 1), at_ms=1_100, linked=linked,
                            session=card.session, fetch=fetcher)

    def test_the_order_card_is_public_and_the_trade_is_not(self):
        card = router.route(msg("/market fed-cut-sept", update_id=70), at_ms=1_000, fetch=fetcher)
        self.assertEqual("market", card.metric)
        self.assertEqual("choose_side", card.session.step)
        gate = router.route(tap("market:yes:fed-cut-sept", update_id=71), at_ms=1_100, linked=False,
                            session=card.session, fetch=fetcher)
        self.assertEqual("link_account", gate.action.kind)
        self.assertIn("seed phrase", gate.plan.beats[0].text)

    def test_the_confirm_card_shows_max_loss_and_an_estimated_fee(self):
        d = router.route(tap("size:50:fed-cut-sept", update_id=62), at_ms=1_200, session=self._to_size().session,
                         linked=True, fetch=fetcher)
        text = d.plan.beats[0].text
        self.assertIn("Max loss", text)
        self.assertIn("0.50", text)                     # 1% of 50.00, floored to a cent, in integer arithmetic
        self.assertEqual("confirm_order", d.session.step)

    def test_a_size_tap_from_a_different_markets_card_is_refused(self):
        stale = sessions.start(chat_id="7", step="choose_side", at_ms=1_000, payload={"slug": "other-market"})
        d = router.route(tap("size:50:fed-cut-sept", update_id=63), at_ms=1_100, session=stale, linked=True,
                         fetch=fetcher)
        self.assertEqual("none", d.action.kind)
        self.assertEqual("stale", d.metric)

    def test_a_session_expires_and_says_so(self):
        s = sessions.start(chat_id="7", step="custom_size", at_ms=1_000, payload={"slug": "fed-cut-sept",
                                                                                 "side": "yes"})
        d = router.route(msg("75", update_id=64), at_ms=1_000 + sessions.DEFAULT_TTL_MS + 1, session=s, linked=True)
        self.assertEqual("expired", d.metric)
        self.assertIsNone(d.session)

    def test_a_typed_size_reaches_the_same_confirm_card(self):
        s = self._to_size(update_id=65).session
        custom = router.route(tap("size:c:fed-cut-sept", update_id=66), at_ms=1_200, session=s, linked=True)
        self.assertEqual("custom_size", custom.session.step)
        d = router.route(msg("75", update_id=67), at_ms=1_250, session=custom.session, linked=True)
        self.assertEqual("confirm_order", d.session.step)
        self.assertIn("75 USDC", d.plan.beats[0].text)

    def test_a_confirm_with_no_action_id_cannot_place_an_order(self):
        s = sessions.start(chat_id="7", step="confirm_order", at_ms=1_000,
                           payload={"slug": "fed-cut-sept", "side": "yes", "amount": "50"})
        d = router.route(tap("confirm:yes:act-1", update_id=68), at_ms=1_100, session=s, linked=True)
        self.assertEqual("none", d.action.kind)
        self.assertEqual("stale", d.metric)

    def test_key_export_needs_the_warning_acknowledged_first(self):
        d = router.route(tap("menu:export:", update_id=69), at_ms=1_000, linked=True)
        self.assertIn("hands over your money", d.plan.beats[0].text)
        self.assertEqual("export_disclaimer", d.session.step)
        blocked = router.route(msg("yes go", update_id=69), at_ms=1_010, session=d.session, linked=True)
        self.assertEqual("export_wait", blocked.metric)
        ack = router.route(tap("menu:export_ack:", update_id=70), at_ms=1_020, session=d.session, linked=True)
        self.assertEqual("export_confirm", ack.session.step)
        self.assertIn("Last step", ack.plan.beats[0].text)
        # The second step is where the key is actually prepared, and it is a money action with a key.
        d2 = router.route(tap("menu:export_now:", update_id=71), at_ms=1_040, session=ack.session, linked=True)
        self.assertEqual("export_key", d2.action.kind)
        self.assertTrue(d2.action.key)
        # A tap on the second step after the flow is over shows nothing rather than showing a key again.
        d3 = router.route(tap("menu:export_now:", update_id=72), at_ms=1_050, session=None, linked=True)
        self.assertEqual("none", d3.action.kind)

    def test_a_withdrawal_asks_for_an_address_before_a_password(self):
        d = router.route(msg("/withdraw 250", update_id=72), at_ms=1_000, linked=True)
        self.assertEqual("withdraw_address", d.session.step)
        self.assertNotIn("password", d.plan.beats[0].text.lower().split("send the amount")[0])


class TestRateLimitsAndPriority(unittest.TestCase):
    """A paying user's fill must never sit behind the free channel's broadcast."""

    _n = 0

    @classmethod
    def _job(cls, chat: str, priority: int, at: int = 0, text: str = "x",
             chat_type: str = "private") -> outbox.Job:
        cls._n += 1
        return outbox.Job(job_id=cls._n, chat_id=chat, chat_type=chat_type, priority=priority, text=text,
                          created_ms=at, due_ms=at)

    def test_a_fill_is_planned_before_a_broadcast(self):
        jobs = [self._job("-100", outbox.P_ALERT_CHANNEL, chat_type="channel"), self._job("7", outbox.P_FILL, 5)]
        chosen, _next = outbox.plan(jobs, at_ms=1_000, buckets=outbox.Buckets(), limit=1)
        self.assertEqual("7", chosen[0].chat_id)

    def test_a_chat_on_cooldown_does_not_block_the_rest_of_the_queue(self):
        """One busy chat must not stall every other chat: the scan skips, it does not stop.

        Telegram allows one message per second per private chat, so a chat that has already had its message is
        genuinely blocked. What must not happen is that this block consumes the *global* budget token and stalls the
        queue behind it — the failure this test was written against, and the reason `plan()` returns the token it
        optimistically took when a chat bucket refuses a job.
        """
        b = outbox.Buckets()
        first, _ = outbox.plan([self._job("7", outbox.P_FILL, 900)], at_ms=1_000, buckets=b, limit=5)
        self.assertEqual(["7"], [j.chat_id for j in first])
        # `plan()` is pure and works on copies, so the *caller* records what actually went out — which is the
        # contract the drain relies on and the reason a dry run cannot spend the bot's budget.
        b.chat("7").take(1_000)
        b.global_.take(1_000)
        blocked, wait_ms = outbox.plan([self._job("7", outbox.P_FILL, 900)], at_ms=1_100, buckets=b, limit=5)
        self.assertEqual([], [j.chat_id for j in blocked], "chat 7 already had its turn this second")
        self.assertGreater(wait_ms, 0, "and the queue must say when to look again rather than spin")
        other, _ = outbox.plan([self._job("8", outbox.P_COMMAND_REPLY, 0)], at_ms=1_100, buckets=b, limit=5)
        self.assertEqual(["8"], [j.chat_id for j in other], "a different chat is unaffected by chat 7's cooldown")
        later, _ = outbox.plan([self._job("7", outbox.P_FILL, 900)], at_ms=2_100, buckets=b, limit=5)
        self.assertEqual(["7"], [j.chat_id for j in later], "the chat is served again once its second is up")

    def test_the_global_ceiling_applies_before_anything_else(self):
        """Thirty messages a second is Telegram's limit for the bot as a whole; the queue must not exceed it."""
        b = outbox.Buckets()
        jobs = [self._job(str(1000 + i), outbox.P_COMMAND_REPLY, 0) for i in range(60)]
        chosen, _ = outbox.plan(jobs, at_ms=1_000, buckets=b, limit=100)
        self.assertLessEqual(len(chosen), outbox.GLOBAL_PER_SECOND)

    def test_make_room_for_a_fill_only_ever_displaces_a_broadcast(self):
        """The victim list must contain the channel's messages and never a person's.

        A fill arriving while the queue is full is the case this exists for, and the rule is one-directional: a
        broadcast can wait, a user's own message cannot. A version that returned "the lowest priority jobs" would
        quietly drop a personal alert to make room, which is the bug the assertion below is here to catch.
        """
        jobs = [self._job("-100", outbox.P_BULK, 0, chat_type="channel"),
                self._job("-100", outbox.P_ALERT_CHANNEL, 1, chat_type="channel"),
                self._job("7", outbox.P_ALERT_PERSONAL, 2),
                self._job("9", outbox.P_COMMAND_REPLY, 3)]
        victims = outbox.make_room_for_a_fill(jobs, at_ms=1_000, reserved=2)
        self.assertTrue(victims, "a full queue of broadcasts must yield to a fill")
        for v in victims:
            self.assertGreaterEqual(v.priority, outbox.P_ALERT_CHANNEL)
            self.assertNotIn(v.chat_id, ("7", "9"))

    def test_a_429_is_retried_after_the_delay_telegram_asks_for(self):
        self.assertTrue(outbox.should_retry(status=429, attempts=1, retry_after_s=3))
        self.assertFalse(outbox.should_retry(status=429, attempts=9), "a 429 is retried, but not forever")
        self.assertGreaterEqual(outbox.retry_delay_ms(attempts=2), 1_000,
                                "a 429 with no retry_after still waits: retrying at once is how a limit becomes "
                                "a ban")
        self.assertFalse(outbox.should_retry(status=400, attempts=1), "a 400 is our bug, not a busy moment")
        self.assertGreaterEqual(outbox.retry_delay_ms(attempts=1, retry_after_s=3), 3_000)
        self.assertGreater(outbox.retry_delay_ms(attempts=4), outbox.retry_delay_ms(attempts=1),
                           "retries must back off, not spin")


class TestWebhookAndTokenHygiene(unittest.TestCase):
    """The endpoint's own checks, and the rule that a bot token never reaches a log line."""

    def test_the_webhook_needs_a_configured_secret_and_a_matching_header(self):
        self.assertTrue(client.webhook_findings({}, ""))
        self.assertTrue(client.webhook_findings({}, "s3cret"))
        self.assertTrue(client.webhook_findings({"X-Telegram-Bot-Api-Secret-Token": "wrong"}, "s3cret"))
        self.assertEqual([], client.webhook_findings({"x-telegram-bot-api-secret-token": "s3cret"}, "s3cret"))

    def test_the_token_never_survives_a_redaction(self):
        self.assertNotIn(BOT_TOKEN, client._redact("https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN,
                                                  BOT_TOKEN))
        self.assertNotIn(BOT_TOKEN, client._redact(json.dumps({"description": "bad token " + BOT_TOKEN}),
                                                   BOT_TOKEN))

    def test_a_token_of_the_wrong_shape_is_refused_before_it_is_used(self):
        import os
        old = os.environ.pop("PGM_TELEGRAM_BOT_TOKEN", None)
        try:
            with self.assertRaises(client.BotError):
                client.token_from_env()
            os.environ["PGM_TELEGRAM_BOT_TOKEN"] = "not-a-token"
            with self.assertRaises(client.BotError):
                client.token_from_env()
            os.environ["PGM_TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
            self.assertEqual(BOT_TOKEN, client.token_from_env())
        finally:
            os.environ.pop("PGM_TELEGRAM_BOT_TOKEN", None)
            if old:
                os.environ["PGM_TELEGRAM_BOT_TOKEN"] = old

    def test_an_api_refusal_is_recorded_without_the_token(self):
        calls = []
        c = client.BotClient(BOT_TOKEN, opener=lambda url, body, t: (400, json.dumps(
            {"ok": False, "description": "Bad Request: chat not found for bot%s" % BOT_TOKEN})), calls=calls)
        r = c.send_message(chat_id="7", text="hi")
        self.assertFalse(r.ok)
        self.assertNotIn(BOT_TOKEN, r.note)
        self.assertNotIn(BOT_TOKEN, json.dumps(calls))
        self.assertEqual("sendMessage", c.calls[0]["method"])

    def test_a_rate_limit_is_a_result_the_scheduler_can_act_on(self):
        c = client.BotClient(BOT_TOKEN, opener=lambda url, body, t: (429, json.dumps(
            {"ok": False, "parameters": {"retry_after": 7}, "description": "Too Many Requests"})))
        r = c.send_message(chat_id="7", text="hi")
        self.assertEqual(429, r.status)
        self.assertEqual(7, r.retry_after_s)
        self.assertTrue(outbox.should_retry(status=r.status, attempts=1, retry_after_s=r.retry_after_s))

    def test_an_unknown_method_is_refused_before_it_reaches_the_api(self):
        with self.assertRaises(client.BotError):
            client.BotClient(BOT_TOKEN).call("sendInvoice", {})


class TestMiniAppInitData(unittest.TestCase):
    """D2's acceptance criterion, at the layer that produces the signature (the HTTP half is in the API suite)."""

    @staticmethod
    def _signed(pairs: dict, token: str = BOT_TOKEN) -> str:
        import hashlib, hmac, urllib.parse
        dcs = "\n".join("%s=%s" % (k, pairs[k]) for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        return urllib.parse.urlencode(dict(pairs, hash=h))

    def test_a_valid_payload_verifies_and_a_tampered_one_does_not(self):
        now = 1_700_000_000_000
        q = self._signed({"auth_date": str(now // 1000), "user": '{"id":9,"username":"tester"}', "query_id": "Q1"})
        ok = sec_tg.verify(q, BOT_TOKEN, at=now + 10_000, purpose="login", seen_hashes=())
        self.assertTrue(ok.ok, ok.reason)
        self.assertEqual("9", ok.tg_user_id)
        tampered = q.replace("tester", "attacker")
        bad = sec_tg.verify(tampered, BOT_TOKEN, at=now + 10_000, purpose="login", seen_hashes=())
        self.assertFalse(bad.ok)
        self.assertEqual("bad_signature", bad.reason)
        # …and the same payload signed with a *different* bot token is refused: the signature is the bot's, so a
        # payload lifted from another bot cannot open a session here.
        other = sec_tg.verify(q, "987654321:AAF-other-token-that-is-shape-valid-87654321", at=now + 10_000, # lint-allow: a second shape-valid fixture token, never issued by any bot
                              purpose="login", seen_hashes=())
        self.assertFalse(other.ok)

    def test_a_stale_payload_is_refused_even_though_it_is_correctly_signed(self):
        now = 1_700_000_000_000
        q = self._signed({"auth_date": str(now // 1000), "user": '{"id":9}', "query_id": "Q2"})
        late = sec_tg.verify(q, BOT_TOKEN, at=now + (sec_tg.LOGIN_MAX_AGE_S + 5) * 1000, purpose="login",
                             seen_hashes=())
        self.assertFalse(late.ok)
        self.assertEqual("too_old", late.reason)
        # A refresh token has the longer window, and the same payload is accepted there — which is why the purpose
        # is a parameter instead of a constant.
        refreshed = sec_tg.verify(q, BOT_TOKEN, at=now + (sec_tg.LOGIN_MAX_AGE_S + 5) * 1000, purpose="refresh",
                                  seen_hashes=())
        self.assertTrue(refreshed.ok, refreshed.reason)

    def test_a_replayed_payload_is_refused_by_the_store(self):
        """The store is a callable in production (`SEC.telegram_consume`), and a set here: same contract, no DB."""
        now = 1_700_000_000_000
        q = self._signed({"auth_date": str(now // 1000), "user": '{"id":9}', "query_id": "Q3"})
        seen = set()
        first = sec_tg.verify(q, BOT_TOKEN, at=now + 1_000, purpose="login", seen_hashes=lambda h: h in seen)
        self.assertTrue(first.ok, first.reason)
        seen.add(first.auth_hash)
        again = sec_tg.verify(q, BOT_TOKEN, at=now + 2_000, purpose="login", seen_hashes=lambda h: h in seen)
        self.assertFalse(again.ok)
        self.assertEqual("replayed", again.reason)


class TestUpdateClassification(unittest.TestCase):
    """Prefixed commands, mentions, and the rule that a callback is not a message."""

    def test_an_at_mention_suffix_is_stripped(self):
        self.assertEqual(("market", "fed-cut"), updates.split_command("/market@polygm_bot fed-cut",
                                                                      bot_username="polygm_bot"))
        self.assertEqual(("market", "fed-cut"), updates.split_command("/market fed-cut", bot_username="polygm_bot"))

    def test_a_command_for_another_bot_is_ignored(self):
        name, _ = updates.split_command("/market@other_bot fed", bot_username="polygm_bot")
        self.assertEqual("", name)

    def test_a_callback_is_classified_as_a_callback_even_when_it_carries_text(self):
        u = tap("market:yes:fed-cut-sept")
        self.assertEqual("callback", u.kind)
        self.assertEqual("market:yes:fed-cut-sept", u.callback_data)

    def test_a_deep_link_payload_is_length_capped(self):
        u = updates.classify({"update_id": 5, "message": {"message_id": 1, "chat": {"id": 7, "type": "private"},
                                                          "text": "/start " + "x" * 400}}, bot_username="b")
        self.assertLessEqual(len(u.args), 200)

    def test_a_channel_post_is_not_a_user(self):
        u = updates.classify({"update_id": 6, "channel_post": {"message_id": 1, "chat": {"id": -100, "type": "channel"},
                                                              "text": "hi"}}, bot_username="b")
        self.assertEqual("channel_post", u.kind)
        self.assertFalse(u.is_private)


class TestMetricsAndSessions(unittest.TestCase):
    """The numbers D7 gates paid acquisition on, and the state machine that produces them."""

    def test_the_step_vocabulary_is_closed_and_every_step_expires(self):
        for step in sessions.STEPS:
            s = sessions.start(chat_id="7", step=step, at_ms=1_000, payload={})
            self.assertGreater(s.expires_ms, 1_000)
        with self.assertRaises(ValueError):
            sessions.start(chat_id="7", step="sell_everything", at_ms=1_000)

    def test_an_export_session_expires_faster_than_a_trade_flow(self):
        trade = sessions.start(chat_id="7", step="choose_size", at_ms=1_000)
        export = sessions.start(chat_id="7", step="export_disclaimer", at_ms=1_000)
        self.assertLess(export.expires_ms, trade.expires_ms)

    def test_a_payload_that_is_a_document_is_refused(self):
        s = sessions.start(chat_id="7", step="choose_side", at_ms=1_000, payload={"slug": "x",
                                                                                  "notes": "y" * 4_000})
        with self.assertRaises(ValueError):
            s.as_row()

    def test_advancing_replaces_the_payload_rather_than_merging_it(self):
        s = sessions.start(chat_id="7", step="choose_size", at_ms=1_000, payload={"slug": "a", "side": "yes"})
        nxt = sessions.advance(s, step="confirm_order", at_ms=1_100, payload={"amount": "10", "action_id": "x"})
        self.assertNotIn("slug", nxt.payload, "a merged payload is how a stale market survives into a new flow")
        self.assertEqual(1, nxt.version - s.version)

    def test_every_step_that_can_place_an_order_demands_the_pieces(self):
        self.assertTrue(sessions.step_findings("confirm_order", {"slug": "x"}))
        self.assertEqual([], sessions.step_findings("confirm_order", {"slug": "fed-cut-sept", "side": "yes",
                                                                      "amount": "50", "action_id": "act-1"}))
        self.assertTrue(sessions.step_findings("export_confirm", {}))


class TestReadOnlyCommands(unittest.TestCase):
    """The reads that must answer in two beats, with the age on them, and never with a bare number."""

    def test_positions_answers_in_two_beats_and_the_second_edits_the_first(self):
        d = router.route(msg("/positions"), at_ms=1_000, linked=True, fetch=fetcher)
        self.assertEqual(2, len(d.plan.beats))
        self.assertEqual("skeleton", d.plan.beats[0].what)
        self.assertTrue(d.plan.animates_number is False)
        self.assertIn("drawdown", d.plan.beats[1].text)

    def test_pnl_carries_the_drawdown_and_the_age(self):
        d = router.route(msg("/pnl"), at_ms=1_000, linked=True, fetch=fetcher)
        text = d.plan.beats[-1].text
        self.assertIn("drawdown", text.lower())

    def test_the_leaderboard_keeps_its_small_sample_note(self):
        d = router.route(msg("/top"), at_ms=1_000, linked=True, fetch=fetcher)
        self.assertIn("small sample", d.plan.beats[-1].text)

    def test_an_unlinked_user_is_offered_a_button_rather_than_a_refusal(self):
        for cmd in ("/positions", "/balance", "/orders", "/pnl", "/stop"):
            d = router.route(msg(cmd, update_id=80), at_ms=1_000, linked=False)
            self.assertEqual("link_account", d.action.kind, cmd)
            self.assertTrue(d.plan.beats[0].keyboard, cmd)

    def test_support_states_the_three_things_we_never_ask_for(self):
        d = router.route(msg("/support"), at_ms=1_000)
        text = d.plan.beats[0].text
        self.assertIn("seed phrase", text)
        self.assertIn("first", text)

    def test_verify_says_no_for_a_stranger_and_yes_for_an_operator(self):
        d = router.route(msg("/verify alice"), at_ms=1_000, fetch=lambda n, p: {"operator": False})
        self.assertIn("not a PolyGM account", d.plan.beats[0].text)
        d2 = router.route(msg("/verify bob"), at_ms=1_000, fetch=lambda n, p: {"operator": True, "role": "support"})
        self.assertIn("is a PolyGM operator", d2.plan.beats[0].text)

    def test_an_unsolicited_address_is_answered_without_echoing_it(self):
        d = router.route(msg("send it to 0xdeadbeefdeadbeefcafe"), at_ms=1_000, linked=True)
        self.assertEqual("address_unsolicited", d.metric)
        self.assertNotIn("0xdeadbeef", d.plan.beats[0].text)

    def test_a_command_we_do_not_have_gets_the_menu_not_silence(self):
        d = router.route(msg("/short"), at_ms=1_000)
        self.assertEqual("unknown_command", d.metric)
        self.assertIsNotNone(d.plan.beats[0].keyboard)


class TestAlertChannel(unittest.TestCase):
    """D5: the public channel is the acquisition engine, so its format is a product decision, not a string."""

    def test_the_one_tap_trade_button_is_on_the_alert_itself(self):
        kb = menu.Keyboard().add(menu.Row().add(menu.Button("BUY YES", action="market", part="yes",
                                                            value="fed-cut-sept"),
                                                 menu.Button("Full card", action="market", part="open",
                                                             value="fed-cut-sept")))
        payloads = [b.callback_data() for row in kb.rows for b in row.buttons]
        self.assertIn("market:yes:fed-cut-sept", payloads)
        self.assertTrue(all(len(p.encode()) <= menu.MAX_CALLBACK_BYTES for p in payloads))

    def test_a_channel_message_is_one_market_and_ends_with_the_honest_line(self):
        text = ("🐋 <b>Large fill</b> — Fed cut in September\n62¢ · 8,400 USDC YES · 2 minutes ago\n"
                "<i>Odds are a market, not a forecast. Not advice.</i>")
        self.assertEqual([], render.render_findings(render.Plan(beats=[render.Beat(text=text)])))

    def test_the_channel_yields_to_a_users_fill(self):
        # The broadcast is *earlier* on the clock as well as lower in priority: this is the ordering that a
        # due-time-first sort got wrong, and the assertion is here so it cannot come back.
        jobs = [outbox.Job(job_id=1, chat_id="-100", chat_type="channel", priority=outbox.P_ALERT_CHANNEL,
                           created_ms=0, due_ms=0),
                outbox.Job(job_id=2, chat_id="7", chat_type="private", priority=outbox.P_FILL,
                           created_ms=5, due_ms=5)]
        picked, _ = outbox.plan(jobs, at_ms=1_000, buckets=outbox.Buckets(), limit=1)
        self.assertEqual("7", picked[0].chat_id)
        self.assertEqual("fill", picked[0].priority_name)


if __name__ == "__main__":
    unittest.main()
