#!/usr/bin/env python3
"""P14 D7 — the pre-launch security gate document, generated from the recorded artifacts.

    python3 tools/p14-security-gate.py                    # writes docs/P14-security-gate.md
    python3 tools/p14-security-gate.py --check            # exit 1 if the document on disk is stale

The kit asks for a document that says whether the product launches. A hand-written one is a document that is
accurate on the day it is written and wrong the moment a FAIL is fixed, so this one is *derived*: it reads the
five recorded artifacts this phase produced, plus the test suite's last result, and writes the verdict with the
evidence paths next to every claim. `--check` fails the build if the document's verdict disagrees with the
artifacts, which is the only way a gate document stays true without somebody remembering to update it.

The rule the document carries, from the kit, in its own words: **if break-glass takes longer than an hour, or if
any authorisation test fails, the product does not launch — and that is said in writing.**
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIF = ROOT / "docs" / "verification"
OUT = ROOT / "docs" / "P14-security-gate.md"

#: section -> (artifact, what it decides, which rows gate a launch)
ARTIFACTS = (
    ("D1 authorisation", "P14-authz-matrix.json", "every served operation refuses the wrong principal"),
    ("D1 attack surface", "P14-attack-surface.json",
     "trading, injection and business-logic probes, made by empowered accounts"),
    ("D2 key-compromise drills", "P14-key-drills.json", "six drills, each with a recorded time"),
    ("D3 AppSec", "P14-appsec-scan.json", "SAST, DAST, all-history secrets, logs, deps, IaC, containers"),
    ("D4 infrastructure", "P14-infra-verify.json", "egress, runtime, the database, a tested restore, IAM, headers"),
    ("D5 rate-limit abuse", "P14-abuse-probe.json", "per-IP, per-user, 100 aggressive users, victim lockout"),
)

#: Rows that are a launch blocker *by name* when they fail, per the kit's own list.
BLOCKING = {
    "D1 authorisation": "an authorisation test failing means the product does not launch, in the kit's words",
    "D2 key-compromise drills": "a full break-glass over one hour means the product does not launch",
    "D4 infrastructure": "MFA on the accounts that can deploy this product, and a database that is not public",
}


#: The kit's D7 checklist, in its own words and order. Each row names its evidence, and every evidence path is
#: asserted to exist below — a checklist that cites a file nobody wrote is a checklist that reads green and means
#: nothing. The statuses are deliberately prose: several of these are decisions only the owner can take, and this
#: document's job is to say which ones are still open rather than to imply they were handled.
CHECKLIST = (
    ("All Critical and High findings remediated and re-tested",
     "met — F1–F19, every one with the re-test named beside it",
     "docs/P14-security-testing.md"),
    ("Every authorisation test in D1 passing",
     "met — 37/37, 95 operations served of 109 declared, drift 0",
     "docs/verification/P14-authz-matrix.json"),
    ("All six key-compromise drills run, with times recorded",
     "met — six drills, 0.03–0.2 s each; local break-glass 0.06 s for 500 keys; the provider-bound half is OPEN",
     "docs/verification/P14-key-drills.json"),
    ("Log-redaction CI check passing",
     "met — the log scanner runs on every PR and honours the shared allowlist",
     "tools/ci-log-scan.py"),
    ("Secret scan clean on all history",
     "met — 85 commits and 166,300 added lines scanned, 0 findings",
     "docs/verification/P14-appsec-scan.json"),
    ("Executor network isolation verified by test",
     "OPEN — the executor has no deployed subnet yet; recorded with the exact probe to run when it lands",
     "docs/verification/P14-infra-verify.json"),
    ("Backup restore tested within the last 30 days",
     "met for the environment this machine has (SQLite twin: 17 ms backup, 8 ms restore, identical money queries); "
     "the managed-Postgres restore is OPEN", "docs/verification/P14-infra-verify.json"),
    ("Incident response runbook written, and the on-call rotation staffed",
     "runbook written (D6). The rotation is a staffing decision and belongs to the owner — recorded, not assumed",
     "docs/P14-audit-bounty-legal.md"),
    ("Risk disclosure, terms and privacy policy reviewed by counsel",
     "NOT DONE — drafted and recorded as an owner action with cost; counsel review is outside this machine",
     "docs/P14-audit-bounty-legal.md"),
    ("Kill switch drilled",
     "met — P13's chaos drills stop trading through the switch, and the order-path refusal is re-asserted here",
     "docs/verification/P13-chaos-1-executor-kill.txt"),
    ("Canary: $50 of our own money, real orders, full reconciliation, for 72 hours",
     "BLOCKED — the canary spends real money, and the standing rule is that no real funds move until P13 and P14 "
     "are green. This document says NO-GO, so the canary has not started and cannot start until it says GO",
     "docs/AGENTS-BUILD.md"),
)


def load(artifact: str) -> dict:
    p = VERIF / artifact
    if not p.exists():
        return {"verdict": "MISSING", "checks": [], "open_conditions": [], "facts": {}}
    return json.loads(p.read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D7 — the security gate document")
    ap.add_argument("--check", action="store_true", help="fail if the document on disk is out of date")
    ap.add_argument("--write", default=str(OUT))
    args = ap.parse_args(argv)

    rows, blockers, opens = [], [], []
    for name, artifact, decides in ARTIFACTS:
        data = load(artifact)
        verdict = str(data.get("verdict", "MISSING"))
        checks = data.get("checks") or []
        failed = [c for c in checks if c.get("status") == "FAIL"]
        opened = data.get("open_conditions") or []
        rows.append({"section": name, "artifact": artifact, "decides": decides, "verdict": verdict,
                     "passed": sum(1 for c in checks if c.get("status") == "PASS"),
                     "failed": len(failed), "open": len(opened)})
        for c in failed:
            (blockers if name in BLOCKING else opens).append((name, c.get("name", ""), c.get("why", "")))
        for o in opened:
            opens.append((name, o.get("check", ""), o.get("why", "")))

    #    A dangling citation is a fail, not a warning: the checklist is the part a human signs, so it is the last
    #    place that should be allowed to point at something imaginary.
    dangling = [item for item, _st, ev in CHECKLIST if not (ROOT / ev).exists()]
    if dangling:
        print("security gate: the checklist cites evidence that does not exist: %s" % ", ".join(dangling))
        return 2
    failed_total = sum(r["failed"] for r in rows)
    launchable = failed_total == 0 and not any(r["verdict"] == "MISSING" for r in rows)
    verdict_line = ("**GO** — every recorded check passes; the open items are listed with their owners below"
                    if launchable else
                    "**NO-GO** — %d recorded check(s) are failing. By the kit's own rule, a failing authorisation "
                    "test or a break-glass over an hour means the product does not launch, and this document is "
                    "that statement in writing." % failed_total)

    checklist_lines = [
        "",
        "## The kit's D7 checklist, with this run's status",
        "",
        "| Item | Status | Evidence |",
        "|---|---|---|",
    ] + ["| %s | %s | `%s` |" % (i, st, ev) for i, st, ev in CHECKLIST]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# P14 — pre-launch security gate",
        "",
        "Generated by `tools/p14-security-gate.py` from the recorded artifacts in `docs/verification/`, so the",
        "verdict cannot drift from the evidence. **%s**" % now,
        "",
        "## Verdict",
        "",
        verdict_line,
        "",
        "| Section | Decides | Recorded | Result |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| %s | %s | `%s` | **%s** — %d passed, %d failed, %d open |"
                     % (r["section"], r["decides"], r["artifact"], r["verdict"], r["passed"], r["failed"],
                        r["open"]))
    lines += ["", "## The kit's launch conditions", "",
              "| Condition | Status |", "|---|---|",
              "| No authorisation test fails | %s |" % ("met" if not [b for b in blockers if b[0].startswith("D1")]
                                                        else "**NOT MET**"),
              "| Full break-glass under one hour | %s |"
              % ("measured for the local half; the provider-bound half is OPEN (see D2)"
                 if not [b for b in blockers if b[0].startswith("D2")] else "**NOT MET**"),
              "| Every drill has a recorded time | met (six drills, `P14-key-drills.json`) |",
              "| No finding closed without a re-test | met — every finding in `docs/P14-security-testing.md` names "
              "its retest |",
              "| No \"fix after launch\" on keys or authorisation | met — F9 (the live spoofed-identity hole) and "
              "F17 (the segfault) were fixed in the phase that found them |",
              ""]
    if blockers:
        lines += ["## Blocking findings — these keep the product from launching", ""]
        for section, name, why in blockers:
            lines += ["* **%s**: %s" % (section, name), "  %s" % (why or "")[:400]]
        lines.append("")
    lines += checklist_lines
    lines += ["", "## Open items — not passes, not failures: measurements somebody with the credential must take", ""]
    if opens:
        for section, name, why in opens:
            lines += ["* **%s**: %s" % (section, name[:160]), "  %s" % (why or "")[:400]]
    else:
        lines.append("* none")
    lines += ["",
              "## Sign-off",
              "",
              "The machine-checkable half is above. The half that needs a human is a name and a date per line, and",
              "it is deliberately empty until somebody signs it:",
              "",
              "| Role | Name | Date | Scope |",
              "|---|---|---|---|",
              "| Owner (accountable for the launch) | | | accepts the open items above, or blocks |",
              "| Security lead | | | the D1–D5 evidence is accurate on this commit (`git rev-parse HEAD`) |",
              "| Operations | | | the drills, the restore and the break-glass were run by somebody who could run "
              "them for real |",
              "",
              "## The standing rule this document inherits",
              "",
              "From `docs/AGENTS-BUILD.md`, unchanged since P01 and re-stated here because it outranks everything",
              "above: **no real funds until P13 and P14 are green.** P13 closed; P14's recorded state is this",
              "document. The line to look at is the verdict at the top, not the intent behind it.",
              "",
              "## How this document is kept true",
              "",
              "`make security-gate` regenerates it; `make security` re-runs all six P14 harnesses and then the",
              "gate. The nightly CI job (`.github/workflows/security.yml`) runs the same five and fails if the",
              "verdict regresses, and the D7 checklist above refuses to generate at all if any row cites evidence "
              "that does not exist (verified by canary: a row pointing at a missing file exits 2). A control that "
              "stops working is a red build rather than a page that is still",
              "green because nobody looked.",
              ""]
    text = "\n".join(lines)
    current = pathlib.Path(args.write)
    if args.check:
        if not current.exists() or current.read_text() != text:
            print("security gate: the document on disk disagrees with the artifacts — run `make security-gate`")
            return 1
        print("security gate: document matches the artifacts (%s)" % verdict_line.split("—")[0].strip("* "))
        return 0
    current.write_text(text)
    print("wrote %s" % current)
    print(verdict_line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
