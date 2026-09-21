#!/usr/bin/env python3
"""P13's gate check: does this phase's evidence actually exist, and does each check fail when broken?

    python3 tools/p13-gate-check.py --record docs/verification/P13-gate.txt
    python3 tools/p13-gate-check.py --json docs/verification/P13-gate.json
    python3 tools/p13-gate-check.py --skip-heavy     # read the recorded evidence, re-run only the cheap checks

Every phase in this build has a gate, and P13's is unusual in one way: **its subject is the other gates.** So the
tool is built around the things a test suite gets wrong about itself — the things that let a green build ship a
money bug:

  1. **A matrix row with no test, and a test with no row.** `tools/p13-money-matrix.py` enumerates the kit's money
     paths and maps each to the test that proves it. This gate resolves every row against the *collected* test ids
     itself, rather than trusting the matrix's own checker, and refuses to pass on a row that points at a test
     which no longer exists — a stale row is worse than a missing one, because the matrix reads as complete.

  2. **Evidence that is not written down.** The chaos drills and the load harness each produce a verdict, and a
     gate that keeps its answer in memory produces nothing a reader can check. This one requires the recorded index
     and the artifact files it names, and re-runs the drills and the load suite unless `--skip-heavy` says to read
     the record instead.

  3. **A workflow that describes work instead of doing it.** D8 is a set of workflows; the gate parses them and
     fails if the money matrix is not in the PR path, if the drills and the load run are not in the nightly path,
     or if a release could be cut without the phase gate running at all.

  4. **Quarantine without an expiry.** D8 allows flaky tests to be quarantined; it does not allow them to be
     forgotten. Every entry needs a reason, an owner and a date.

  5. **Coverage gated globally.** The kit is explicit: coverage is reported and not gated, except the money paths,
     which is what section 1 is. A workflow that fails a build on a global percentage is the forbidden thing, so
     the gate looks for one and fails.

Every section's logic is a pure function over its inputs, and every section has a canary that feeds it a broken
input and requires it to complain. That shape is the whole point: the first version of this file asserted a canary
by comparing literals, which proves nothing — a check that cannot fail is a check that is not checking.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
VERIF = ROOT / "docs" / "verification"


def load_tool(path: Path, name: str):
    """Import a `tools/` script whose filename is not a module name (they are all `p13-something.py`).

    Named `load_tool` and not `load`: the load-test section below defines its own `load(g, heavy)`, which
    silently shadowed this one and made the matrix section call it with the wrong arguments. A helper whose name
    collides with a section is a helper that stops being called.
    """
    spec = spec_from_file_location(name, str(path))
    mod = module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run(cmd: list[str], *, timeout: int = 1800, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})


class Gate:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, why: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, "" if ok else why))
        return ok

    def canary(self, name: str, problems: list[str], what_broke: str) -> bool:
        """A section's canary: the same pure checker, fed something deliberately wrong, must report it."""
        return self.check(name, bool(problems), "the checker accepted %s" % what_broke)

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [r for r in self.results if r[0] == "FAIL"]


# --------------------------------------------------------------------------------------- 1. the money matrix
def row_problems(rows: list[dict], collected: set[str]) -> list[str]:
    """Rows → the tests they name. Pure, so the canary can run it over a row that names nothing real."""
    problems: list[str] = []
    seen = set()
    for row in rows:
        rid = row.get("id", "?")
        if rid in seen:
            problems.append("%s: duplicate row id" % rid)
        seen.add(rid)
        tests = row.get("tests") or []
        if not tests:
            problems.append("%s: no test proves it" % rid)
        for test in tests:
            if test not in collected:
                problems.append("%s: %s is not collected" % (rid, test))
        for artifact in row.get("artifacts") or []:
            if not (ROOT / artifact).exists():
                problems.append("%s: %s does not exist" % (rid, artifact))
    return problems


def matrix(g: Gate) -> dict:
    tool = load_tool(ROOT / "tools" / "p13-money-matrix.py", "pgm_matrix")
    collected = tool._collected()
    problems = row_problems(tool.ROWS, collected)
    g.check("every one of the matrix's %d rows resolves to a collected test" % len(tool.ROWS), not problems,
            "; ".join(problems[:5]))
    canary = row_problems(list(tool.ROWS) + [{"id": "XX-1", "tests": ["test_nothing.py::NoSuch::nope"]}], collected)
    g.canary("matrix canary: a row naming a test that does not exist is refused", canary, "an unresolvable row")

    # The two false greens this matrix shipped, replayed as canaries — because a gate that only checks the
    # happy path would have shipped both of them again. Both were found in production of this repo's own
    # tooling by noticing a runtime (0.3 s for 62 tests) rather than a verdict.
    ids = sorted({t for row in tool.ROWS for t in row["tests"]})
    _, not_run = tool.summarise(returncode=4, ids=ids, stderr="",
                                stdout="ERROR: file or directory not found: test_executor.py::TestVenueValidation"
                                       "::test_off_tick_and_small_orders_are_rejected_by_the_mock\n\n"
                                       "no tests ran in 0.00s\n")
    g.canary("matrix canary: a run where pytest collected nothing is refused", [] if not_run["ok"] else ["green"],
             "a matrix that ran no tests at all")
    _, short = tool.summarise(returncode=0, ids=ids, stderr="",
                              stdout="......................................\n"
                                     "40 passed, 22 skipped in 9.10s\n")
    g.canary("matrix canary: a run that skipped a third of the mapped tests is refused",
             [] if short["ok"] else ["refused"], "a run where 22 mapped tests were skipped")
    # The mirror of a must-refuse canary: this input is GREEN, and the parser must accept it. The warnings
    # summary below names a test that passed, which the old node-id-shaped regex read as a failure.
    warn_bad, with_warning = tool.summarise(
        returncode=0, ids=ids, stderr="",
        stdout="=============================== warnings summary ===============================\n"
               "tests/test_wallet_api.py::TestWithdrawCeremony::test_a_destination_nobody_allowlisted_is_refused_before_the_password_is_read\n"
               "  /x: PytestWarning: something\n"
               "%d passed, 1 warning in 14.24s\n" % len(ids))
    g.check("matrix canary: a green run whose warnings summary names a test is still green (must-accept)",
            with_warning["ok"] and not warn_bad, str(with_warning) + " bad=%s" % warn_bad)
    return {"rows": len(tool.ROWS), "collected_ids": len(collected)}


def matrix_tests(g: Gate, *, heavy: bool) -> dict:
    files = ["tests/test_p13_money_matrix.py", "tests/test_p13_contracts.py", "tests/test_p13_properties.py"]
    tmp = {"PGM_TEST_TMPDIR": str(Path.home() / ".cache" / "pytest-tmp")}
    if not heavy:
        out = run([PY, "-m", "pytest", *files, "--collect-only", "-q"], timeout=600, env=tmp)
        n = len(re.findall(r"::", out.stdout))
        g.check("the P13 test files import and collect (%d test ids)" % n, out.returncode == 0 and n > 20,
                (out.stdout + out.stderr)[-300:])
        return {"collected": n}
    out = run([PY, "-m", "pytest", *files, "-q", "-p", "no:warnings"], env=tmp)
    tail = [ln for ln in out.stdout.strip().splitlines() if ln.strip()][-1:]
    summary = tail[0].strip() if tail else ""
    g.check("the money matrix, the contracts and the property sweep run green: %s" % summary,
            out.returncode == 0, out.stdout[-400:])
    return {"summary": summary}


# ------------------------------------------------------------------------------------ 2. the contract record
def contracts(g: Gate) -> dict:
    tool = ROOT / "tools" / "build-p13-contract-fixtures.py"
    target = ROOT / "tests" / "fixtures" / "p13" / "contracts.json"
    out = run([PY, str(tool), "--check"], timeout=600)
    g.check("the recorded contract fixtures still match the product", out.returncode == 0,
            (out.stdout + out.stderr)[-300:])
    if not target.exists():
        g.check("a perturbed fixture is refused by the checker", False, "contracts.json is missing")
        return {}
    original = target.read_text(encoding="utf-8")
    try:
        data = json.loads(original)
        victim = next((k for k, v in data.items() if isinstance(v, dict) and v), None)
        if victim is None:
            g.check("a perturbed fixture is refused by the checker", False, "no mapping to perturb")
        else:
            data[victim] = {"$": "not the shape we recorded"}
            target.write_text(json.dumps(data, indent=2), encoding="utf-8")
            broken = run([PY, str(tool), "--check"], timeout=600)
            g.canary("contract canary: a perturbed fixture (%s) is refused by the checker" % victim,
                     [] if broken.returncode == 0 else ["refused"], "a fixture that no longer matches")
    finally:
        target.write_text(original, encoding="utf-8")
    return {"fixture_bytes": len(original)}


# ---------------------------------------------------------------------------------------- 3. the chaos record
def chaos_problems(index: dict) -> list[str]:
    """Every drill: PASS, an artifact that exists, and an expected outcome that was written before it ran.

    That last one is the kit's own constraint ("every chaos test has a written expected outcome before it is
    run") and the reason it is checked here rather than trusted: a drill whose expectation is written after the
    observations is a drill that can only ever pass.
    """
    problems = []
    for key, drill in (index.get("drills") or {}).items():
        if str(drill.get("verdict")) != "PASS":
            problems.append("drill %s: %s" % (key, drill.get("verdict")))
        artifact = drill.get("artifact")
        if artifact and not (ROOT / artifact).exists():
            problems.append("drill %s: artifact %s is missing" % (key, artifact))
        if not str(drill.get("expected") or "").strip():
            problems.append("drill %s: no expected outcome was written before the run" % key)
    if not (index.get("drills") or {}):
        problems.append("the index records no drills")
    return problems


def chaos(g: Gate, *, heavy: bool) -> dict:
    index = VERIF / "P13-chaos-suite.json"
    if heavy:
        run([PY, str(ROOT / "tools" / "p13-chaos-suite.py"), "--skip-live", "--record",
             str(VERIF / "P13-chaos-suite.txt"), "--json", str(index)])
    if not index.exists():
        g.check("the chaos drills have a recorded index", False, "missing %s" % index)
        return {}
    data = json.loads(index.read_text(encoding="utf-8"))
    drills = data.get("drills") or {}
    problems = chaos_problems(data)
    g.check("all %d recorded chaos drills pass and their artifacts exist" % len(drills), not problems,
            "; ".join(problems[:4]))
    canary = chaos_problems({"drills": {"6": {"verdict": "FAIL", "artifact": "docs/verification/nope.txt",
                                            "expected": "what the kit asked for"}}})
    g.canary("chaos canary: a recorded FAIL is reported, not counted", canary, "a failing drill")
    g.canary("chaos canary: a drill with no written expectation is refused",
             chaos_problems({"drills": {"6": {"verdict": "PASS", "artifact": "docs/verification/P13-chaos-6-venue-429-storm.txt"}}}),
             "a drill whose expected outcome was never written down")
    live = drills.get("2") or {}
    return {"drills": len(drills), "live_drill": live.get("verdict", "not recorded")}


# ----------------------------------------------------------------------------------------- 4. the load record
#: What the kit's soak clause means as numbers, checked against the run's own JSON rather than a substring.
#: 200 fills/s for 1,800 s is 360,000 fills; "no duplicate alerts" is zero; "no leak" is the second half's RSS
#: growth, and the 6 MB ceiling is the harness's own assertion, restated here so a doctored transcript cannot
#: pass without the JSON agreeing.
SOAK_CLAUSE = {"seconds": 1800, "rate": 200, "expected_fills": 360_000, "max_second_half_mb": 6.0}


def soak_problems(soak: dict | None) -> list[str]:
    if not soak:
        return ["there is no machine-readable soak result"]
    problems = []
    if int(soak.get("seconds") or 0) < SOAK_CLAUSE["seconds"]:
        problems.append("the run was %.0f s, not the kit's %d s" % (float(soak.get("seconds") or 0),
                                                                   SOAK_CLAUSE["seconds"]))
    if float(soak.get("elapsed_s") or 0) < SOAK_CLAUSE["seconds"] * 0.98:
        problems.append("the run covered %.0f s of wall clock for a %d s paced soak — it did not drive the rate "
                        "it claims" % (float(soak.get("elapsed_s") or 0), SOAK_CLAUSE["seconds"]))
    if int(soak.get("rate") or 0) != SOAK_CLAUSE["rate"]:
        problems.append("the run drove %s fills/s, not %d" % (soak.get("rate"), SOAK_CLAUSE["rate"]))
    if int(soak.get("fills") or 0) < SOAK_CLAUSE["expected_fills"] * 0.999:
        problems.append("only %s of %d fills were consumed"
                        % (soak.get("fills"), SOAK_CLAUSE["expected_fills"]))
    if int(soak.get("duplicate_deliveries") or 0) != 0:
        problems.append("%s duplicate deliveries" % soak.get("duplicate_deliveries"))
    if float(soak.get("rss_second_half_mb") or 0) > SOAK_CLAUSE["max_second_half_mb"]:
        problems.append("RSS grew %.1f MB in the second half" % float(soak["rss_second_half_mb"]))
    if int(soak.get("alerts_fired") or 0) == 0:
        problems.append("no alerts fired, so the alert path was not under load at all")
    return problems


def load_problems(quick_text: str, soak: dict | None) -> list[str]:
    problems = []
    if "LOAD: PASS" not in quick_text:
        problems.append("the quick run did not pass: %s" % [ln for ln in quick_text.splitlines() if "FAIL" in ln][:1])
    problems.extend(soak_problems(soak))
    return problems


def load(g: Gate, *, heavy: bool) -> dict:
    quick = VERIF / "P13-load-quick.txt"
    if heavy:
        run([PY, str(ROOT / "tools" / "p13-load.py"), "--test", "all", "--quick", "--record", str(quick),
             "--json", str(VERIF / "P13-load-quick.json")])
    quick_text = quick.read_text(encoding="utf-8") if quick.exists() else ""
    soak_files = sorted(VERIF.glob("P13-soak-1800s.json"))
    soak = json.loads(soak_files[-1].read_text(encoding="utf-8")) if soak_files else None
    problems = load_problems(quick_text, soak) if quick.exists() else ["no quick load run on record"]
    g.check("the load harness's quick run passes and the kit's 30-minute soak meets its clause", not problems,
            "; ".join(problems))
    g.canary("load canary: a soak with duplicates, or a short one, is refused",
             soak_problems({"seconds": 300, "elapsed_s": 302.0, "rate": 200, "fills": 60_000,
                            "duplicate_deliveries": 6_000, "rss_second_half_mb": 1.0, "alerts_fired": 10}),
             "a 5-minute soak with 6,000 duplicate deliveries")
    return {"quick": "PASS" if "LOAD: PASS" in quick_text else "FAIL",
            "soak": {k: (soak or {}).get(k) for k in ("seconds", "rate", "fills", "alerts_fired",
                                                      "duplicate_deliveries", "rss_second_half_mb")}}


# -------------------------------------------------------------------------------------- 5. the frontend half
def frontend_problems(files: dict[str, bool], spec_names: list[str]) -> list[str]:
    problems = []
    for name, present in files.items():
        if not present:
            problems.append("%s is missing" % name)
    if len(spec_names) < 3:
        problems.append("the browser suite has %d specs; the kit names three flows" % len(spec_names))
    return problems


def frontend(g: Gate, *, heavy: bool) -> dict:
    props = ROOT / "web" / "src" / "num" / "p13-property.test.tsx"
    a11y = ROOT / "web" / "src" / "screens" / "p13-a11y.test.tsx"
    config = ROOT / "web" / "playwright.config.ts"
    e2e = sorted((ROOT / "web" / "e2e").glob("*.spec.ts")) if (ROOT / "web" / "e2e").exists() else []
    problems = frontend_problems({"the number-layer property sweep": props.exists(), "the surface a11y suite": a11y.exists(),
                                  "playwright.config.ts": config.exists()},
                                 [p.name for p in e2e])
    g.check("the frontend P13 suites exist and cover the kit's flows (%d specs)" % len(e2e), not problems,
            "; ".join(problems))
    g.canary("frontend canary: a missing suite or a thinned browser suite is refused",
             frontend_problems({"the number-layer property sweep": False}, ["only.spec.ts"]),
             "two missing suites")
    text = "".join(p.read_text(encoding="utf-8") for p in e2e) + (config.read_text(encoding="utf-8") if config.exists() else "")
    g.check("the browser suite keeps the Telegram webview profile and the layout claim",
            "telegram-webview" in text and "scrollWidth" in text,
            "the webview project or the overflow assertion is gone")
    vitest_cfg = (ROOT / "web" / "vitest.config.mts").read_text(encoding="utf-8") if         (ROOT / "web" / "vitest.config.mts").exists() else ""
    g.check("the project's own test command collects these files (no suite that only this gate runs)",
            "src/**/*.test.{ts,tsx}" in vitest_cfg, "vitest.config.mts no longer includes src/**/*.test.{ts,tsx}")
    if not (ROOT / "web" / "node_modules").exists():
        # Deferral, not a pass: the files above are what the gate can prove without node_modules, and `npm test`
        # in the `web` job is what runs them. The gate says so instead of silently counting them.
        g.check("the frontend P13 suites are deferred to the `web` job (there is no node_modules here)", True,
                "")
        return {"specs": [p.name for p in e2e], "vitest": "deferred"}
    if not heavy:
        return {"specs": [p.name for p in e2e], "vitest": "skipped (--skip-heavy)"}
    out = subprocess.run(["npx", "vitest", "run", "src/num/p13-property.test.tsx", "src/screens/p13-a11y.test.tsx",
                          "--reporter", "dot"], cwd=ROOT / "web", capture_output=True, text=True, timeout=900)
    tail = [ln.strip() for ln in out.stdout.strip().splitlines() if "Tests" in ln][-1:]
    g.check("the frontend P13 suites pass: %s" % (tail[0] if tail else ""), out.returncode == 0,
            (out.stdout + out.stderr)[-400:])
    return {"specs": [p.name for p in e2e], "vitest": tail[0] if tail else "ran"}


# ----------------------------------------------------------------------------------------- 6. the CI gates
def workflow_problems(texts: dict[str, str], quarantine: str | None) -> list[str]:
    problems = []
    blob = "\n".join(texts.values())
    if not any("pull_request" in t for t in texts.values()):
        problems.append("no workflow runs on a pull request")
    if not any("schedule" in t for t in texts.values()):
        problems.append("no workflow runs on a schedule")
    if not any("release" in name or re.search(r"tags:\s*\n\s*-\s*[\"']?v", t) for name, t in texts.items()):
        problems.append("no workflow runs on a release tag")
    if "p13-money-matrix.py" not in blob:
        problems.append("the money matrix is not in any workflow")
    if "p13-chaos-suite.py" not in blob or "p13-load.py" not in blob:
        problems.append("the chaos suite or the load harness is not in any workflow")
    if "p13-gate-check.py" not in blob:
        problems.append("the phase gate is not in any workflow")
    if "--cov-fail-under" in blob or "fail-under" in blob:
        problems.append("a workflow gates the build on a global coverage percentage")
    if quarantine is not None:
        for line in quarantine.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not re.search(r"@\d{4}-\d{2}-\d{2}$", line):
                problems.append("quarantine entry without an expiry: %s" % line)
    return problems


def workflows(g: Gate) -> dict:
    files = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    texts = {p.name: p.read_text(encoding="utf-8") for p in files}
    q = ROOT / "tests" / "quarantine.txt"
    qtext = q.read_text(encoding="utf-8") if q.exists() else None
    problems = workflow_problems(texts, qtext)
    if qtext is None:
        problems.append("tests/quarantine.txt is missing, so flaky tests have nowhere legitimate to go")
    g.check("the CI gates cover PR, nightly and release, run this phase's checks, and never gate coverage",
            not problems, "; ".join(problems[:4]))
    g.canary("ci canary: a workflow that runs nothing is refused",
             workflow_problems({"nothing.yml": "on: push\njobs:\n  x:\n    steps:\n      - run: echo hi\n"},
                               "test_x.py::test_y  flaky, nobody, forever\n"),
             "a workflow with no gates and a quarantine entry with no expiry")
    return {"workflows": list(texts), "quarantine_entries": len([ln for ln in (qtext or "").splitlines()
                                                                 if ln.strip() and not ln.startswith("#")])}


# ------------------------------------------------------------------------------------ 7. the floor still works
def floor(g: Gate) -> None:
    for name, cmd in (
        ("every lint rule fires on a planted violation", [PY, str(ROOT / "tools" / "lint-rules.py"), "--canary"]),
        ("the OpenAPI contract audit passes", [PY, str(ROOT / "tools" / "check-openapi.py")]),
        ("the SQLite migration subset is current", [PY, str(ROOT / "tools" / "build-sqlite-migrations.py"), "--check"]),
        ("the P12 gate still passes", [PY, str(ROOT / "tools" / "p12-gate-check.py")]),
    ):
        out = run(cmd, timeout=900)
        g.check(name, out.returncode == 0, (out.stdout + out.stderr)[-300:])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    heavy = "--skip-heavy" not in args
    record = args[args.index("--record") + 1] if "--record" in args else None
    json_path = args[args.index("--json") + 1] if "--json" in args else None

    g = Gate()
    facts = {"matrix": matrix(g), "matrix_tests": matrix_tests(g, heavy=heavy), "contracts": contracts(g),
             "chaos": chaos(g, heavy=heavy), "load": load(g, heavy=heavy), "frontend": frontend(g, heavy=heavy),
             "ci": workflows(g)}
    floor(g)
    facts["checks"] = len(g.results)

    for status, name, why in g.results:
        print("%-4s %s" % (status, name + (("  — " + why) if why else "")))
    failed = g.failed
    summary = "p13-gate-check: %d passed, %d failed%s" % (
        len(g.results) - len(failed), len(failed), "" if heavy else "  (--skip-heavy)")
    print("\n" + summary)

    if record:
        target = Path(record) if os.path.isabs(record) else ROOT / record
        target.parent.mkdir(parents=True, exist_ok=True)
        invoked = [a for a in args if a not in ("--record", record, "--json", json_path)]
        lines = ["# P13 gate — recorded by tools/p13-gate-check.py",
                 "# command: python3 tools/p13-gate-check.py %s" % " ".join(invoked),
                 "# date: %s" % __import__("datetime").date.today().isoformat(), ""]
        lines += ["%-4s %s" % (st, nm + (("  — " + w) if w else "")) for st, nm, w in g.results]
        lines += ["", summary]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("recorded: %s" % target)
    if json_path:
        target = Path(json_path) if os.path.isabs(json_path) else ROOT / json_path
        target.write_text(json.dumps({"summary": summary, "facts": facts,
                                      "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results]},
                                     indent=2), encoding="utf-8")
        print("json: %s" % target)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
