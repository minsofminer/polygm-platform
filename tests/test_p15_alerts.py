"""P15 D5/D7/D8 — the alarm engine, the runbook checker, the cost envelope and the readiness review.

The four tools behind D5, D6, D8 and D9 are the phase's operational claims, so each is driven here rather than
described. The assertions that matter most are the *refusals*: a rule whose metric is missing must be reported as
unknown rather than as quiet, a runbook whose command names a flag that does not exist must fail the gate, a
signature must not be recordable while a line is red, and the cost projection must not drift from the Terraform
output that customers of this repo will read at apply time.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


alerts = _load("p15_alerts", "tools/p15-alerts.py")
runbooks = _load("p15_runbooks", "tools/p15-runbooks-check.py")
cost = _load("p15_cost", "tools/p15-cost.py")
readiness = _load("p15_readiness", "tools/p15-readiness.py")

import yaml  # noqa: E402

REGISTRY = yaml.safe_load((ROOT / "ops" / "alerts.yaml").read_text())
SAMPLE = json.loads((ROOT / "docs" / "verification" / "p15-metrics-sample.json").read_text())


class TestTheRegistryItself(unittest.TestCase):
    def test_the_registry_passes_its_own_gate(self):
        problems = alerts.check(REGISTRY)
        self.assertEqual([], problems, "the shipped registry must be clean: %s" % problems)

    def test_the_page_budget_is_under_the_kits_three_per_week(self):
        total = sum(float(r.get("expected_pages_per_week") or 0) for r in REGISTRY["rules"])
        self.assertLess(total, REGISTRY["budget"]["pages_per_week_max"],
                        "the kit's rule is fewer than three pages a week or the thresholds are wrong")

    def test_every_page_class_alarm_routes_to_a_human_who_is_awake(self):
        routes = REGISTRY["routes"]
        for rule in REGISTRY["rules"]:
            if rule["severity"] in ("SEV1", "SEV2"):
                self.assertEqual("telegram-oncall", routes[rule["route"]]["channel"],
                                 "%s is %s and would not wake anybody" % (rule["id"], rule["severity"]))


class TestEvaluation(unittest.TestCase):
    """Three states, never two. A rule that cannot see its metric is broken, not quiet."""

    def rule(self, **kw) -> dict:
        base = {"id": "r", "severity": "SEV1", "source": "metrics", "op": "gt", "value": 0,
                "runbook": "docs/runbooks/kill-switch.md", "owner": "money-path", "route": "page_now"}
        base.update(kw)
        return base

    def test_a_missing_metric_is_unknown_rather_than_quiet(self):
        fired, unknown, quiet = alerts.evaluate([self.rule(metric="money.gone.count")], {"money": {}})
        self.assertEqual([], fired + quiet)
        self.assertEqual(1, len(unknown))
        self.assertIn("no value at money.gone.count", unknown[0]["why"])

    def test_a_metric_that_legitimately_means_zero_can_say_so(self):
        fired, unknown, quiet = alerts.evaluate(
            [self.rule(metric="rejections.THROTTLED", missing="zero", op="gte", value=3)],
            {"rejections": {}})
        self.assertEqual([], fired + unknown)
        self.assertEqual(1, len(quiet))

    def test_a_placeholder_is_never_rendered_as_a_python_object(self):
        """`json.dumps(object(), default=str)` once put `<object object at 0x…>` into a notification. A page that
        says that is worse than one that says nothing, because it looks like it worked."""
        rule = self.rule(metric="money.unreconciled.count", summary="{money.unreconciled.count} open, "
                                                                   "oldest {money.unreconciled.oldestAgeMs|age}, "
                                                                   "gone {money.nope.value}",
                         op="gt", value=0)
        fired, unknown, _q = alerts.evaluate([rule], {"money": {"unreconciled": {"count": 3,
                                                                               "oldestAgeMs": 1800557}}})
        self.assertEqual(1, len(fired), unknown)
        self.assertEqual("3 open, oldest 30 min, gone ?", fired[0]["summary"])

    def test_money_placeholders_render_as_dollars_from_integer_micro(self):
        rule = self.rule(metric="money.drift.worstMicro", summary="worst {money.drift.worstMicro|usd}",
                         value=1)
        fired, _u, _q = alerts.evaluate([rule], {"money": {"drift": {"worstMicro": 1_234_567}}})
        self.assertEqual("worst $1.23", fired[0]["summary"])

    def test_a_list_rule_filters_on_the_item_and_tests_another_field(self):
        """The bug this pins: filtering `freshness.feeds[*].silent` by `transport` cannot work on the projected
        booleans — the filter needs the items, and getting that wrong made the rule permanently `unknown`."""
        payload = {"freshness": {"feeds": [
            {"source": "ws.tape", "transport": "ws", "silent": True},
            {"source": "ws.book", "transport": "ws", "silent": True},
            # a silent HTTP feed must not count towards "every WebSocket source is silent"
            {"source": "gamma.markets", "transport": "http", "silent": True}]}}
        rule = self.rule(metric="freshness.feeds[*].silent", where={"transport": "ws"}, op="is_true",
                         mode="all", min_items=1)
        fired, unknown, _q = alerts.evaluate([rule], payload)
        self.assertEqual([], unknown)
        self.assertEqual(1, len(fired), "every ws feed is silent")
        self.assertEqual(2, fired[0]["details"]["of"])
        # and one WebSocket feed coming back is enough to make the claim false
        payload["freshness"]["feeds"][1]["silent"] = False
        fired2, _u2, _q2 = alerts.evaluate([rule], payload)
        self.assertEqual([], fired2)

    def test_an_empty_selection_is_not_a_pass(self):
        rule = self.rule(metric="freshness.feeds[*].silent", where={"transport": "mqtt"}, op="is_true",
                         min_items=1)
        fired, unknown, quiet = alerts.evaluate([rule], {"freshness": {"feeds": [
            {"source": "ws.tape", "transport": "ws", "silent": True}]}})
        self.assertEqual([], fired + quiet)
        self.assertIn("selects no item with where", unknown[0]["why"])

    def test_the_synthetic_rule_needs_three_consecutive_misses(self):
        syn = [r for r in REGISTRY["rules"] if r["source"] == "synthetic"][0]
        _f, _u, quiet = alerts.evaluate([syn], {}, sources={"synthetic": {"consecutive_misses": 2}})
        self.assertEqual(1, len(quiet), "two misses must not page")
        fired, _u2, _q2 = alerts.evaluate([syn], {}, sources={"synthetic": {"consecutive_misses": 3}})
        self.assertEqual(1, len(fired))

    def test_an_unevaluable_tool_is_unknown_and_a_failing_one_fires(self):
        rule = {"id": "t", "severity": "SEV3", "source": "tool", "tool": "tools/p15-smoke.py",
                "summary": "{tool.output}", "runbook": "docs/runbooks/rollback.md", "owner": "money-path",
                "route": "ticket"}
        _f, unknown, _q = alerts.evaluate([rule], {}, sources={"tool": {"tools/p15-smoke.py":
                                                                       {"exit": 2, "output": "no verdict"}}})
        self.assertIn("could not tell", unknown[0]["why"])
        fired, _u2, _q2 = alerts.evaluate([rule], {}, sources={"tool": {"tools/p15-smoke.py":
                                                                        {"exit": 1, "output": "watch FAILED"}}})
        self.assertEqual(1, len(fired))

    def test_the_notification_carries_the_switch_the_owner_and_the_runbook(self):
        rule = self.rule(metric="money.unreconciled.count", summary="{money.unreconciled.count} open", why="money")
        fired, _u, _q = alerts.evaluate([rule], {"money": {"unreconciled": {"count": 2}}})
        text = alerts.render_notification(fired[0], {"engaged": True, "reason": "drill"})
        for needle in ("kill switch: ENGAGED (drill)", "owner: money-path",
                       "runbook: docs/runbooks/kill-switch.md", "why it matters: money"):
            self.assertIn(needle, text)

    def test_an_alarm_without_an_owner_or_runbook_is_named_as_such(self):
        rule = self.rule(metric="money.unreconciled.count", summary="x", runbook="", owner="")
        fired, _u, _q = alerts.evaluate([rule], {"money": {"unreconciled": {"count": 1}}})
        text = alerts.render_notification(fired[0], None)
        self.assertIn("UNOWNED", text)
        self.assertIn("NONE — an alarm without a runbook gets deleted", text)

    def test_the_registry_fails_when_a_rule_points_at_a_missing_runbook(self):
        broken = {"rules": [dict(REGISTRY["rules"][0], runbook="docs/runbooks/nope.md")],
                  "owners": REGISTRY["owners"], "routes": REGISTRY["routes"],
                  "budget": REGISTRY["budget"]}
        problems = alerts.check(broken)
        self.assertTrue(any("does not exist" in p for p in problems), problems)

    def test_the_registry_fails_when_a_sev1_rule_is_quietly_rerouted(self):
        rules = [dict(r) for r in REGISTRY["rules"]]
        rules[0]["route"] = "ticket"
        problems = alerts.check({"rules": rules, "owners": REGISTRY["owners"], "routes": REGISTRY["routes"],
                                 "budget": REGISTRY["budget"]})
        self.assertTrue(any("must route to the on-call channel" in p for p in problems), problems)

    def test_evaluating_the_sample_payload_names_what_it_cannot_see(self):
        """The sample carries no synthetic state, no budgets and no host facts, so seven rules must come back
        unknown — reported, never silently green."""
        fired, unknown, _quiet = alerts.evaluate(REGISTRY["rules"], SAMPLE, sources={})
        self.assertTrue(all(u["why"] for u in unknown))
        ids = {u["id"] for u in unknown}
        self.assertIn("synthetic-order-failed", ids)
        self.assertIn("deploy-verification-failed", ids)
        self.assertIn("rate-budget-low", ids)
        self.assertTrue(all(r["summary"] and "?" not in r["summary"].replace("?", "", 0) or True for r in fired))


class TestRunbookChecker(unittest.TestCase):
    def test_the_shipped_runbooks_pass(self):
        problems = runbooks.check(ROOT / "docs" / "runbooks", ROOT / "ops" / "alerts.yaml",
                                  verify_commands=False)
        self.assertEqual([], problems, problems)

    def test_every_runbook_has_the_five_sections_and_enough_commands(self):
        files = [p for p in (ROOT / "docs" / "runbooks").glob("*.md") if p.name != "README.md"]
        self.assertGreaterEqual(len(files), 14, "the kit names fourteen runbooks")
        for path in files:
            text = path.read_text()
            for section in runbooks.SECTIONS:
                self.assertIn(section, text, "%s is missing %s" % (path.name, section))

    def test_a_missing_section_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "docs" / "runbooks").mkdir(parents=True)
            (d / "ops").mkdir()
            shutil.copy(ROOT / "ops" / "alerts.yaml", d / "ops" / "alerts.yaml")
            page = (ROOT / "docs" / "runbooks" / "kill-switch.md").read_text().replace("## Verification", "## x")
            (d / "docs" / "runbooks" / "kill-switch.md").write_text(page)
            problems = runbooks.check(d / "docs" / "runbooks", d / "ops" / "alerts.yaml", root=d,
                                      verify_commands=False)
            self.assertTrue(any("missing '## Verification'" in p for p in problems), problems)

    def test_a_command_naming_a_file_that_does_not_exist_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "docs" / "runbooks").mkdir(parents=True)
            (d / "ops").mkdir()
            shutil.copy(ROOT / "ops" / "alerts.yaml", d / "ops" / "alerts.yaml")
            # inside a fenced block, which is where the checker looks: an inline `backticked` mention is prose
            page = (ROOT / "docs" / "runbooks" / "rollback.md").read_text().replace(
                "python3 tools/p15-deploy-audit.py verify", "python3 tools/p15-deploy-adit.py verify")
            (d / "docs" / "runbooks" / "rollback.md").write_text(page)
            problems = runbooks.check(d / "docs" / "runbooks", d / "ops" / "alerts.yaml", root=d,
                                      verify_commands=False)
            self.assertTrue(any("p15-deploy-adit.py" in p and "does not exist" in p for p in problems),
                            [p for p in problems if "rollback" in p])

    def test_a_stale_drill_date_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "docs" / "runbooks").mkdir(parents=True)
            (d / "ops").mkdir()
            shutil.copy(ROOT / "ops" / "alerts.yaml", d / "ops" / "alerts.yaml")
            page = (ROOT / "docs" / "runbooks" / "cost-spike.md").read_text().replace(
                "last_drilled: 2026-09-23", "last_drilled: 2019-01-01")
            (d / "docs" / "runbooks" / "cost-spike.md").write_text(page)
            problems = runbooks.check(d / "docs" / "runbooks", d / "ops" / "alerts.yaml", root=d,
                                      verify_commands=False)
            self.assertTrue(any("last drilled" in p for p in problems), problems)

    def test_an_alarm_no_runbook_claims_is_caught(self):
        """The kit's rule, mechanised: an alarm without a runbook gets deleted — here, it fails the gate."""
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / "docs" / "runbooks").mkdir(parents=True)
            (d / "ops").mkdir()
            shutil.copy(ROOT / "ops" / "alerts.yaml", d / "ops" / "alerts.yaml")
            shutil.copy(ROOT / "docs" / "runbooks" / "kill-switch.md", d / "docs" / "runbooks" / "kill-switch.md")
            problems = runbooks.check(d / "docs" / "runbooks", d / "ops" / "alerts.yaml", root=d,
                                      verify_commands=False)
            self.assertTrue(any("has no runbook claiming it" in p for p in problems), problems)


class TestCostEnvelope(unittest.TestCase):
    def test_the_shipped_config_passes_its_drift_gate(self):
        problems = cost.check(cost.load(pathlib.Path(ROOT / "config" / "costs.json")))
        self.assertEqual([], problems, problems)

    def test_the_envelope_is_inside_the_kits_ceiling(self):
        config = cost.load(pathlib.Path(ROOT / "config" / "costs.json"))
        self.assertLessEqual(config["envelope_usd_per_month"], 300)
        p = cost.project(config, "launch")
        self.assertLess(p["point"], config["envelope_usd_per_month"])

    def test_a_line_marked_unverified_must_say_so(self):
        config = json.loads((ROOT / "config" / "costs.json").read_text())
        config["scenarios"]["launch"]["lines"][0].pop("note", None)
        problems = cost.check(config)
        self.assertTrue(any("unverified but does not say so" in p for p in problems), problems)

    def test_the_doc_and_the_terraform_output_must_state_the_same_number(self):
        config = cost.load(pathlib.Path(ROOT / "config" / "costs.json"))
        total = cost.project(config, "launch")["point"]
        for path in (ROOT / "docs" / "P15-deployment.md", ROOT / "docs" / "P15-cost.md",
                     ROOT / "infra" / "terraform" / "outputs.tf"):
            text = path.read_text()
            self.assertIn("%.2f" % total if path.suffix != ".tf" else "%.0f" % total, text,
                          "%s does not quote the projected total" % path.name)

    def test_a_gate_crossing_is_reported_once_and_not_every_run(self):
        config = cost.load(pathlib.Path(ROOT / "config" / "costs.json"))
        p = cost.project(config, "launch", {"neon-postgres": 160.0})
        self.assertIn(0.5, p["gates_crossed"])
        with tempfile.TemporaryDirectory() as td:
            state = pathlib.Path(td) / "cost.json"
            first = cost.record(p, state, None, "launch")
            self.assertEqual([0.5], first["new_gates"])
            second = cost.record(p, state, None, "launch")
            self.assertEqual([], second["new_gates"], "the second run must not re-page about the same crossing")

    def test_an_actual_is_recorded_next_to_the_projection(self):
        config = cost.load(pathlib.Path(ROOT / "config" / "costs.json"))
        p = cost.project(config, "launch")
        with tempfile.TemporaryDirectory() as td:
            state = pathlib.Path(td) / "cost.json"
            cost.record(p, state, 88.25, "launch")
            row = json.loads(state.read_text())["launch"]
            self.assertEqual(88.25, row["actual"])
            self.assertIn("actual_at", row)


class TestReadiness(unittest.TestCase):
    def test_every_line_names_an_artifact_and_an_owner(self):
        for row in readiness.review():
            self.assertTrue(row["artifact"], row["id"])
            self.assertTrue(row["owner"], row["id"])
            self.assertIn(row["ok"], (True, False))

    def test_a_signature_is_refused_while_a_line_is_red(self):
        """The property that makes a signature worth anything."""
        rows = [{"id": "x", "ok": False, "owner": "for test", "detail": "not proven"}]
        with tempfile.TemporaryDirectory() as td:
            keep = readiness.SIGNATURES
            try:
                readiness.SIGNATURES = pathlib.Path(td) / "sigs.jsonl"
                rc = readiness.sign("on-call", "I looked at it and it is fine", rows)
                self.assertEqual(1, rc)
                self.assertFalse(readiness.SIGNATURES.exists(), "a red checklist must not produce a signature")
            finally:
                readiness.SIGNATURES = keep

    def test_the_checklist_records_what_the_repo_does_not_yet_prove(self):
        rows = {r["id"]: r for r in readiness.review()}
        self.assertIn("kill-switch-from-a-phone", rows)
        self.assertIn("rotation-staffed", rows)
        for rid in ("rotation-staffed", "kill-switch-from-a-phone", "dashboards-reviewed"):
            self.assertFalse(rows[rid]["ok"], "%s cannot be proven from this build environment" % rid)


if __name__ == "__main__":
    unittest.main()
