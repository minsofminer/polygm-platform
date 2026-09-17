#!/usr/bin/env python3
"""Prove the P04 gate can fail, by breaking the product on a copy and requiring the gate to say so.

Why this file exists: a gate that reports 46/46 could be reporting it because nothing can fail. Each mutant
below is a bug this phase ACTUALLY had (or the exact inverse of a rule the phase depends on), applied to a
scratch copy of the tree, then `tools/p04-gate-check.py` is run there. A mutant the gate does not catch is
reported, not forgiven — it means the rule is prose.

    python3 tools/p04-mutation-test.py            # all mutants
    python3 tools/p04-mutation-test.py --only seed-literal,pipe-tail
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "__pycache__", "var", ".pytest_cache", "dist", "build", ".venv",
             ".parcel-cache", ".next"}

# The fixture this mutant plants has to LOOK like a live GitHub token, or it does not test the scan. It is
# assembled at runtime so that no credential-shaped literal exists in tracked source: a secret-scan gate that
# trips on its own test fixture is a gate people will mute, and muting it is how the next real key gets in.
# The scan still catches the planted copy in the mutated tree, which is the behaviour under test.
PLANTED = "GH_TOKEN = \"%s\"   # mutant: a pasted key" % ("ghp" + chr(95) + "A" * 40)


# (name, relative file, text to find, replacement, needs_full_gate)
# Every anchor was taken from the shipped file with an exact-text match, and main() re-verifies all of them
# against the working tree BEFORE copying: a mutant whose anchor is missing is not a caught bug, and a
# silently-dead mutant is the failure mode I refuse to ship a second time. Anchors that occur more than once
# (`{{NOW_MS}}`, 199 times in seed.sql) are fine for a first-occurrence mutation because the replacement is
# applied with count=1 and the file is otherwise untouched — but only for data files: a code mutation with an
# ambiguous anchor would depend on which match the parser happened to hit, so apply_mutation refuses those.
MUTANTS: list[tuple[str, str, str, str, bool]] = [
    # --- money --------------------------------------------------------------------
    ("float-in-money", "packages/polygm_core/money/cents.py",
     "def fmt_usdc(micro: int) -> str:",
     "def _mutant_leak(micro: int) -> float:\n    return micro * 1.0\n\n\ndef fmt_usdc(micro: int) -> str:", False),
    ("no-roundtrip-guard", "packages/polygm_core/money/cents.py",
     "    if Decimal(repr(f)).scaleb(SCALE).to_integral_value() != micro:",
     "    if False:", False),
    ("numeric-money-column", "db/migrations/0002_money.sql",
     "    notional_micro      BIGINT NOT NULL CHECK (notional_micro >= 0),",
     "    notional_micro      NUMERIC(20,6) NOT NULL CHECK (notional_micro >= 0),", False),
    ("generated-sql-stale", "db/migrations-sqlite/0001_core.sql",
     "    first_seen_ms       INTEGER NOT NULL,\n", "", False),
    # --- risk gate ------------------------------------------------------------------
    ("kill-switch-not-first", "packages/polygm_core/risk/gate.py",
     "    if kill_switch:", "    if False and kill_switch:", False),
    ("denial-spends-cap", "packages/polygm_core/risk/gate.py",
     "notional_micro: int = 0", "notional_micro: int = 1", False),
    ("tick-raw-string", "packages/polygm_core/risk/gate.py",
     "        v = Decimal(value)", "        return value", False),
    ("limits-frozen-at-import", "services/api/app.py",
     "    limits = Limits(max_order_notional_micro=f.max_order_notional_micro,",
     "    limits = LIMITS or Limits(max_order_notional_micro=0,", False),
    # --- idempotency ----------------------------------------------------------------
    ("key-not-released", "services/api/app.py",
     '    except (ScaleError, KeyError, ValueError, TypeError):\n        idem.abandon(x_user_id, idempotency_key)\n        return err("BAD_AMOUNT", rid)',
     '    except (ScaleError, KeyError, ValueError, TypeError):\n        return err("BAD_AMOUNT", rid)', False),
    ("insert-not-upsert", "services/api/app.py",
     "    if existing:", "    if False and existing:", False),
    # --- the wire contract ----------------------------------------------------------
    ("price-is-a-number", "services/api/app.py",
     'PRICE_SCHEMA = {\n    "type": "string",', 'PRICE_SCHEMA = {\n    "type": "number",', False),
    ("contract-loses-202", "contracts/openapi.yaml",
     '        "202":\n          description: queued for the executor',
     '        "200":\n          description: queued for the executor', False),
    ("undeclared-202", "services/api/app.py",
     '@app.post("/v1/orders", status_code=202, responses=ORDER_RESPONSES,',
     '@app.post("/v1/orders", responses=ORDER_RESPONSES,', True),
    ("detail-leak", "services/api/app.py",
     '"requestId": request_id}}, status_code=status,',
     '"detail": str(detail), "requestId": request_id}}, status_code=status,', False),
    ("gate-telemetry-dropped", "services/api/app.py",
     '    resp.headers["x-risk-latency-ms"] = f"{d.latency_ms:.2f}"\n    resp.headers["x-risk-checks"] = str(len(d.checks_run))\n    return resp',
     "    return resp", True),
    # --- flags & readiness ----------------------------------------------------------
    ("readyz-never-recovers", "services/api/app.py",
     "    STORE.current()\n    try:\n        _db.execute(\"SELECT 1\").fetchone()",
     "    try:\n        _db.execute(\"SELECT 1\").fetchone()", False),
    ("truthy-flag-on", "packages/polygm_core/config/flags.py",
     'if json.loads(r[1]).get("on") is True', 'if json.loads(r[1]).get("on")', False),
    # --- data lifecycle -------------------------------------------------------------
    ("seed-literal-timestamp", "db/seed.sql", "{{NOW_MS}}", "1700000000000", False),
    # Renaming the trigger is NOT a break (it still fires), so the mutant deletes the statement — the way a
    # "cleanup" commit that moves triggers between files actually breaks the rule.
    ("no-append-only-trigger", "db/migrations/0005_triggers.sql",
     "CREATE TRIGGER append_only_cash_ledger          BEFORE UPDATE OR DELETE ON cash_ledger          FOR EACH ROW EXECUTE FUNCTION polygm_reject_mutation();\n",
     "", False),
    # --- the harness itself: compose, Makefile, tool paths ---------------------------
    ("compose-missing-dockerfile", "docker-compose.yml",
     "dockerfile: services/executor-mock/Dockerfile}", "dockerfile: services/executor-mock/Nope.dockerfile}", False),
    ("pipe-tail", "Makefile",
     '\t@$(PY) -m unittest discover -s tests -p "test_*.py" -q',
     '\t$(PY) -m unittest discover -s tests -p "test_*.py" -q | tail -3', False),
    ("make-calls-missing-tool", "Makefile",
     "\t$(PY) tools/p04-mutation-test.py", "\t$(PY) tools/p04-does-not-exist.py", False),
    ("compose-duplicate-key", "docker-compose.yml", '    restart: "no"\n',
     '    restart: "no"\n    restart: "always"\n', False),
    # The secret scan's own regression: the first draft grepped `git grep`, which is blind to uncommitted
    # files, i.e. blind to the phase in progress. This mutant plants a GitHub-shaped token in NEW code.
    ("planted-secret-untracked", "services/api/app.py",
     '@app.post("/v1/orders", status_code=202',
     PLANTED + '\n\n\n@app.post("/v1/orders", status_code=202', False),
]


def make_copy(dst: Path) -> None:
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        return {n for n in names if n in SKIP_DIRS}
    shutil.copytree(ROOT, dst, ignore=ignore)
    # The gate's secret scan uses `git grep`, which needs an index. Initialising one in the copy keeps that
    # check honest; without it, `git grep` errors and a naive scan reports "clean".
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(cmd, cwd=str(dst), capture_output=True, text=True, check=False)


def run_gate(dst: Path, full: bool) -> tuple[int, str]:
    argv = [sys.executable, "tools/p04-gate-check.py"] + ([] if full else ["--fast"])
    env = dict(os.environ, PWD=str(dst))
    p = subprocess.run(argv, cwd=str(dst), env=env, capture_output=True, text=True, timeout=1200)
    return p.returncode, (p.stdout + p.stderr)


def apply_mutation(dst: Path, rel: str, old: str, new: str) -> str | None:
    f = dst / rel
    if not f.is_file():
        return "file not found: %s" % rel
    txt = f.read_text()
    if old not in txt:
        return "anchor not present in %s: %r" % (rel, old[:60])
    if txt.count(old) > 1 and rel.endswith(".py"):
        return "anchor is ambiguous in %s (%d matches) — the mutant would be non-deterministic" % (rel, txt.count(old))
    f.write_text(txt.replace(old, new, 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true", help="leave the copies behind for inspection")
    a = ap.parse_args()
    wanted = {x.strip() for x in a.only.split(",") if x.strip()}
    names = [m[0] for m in MUTANTS]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        print("duplicate mutant names:", dupes)
        return 2

    bad = []
    for name, rel, old, _new, _f in MUTANTS:
        f = ROOT / rel
        if not f.is_file():
            bad.append("%s: %s missing" % (name, rel))
        elif f.read_text().count(old) < 1:
            bad.append("%s: anchor not present in %s" % (name, rel))
    if bad:
        print("MUTANT ANCHORS DO NOT MATCH THE TREE:")
        for b in bad:
            print("  " + b)
        print("\nThis is not a product bug and not a pass. Fix the anchors; a silent skip here would "
              "shrink the proof set without saying so.")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="p04-mutation-"))
    base = tmp / "baseline"
    print("copying the tree to %s and checking the CLEAN gate first" % base)
    make_copy(base)
    rc, out = run_gate(base, full=False)
    if rc != 0:
        print("BASELINE GATE DOES NOT PASS — nothing here can be trusted:")
        print(out[-2500:])
        return 1
    print("  baseline: %s" % out.splitlines()[0])

    caught: list[str] = []
    missed: list[tuple[str, str]] = []
    broken: list[tuple[str, str]] = []
    for name, rel, old, new, full in MUTANTS:
        if wanted and name not in wanted:
            continue
        dst = tmp / ("m-" + name)
        shutil.rmtree(dst, ignore_errors=True)
        make_copy(dst)
        err = apply_mutation(dst, rel, old, new)
        if err:
            broken.append((name, err))
            print("  %-26s MUTANT-ERROR  %s" % (name, err))
            continue
        rc, out = run_gate(dst, full=full)
        fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]
        head = out.splitlines()[0] if out.splitlines() else "(no output)"
        if rc != 0 and fails:
            caught.append(name)
            print("  %-26s caught by: %s" % (name, fails[0][:100]))
        else:
            missed.append((name, head))
            print("  %-26s *** NOT CAUGHT *** (%s)" % (name, head[:80]))
        if not a.keep:
            shutil.rmtree(dst, ignore_errors=True)

    print("\nmutation report: %d caught, %d NOT caught, %d mutant-construct errors (%d mutants)"
          % (len(caught), len(missed), len(broken), len(caught) + len(missed) + len(broken)))
    for name, why in missed:
        print("  UNCAUGHT %s: the gate cannot see this class of breakage (%s)" % (name, why))
    for name, why in broken:
        print("  BROKEN   %s: %s" % (name, why))
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("copies left in", tmp)
    print("\nA missed mutant is not a pass for the product — it is a rule the gate only states in prose.")
    return 1 if (missed or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
