"""P10 · D8 Automation, through the HTTP layer.

What these tests defend, in the order a user meets it:

  * a rule cannot be saved armed, and it cannot be armed without a dry run the engine actually observed — the two
    refusals (`DRY_RUN_REQUIRED`, and `dryRunOnly` on the create response) are the whole safety story;
  * the builder's vocabulary is the engine's vocabulary: an unknown trigger kind, an unknown field, or an
    eight-row tree is refused at save time rather than failing at fire time;
  * every evaluation — including the ones that did nothing — is a run row with a reason, and the dry run is one of
    those rows rather than a flag the client can claim;
  * the 5-minute crypto template is *listed* with its fee arithmetic when the maths withholds it, and the
    protective templates are offered because they can only reduce a loss;
  * the write is idempotent: the same key twice creates one rule and answers with the first answer.
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


class AutomationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = import_app("automation-api")
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)
        row = cls.app._db.execute("SELECT id FROM markets ORDER BY id LIMIT 1").fetchone()
        cls.market = str(row[0])
        # A second account for the ownership tests. It has to exist: the API's idempotency record is keyed to a
        # user, so a request from an account with no row is refused by the database before any rule is looked at —
        # and "somebody else's rule" has to be tested with somebody who exists.
        cls.app._db.execute("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES ('u-other', ?, 'free')",
                            (cls.app._now_ms(),))
        cls.app._db.commit()

    def key(self, name: str) -> dict:
        return {**USER, "Idempotency-Key": "auto-%s-%s" % (name, abs(hash(name)) % 10_000)}

    def payload(self, **over) -> dict:
        body = {
            "kind": "exit",
            "name": "exit before resolution",
            "match": "all",
            "triggers": [{"kind": "time", "at_ms": self.app._now_ms() + 600_000, "once": True}],
            "actions": [{"kind": "close_position", "method": "market", "max_slippage_bps": 100}],
            "targets": [{"marketId": self.market}],
            "maxPerDay": 12,
            "minIntervalMs": 60_000,
            "maxLossMicro": 5_000_000,
        }
        body.update(over)
        return body

    def create(self, name="create", **over):
        return self.client.post("/v1/automations", json=self.payload(**over), headers=self.key(name))

    def rules(self):
        r = self.client.get("/v1/automations", headers=USER)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    # ------------------------------------------------------------------ the list and its vocabulary

    def test_the_list_needs_a_session_and_carries_the_engine_limits(self):
        self.assertEqual(401, self.client.get("/v1/automations").status_code)
        body = self.rules()
        self.assertIn("rules", body)
        self.assertIn("halt", body)
        vocab = body["vocabulary"]
        self.assertEqual(vocab["limits"]["maxLeaves"], 8)
        self.assertEqual(vocab["limits"]["minIntervalMs"], 60_000)
        # The vocabulary is the engine's own kinds, not a hand-written list: a builder that offered a trigger the
        # engine refuses would be a form that saves rules that can never fire.
        kinds = [x["kind"] for x in vocab["triggers"]]
        self.assertEqual(sorted(kinds), sorted(["price_cross", "time", "signal", "book_imbalance", "new_market"]))
        self.assertIn("cancel_open", [x["kind"] for x in vocab["actions"]])
        # `cancel_open` can never mean "cancel everything I have": the engine refuses that scope, so the form is
        # not offered it either.
        cancel = next(x for x in vocab["actions"] if x["kind"] == "cancel_open")
        self.assertNotIn("all", next(f for f in cancel["fields"] if f["name"] == "scope")["options"])

    # ------------------------------------------------------------------ saving a rule

    def test_a_rule_is_saved_as_a_dry_run_and_never_armed(self):
        r = self.create("first")
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["dryRunOnly"])
        self.assertEqual(body["rule"]["status"], "dry_run")
        self.assertFalse(body["rule"]["enabled"])
        self.assertIsNone(body["rule"]["dryRunCompletedMs"])
        self.assertEqual(body["rule"]["name"], "exit before resolution")

    def test_a_rule_with_no_loss_ceiling_is_refused_not_saved_with_zero(self):
        """The ceiling is not optional for a rule that can move money, and the refusal names the field.

        `automation_rule_policy` has a CHECK (`redemption_needs_no_ceiling`) that only `auto_redeem` is exempt
        from, so a payload without `maxLossMicro` used to reach the insert and come back as a 500 INTERNAL: a
        database constraint wearing the code of a service fault, which tells the builder user nothing about the
        field they left empty. The P10 gate found it by saving the smallest legal rule it could.
        """
        body = self.payload()
        body.pop("maxLossMicro")
        r = self.client.post("/v1/automations", json=body, headers=self.key("noceiling"))
        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("maxLossMicro", json.dumps(r.json()))
        before = len(self.rules()["rules"])
        body["kind"] = "auto_redeem"
        body["maxLossMicro"] = 0
        ok = self.client.post("/v1/automations", json=body, headers=self.key("redeemceiling"))
        self.assertEqual(200, ok.status_code, ok.text)
        self.assertEqual("auto_redeem", ok.json()["rule"]["kind"])
        self.assertEqual(before + 1, len(self.rules()["rules"]))

    def test_a_write_without_an_idempotency_key_is_a_400(self):
        r = self.client.post("/v1/automations", json=self.payload(), headers=USER)
        self.assertEqual(400, r.status_code, r.text)
        self.assertEqual(r.json()["error"]["code"], "IDEM_KEY_REQUIRED")

    def test_the_same_key_twice_creates_one_rule_and_replays_the_first_answer(self):
        before = len(self.rules()["rules"])
        # The body is built ONCE. A payload rebuilt per call re-derives its own `at_ms`, and a key that is reused
        # for a *different* body is a conflict by design (the test below this one) — an idempotency key names an
        # operation, not a route.
        body = self.payload()
        first = self.client.post("/v1/automations", json=body, headers=self.key("idem"))
        self.assertEqual(200, first.status_code, first.text)
        again = self.client.post("/v1/automations", json=body, headers=self.key("idem"))
        self.assertEqual(200, again.status_code, again.text)
        self.assertEqual(first.json(), again.json(), "a replay must be the stored answer, not a re-derived one")
        self.assertEqual(len(self.rules()["rules"]), before + 1)

    def test_a_different_body_under_the_same_key_is_a_conflict_not_a_silent_replay(self):
        self.create("conflict")
        clash = self.payload(name="something else")
        r = self.client.post("/v1/automations", json=clash, headers=self.key("conflict"))
        self.assertEqual(409, r.status_code, r.text)
        self.assertEqual(r.json()["error"]["code"], "IDEM_CONFLICT")

    # ------------------------------------------------------------------ the builder cannot lie

    def test_an_unknown_trigger_kind_is_refused_with_the_kind_named(self):
        r = self.create("badkind", triggers=[{"kind": "webhook_hit", "url": "https://x"}])
        self.assertEqual(422, r.status_code, r.text)
        body = r.json()["error"]
        self.assertIn("webhook_hit", body["message"], "the refusal names the kind it does not know")
        self.assertIn("webhook_hit", body["message"])
        self.assertIn("price_cross", body["message"], "and the kinds it does have, so the form can offer them")
        # The envelope is the contract's four keys. A `where` array would be a fifth, and the field names are
        # already in the sentence where a user reads them.
        self.assertEqual(sorted(body), ["code", "message", "requestId", "retryable"])

    def test_an_expression_is_refused_as_an_unknown_field(self):
        r = self.create("expr", triggers=[{"kind": "price_cross", "expr": "mid > 0.5"}])
        self.assertEqual(422, r.status_code, r.text)
        msg = r.json()["error"]["message"]
        self.assertIn("expr", msg, "the builder has no expression syntax, and the refusal says so by name")
        self.assertIn("op, price_micro, uses", msg, "and lists the fields that trigger does take")

    def test_nine_rows_is_refused_because_the_engine_reads_eight(self):
        rows = [{"kind": "time", "at_ms": self.app._now_ms() + 60_000, "once": True} for _ in range(9)]
        r = self.create("toomany", triggers=rows)
        self.assertEqual(422, r.status_code, r.text)
        self.assertIn("8", r.text)

    def test_a_rule_with_no_market_is_refused(self):
        r = self.create("notarget", targets=[])
        self.assertEqual(422, r.status_code, r.text)

    def test_an_unknown_market_is_a_404_and_nothing_is_saved(self):
        before = len(self.rules()["rules"])
        r = self.create("badmarket", targets=[{"marketId": "0x-not-a-market"}])
        self.assertEqual(404, r.status_code, r.text)
        self.assertEqual(len(self.rules()["rules"]), before)

    # ------------------------------------------------------------------ the dry run is the door

    def test_arming_without_a_dry_run_is_refused_with_the_next_step_named(self):
        rule_id = self.create("arm2").json()["rule"]["ruleId"]
        # A different key from the create: the key names THIS operation. Reusing it would be a conflict, and a
        # conflict here would hide the refusal this test exists to check.
        r = self.client.post("/v1/automations/guards", json={"ruleId": rule_id, "state": "active"},
                             headers=self.key("arm2b"))
        self.assertEqual(409, r.status_code, r.text)
        body = r.json()
        self.assertEqual(body["error"]["code"], "DRY_RUN_REQUIRED")
        # The refusal names the call that clears it: a refusal a user cannot act on reads as a bug.
        self.assertIn("POST /v1/automations/preview", body["error"]["message"], r.text)
        listed = next(x for x in self.rules()["rules"] if x["ruleId"] == rule_id)
        self.assertEqual(listed["status"], "dry_run")
        self.assertFalse(listed["enabled"])

    def test_a_dry_run_completes_the_rule_and_then_arming_works(self):
        rule_id = self.create("arm3").json()["rule"]["ruleId"]
        preview = self.client.post("/v1/automations/preview", json={"ruleId": rule_id}, headers=self.key("prev3"))
        self.assertEqual(200, preview.status_code, preview.text)
        body = preview.json()
        self.assertTrue(body["dryRun"])
        self.assertTrue(body["dryRunCompleted"])
        self.assertIn("leaves", body["simulation"])
        self.assertIn("fires", body["simulation"])
        armed = self.client.post("/v1/automations/guards", json={"ruleId": rule_id, "state": "active"},
                                 headers=self.key("arm3b"))
        self.assertEqual(200, armed.status_code, armed.text)
        self.assertEqual(armed.json()["rule"]["status"], "active")
        self.assertTrue(armed.json()["rule"]["enabled"])
        # Pausing is always allowed, and it is recorded as its own run row: "why did my rule stop" has an answer.
        paused = self.client.post("/v1/automations/guards",
                                  json={"ruleId": rule_id, "state": "paused", "reason": "I am watching this one"},
                                  headers=self.key("arm3c"))
        self.assertEqual(paused.json()["rule"]["status"], "paused")
        self.assertIn("I am watching this one", paused.json()["rule"]["pausedReason"])

    def test_a_preview_of_a_draft_needs_no_rule_and_saves_nothing(self):
        before = len(self.rules()["rules"])
        draft = self.payload(name="never saved")
        draft.pop("targets", None)
        r = self.client.post("/v1/automations/preview",
                             json={**draft, "targets": [{"marketId": self.market}]}, headers=self.key("draft"))
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["dryRun"])
        self.assertIn("simulation", r.json())
        self.assertEqual(len(self.rules()["rules"]), before, "a preview is not a save")

    def test_the_run_history_holds_every_evaluation_with_its_reason(self):
        rule_id = self.create("history").json()["rule"]["ruleId"]
        # A time trigger an hour out: the tick skips, and the skip is a row. Then one dry run that fires.
        self.client.post("/v1/automations/preview", json={"ruleId": rule_id}, headers=self.key("hist-a"))
        rows = self.client.get("/v1/automations/runs", params={"ruleId": rule_id}, headers=USER).json()["rows"]
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "dry_run")
        self.assertIn(rows[0]["outcome"], ("would_place", "skipped"))
        self.assertTrue(rows[0]["sentence"], "every row has a sentence: that is the answer to why")
        self.assertTrue(rows[0]["leaves"], "the trigger's leaves travel with the row")
        # Somebody else's rule is not readable, and the refusal is a 404 rather than an empty list.
        other = self.client.get("/v1/automations/runs", params={"ruleId": rule_id}, headers={"X-User-Id": "u-other"})
        self.assertEqual(404, other.status_code, other.text)

    def test_another_account_cannot_pause_this_rule(self):
        rule_id = self.create("owner").json()["rule"]["ruleId"]
        r = self.client.post("/v1/automations/guards", json={"ruleId": rule_id, "state": "paused"},
                             headers={"X-User-Id": "u-other", "Idempotency-Key": "auto-owner-other"})
        self.assertEqual(404, r.status_code, r.text)

    # ------------------------------------------------------------------ the templates and their arithmetic

    def test_the_five_minute_crypto_entry_is_shown_with_the_arithmetic_that_withholds_it(self):
        r = self.client.get("/v1/automations/templates", params={"feeType": "taker", "feeRateBps": 200,
                                                                 "edgeAvailableBp": 12, "latencyMs": 900},
                            headers=USER)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        entry = next(t for t in body["templates"] if t["templateId"] == "entry-momentum-5m")
        self.assertFalse(entry["available"], "a measured edge under the hurdle must withhold the entry")
        self.assertTrue(entry["why"], "a withheld template still says why")
        self.assertIsNotNone(entry["feeArithmetic"])
        self.assertGreater(entry["feeArithmetic"]["edgeNeededBp"], entry["feeArithmetic"]["edgeAvailableBp"])
        self.assertTrue(any(t["available"] for t in body["templates"]),
                        "the protective half ships whatever the fee maths says")

    def test_the_entry_template_ships_when_the_measured_edge_clears_the_hurdle(self):
        # `feeRateBps: 0` does NOT mean free: the engine refuses to ship an entry rule on a market whose fee
        # schedule says a fee exists and whose rate is zero, because that is a missing number rather than a
        # measured one. A real rate, a maker rebate and a measured edge is the configuration that clears.
        r = self.client.get("/v1/automations/templates",
                            params={"feeType": "maker", "feeRateBps": 50, "edgeAvailableBp": 1500,
                                    "latencyMs": 500},
                            headers=USER)
        body = r.json()
        entry = next(t for t in body["templates"] if t["templateId"] == "entry-momentum-5m")
        self.assertTrue(body["ships"])
        self.assertTrue(entry["available"])
        self.assertIn("clear", entry["why"])
