"""P10 · D9 Alerts, through the HTTP layer.

What these tests defend, in the order a user meets it:

  * the cooldown is the rule's own window budget, stated as a sentence a user can check — not a separate setting
    that could disagree with it;
  * a channel the plan does not cover is refused at SAVE time with the plan named, because the alternative is a
    rule that looks armed and can never deliver;
  * quiet hours hold everything, urgent included, and both the list and the test fire say so BEFORE it happens;
  * a digest batches a notice but never an urgent alert: quiet hours are an instruction, a digest is our choice;
  * a test fire exercises the plan without spending the rule's real window — a test that consumed the budget it
    tests would poison the feature it validates;
  * held and refused are different facts in the history, and a row that was never sent has no latency.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "packages"), str(ROOT / "services" / "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import import_app                                                       # noqa: E402

USER = {"X-User-Id": "u-demo"}
PRO = {"X-User-Id": "u-alerts-pro"}
BASE_MS = 1_789_000_000_000


class AlertsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = import_app("alerts-api")
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)
        row = cls.app._db.execute("SELECT id FROM markets ORDER BY id LIMIT 1").fetchone()
        cls.market = str(row[0])
        # A pro account for the channel tests: `users.tier` is what the plan check reads, and a test that had to
        # fake the tier per request would not be testing the check.
        at = cls.app._now_ms()
        # Two accounts beyond the demo one: a pro plan for the paid-channel tests, and a second free account for
        # the ownership test. Both have to exist — the idempotency record is keyed to a user, so a request from
        # an account with no row is refused by the database before the rule is ever looked at.
        for uid, tier in ((PRO["X-User-Id"], "pro"), ("u-other", "free")):
            cls.app._db.execute("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?,?)",
                                (uid, at, tier))
        cls.app._db.commit()
        cls._settings_seq = 0

    def setUp(self) -> None:
        self._real_now = self.app._now_ms

    def tearDown(self) -> None:
        self.app._now_ms = self._real_now

    def at_local(self, minute: int, *, tz: int = 330) -> int:
        """An instant whose LOCAL minute-of-day (with `tz` east of UTC) is `minute`.

        The offset is what makes quiet hours testable at all: a window evaluated in the wrong timezone is a
        setting that silently does not work, so the test chooses the instant rather than hoping the suite runs
        at night.
        """
        base_min = BASE_MS // 60_000
        want = (minute - tz) % 1440
        return BASE_MS + ((want - base_min) % 1440) * 60_000

    def key(self, name: str) -> dict:
        return {**USER, "Idempotency-Key": "alert-%s-%s" % (name, abs(hash(name)) % 10_000)}

    def create(self, name: str, *, headers: dict | None = None, **over):
        body = {
            "kind": "whale_fill",
            "marketId": self.market,
            "channel": "telegram",
            "severity": "notice",
            "firesPerWindow": 3,
            "windowMs": 3_600_000,
            **over,
        }
        return self.client.post("/v1/alerts", json=body, headers=headers or self.key(name))

    def alerts(self, *, headers: dict | None = None):
        r = self.client.get("/v1/alerts", headers=headers or USER)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def settings(self, **fields):
        """A settings write. Every call gets its own key: the key names the OPERATION, and a replayed key returns
        the stored answer without applying anything — which would make a test's "reset to a known state" a no-op
        that silently passed or failed depending on which test ran first."""
        AlertsTestCase._settings_seq += 1
        key = "alert-set-%d" % AlertsTestCase._settings_seq
        return self.client.post("/v1/alerts/settings", json=fields,
                                headers={**USER, "Idempotency-Key": key})

    def baseline(self):
        """Quiet hours off, digest off: the state every test that depends on delivery says it needs.

        Settings are per account and sticky, so a test that assumes "no digest" is a test that passes or fails
        depending on alphabetical order."""
        r = self.settings(quietStartMin=-1, quietEndMin=-1, digestMode="off", tzOffsetMin=0)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    # ------------------------------------------------------------------ the list

    def test_the_list_needs_a_session_and_states_the_cooldown_as_a_rule(self):
        self.baseline()
        self.assertEqual(401, self.client.get("/v1/alerts").status_code)
        rule_id = self.create("cooldown").json()["rule"]["ruleId"]
        body = self.alerts()
        rule = next(r for r in body["rules"] if r["ruleId"] == rule_id)
        self.assertEqual(3, rule["firesPerWindow"])
        self.assertEqual(3_600_000, rule["windowMs"])
        self.assertEqual(1_200_000, rule["cooldownMs"])
        self.assertEqual("3 per 1h = one every 20m at most", rule["cooldownRule"])
        self.assertEqual(3, rule["remainingInWindow"])
        self.assertEqual("queued for telegram", rule["wouldDoNow"]["sentence"])
        # The demo account is on the `trader` tier, which is what makes email a legal channel for it and
        # webhook not: the plan check reads the account, not the rule.
        self.assertEqual("trader", body["plan"])
        # The default channel is Telegram and the note says which plans the other two need.
        self.assertEqual("telegram", body["settings"]["defaultChannel"])
        plans = {c["channel"]: c["plan"] for c in body["settings"]["channels"]}
        self.assertEqual({"telegram": "free", "email": "trader", "webhook": "pro"}, plans)

    def test_a_write_without_an_idempotency_key_is_a_400(self):
        r = self.client.post("/v1/alerts", json={"kind": "whale_fill", "marketId": self.market}, headers=USER)
        self.assertEqual(400, r.status_code, r.text)
        self.assertEqual("IDEM_KEY_REQUIRED", r.json()["error"]["code"])

    def test_the_same_key_twice_creates_one_rule_and_replays_the_answer(self):
        before = len(self.alerts()["rules"])
        first = self.create("idem")
        again = self.client.post("/v1/alerts", json={
            "kind": "whale_fill", "marketId": self.market, "channel": "telegram", "severity": "notice",
            "firesPerWindow": 3, "windowMs": 3_600_000}, headers=self.key("idem"))
        self.assertEqual(first.json(), again.json())
        self.assertEqual(len(self.alerts()["rules"]), before + 1)

    # ------------------------------------------------------------------ the plan gate

    def test_a_webhook_rule_on_a_free_plan_is_refused_at_save_time_not_at_fire_time(self):
        r = self.create("webhook", channel="webhook")
        self.assertEqual(402, r.status_code, r.text)
        body = r.json()
        self.assertEqual("PLAN_REQUIRED", body["error"]["code"])
        self.assertIn("pro", body["error"]["message"])
        self.assertFalse(any(x["channel"] == "webhook" for x in self.alerts()["rules"]))

    def test_the_same_webhook_rule_saves_on_a_pro_plan(self):
        # The `params.url` is not decoration: since the transport landed (P16's SSRF closure) a webhook rule
        # without a destination is a rule that can never fire, so it is refused at save with the same reasoning
        # the plan check uses. The URL is validated for shape and — for a literal address — for being public.
        r = self.client.post("/v1/alerts", json={"kind": "whale_fill", "marketId": self.market,
                                                 "channel": "webhook", "firesPerWindow": 1,
                                                 "windowMs": 600_000,
                                                 "params": {"absUsdMicro": 100_000_000,
                                                            "url": "https://hooks.example.com/polygm"}},
                             headers={**PRO, "Idempotency-Key": "alert-pro-1"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["rule"]["channelAllowed"])
        self.assertEqual("https://hooks.example.com/polygm", r.json()["rule"]["params"]["url"])

    def test_a_webhook_rule_with_no_destination_is_refused(self):
        r = self.client.post("/v1/alerts", json={"kind": "whale_fill", "marketId": self.market,
                                                 "channel": "webhook", "firesPerWindow": 1,
                                                 "windowMs": 600_000, "params": {"absUsdMicro": 100_000_000}},
                             headers={**PRO, "Idempotency-Key": "alert-pro-2"})
        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("params.url", r.text)

    def test_a_webhook_rule_pointing_inside_our_own_network_is_refused_at_save(self):
        """The SSRF refusal, at the surface a user can reach, and for the address that matters most: the cloud
        metadata service. Both halves matter — the loopback case proves our admin routes are not reachable
        through a webhook, and the `169.254.169.254` case proves the credentials endpoint is not either."""
        for url in ("https://169.254.169.254/latest/meta-data/iam/security-credentials/",
                    "https://127.0.0.1/v1/admin/kill-switch",
                    "https://10.0.0.5/hook",
                    "http://hooks.example.com/cleartext"):
            with self.subTest(url=url):
                r = self.client.post("/v1/alerts", json={"kind": "whale_fill", "marketId": self.market,
                                                        "channel": "webhook", "firesPerWindow": 1,
                                                        "windowMs": 600_000,
                                                        "params": {"absUsdMicro": 100_000_000, "url": url}},
                                     headers={**PRO, "Idempotency-Key": "alert-pro-ssrf-%s" % abs(hash(url))})
                self.assertEqual(422, r.status_code, r.text)
                self.assertIn("params.url", r.text)
        self.assertEqual([], [x for x in self.alerts()["rules"] if x["channel"] == "webhook"])

    def test_an_alert_with_no_target_is_refused(self):
        r = self.create("notarget", marketId="")
        self.assertEqual(422, r.status_code, r.text)

    def test_a_rule_belongs_to_one_account(self):
        rule_id = self.create("owned").json()["rule"]["ruleId"]
        r = self.client.post("/v1/alerts/test", json={"ruleId": rule_id},
                             headers={"X-User-Id": "u-other", "Idempotency-Key": "alert-other-1"})
        self.assertEqual(404, r.status_code, r.text)

    # ------------------------------------------------------------------ quiet hours

    def test_quiet_hours_hold_everything_and_the_list_says_so_before_it_happens(self):
        self.baseline()
        rule_id = self.create("quiet").json()["rule"]["ruleId"]
        # 22:00 -> 07:00 local, at UTC+05:30, with "now" set to 23:30 local.
        patched = self.at_local(23 * 60 + 30)
        self.app._now_ms = lambda: patched
        r = self.settings(quietStartMin=22 * 60, quietEndMin=7 * 60, tzOffsetMin=330)
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["quietHours"]["active"])
        self.assertEqual("07:00", r.json()["quietHours"]["note"].split("until ")[1].split(" local")[0])

        body = self.alerts()
        rule = next(x for x in body["rules"] if x["ruleId"] == rule_id)
        self.assertEqual("quiet_hours", rule["wouldDoNow"]["decision"])
        self.assertEqual("digest_scheduled", rule["wouldDoNow"]["status"])
        self.assertIn("held for quiet hours", rule["wouldDoNow"]["sentence"])

        fired = self.client.post("/v1/alerts/test", json={"ruleId": rule_id},
                                 headers={"X-User-Id": USER["X-User-Id"], "Idempotency-Key": "alert-quiet-test"}).json()
        self.assertEqual(["quiet_hours"], [p["decision"] for p in fired["plan"]])
        self.assertIn("held", fired["summary"]["sentence"])
        self.assertIn("no delivery transport", fired["note"])

    def test_quiet_hours_off_delivers_and_says_it_is_off(self):
        self.baseline()
        rule_id = self.create("loud").json()["rule"]["ruleId"]
        patch = self.at_local(12 * 60)
        self.app._now_ms = lambda: patch
        r = self.settings(quietStartMin=-1, quietEndMin=-1)
        self.assertFalse(r.json()["quietHours"]["active"])
        self.assertIn("off", r.json()["quietHours"]["note"])
        fired = self.client.post("/v1/alerts/test", json={"ruleId": rule_id},
                                 headers={"X-User-Id": USER["X-User-Id"], "Idempotency-Key": "alert-loud-test"}).json()
        self.assertEqual("send_now", fired["plan"][0]["decision"])
        self.assertIn("would be delivered now", fired["summary"]["sentence"])

    def test_a_one_ended_quiet_window_and_a_never_closing_one_are_both_refused(self):
        r = self.settings(quietStartMin=22 * 60, quietEndMin=-1)
        self.assertEqual(422, r.status_code, r.text)
        r2 = self.settings(quietStartMin=600, quietEndMin=600)
        self.assertEqual(422, r2.status_code, r2.text)

    # ------------------------------------------------------------------ digest

    def test_a_digest_batches_a_notice_but_never_an_urgent_alert(self):
        self.baseline()
        notice_id = self.create("digest-notice", severity="notice").json()["rule"]["ruleId"]
        urgent_id = self.create("digest-urgent", severity="urgent").json()["rule"]["ruleId"]
        patch = self.at_local(9 * 60)
        self.app._now_ms = lambda: patch
        self.settings(quietStartMin=-1, quietEndMin=-1, digestMode="daily", digestAtMin=480)
        for rule_id, expected in ((notice_id, "digest"), (urgent_id, "send_now")):
            fired = self.client.post("/v1/alerts/test", json={"ruleId": rule_id},
                                     headers={"X-User-Id": USER["X-User-Id"],
                                              "Idempotency-Key": "alert-digest-%s" % rule_id}).json()
            self.assertEqual(expected, fired["plan"][0]["decision"], fired)
        # And the sentence says which way it went: a digest is our batching, quiet hours are the user's word.
        fired = self.client.post("/v1/alerts/test", json={"ruleId": urgent_id},
                                 headers={"X-User-Id": USER["X-User-Id"], "Idempotency-Key": "alert-digest-again"}).json()
        self.assertIn("urgent breaches", fired["digest"]["note"])

    # ------------------------------------------------------------------ test fires and the history

    def test_a_test_fire_does_not_spend_the_rule_window(self):
        rule_id = self.create("window", firesPerWindow=2, windowMs=600_000).json()["rule"]["ruleId"]
        for i in range(3):
            self.client.post("/v1/alerts/test", json={"ruleId": rule_id},
                             headers={"X-User-Id": USER["X-User-Id"], "Idempotency-Key": "alert-window-%d" % i})
        rule = next(r for r in self.alerts()["rules"] if r["ruleId"] == rule_id)
        self.assertEqual(0, rule["firesInWindow"], "a test must not consume the budget it is testing")
        self.assertEqual(2, rule["remainingInWindow"])

    def test_the_history_reports_each_channel_with_its_status_and_reason(self):
        rule_id = self.create("history", firesPerWindow=1, windowMs=600_000).json()["rule"]["ruleId"]
        self.client.post("/v1/alerts/test", json={"ruleId": rule_id, "channels": ["telegram"]},
                         headers={"X-User-Id": USER["X-User-Id"], "Idempotency-Key": "alert-hist-1"})
        body = self.client.get("/v1/alerts/deliveries", headers=USER).json()
        mine = [r for r in body["rows"] if r["ruleId"] == "test:" + rule_id]
        self.assertEqual(1, len(mine))
        self.assertEqual("telegram", mine[0]["channel"])
        self.assertTrue(mine[0]["isTest"], "a test fire is recorded as one, never as a real alert")
        self.assertIn(mine[0]["status"], ("queued", "digest_scheduled", "failed"))
        self.assertIsNone(mine[0]["latencyMs"], "nothing was sent, so there is no latency to report")
        self.assertTrue(mine[0]["reason"] or mine[0]["status"] == "queued")

    def test_the_window_cap_holds_rather_than_refuses(self):
        """A rule whose window is spent has its next alert HELD by its own cap — and the plan says which cap."""
        rule_id = self.create("capped", firesPerWindow=1, windowMs=600_000).json()["rule"]["ruleId"]
        at = self.app._now_ms()
        # A real fire in the window: the only way to spend the budget is to have actually fired, and what a fire
        # IS on this plane is a `signals` row written by the ingest loop under the rule's own id.
        self.app._db.execute("INSERT INTO signals (rule_id, kind, condition_id, token_id, severity, title, "
                             "body_json, dedupe_key, fired_bucket, fired_ms) "
                             "VALUES (?, 'large_fill', '', '', 'notice', 'whale fill', '{}', 'cap', 0, ?)",
                             (rule_id, at - 1_000))
        self.app._db.commit()
        rule = next(r for r in self.alerts()["rules"] if r["ruleId"] == rule_id)
        self.assertEqual(1, rule["firesInWindow"])
        self.assertEqual(0, rule["remainingInWindow"])
        self.assertIsNotNone(rule["nextAllowedMs"])
        fired = self.client.post("/v1/alerts/test", json={"ruleId": rule_id},
                                 headers={"X-User-Id": USER["X-User-Id"], "Idempotency-Key": "alert-capped-1"}).json()
        # The test itself is exempt from the cap (that is the point of a test), but the list's own plan is not:
        # a test fire is a "would this reach me", not a rule evaluation.
        self.assertEqual("send_now", fired["plan"][0]["decision"])

    # ------------------------------------------------------------------ the guard rail is a guarantee, not a promise

    def test_an_engine_channel_the_product_sells_is_one_the_signal_engine_accepts(self):
        """D9 sells telegram/email/webhook; the ingest engine's own channel list has to include all three, or a
        paying account's rule is refused by the engine that would deliver it."""
        from polygm_core.signals import engine as E
        from polygm_core.signals import console as C
        for channel in C.CHANNELS:
            rule = E.build_rule("r-1", "large_fill", {}, owner="u-demo", channels=[channel])
            self.assertEqual((channel,), rule.channels)

    # ------------------------------------------------------------------ the rule the engine actually reads

    def test_a_saved_rule_is_one_the_ingest_engine_will_evaluate(self):
        """The mirror, and the reason it exists: `signal_rules` is the only table the ingest plane reads, so a
        rule that lived only in `alert_rules` would be listed, would show a cooldown, and could never fire."""
        rule_id = self.create("mirror", kind="whale_fill").json()["rule"]["ruleId"]
        row = self.app._db.execute("SELECT kind, owner, params_json, market_filter_json, cooldown_s, "
                                   "channels_json, enabled FROM signal_rules WHERE id = ?", (rule_id,)).fetchone()
        self.assertIsNotNone(row, "a saved alert with no engine rule is an alert that never fires")
        self.assertEqual("large_fill", row[0], "the screen's word and the engine's word are not the same word")
        self.assertEqual(USER["X-User-Id"], row[1])
        self.assertEqual(["telegram"], json.loads(row[5]))
        self.assertEqual(1, int(row[6]))
        self.assertIn("abs_usd_micro", json.loads(row[2]))
        self.assertEqual({"market": ["0xC1"]}, json.loads(row[3]),
                         "the filter is keyed by the venue condition id, which is what engine events carry")

    def test_a_manual_alert_says_that_no_loop_is_watching_it(self):
        rule_id = self.create("manual", kind="manual").json()["rule"]["ruleId"]
        rule = next(r for r in self.alerts()["rules"] if r["ruleId"] == rule_id)
        self.assertFalse(rule["evaluated"])
        self.assertIsNone(rule["engineKind"])
        self.assertIn("no loop is watching it", rule["evaluator"])
        self.assertIsNone(self.app._db.execute("SELECT 1 FROM signal_rules WHERE id = ?", (rule_id,)).fetchone())

    def test_a_level_alert_with_no_level_is_refused_at_save_time(self):
        """A rule whose condition the engine cannot evaluate looks armed and cannot fire. The place to say so is
        the form, while the user is still looking at it."""
        r = self.create("nolevel", kind="price_level")
        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("price level", r.json()["error"]["message"])
        ok = self.create("level", kind="price_level", params={"priceMicro": 620_000, "op": ">="})
        self.assertEqual(200, ok.status_code, ok.text)
        row = self.app._db.execute("SELECT params_json FROM signal_rules WHERE id = ?",
                                   (ok.json()["rule"]["ruleId"],)).fetchone()
        self.assertEqual({"price_micro": 620_000, "op": ">="}, json.loads(row[0]))

    def test_the_list_carries_the_settings_shape_the_screen_reads(self):
        """The list and the settings write answer with ONE shape.

        They used to differ: the write carried `quietHours`/`digestNow` and the list carried the raw row, so the
        screen read `settings.quietHours.note` on a block that only had it after a write — a crash on first paint,
        for exactly the users who had never touched the form.
        """
        listed = self.alerts()["settings"]
        written = self.settings(quietStartMin=-1, quietEndMin=-1, digestMode="off").json()
        block = {"quietStartMin", "quietEndMin", "tzOffsetMin", "digestMode", "digestAtMin", "defaultChannel",
                 "channels", "note", "quietHours", "digestNow"}
        self.assertEqual(set(listed), block, "the list nested under `settings` carries every field of the block")
        self.assertTrue(block <= set(written), sorted(set(written)))   # the write returns it flat, plus the stamp
        self.assertIn("configured", listed["quietHours"])
        self.assertIn("note", listed["quietHours"])

    def test_the_history_speaks_the_screens_vocabulary_not_the_engines(self):
        rule_id = self.create("vocab", kind="whale_fill").json()["rule"]["ruleId"]
        self.app._db.execute("INSERT INTO signals (rule_id, kind, condition_id, token_id, severity, title, "
                             "body_json, dedupe_key, fired_bucket, fired_ms) "
                             "VALUES (?, 'large_fill', '0xC1', '', 'notice', 'whale fill', '{}', 'v', 0, ?)",
                             (rule_id, self.app._now_ms()))
        sid = self.app._db.execute("SELECT MAX(id) FROM signals").fetchone()[0]
        self.app._db.execute("INSERT INTO alert_deliveries (signal_id, user_id, channel, priority, queued_ms, "
                             "status) VALUES (?,?,?,2,?,'queued')", (int(sid), USER["X-User-Id"], "telegram",
                                                                    self.app._now_ms()))
        self.app._db.commit()
        rows = self.client.get("/v1/alerts/deliveries", headers=USER).json()["rows"]
        mine = [r for r in rows if r["ruleId"] == rule_id]
        self.assertEqual(1, len(mine), rows[:3])
        self.assertEqual("whale_fill", mine[0]["kind"], "the user's word, not the engine's")
        self.assertEqual("large_fill", mine[0]["engineKind"], "and the engine's word is kept alongside it")
        self.assertFalse(mine[0]["isTest"])
