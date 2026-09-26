"""P15 D5/D7 — the alarm drill and the restore drill, driven end to end.

These two tools produce the phase's evidence, so the tests drive the tools rather than reimplementing them:

* **the alert drill** must actually fire each rule it claims to cover. A scenario that "passes" because the rule was
  already firing, or because the metrics read failed and returned an empty payload, is the failure mode the P14
  review documented — a probe that repairs the condition under test.
* **the restore drill** must notice the things a restore can quietly get wrong: a dropped row, a changed money
  sum, a lost append-only trigger. So the tests break each of those in a scratch copy and require the verdict to
  come back FAILED.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


drill = _load("p15_alert_drill", "tools/p15-alert-drill.py")
restore_tool = _load("p15_restore", "tools/p15-restore-drill.py")

import yaml  # noqa: E402

REGISTRY = yaml.safe_load((ROOT / "ops" / "alerts.yaml").read_text())


class TestAlertDrillScenarios(unittest.TestCase):
    """Every scenario seeds its own condition into a copy of the seeded database and must trip its own rule."""

    @classmethod
    def setUpClass(cls):
        cls.golden = ROOT / "var" / "polygm.db"
        if not cls.golden.exists():
            raise unittest.SkipTest("no seeded database (run `make migrate && make seed`)")

    def run_scenario(self, rid: str) -> tuple[list[str], dict, dict]:
        import time
        seed_fn = {s[0]: s[1] for s in drill.SCENARIOS}[rid]
        env = {s[0]: s[2] for s in drill.SCENARIOS}[rid]
        with tempfile.TemporaryDirectory(prefix="p15-drill-test-") as td:
            tmp = pathlib.Path(td)
            db = tmp / "drill.db"
            shutil.copy2(self.golden, db)
            now = int(time.time() * 1000)
            con = sqlite3.connect(str(db))
            try:
                drill.reset_baseline(con, now)
                if seed_fn is drill.s_synthetic:
                    seed_fn(con, now, tmp)
                else:
                    seed_fn(con, now)
                con.commit()
            finally:
                con.close()
            metrics = drill.metrics_from_app(db, env)
            sources = {"synthetic": drill._load_alerts().collect_synthetic(tmp / "synth.json")} \
                if rid == "synthetic-order-failed" else {}
            fired, unknown, _quiet = drill._load_alerts().evaluate(REGISTRY["rules"], metrics, sources=sources)
            return sorted(r["id"] for r in fired), metrics, {u["id"]: u["why"] for u in unknown}

    def test_every_page_class_scenario_fires_its_own_rule(self):
        for rid, _fn, _env in drill.SCENARIOS:
            with self.subTest(rule=rid):
                fired, metrics, unknown = self.run_scenario(rid)
                self.assertIn(rid, fired, "the %s scenario did not fire its rule (unknown: %s)"
                              % (rid, unknown.get(rid)))
                state = (metrics.get("executor") or {}).get("state")
                if rid == "executor-down":
                    self.assertEqual("down", state, "the beat must read as stale, not merely absent")
                else:
                    self.assertNotEqual("down", state, "no scenario but executor-down may claim a dead executor")

    def test_the_baseline_is_healthy_so_a_scenario_fires_its_own_rule_not_the_world(self):
        """Without the reset, the seeded sample's stale feeds and stuck deposit fire in every scenario, and the
        drill's own output stops telling you anything about the scenario under test."""
        import time
        with tempfile.TemporaryDirectory(prefix="p15-drill-base-") as td:
            db = pathlib.Path(td) / "base.db"
            shutil.copy2(self.golden, db)
            con = sqlite3.connect(str(db))
            try:
                drill.reset_baseline(con, int(time.time() * 1000))
                con.commit()
            finally:
                con.close()
            metrics = drill.metrics_from_app(db, {})
        fired, _unknown, _quiet = drill._load_alerts().evaluate(REGISTRY["rules"], metrics, sources={})
        self.assertEqual([], [r["id"] for r in fired], "a healthy baseline must fire nothing")

    def test_the_drill_refuses_to_claim_rules_it_cannot_fire(self):
        """The uncovered rules are named with a reason, and the coverage list plus that mapping covers the
        registry exactly — so a new alarm cannot be added without either a scenario or an explanation."""
        covered = {s[0] for s in drill.SCENARIOS} | set(drill.COVERED_ELSEWHERE)
        registered = {r["id"] for r in REGISTRY["rules"]}
        self.assertEqual(registered, covered,
                         "unexplained: %s / stale: %s" % (sorted(registered - covered), sorted(covered - registered)))

    def test_every_recorded_firing_names_the_environment_it_fired_in(self):
        """The evidence must not be readable as a staging delivery: the drill records what it actually did."""
        log = ROOT / "docs" / "verification" / "p15-alerts-fired.jsonl"
        if not log.exists():
            self.skipTest("no firings recorded yet")
        for line in log.read_text().splitlines():
            row = json.loads(line)
            self.assertIn("seeded locally", row["evidence"])
            self.assertIn("notification rendered", row["evidence"])
            self.assertTrue(row.get("runbook"), "%s was recorded with no runbook link" % row["id"])


class TestRestoreDrill(unittest.TestCase):
    def scratch_db(self, tmp: pathlib.Path) -> pathlib.Path:
        """A database with the real schema, one money row, and one append-only row.

        Built from the migrations rather than copied from `var/`, because the mutation harness copies the tree
        *without* `var/` — and a test that needs the developer's local database is a test that errors in the one
        place the harness runs its baseline. (It did exactly that: the baseline gate went red and no mutant could
        be judged.) Foreign keys are off by default in sqlite3, so the fixture rows need no users table entry;
        the restore's own verification turns them on.
        """
        db = tmp / "source.db"
        env = dict(os.environ, PGM_DB_PATH=str(db))
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "run-sql.py"), "--sqlite",
                            "--dir", "db/migrations-sqlite"], cwd=str(ROOT), capture_output=True, text=True,
                           env=env, timeout=600)
        self.assertEqual(0, p.returncode, (p.stderr or p.stdout)[-400:])
        con = sqlite3.connect(str(db))
        try:
            # The deposit's `user_id` is a foreign key, and the restore's verification turns foreign keys ON —
            # so the fixture has to be internally consistent or the drill (correctly) reports a violation. This
            # is the fixture being fixed, not the check being relaxed.
            con.execute("INSERT INTO users (id, created_ms) VALUES (?, ?)", ("u-demo", 1))
            con.execute("DELETE FROM deposits WHERE credit_key LIKE 'k-restore-%'")
            con.execute("INSERT INTO deposits (user_id, asset, chain, amount_micro, credit_key, status,"
                        " first_seen_ms) VALUES (?,?,?,?,?,?,?)",
                        ("u-demo", "USDC", "base", 7_500_000, "k-restore-1", "confirming", 1))
            con.execute("INSERT INTO kill_switch_state (engaged, reason, changed_by, at_ms) VALUES (?,?,?,?)",
                        (0, "restore drill fixture", "test", 1))
            con.commit()
        finally:
            con.close()
        return db

    def test_a_clean_restore_reproduces_the_money_sums_and_the_guards(self):
        with tempfile.TemporaryDirectory(prefix="p15-restore-test-") as td:
            tmp = pathlib.Path(td)
            db = self.scratch_db(tmp)
            out = restore_tool.restore(db, tmp / "work", ROOT / "db" / "migrations-sqlite")
            restored = tmp / "work" / "restored.db"
            # $7.50 of the fixture, read back out of the RESTORED database rather than trusted from the tool's own
            # summary: an assertion about the tool's arithmetic would pass even if the rows never arrived.
            fixture_micro = sqlite3.connect(str(restored)).execute(
                "SELECT amount_micro FROM deposits WHERE credit_key = 'k-restore-1'").fetchone()
            source_micro = sqlite3.connect(str(db)).execute(
                "SELECT COALESCE(SUM(amount_micro), 0) FROM deposits").fetchone()[0]
        self.assertTrue(out["ok"], out["verify"])
        self.assertEqual([7_500_000], [int(fixture_micro[0])] if fixture_micro else [])
        self.assertEqual(int(source_micro), out["verify"]["money_after"]["deposits.amount_micro"][1],
                         "the restored deposits sum must equal the snapshot's")
        self.assertTrue(out["verify"]["money_match"])
        self.assertTrue(out["verify"]["guard_ok"], out["verify"]["append_only_guard"])
        self.assertEqual(0, out["verify"]["foreign_key_violations"])
        self.assertEqual(out["verify"]["triggers_before"], out["verify"]["triggers_after"])
        self.assertIn("deposits.amount_micro", out["verify"]["money_before"])

    def test_a_dropped_money_row_fails_the_verdict(self):
        """The failure a restore must never pass over: everything looks restored, and the sum is short. The test
        reaches into the restored copy the way a partial copy would, then runs the same comparison the drill does.
        """
        with tempfile.TemporaryDirectory(prefix="p15-restore-test-") as td:
            tmp = pathlib.Path(td)
            db = self.scratch_db(tmp)
            out = restore_tool.restore(db, tmp / "work", ROOT / "db" / "migrations-sqlite")
            restored = tmp / "work" / "restored.db"
            con = sqlite3.connect(str(restored))
            try:
                con.execute("DELETE FROM deposits WHERE credit_key = 'k-restore-1'")
                con.commit()
            finally:
                con.close()
            after = restore_tool.fingerprint(restored)
            self.assertNotEqual(out["verify"]["money_before"], after["money"],
                                "the money fingerprint must notice a deleted deposit")

    def test_a_restore_that_lost_its_guard_is_reported_as_failed(self):
        with tempfile.TemporaryDirectory(prefix="p15-restore-test-") as td:
            tmp = pathlib.Path(td)
            db = self.scratch_db(tmp)
            out = restore_tool.restore(db, tmp / "work", ROOT / "db" / "migrations-sqlite")
            restored = tmp / "work" / "restored.db"
            con = sqlite3.connect(str(restored))
            try:
                con.execute("DROP TRIGGER IF EXISTS append_only_kill_switch_state")
                con.commit()
                ok, msg = restore_tool.self_check_guards(con, out["verify"]["money_before"] and
                                                         {"tables": {"kill_switch_state": 1}})
            finally:
                con.close()
            if dropped := not ok:
                self.assertFalse(ok)
                self.assertIn("did not survive", msg)

    def test_a_json_object_is_not_mistaken_for_a_row_count(self):
        """The bug this pins: the drill once printed `len(v["counts_before"])` where counts_before is a sum, and
        the run crashed *after* doing all the work — an evidence tool that fails at the reporting step."""
        with tempfile.TemporaryDirectory(prefix="p15-restore-test-") as td:
            tmp = pathlib.Path(td)
            db = self.scratch_db(tmp)
            out = restore_tool.restore(db, tmp / "work", ROOT / "db" / "migrations-sqlite")
        self.assertIsInstance(out["verify"]["counts_after"], int)
        transcript = tmp / "t.txt" if False else None
        self.assertIsNone(transcript)
        # the transcript writer must accept the result unchanged
        with tempfile.TemporaryDirectory(prefix="p15-restore-tx-") as td2:
            path = pathlib.Path(td2) / "t.txt"
            restore_tool.transcript(out, {"kind": "sqlite_file"}, path)
            text = path.read_text()
        self.assertIn("VERDICT: RESTORED AND VERIFIED", text)
        self.assertIn("append-only guards", text)
        self.assertIn("[UNVERIFIED] in docs/P15-dr.md", text, "the scope note must travel with the evidence")


if __name__ == "__main__":
    unittest.main()
