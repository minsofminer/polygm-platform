"""P15 D3 — the drill that proves the drain guard can refuse, and the one that proves the ledger notices an edit.

Two things a deploy must never get wrong, and neither is provable by reading the code:

* the drain guard's **fail-closed** direction. A guard that returns "safe" when it cannot reach the API is worse
  than no guard, because the deploy script will happily replace the one process that may be mid-order. So the
  guard is driven here against a real (local, stub) HTTP server in all four states: in-flight, cleared, refused,
  unreachable — and the exit code is the assertion.

* the deploy ledger's **tamper-evidence**. A diary that nobody checks is a diary; the ledger is hash-chained and
  each entry's commit has to exist in git. The test edits one character of one field and asserts `verify` fails
  and names the entry.

The stub server is deliberately the *wire* rather than a mocked function: the guard's exit code is a property of
how it reacts to an HTTP status, a timeout, and a body it did not expect.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load("p15_guard", "tools/p15-drain-guard.py")
audit = _load("p15_audit", "tools/p15-deploy-audit.py")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Stub:
    """A tiny admin API. `script` is the sequence of drain-status bodies; the last one repeats."""

    def __init__(self, script: list[tuple[int, dict]]):
        self.script = script
        self.calls = 0
        self.posts: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # the test's output is the assertion, not the server's
                pass

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                if self.path != "/v1/admin/drain-status":
                    return self._send(404, {"error": {"code": "NOT_FOUND"}})
                stub.calls += 1
                code, body = stub.script[min(stub.calls - 1, len(stub.script) - 1)]
                self._send(code, body)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                stub.posts.append(json.loads(self.rfile.read(n) or b"{}"))
                self._send(200, {"name": "executor_draining", "value": True, "atMs": 1, "reason": "x"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def run_guard(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = guard.main(argv)
    return rc, buf.getvalue()


def run_audit(argv: list[str]) -> tuple[int, str]:
    """Both streams: a refusal that a human reads may be written to either, and the test asserts on the words."""
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = audit.main(argv)
    return rc, buf.getvalue()


class TestDrainGuard(unittest.TestCase):
    def guard_against(self, stub: Stub, *extra: str) -> tuple[int, str]:
        self.addCleanup(stub.close)
        return run_guard(["--api", stub.url, "--token", "t", "--timeout", "0.4", "--interval", "0.05", *extra])

    def test_an_in_flight_order_blocks_the_replace_and_the_guard_says_so(self):
        """`submitting`/`uncertain` are the states where the process may be mid-conversation with the venue."""
        stub = Stub([(200, {"blockingIntents": 2, "openIntents": 2,
                            "blockingStates": ["submitting", "uncertain"], "draining": True,
                            "safeToReplace": False})])
        rc, out = self.guard_against(stub)
        self.assertEqual(guard.UNSAFE_TIMEOUT, rc, out)
        self.assertIn("NOT replacing", out)
        self.assertIn("reconciliation", out)
        self.assertGreaterEqual(stub.calls, 2, "a single poll is not a wait: the guard must keep asking")

    def test_the_guard_waits_and_goes_green_when_the_intent_resolves(self):
        """The common case: the ambiguity resolves in seconds and the deploy proceeds without a human."""
        stub = Stub([(200, {"blockingIntents": 1, "openIntents": 1, "blockingStates": ["submitting"]}),
                     (200, {"blockingIntents": 0, "openIntents": 0, "blockingStates": []})])
        rc, out = self.guard_against(stub)
        self.assertEqual(guard.SAFE, rc, out)
        self.assertIn("safe", out)

    def test_a_queued_intent_is_reported_but_does_not_block(self):
        """The queue is durable rows in Postgres; the next executor drains it. Blocking on it would mean never
        deploying again during a busy market."""
        stub = Stub([(200, {"blockingIntents": 0, "openIntents": 12, "blockingStates": []})])
        rc, out = self.guard_against(stub)
        self.assertEqual(guard.SAFE, rc, out)
        self.assertIn("12 open intent(s) queued", out)

    def test_an_unreachable_api_is_fail_closed(self):
        rc, out = run_guard(["--api", "http://127.0.0.1:%d" % free_port(), "--token", "t",
                             "--timeout", "0.4", "--interval", "0.05"])
        self.assertEqual(guard.CANNOT_TELL, rc, out)
        self.assertIn("cannot establish safety", out)

    def test_a_refused_admin_call_is_fail_closed(self):
        """A wrong token is an operator error, and the direction of the error matters: refuse to deploy."""
        stub = Stub([(403, {"error": {"code": "ADMIN_REQUIRED"}})])
        rc, out = self.guard_against(stub)
        self.assertEqual(guard.CANNOT_TELL, rc, out)
        self.assertIn("refused", out)

    def test_a_misconfigured_box_is_fail_closed(self):
        """503 SIGNER_UNAVAILABLE (no admin token configured) must not be read as a green light."""
        stub = Stub([(503, {"error": {"code": "SIGNER_UNAVAILABLE"}})])
        rc, out = self.guard_against(stub)
        self.assertEqual(guard.CANNOT_TELL, rc, out)

    def test_setting_and_clearing_draining_is_a_recorded_flag_change(self):
        """The flag the whole fleet reads, written through the audit-carrying route, with a reason a reviewer can
        read at 2am. The guard does not write the flag table directly and must not learn how."""
        stub = Stub([(200, {"blockingIntents": 0, "openIntents": 0, "blockingStates": []})])
        rc, out = self.guard_against(stub, "--set-draining", "--clear-draining", "--reason", "deploy 1.4.2")
        self.assertEqual(guard.SAFE, rc, out)
        self.assertEqual(["executor_draining", "executor_draining"], [p["name"] for p in stub.posts])
        self.assertEqual([True, False], [p["value"] for p in stub.posts])
        self.assertTrue(all(p["reason"].startswith("deploy 1.4.2") for p in stub.posts), stub.posts)
        self.assertGreaterEqual(len(stub.posts[0]["reason"]), 4)


class TestDeployAudit(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.ledger = self.dir / "audit.jsonl"

    def begin(self, env="staging", reason="drill") -> str:
        rc, out = run_audit(["begin", "--env", env, "--actor", "tester", "--reason", reason,
                             "--ledger", str(self.ledger)])
        self.assertEqual(0, rc, out)
        return out.strip()

    def test_a_deploy_records_who_what_when_and_which_commit(self):
        eid = self.begin()
        entry = json.loads(self.ledger.read_text().splitlines()[0])
        self.assertEqual("begin", entry["event"])
        self.assertEqual("tester", entry["actor"])
        self.assertEqual(guard.GENESIS if hasattr(guard, "GENESIS") else "0" * 64, entry["prev_hash"])
        self.assertRegex(entry["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(entry["tree"], r"^[0-9a-f]{40}$")
        self.assertEqual(eid, entry["id"])

    def test_the_ledger_chains_and_verifies(self):
        eid = self.begin()
        rc, out = run_audit(["finish", "--id", eid, "--result", "ok", "--note", "smoke green",
                             "--ledger", str(self.ledger)])
        self.assertEqual(0, rc, out)
        entries = [json.loads(l) for l in self.ledger.read_text().splitlines()]
        self.assertEqual(2, len(entries))
        self.assertEqual(entries[0]["hash"], entries[1]["prev_hash"], "the chain must link the two entries")
        rc, out = run_audit(["verify", "--ledger", str(self.ledger)])
        self.assertEqual(0, rc, out)
        self.assertIn("Verdict: PASS", out)

    def test_one_edited_field_is_a_failed_verification(self):
        """The property that makes it an audit rather than a diary."""
        eid = self.begin()
        run_audit(["finish", "--id", eid, "--result", "ok", "--note", "fine", "--ledger", str(self.ledger)])
        lines = self.ledger.read_text().splitlines()
        lines[0] = lines[0].replace('"actor": "tester"', '"actor": "someone else"')
        self.ledger.write_text("\n".join(lines) + "\n")
        rc, out = run_audit(["verify", "--ledger", str(self.ledger)])
        self.assertEqual(1, rc, out)
        self.assertIn("does not match its content", out)

    def test_a_deleted_entry_breaks_the_chain(self):
        a = self.begin(env="staging")
        run_audit(["finish", "--id", a, "--result", "ok", "--ledger", str(self.ledger)])
        b = self.begin(env="prod")
        run_audit(["finish", "--id", b, "--result", "ok", "--ledger", str(self.ledger)])
        lines = self.ledger.read_text().splitlines()
        self.ledger.write_text("\n".join([lines[0]] + lines[2:]) + "\n")   # drop the first finish
        rc, out = run_audit(["verify", "--ledger", str(self.ledger)])
        self.assertEqual(1, rc, out)
        self.assertIn("prev_hash does not match", out)

    def test_an_entry_claiming_a_commit_this_repository_does_not_have_fails(self):
        self.begin()
        lines = self.ledger.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["commit"] = "dead" * 10
        entry.pop("hash")
        entry["hash"] = audit.digest_of(entry)
        self.ledger.write_text(json.dumps(entry, sort_keys=True) + "\n")
        rc, out = run_audit(["verify", "--ledger", str(self.ledger)])
        self.assertEqual(1, rc, out)
        self.assertIn("is not in this repository", out)

    def test_finishing_a_deploy_that_never_began_is_refused(self):
        rc, out = run_audit(["finish", "--id", "20260923T000000Z-prod", "--result", "ok",
                             "--ledger", str(self.ledger)])
        self.assertEqual(1, rc, out)
        self.assertIn("no begin entry", out)

    def scratch_repo(self, commit: bool) -> pathlib.Path:
        """A checkout of its own. `PGM_DEPLOY_REPO` is how the drill and these two tests point the tool at one:
        a real deploy asks git about the tree it was launched from, and that is the only tree this tool should
        ever record."""
        repo = self.dir / ("born" if commit else "unborn")
        repo.mkdir()
        self.run_git(repo, "init", "-q")
        if commit:
            (repo / "README.md").write_text("scratch\n")
            self.run_git(repo, "add", "-A")
            self.run_git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
        return repo

    def run_git(self, repo: pathlib.Path, *args: str) -> None:
        p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=False)
        self.assertEqual(0, p.returncode, "%s failed: %s" % (" ".join(args), p.stderr))

    def test_an_unborn_repository_cannot_open_a_deploy_entry(self):
        """The state of a box where the deploy script runs by hand for the first time: `git init`, no commit.

        An entry with an empty commit is worse than no entry — the ledger would look complete and would name
        nothing to roll back to. So `begin` refuses, exits 2 (a refusal, distinct from a failed verification),
        and leaves the ledger untouched."""
        repo = self.scratch_repo(commit=False)
        with mock.patch.dict(os.environ, {"PGM_DEPLOY_REPO": str(repo)}):
            rc, out = run_audit(["begin", "--env", "staging", "--actor", "tester",
                                 "--ledger", str(self.ledger)])
        self.assertEqual(audit.UNBORN, rc, out)
        self.assertIn("HEAD is unborn", out)
        self.assertFalse(self.ledger.exists(), "a refused deploy must leave no entry behind")

    def test_verify_fails_closed_when_no_commit_can_be_checked(self):
        """A ledger written in the real repository, then verified against one that cannot resolve any commit.

        Every per-entry commit check would pass vacuously there. The verdict must be FAIL: a verification that
        cannot check the thing it exists to check is not a pass."""
        eid = self.begin()
        run_audit(["finish", "--id", eid, "--result", "ok", "--ledger", str(self.ledger)])
        repo = self.scratch_repo(commit=False)
        with mock.patch.dict(os.environ, {"PGM_DEPLOY_REPO": str(repo)}):
            rc, out = run_audit(["verify", "--ledger", str(self.ledger)])
        self.assertEqual(1, rc, out)
        self.assertIn("no commit can be verified", out)
        self.assertIn("Verdict: FAIL", out)

    def test_both_refusals_stand_down_in_a_repository_that_has_a_commit(self):
        """The control: the same two commands against a checkout with one commit. `begin` records a real revision
        and `verify` can check it, so the refusals above are about the unborn HEAD and not about scratch repos."""
        repo = self.scratch_repo(commit=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True,
                              text=True).stdout.strip()
        scratch = self.dir / "scratch.jsonl"      # its own ledger: a chain of entries written in one repository
        with mock.patch.dict(os.environ, {"PGM_DEPLOY_REPO": str(repo)}):
            rc, out = run_audit(["begin", "--env", "staging", "--ledger", str(scratch)])
            self.assertEqual(0, rc, out)
            eid = out.strip()
            entry = json.loads(scratch.read_text().splitlines()[0])
            self.assertEqual(head, entry["commit"], "the entry names the revision of the repository it recorded in")
            rc, out = run_audit(["finish", "--id", eid, "--result", "ok", "--note", "control",
                                 "--ledger", str(scratch)])
            self.assertEqual(0, rc, out)
            rc, out = run_audit(["verify", "--ledger", str(scratch)])
        self.assertEqual(0, rc, out)
        self.assertIn("Verdict: PASS", out)


if __name__ == "__main__":
    unittest.main()
