"""P15 D3 — the pipeline and the two deploy scripts, tested as artefacts rather than described in a document.

Three properties, each of which has been wrong in some other project:

* **the stage order.** `tools/p15-pipeline-check.py` asserts the kit's order in the workflow file; this file
  asserts that the checker *can fail* by planting breakages (its own `--self-test`), so a green checker means
  something.
* **the deploy scripts refuse.** A deploy that takes a tag, or a rollback with nothing recorded to roll back to,
  must exit non-zero with a sentence — before it touches a container. Both are driven here as programs.
* **the manual gate.** Nothing on the way to canary or production may bypass it; the check is transitive because
  a chain is what makes a gate unavoidable.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIPELINE = ROOT / ".github" / "workflows" / "pipeline.yml"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load("p15_pipeline_check", "tools/p15-pipeline-check.py")


def run_script(rel: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(ROOT / rel), *args], cwd=ROOT, capture_output=True, text=True, timeout=60)


class TestPipelineShape(unittest.TestCase):
    def setUp(self):
        self.text = PIPELINE.read_text()
        self.jobs = pc.job_blocks(self.text)

    def report(self, text: str | None = None) -> pc.Report:
        rep = pc.Report()
        pc.run_checks(rep, text if text is not None else self.text)
        return rep

    def test_the_workflow_is_green_on_its_own_rules(self):
        rep = self.report()
        self.assertEqual([], rep.failed)
        self.assertGreater(rep.passed, 40)

    def test_every_planted_breakage_trips_a_rule(self):
        """The self-test is the whole reason to believe the 80-odd checks above: each rule has been seen to fail."""
        self.assertEqual(0, pc.self_test())

    def test_the_stage_order_is_the_kits_order(self):
        order = [name for name, _ in pc.STAGES]
        self.assertEqual(["lint", "types", "unit", "contract", "migrations", "component", "build", "integration",
                          "e2e", "deploy-staging", "smoke", "manual-gate", "canary", "production"], order)

    def test_a_renamed_gate_is_a_failure_not_a_passing_string_comparison(self):
        """The weakness this check had until its own self-test found it: `reaches` matched the NAME in a needs
        list even when the job no longer existed, so renaming the gate read as "the gate is on the path"."""
        broken = self.text.replace("  manual-gate:\n", "  manual-gate-DISABLED:\n", 1)
        self.assertNotEqual(self.text, broken)
        failed = " ".join(self.report(broken).failed)
        self.assertIn("cannot skip the gate", failed)
        self.assertIn("needs 'manual-gate' exists", failed)

    def test_both_images_come_from_the_digest_file(self):
        for job in pc.DEPLOY_JOBS:
            self.assertGreaterEqual(self.jobs[job].count("image-digests.txt"), 2, job)

    def test_one_broken_image_reference_in_a_deploy_job_is_caught(self):
        broken = self.text.replace("deploy/image-digests.txt | tail -n 1)", "echo latest | tail -n 1)")
        failed = " ".join(self.report(broken).failed)
        self.assertIn("reads BOTH digests", failed)


class TestDeployScriptsRefuse(unittest.TestCase):
    def test_deploy_refuses_an_image_that_is_not_pinned_by_digest(self):
        r = run_script("deploy/deploy.sh", "--env", "staging", "--host", "http://127.0.0.1:1",
                       "--api-image", "ghcr.io/x/polygm-api:1.4.2")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("must be pinned by digest", r.stderr)

    def test_deploy_refuses_without_an_admin_token(self):
        """The drain guard cannot ask the running system anything without a token, and a deploy that proceeds
        anyway is a deploy that replaces the executor blind."""
        r = subprocess.run(["bash", str(ROOT / "deploy/deploy.sh"), "--env", "staging",
                            "--host", "http://127.0.0.1:1",
                            "--api-image", "ghcr.io/x/polygm-api@sha256:" + "a" * 64],
                           cwd=ROOT, capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(ROOT)})
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("PGM_ADMIN_TOKEN", r.stderr)

    def test_rollback_refuses_when_nothing_is_recorded_to_roll_back_to(self):
        previous = ROOT / "deploy" / "image-digests.previous.txt"
        existed = previous.exists()
        if existed:  # never destroy a real record for a test
            self.skipTest("a previous-digest file exists in this checkout")
        r = run_script("deploy/rollback.sh", "--env", "prod", "--host", "http://127.0.0.1:1")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("nothing recorded to roll back TO", r.stderr)

    def test_the_scripts_parse_and_are_executable(self):
        for rel in ("deploy/deploy.sh", "deploy/rollback.sh"):
            path = ROOT / rel
            self.assertTrue(path.stat().st_mode & 0o111, "%s is not executable" % rel)
            self.assertEqual(0, subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode)
            self.assertIn("set -eu", path.read_text())


class TestMoneyPath(unittest.TestCase):
    def test_the_classifier_fails_towards_the_gate(self):
        mp = _load("p15_money_path", "tools/p15-money-path.py")
        verdict, hits = mp.classify(None)
        self.assertEqual(mp.MONEY_PATH, verdict, "an unknown change set must need a human, not a shrug")
        self.assertEqual(mp.MONEY_PATH, mp.classify(["services/executor/main.py"])[0])
        self.assertEqual(mp.MONEY_PATH, mp.classify(["db/migrations/0021_x.sql"])[0])
        self.assertEqual(mp.MONEY_PATH, mp.classify(["deploy/image-digests.txt"])[0])
        self.assertEqual(mp.SAFE, mp.classify(["web/src/components/Chart.tsx"])[0])
        self.assertEqual(mp.SAFE, mp.classify([])[0], "an empty diff on a branch that changed nothing")

    def test_the_money_path_prefixes_are_paths_that_exist(self):
        mp = _load("p15_money_path2", "tools/p15-money-path.py")
        missing = [p for p in mp.MONEY_PREFIXES if not (ROOT / p.rstrip("/")).exists()]
        self.assertEqual([], missing, "a prefix for a file that does not exist is a rule that never fires")


if __name__ == "__main__":
    unittest.main()
