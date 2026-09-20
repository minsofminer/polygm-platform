"""P12 · D5/D8 — the channel's rules and the operator's switch, tested where they are decided.

The channel is the one surface where a mistake is *public*: a broadcast reaches thousands of phones and cannot be
edited out of them. So the tests here are about restraint rather than feature coverage — that a spike against a
zero hour is not news, that a market younger than half an hour waits, that the channel goes quiet when it has spoken
recently, that a resolution alert without a source is refused, and that a message which promises an outcome cannot
leave. D8's half is the switch: it must have a reason, must not be confused with the trading kill switch, and must
hold messages rather than lose them.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from polygm_core.telegrambot import channel, ops, outbox, render

BOT = "polygm_bot"

FILL = {"kind": "large_fill", "slug": "fed-cut-sept", "market_id": "0xcond", "question": "Will the Fed cut rates "
        "in September 2026?", "category": "economics", "notional_micro": 9_000_000_000, "size_text": "14,516 shares",
        "side": "yes"}
SPIKE = {"kind": "volume_spike", "slug": "btc-150k", "question": "Will Bitcoin close above $150k in 2026?",
         "category": "crypto", "hour_micro": 3_000_000_000, "prior_hour_micro": 1_000_000_000}
NEW = {"kind": "new_market", "slug": "senate-2026", "question": "Which party takes the Senate in 2026?",
       "category": "politics", "liquidity_micro": 40_000_000_000}
RES = {"kind": "resolution_soon", "slug": "cpi-sep", "question": "Will CPI come in under 3.0% for September?",
       "category": "economics", "ends_ms": 2_000_000, "resolution_source": "BLS release",
       "price_text": "71.5¢"}


class TestWhatIsWorthBroadcasting(unittest.TestCase):
    """`qualifies()` decides what a stranger's attention is worth. Every refusal carries its reason."""

    def test_a_large_fill_over_the_floor_passes_and_one_under_it_does_not(self):
        self.assertTrue(channel.qualifies(FILL, at_ms=0)["ok"])
        small = dict(FILL, notional_micro=channel.LARGE_FILL_MICRO - 1)
        out = channel.qualifies(small, at_ms=0)
        self.assertFalse(out["ok"])
        self.assertIn("floor", out["why"])

    def test_a_spike_needs_something_to_spike_against(self):
        """A market's first trade makes its trailing hour zero, and every new market would look like news."""
        out = channel.qualifies(dict(SPIKE, prior_hour_micro=0), at_ms=0)
        self.assertFalse(out["ok"])
        self.assertIn("trailing hour", out["why"])
        self.assertTrue(channel.qualifies(SPIKE, at_ms=0)["ok"])
        weak = channel.qualifies(dict(SPIKE, hour_micro=SPIKE["prior_hour_micro"] * 2), at_ms=0)
        self.assertFalse(weak["ok"], "a 2x hour is not the 2.5x the floor asks for")

    def test_a_resolution_alert_needs_a_source_to_cite(self):
        self.assertTrue(channel.qualifies(dict(RES, kind="new_market", liquidity_micro=1, ends_ms=10), at_ms=0)["ok"])
        out = channel.qualifies(dict(RES, resolution_source=""), at_ms=1_000)
        self.assertFalse(out["ok"])
        self.assertIn("resolution source", out["why"])

    def test_an_unknown_kind_is_refused_rather_than_guessed(self):
        self.assertFalse(channel.qualifies({"kind": "something_happened"}, at_ms=0)["ok"])

    def test_a_new_market_with_no_liquidity_is_not_news(self):
        out = channel.qualifies(dict(NEW, liquidity_micro=0), at_ms=0)
        self.assertFalse(out["ok"])
        self.assertTrue(channel.qualifies(NEW, at_ms=0)["ok"])

    def test_the_excluded_categories_are_excluded(self):
        self.assertFalse(channel.qualifies(dict(NEW, category="demo"), at_ms=0)["ok"])


class TestCadenceAndComposition(unittest.TestCase):
    """How often the channel may speak, and what one of its messages looks like."""

    def test_the_channel_goes_quiet_after_it_has_spoken(self):
        history = [{"kind": "large_fill", "fired_ms": 0}]
        allowed, why = channel.cadence_ok(history, at_ms=60_000, kind="large_fill")
        self.assertFalse(allowed)
        self.assertIn("floor is", why)
        allowed, _ = channel.cadence_ok(history, at_ms=channel.CADENCE["min_gap_ms"] + 1, kind="large_fill")
        self.assertTrue(allowed)

    def test_two_of_a_kind_is_the_cap_and_four_in_an_hour_is_the_cap(self):
        base = channel.CADENCE["min_gap_ms"] + 1
        two = [{"kind": "large_fill", "fired_ms": 0}, {"kind": "large_fill", "fired_ms": base}]
        allowed, why = channel.cadence_ok(two, at_ms=base * 2, kind="large_fill")
        self.assertFalse(allowed)
        self.assertIn("cap", why)
        four = [{"kind": k, "fired_ms": base * i} for i, k in enumerate(("large_fill", "volume_spike", "new_market",
                                                                        "resolution_soon"))]
        allowed, why = channel.cadence_ok(four, at_ms=base * 5, kind="new_market")
        self.assertFalse(allowed, "four alerts in an hour is the overall cap: %s" % why)

    def test_a_broadcast_is_one_market_with_a_deep_link_and_the_age_of_the_price(self):
        plan = channel.compose(FILL, bot_username=BOT, price_text="62.0¢", age_text="2 minutes ago")
        self.assertEqual([], channel.channel_findings(plan, event=FILL))
        text = plan.beats[0].text
        self.assertIn("as of" if False else "2 minutes ago", text)
        self.assertIn("not a forecast", text)
        urls = [b["url"] for row in plan.beats[0].keyboard["inline_keyboard"] for b in row if "url" in b]
        self.assertTrue(all(u.startswith("https://t.me/%s/" % BOT) for u in urls), urls)
        self.assertIn("startapp=fed-cut-sept", urls[0])

    def test_the_deep_link_is_a_lookup_key_and_never_carries_an_account(self):
        url = channel.mini_app_url("Fed-Cut/Sept??", bot_username=BOT)
        self.assertTrue(url.endswith("startapp=fed-cutsept"), url)
        self.assertNotIn("@", url.split("t.me/")[1])
        self.assertLess(len(url), 120, "a deep link is forwarded and quoted; it stays short")

    def test_a_message_that_promises_an_outcome_cannot_leave(self):
        plan = channel.compose(FILL, bot_username=BOT, price_text="62.0¢", age_text="2 minutes ago")
        hot = render.Plan(beats=[render.Beat(text=plan.beats[0].text.replace("Odds are a market, not a forecast.",
                                                                            "This is a guaranteed sure thing."),
                                             keyboard=plan.beats[0].keyboard)])
        self.assertTrue(any("promises" in f for f in channel.channel_findings(hot, event=FILL)))

    def test_a_broadcast_without_a_way_in_is_a_finding(self):
        plan = channel.compose(FILL, bot_username=BOT, price_text="62.0¢", age_text="2 minutes ago")
        bare = render.Plan(beats=[render.Beat(text=plan.beats[0].text)])
        self.assertTrue(any("deep link" in f for f in channel.channel_findings(bare, event=FILL)))

    def test_the_personal_copy_can_place_a_trade_because_it_has_a_conversation(self):
        plan = channel.personal_copy(FILL, bot_username=BOT, price_text="62.0¢", age_text="2 minutes ago",
                                     why="you follow this market")
        payloads = [b["callback_data"] for row in plan.beats[0].keyboard["inline_keyboard"] for b in row
                    if "callback_data" in b]
        self.assertIn("market:yes:fed-cut-sept", payloads)
        self.assertIn("you follow this market", plan.beats[0].text)
        self.assertEqual(outbox.P_ALERT_PERSONAL, plan.priority)


class TestPersonalDelivery(unittest.TestCase):
    """Quiet hours, the digest, and the three kinds that may buzz at 3am."""

    def test_a_fill_ignores_quiet_hours_and_a_watch_alert_waits(self):
        midnight = 1_700_006_400_000                     # 2023-11-15T00:00:00Z, inside the quiet window
        self.assertTrue(channel.is_quiet(midnight))
        self.assertTrue(channel.deliver_now("fill", at_ms=midnight)[0], "a fill is about the user's own money")
        self.assertTrue(channel.deliver_now("stop", at_ms=midnight)[0])
        send, why = channel.deliver_now("watch", at_ms=midnight)
        self.assertFalse(send)
        self.assertIn("digest", why)

    def test_the_digest_is_a_list_with_a_button_each_and_a_hard_cap(self):
        alerts = [{"slug": "m-%d" % i, "question": "Market %d" % i, "price_text": "5%d¢" % i} for i in range(12)]
        plan = channel.digest(alerts, bot_username=BOT, at_ms=1_000)
        rows = plan.beats[0].keyboard["inline_keyboard"]
        self.assertEqual(8, len(rows), "a digest that needs scrolling is one nobody reads")
        self.assertIn("and 4 more in /alerts", plan.beats[0].text)
        self.assertEqual(outbox.P_BULK, plan.priority)

    def test_an_empty_digest_says_so_rather_than_sending_nothing(self):
        plan = channel.digest([], bot_username=BOT, at_ms=1_000)
        self.assertIn("Nothing waited", plan.beats[0].text)


class TestKillSwitch(unittest.TestCase):
    """D8: a switch for messages that is not a switch for trades."""

    def test_the_two_switches_are_separate_tables_and_separate_decisions(self):
        """P06 halts *orders*; this one pauses *delivery*. One table each, and different reasons to exist.

        Asserted against the migrations rather than a doc, because the failure this guards against is somebody
        "simplifying" the two switches into one during a refactor — at which point an operator pausing marketing
        would stop trading, or worse, the reverse.
        """
        root = Path(__file__).resolve().parents[1] / "db" / "migrations"
        trading = (root / "0001_core.sql").read_text()
        bot = (root / "0019_telegram.sql").read_text()
        self.assertIn("CREATE TABLE kill_switch_state", trading)
        self.assertIn("CREATE TABLE IF NOT EXISTS telegram_kill_state", bot)
        self.assertNotIn("telegram_kill_state", trading)
        self.assertNotIn("kill_switch_state ", bot)
        # The bot's switch is *scoped* (channel / personal / all) because an operator pausing the megaphone must
        # still be able to tell a user about their own fill; the trading switch has no such distinction.
        self.assertIn("scope", bot)
        self.assertIn("CHECK (NOT engaged OR length(reason) >= 8)", bot,
                      "an unexplained kill switch is indistinguishable from an outage")

    def test_engaging_needs_a_reason_and_a_scope(self):
        self.assertTrue(ops.kill_findings(True, scope="all", reason="short", by="ops"))
        self.assertTrue(ops.kill_findings(True, scope="loud", reason="a good long reason here", by="ops"))
        self.assertTrue(ops.kill_findings(True, scope="all", reason="a good long reason here"))
        self.assertEqual([], ops.kill_findings(True, scope="channel", reason="bot is rate limited by Telegram",
                                               by="ops"))
        self.assertEqual([], ops.kill_findings(False, scope="all", reason=""))

    def test_scope_decides_what_is_held_back(self):
        channel_only = ops.KillState(engaged=True, scope="channel", reason="holding the megaphone", by="ops")
        self.assertTrue(ops.blocks(channel_only, "channel"))
        self.assertFalse(ops.blocks(channel_only, "personal"),
                         "pausing the channel must not stop a user hearing about their own fill")
        everything = ops.KillState(engaged=True, scope="all", reason="full pause while we investigate", by="ops")
        self.assertTrue(ops.blocks(everything, "personal"))

    def test_a_disengaged_switch_blocks_nothing(self):
        self.assertFalse(ops.blocks(ops.KillState(), "channel"))
        self.assertFalse(ops.blocks(ops.KillState(), "personal"))


class TestOperatorTooling(unittest.TestCase):
    """The dry run, the staged rollout, the recovery plans and the username argument."""

    def test_a_dry_run_renders_the_real_bytes_and_sends_nothing(self):
        plan = channel.compose(FILL, bot_username=BOT, price_text="62.0¢", age_text="2 minutes ago")
        out = ops.dry_run([{"slug": "fed-cut-sept", "plan": plan, "event": FILL}])
        self.assertEqual(1, out["count"])
        item = out["items"][0]
        self.assertTrue(item["has_deep_link"])
        self.assertIn("<b>", item["text"], "a dry run that renders a summary has never caught a broken message")
        self.assertGreater(item["chars"], 40)

    def test_the_default_stage_is_small_and_greedy_only_when_asked(self):
        targets = [{"kind": "large_fill", "category": "economics"}] * 12 + [{"kind": "volume_spike",
                                                                             "category": "crypto"}]
        chosen, held = ops.staged(targets)
        self.assertEqual(10, len(chosen))
        self.assertEqual(3, len(held), "what the stage filtered out is returned, not silently dropped")
        all_of, _ = ops.staged(targets, stage={"kinds": (), "limit": 100})
        self.assertEqual(13, len(all_of))

    def test_the_stage_can_be_narrowed_by_category(self):
        targets = [{"kind": "large_fill", "category": "economics"}, {"kind": "large_fill", "category": "sports"}]
        chosen, held = ops.staged(targets, stage={"categories": ("economics",), "limit": 5})
        self.assertEqual(1, len(chosen))
        self.assertEqual(1, len(held))

    def test_all_three_recovery_paths_name_a_hedge_and_an_owner(self):
        rows = ops.recovery_steps()
        self.assertEqual({"bot_restricted", "channel_banned", "impersonation"}, {r["failure"] for r in rows})
        for r in rows:
            self.assertTrue(r["hedge"], r["failure"])
            self.assertTrue(r["owner"], r["failure"])
            self.assertIn(r["mirror"], ("discord", "discord+web", "web"))
        self.assertEqual([], ops.ops_findings({"kill": ops.KillState(), "queue_depth": 3, "bot_configured": True}))

    def test_a_deep_queue_and_a_missing_token_are_findings(self):
        findings = ops.ops_findings({"kill": ops.KillState(), "queue_depth": 900, "bot_configured": False,
                                     "env": "production"})
        self.assertTrue(any("queue" in f for f in findings))
        self.assertTrue(any("bot token" in f for f in findings))

    def test_the_username_decision_carries_its_reasons(self):
        d = ops.username_decision()
        self.assertEqual("@polygm_bot", d["bot"])
        self.assertIn("trade", d["mini_app_short_names"])
        self.assertIn("per_function_bots", d["rejected"])
        self.assertTrue(d["second_bot"]["when"])


if __name__ == "__main__":
    unittest.main()
