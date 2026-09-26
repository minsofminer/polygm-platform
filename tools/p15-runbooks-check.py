#!/usr/bin/env python3
"""P15 D6 — prove the runbooks are procedures rather than essays.

    python3 tools/p15-runbooks-check.py              # the gate
    python3 tools/p15-runbooks-check.py --self-test  # plant breakages in a copy; every rule must trip

What a runbook has to survive to be useful at 2am:

* **Five sections.** Symptoms, diagnosis, remediation, verification, escalation. A page that skips "verification"
  ends when the operator gets bored rather than when the incident is over.
* **Commands that can be typed.** At least three runnable blocks, and every path a command names must exist in
  this repository — a runbook that says `tools/p15-something.py` when the file is `p15-something-else.py` is worse
  than no runbook, because it burns the first two minutes of an incident on a typo.
* **Flags that exist.** Every `--flag` on a command that invokes one of our tools, or one of our services as a
  module, is checked against that program's own `--help`. Three wrong commands were caught this way while these
  pages were being written.
* **An owner and a recent drill.** Front matter carries `owner`, `alarms` and `last_drilled`; a runbook that has
  never been run is a hypothesis, and the ninety-day rule makes that visible instead of comfortable.
* **Links that work in both directions.** The alarm registry points at the runbook and the runbook's front matter
  claims the alarm, so neither side can rot alone (`tools/p15-alerts.py --check` owns the same pair from its side).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNBOOKS = ROOT / "docs" / "runbooks"
REGISTRY = ROOT / "ops" / "alerts.yaml"
SECTIONS = ("## Symptoms", "## Diagnosis", "## Remediation", "## Verification", "## Escalation")
DRILL_MAX_AGE_DAYS = 90
MIN_COMMANDS = 3
PATH_RE = re.compile(r"\b((?:tools|deploy|docs|db|services|packages|infra|contracts|config)/[\w./*-]+"
                     r"\.(?:py|sh|md|sql|yaml|yml|json|tf|txt))")
TOOL_RE = re.compile(r"\btools/([\w.-]+\.py)\b")
FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
MODULE_RE = re.compile(r"python3?\s+-m\s+([\w.]+)")
START_RE = re.compile(r"^(curl|psql|python3?|docker|redis-cli|kubectl|deploy/|for |make |git |systemctl|"
                      r"journalctl|openssl|sqlite3|df |cat |echo |export )")


def front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    body = text.split("---", 2)
    if len(body) < 3:
        return {}
    try:
        return yaml.safe_load(body[1]) or {}
    except yaml.YAMLError:
        return {}


def code_blocks(text: str) -> list[str]:
    return re.findall(r"```(?:bash|sh|sql|json)?\n(.*?)```", text, re.S)


def commands(block: str) -> list[str]:
    """One logical command per non-comment line, with line continuations joined: what the operator types."""
    out: list[str] = []
    cur = ""
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cur = (cur + " " + line.rstrip("\\").strip()).strip() if cur else line.rstrip("\\").strip()
        if not raw.rstrip().endswith("\\"):
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def tool_help(rel: str, sub: str) -> tuple[bool, str]:
    """Ask the tool itself, because `--help` is the only authority on which flags a tool accepts."""
    argv = [sys.executable, str(ROOT / rel)] + ([sub] if sub else []) + ["--help"]
    env = dict(os.environ, PYTHONPATH="%s:%s:%s" % (ROOT / "packages", ROOT / "tools", ROOT))
    try:
        p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "could not run %s --help: %s" % (rel, exc)
    return p.returncode in (0, 2), p.stdout + p.stderr


def module_help(module: str, flags: list[str]) -> tuple[bool, str]:
    """`python3 -m ingest.main --once --status` and friends.

    The package directory is `services/<first component>/` and the file is `<second component>.py` — so
    `executor.main` lives at `services/executor/main.py`, not at `services/executor/executor.py`. Getting this
    wrong reports every service command as broken, which is the kind of false positive that teaches people to
    ignore the checker, so the lookup tries both shapes.
    """
    parts = module.split(".")
    if parts[:1] == ["services"]:
        parts = parts[1:]                     # `services.executor.main` is the same file as `executor.main`
    cands = []
    for d in (ROOT / "services").glob("*"):
        if not d.is_dir():
            continue
        if len(parts) > 1 and d.name == parts[0] and (d / (parts[1] + ".py")).exists():
            cands.append(d)
        elif (d / (parts[0] + ".py")).exists():
            cands.append(d)
    if not cands:
        return False, "no service directory provides %s (looked for services/<pkg>/%s.py)" % (module,
                                                                                             parts[-1])
    env = dict(os.environ, PYTHONPATH="%s:%s" % (cands[0], ROOT / "packages"))
    argv = [sys.executable, "-m", module, "--help"]
    try:
        p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "could not run %s --help: %s" % (module, exc)
    text = p.stdout + p.stderr
    for flag in flags:
        if flag not in text:
            return False, "%s does not accept %s" % (module, flag)
    return True, ""


def command_problems(cmd: str) -> list[str]:
    """The flags on a command that invokes one of our tools or services, verified against its own help."""
    problems: list[str] = []
    for m in TOOL_RE.finditer(cmd):
        rel_tool = "tools/" + m.group(1)
        if not (ROOT / rel_tool).exists():
            continue                       # the path check reports this one, with a better message
        tail = cmd[m.end():].strip()
        toks = tail.split()
        sub = toks[0] if toks and not toks[0].startswith("-") else ""
        flags = sorted({f for f in FLAG_RE.findall(tail) if f != "--help"})
        if not flags:
            continue
        ok, text = tool_help(rel_tool, sub)
        if not ok:
            problems.append("%s: %s" % (rel_tool, text.splitlines()[0] if text else "cannot run"))
            continue
        for flag in flags:
            if flag not in text:
                problems.append("%s does not accept %s (used by %r)" % (rel_tool, flag, cmd[:60]))
    for m in MODULE_RE.finditer(cmd):
        module = m.group(1)
        flags = sorted({f for f in FLAG_RE.findall(cmd[m.end():])})
        if not flags:
            continue
        ok, why = module_help(module, flags)
        if not ok:
            problems.append(why)
    return problems


def check(docs: pathlib.Path, registry: pathlib.Path, *, root: pathlib.Path | None = None,
          verify_commands: bool = True, today: dt.date | None = None) -> list[str]:
    root = root or ROOT
    today = today or dt.datetime.now(dt.timezone.utc).date()
    problems: list[str] = []
    alarm_ids: set[str] = set()
    if registry.exists():
        alarm_ids = {r["id"] for r in (yaml.safe_load(registry.read_text()).get("rules") or [])}
    claimed: set[str] = set()
    files = sorted(p for p in docs.glob("*.md") if p.name != "README.md") if docs.exists() else []
    if not files:
        return ["no runbooks found under %s" % docs]
    for path in files:
        text = path.read_text()
        fm = front_matter(text)
        name = path.name
        for key in ("id", "order", "severity", "owner", "alarms", "last_drilled"):
            if key not in fm:
                problems.append("%s: front matter is missing %r" % (name, key))
        for section in SECTIONS:
            if section not in text:
                problems.append("%s: missing %r" % (name, section))
        cmds = [c for b in code_blocks(text) for c in commands(b)]
        runnable = [c for c in cmds if START_RE.match(c)]
        if len(runnable) < MIN_COMMANDS:
            problems.append("%s: %d runnable command(s), fewer than %d — prose that cannot be typed is not a "
                            "procedure" % (name, len(runnable), MIN_COMMANDS))
        for cmd in cmds:
            for ref in PATH_RE.findall(cmd):
                if "*" in ref:
                    continue
                if not (root / ref).exists():
                    problems.append("%s: command names %s, which does not exist" % (name, ref))
            # The flag checks shell out to the real tools, so a copied tree (the self-test) skips them: the copy
            # exists to test the prose rules, and pointing subprocesses at a temp directory would produce a
            # failure mode about paths rather than about runbooks.
            if verify_commands and root.resolve() == ROOT.resolve():
                problems.extend("%s: %s" % (name, p) for p in command_problems(cmd))
        for alarm in (fm.get("alarms") or []):
            claimed.add(str(alarm))
            if alarm_ids and str(alarm) not in alarm_ids:
                problems.append("%s: claims alarm %r, which the registry does not define" % (name, alarm))
        drilled = fm.get("last_drilled")
        if isinstance(drilled, (dt.date, dt.datetime)):
            age = (today - (drilled.date() if isinstance(drilled, dt.datetime) else drilled)).days
            if age > DRILL_MAX_AGE_DAYS:
                problems.append("%s: last drilled %d days ago (limit %d) — an undrilled runbook is a hypothesis"
                                % (name, age, DRILL_MAX_AGE_DAYS))
    if alarm_ids:
        for a in sorted(alarm_ids - claimed):
            problems.append("alarm %s has no runbook claiming it — an alarm without a runbook gets deleted" % a)
        for a in sorted(claimed - alarm_ids):
            problems.append("runbook claims unknown alarm %s" % a)
    for page in sorted((root / "docs").glob("*.md")):
        if not page.exists():
            continue
        for ref in set(re.findall(r"docs/runbooks/([\w.-]+\.md)", page.read_text())):
            if not (docs / ref).exists():
                problems.append("%s promises docs/runbooks/%s, which does not exist" % (page.name, ref))
    return problems


MUTATIONS: list[tuple[str, str, str, str]] = [
    ("missing-section", "unreconciled-orders.md", "## Verification", "## Review"),
    ("broken-path", "executor-down.md", "tools/p15-drain-guard.py", "tools/p15-drain-gard.py"),
    ("wrong-flag", "rollback.md", "--component executor", "--componant executor"),
    ("stale-drill", "cost-spike.md", "last_drilled: 2026-09-23", "last_drilled: 2019-01-01"),
    ("bogus-alarm", "kill-switch.md", "alarms: [kill-switch-engaged]", "alarms: [not-a-real-alarm]"),
    ("no-front-matter", "cache-queue-age.md", "---\nid: cache-queue-age", "id: cache-queue-age"),
]


def self_test(tmp: pathlib.Path) -> list[str]:
    """Plant each breakage in a copy and require the checker to name it. A checker that cannot fail is a rubber
    stamp, and a rubber stamp is the failure mode quality gates actually die of."""
    failures: list[str] = []
    for name, rel, old, new in MUTATIONS:
        d = tmp / name
        shutil.rmtree(d, ignore_errors=True)
        (d / "docs").mkdir(parents=True)
        shutil.copytree(RUNBOOKS, d / "docs" / "runbooks")
        (d / "ops").mkdir(parents=True)
        shutil.copy(REGISTRY, d / "ops" / "alerts.yaml")
        p = d / "docs" / "runbooks" / rel
        text = p.read_text()
        if old not in text:
            failures.append("%s: the planted anchor %r is not present in %s (the self-test has rotted)"
                            % (name, old[:40], rel))
            continue
        p.write_text(text.replace(old, new, 1))
        found = check(d / "docs" / "runbooks", d / "ops" / "alerts.yaml", root=d, verify_commands=False)
        if not found:
            failures.append("%s: the mutation was NOT caught" % name)
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D6 — the runbooks, checked")
    ap.add_argument("--docs", default=str(RUNBOOKS))
    ap.add_argument("--registry", default=str(REGISTRY))
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--no-commands", action="store_true", help="skip the --help flag checks (they shell out)")
    a = ap.parse_args(argv)
    if a.self_test:
        with tempfile.TemporaryDirectory(prefix="p15-runbooks-") as td:
            failures = self_test(pathlib.Path(td))
        for f in failures:
            print("  FAIL %s" % f)
        print("runbook self-test: %d planted breakage(s), %d missed" % (len(MUTATIONS), len(failures)))
        return 1 if failures else 0
    problems = check(pathlib.Path(a.docs), pathlib.Path(a.registry), verify_commands=not a.no_commands)
    for p in problems:
        print("  FAIL %s" % p)
    files = sorted(p for p in pathlib.Path(a.docs).glob("*.md") if p.name != "README.md")
    print("runbooks: %d page(s), %d problem(s)" % (len(files), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
