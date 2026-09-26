#!/usr/bin/env python3
"""P15 D3 — the pipeline's shape, checked.

    python3 tools/p15-pipeline-check.py            # does .github/workflows/pipeline.yml still say what we said
    python3 tools/p15-pipeline-check.py --self-test # prove each rule fires (with a planted breakage)

A pipeline that is only read is a pipeline that drifts: somebody adds a job, moves the gate after the deploy
"just for this hotfix", or lets the executor deploy without the drain guard, and nothing in CI notices because CI
is the thing that changed. So the order the kit asks for is a list in this file, the gate's position is an
assertion, and every deploy stage has to prove it goes through the guard.

What is asserted (each with a reason in the failure message):

1. The nine stages the kit names appear in this file, and each one `needs:` the previous — the order is the graph.
2. The manual gate exists, sits after the smoke check and before canary/production, and is a GitHub *environment*
   (the only mechanism here that cannot be self-approved), not a shell `read`.
3. Both promotion stages `needs:` the gate, so there is no path from a commit to real money that skips a person.
4. Every stage that touches the money path runs `deploy/deploy.sh`, which runs `tools/p15-drain-guard.py`.
5. The build writes digests and every deploy consumes them; no stage may reference a `:latest` tag.
6. Every job has a `timeout-minutes` (a hung job on a release branch blocks the next release).
7. Secrets come from `${{ secrets.* }}` or the platform, never from a literal in the file.
8. A rollback job exists, runs on failure of production, and calls `deploy/rollback.sh`.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / ".github" / "workflows" / "pipeline.yml"

#: The kit's D3 order, verbatim: lint → types → unit → contract → component → build → integration → E2E →
#: deploy-staging → smoke → manual gate → canary → production. The migration check is ours and sits with the
#: contract stage, because a migration is a contract change that happens to be SQL.
STAGES: tuple[tuple[str, str | None], ...] = (
    ("lint", None),
    ("types", "lint"),
    ("unit", "types"),
    ("contract", "unit"),
    ("migrations", "contract"),
    ("component", "migrations"),
    ("build", "component"),
    ("integration", "build"),
    ("e2e", "integration"),
    ("deploy-staging", "e2e"),
    ("smoke", "deploy-staging"),
    ("manual-gate", "smoke"),
    ("canary", "manual-gate"),
    ("production", "canary"),
)
#: Jobs that must exist even though the kit's list does not name them (ours: the migration gate).
EXTRA_JOBS: tuple[str, ...] = ()
#: Any job listed here must run the deploy script (and therefore the drain guard).
DEPLOY_JOBS = ("deploy-staging", "canary", "production")
SECRET_LITERAL = re.compile(r"(?i)\b(pgm_[a-z_]*(token|key|secret)\s*[:=]\s*[\"'][^\"'{}$]{8,}|"
                            r"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,})")


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, what: str, ok: bool, why: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failed.append("%s%s" % (what, (" — " + why) if why else ""))


def job_blocks(text: str) -> dict[str, str]:
    """The jobs, by name, as their raw YAML text. Parsed by indentation rather than by a YAML loader so that the
    check reads exactly what a reviewer reads (a loader would happily accept an anchor that hides a `needs`)."""
    jobs: dict[str, list[str]] = {}
    in_jobs = False
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = []
            continue
        if current and (line.startswith("    ") or not line.strip()):
            jobs[current].append(line)
    return {k: "\n".join(v) for k, v in jobs.items()}


def reaches(jobs: dict[str, str], job: str, target: str, seen: set[str] | None = None) -> bool:
    """Transitively: does `target` definitely run before `job`? The chain is what makes the gate unavoidable;
    requiring a direct `needs:` would only prove the file was written the way this checker prefers.

    `target` must EXIST as a job as well as be named: GitHub would fail a `needs:` naming a job that does not
    exist, but a checker that only compares strings happily reports "the gate is on the path" while the gate has
    been renamed out of existence. (Found by this file's own self-test, which is why the self-test exists.)"""
    if target not in jobs:
        return False
    seen = seen or set()
    if job in seen:
        return False
    seen.add(job)
    for n in needs_of(jobs.get(job, "")):
        if n == target or reaches(jobs, n, target, seen):
            return True
    return False


def needs_of(body: str) -> list[str]:
    m = re.search(r"^\s{4}needs:\s*(.+)$", body, re.M)
    if not m:
        return []
    raw = m.group(1).strip()
    if raw.startswith("["):
        return [x.strip().strip("'\"") for x in raw.strip("[]").split(",") if x.strip()]
    return [raw.strip("'\"")]
def run_checks(rep: Report, text: str, *, jobs: dict[str, str] | None = None) -> None:
    jobs = jobs if jobs is not None else job_blocks(text)

    # 1. the stages, in order, each gated by the previous
    for name, prev in STAGES:
        body = jobs.get(name)
        rep.check("stage %r exists" % name, body is not None,
                  "the kit's D3 order names this stage; a pipeline without it is a different pipeline")
        if body is None or prev is None:
            continue
        rep.check("stage %r runs after %r" % (name, prev), prev in needs_of(body),
                  "needs: %s — the order is the graph, not a comment" % (needs_of(body) or "none"))
    for name in EXTRA_JOBS:
        rep.check("stage %r exists" % name, name in jobs, "our extra gate, between contract and component")

    # 2/3. the gate
    gate = jobs.get("manual-gate", "")
    rep.check("the manual gate is an environment with reviewers",
              re.search(r"^\s{4}environment:\s*canary-approval", gate, re.M) is not None,
              "a shell `read -p` can be satisfied by a bot; a protected environment cannot")
    rep.check("the gate classifies the change set", "p15-money-path.py" in gate,
              "the approver has to see whether the change is on the money path")
    for job in ("canary", "production"):
        rep.check("%s cannot skip the gate" % job, reaches(jobs, job, "manual-gate"),
                  "needs: %s — nothing on the way to real money may bypass the approval" % (needs_of(jobs.get(job, "")) or "none"))

    # 4. every deploy goes through the drain guard
    for job in DEPLOY_JOBS:
        body = jobs.get(job, "")
        rep.check("%s deploys with the script" % job, "deploy/deploy.sh" in body,
                  "the drill that is verified must be the command that runs")
        rep.check("%s is an environment" % job, re.search(r"^\s{4}environment:\s*\S+", body, re.M) is not None,
                  "an approval record per environment, not per person's memory")

    # 5. digests, never a tag. BOTH images: a deploy that reads the digest for the api and a tag for the
    #    executor is the money path running something nobody tested, and a substring check cannot see that.
    rep.check("the build reads digests back out of the registry", "docker inspect" in jobs.get("build", ""),
              "a digest is what the registry says the artefact is")

    # 4b. every `needs:` names a job that exists
    for name, body in jobs.items():
        for n in needs_of(body):
            rep.check("%s: needs %r exists" % (name, n), n in jobs,
                      "a dangling needs is a pipeline that will not run at all")
    rep.check("the build records digests", "record-digests" in jobs.get("build", "") or
              "image-digests" in jobs.get("build", ""),
              "the deploy job needs a digest to consume")
    for job in DEPLOY_JOBS:
        body = jobs.get(job, "")
        rep.check("%s reads BOTH digests" % job, body.count("image-digests.txt") >= 2,
                  "api and executor each come from the file the build job wrote (%d reference(s))"
                  % body.count("image-digests.txt"))
        rep.check("%s passes both images by digest" % job,
                  "--api-image" in body and "--executor-image" in body,
                  "deploy.sh refuses a tag; the pipeline must not hand it one")
    rep.check("nothing here pulls :latest", ":latest" not in text and "':latest'" not in text,
              "a moving tag on a money path is a coin flip")

    # 6. timeouts
    for name, body in jobs.items():
        rep.check("job %r has a timeout" % name, re.search(r"^\s{4}timeout-minutes:", body, re.M) is not None,
                  "a hung job on a release branch blocks every release after it")

    # 7. secrets
    rep.check("no secret literal in the pipeline", SECRET_LITERAL.search(text) is None,
              (SECRET_LITERAL.search(text) or re.match("", "")).group(0)[:40] if SECRET_LITERAL.search(text) else "")
    rep.check("secrets come from the platform", "${{ secrets." in text,
              "the deploy jobs need credentials from somewhere, and that somewhere is not this file")

    # 8. rollback
    rb = jobs.get("rollback", "")
    rep.check("a rollback job exists", bool(rb), "rollback is a job, not a resolution")
    rep.check("rollback runs when production fails", "failure()" in rb, "if: failure()")
    rep.check("rollback calls the one command", "deploy/rollback.sh" in rb, "one command, under a minute")


def self_test() -> int:
    """Prove each rule can fail: break the file one way at a time and require the matching failure."""
    text = PIPELINE.read_text()
    cases = [
        # (a substring of the failure message the breakage must produce, the breakage)
        ("runs after", lambda t: t.replace("    needs: smoke\n", "", 1)),
        ("the manual gate is an environment", lambda t: t.replace("environment: canary-approval",
                                                                  "environment: staging", 1)),
        ("cannot skip the gate", lambda t: t.replace("  manual-gate:\n", "  manual-gate-DISABLED:\n", 1)),
        ("deploys with the script", lambda t: t.replace("deploy/deploy.sh --env canary",
                                                        "echo deploy --env canary", 1)),
        ("reads BOTH digests", lambda t: t.replace("deploy/image-digests.txt | tail -n 1)",
                                                   "echo latest | tail -n 1)")),
        ("rollback runs when production fails", lambda t: t.replace("    if: failure()\n", "", 1)),
        ("no secret literal", lambda t: t.replace("  PGM_LOG_FORMAT: json\n",
                                                  "  PGM_BUILDER_TOKEN: \"pgm_live_abcdef123456\"\n", 1)),
        ("has a timeout", lambda t: t.replace("    runs-on: ubuntu-latest\n    timeout-minutes: 15\n",
                                              "    runs-on: ubuntu-latest\n", 1)),
    ]
    misses = []
    for expect, mutate in cases:
        rep = Report()
        run_checks(rep, mutate(text))
        hit = [f for f in rep.failed if expect in f]
        if not hit:
            misses.append("planting %r did not trip any rule that says %r (the rule is decoration)"
                          % (expect, expect))
    for miss in misses:
        print("  FAIL %s" % miss)
    print("pipeline self-test: %d case(s), %d missed" % (len(cases), len(misses)))
    return 1 if misses else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D3 — the pipeline's shape")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not PIPELINE.exists():
        print("pipeline check: %s is missing" % PIPELINE)
        return 1
    rep = Report()
    run_checks(rep, PIPELINE.read_text())
    for f in rep.failed:
        print("  FAIL %s" % f)
    print("pipeline check: %d passed, %d failed" % (rep.passed, len(rep.failed)))
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
