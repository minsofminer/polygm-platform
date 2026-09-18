#!/usr/bin/env python3
"""Prove the P07 gate and the P07 tests can fail, by breaking each security rule on a copy of the tree.

A gate that prints "32/32" may be printing it because nothing can fail. Security code is worse than usual on this
axis: a control that is silently inert still returns the shape of a refusal to a caller who never tries the
adversarial case, and the tests in this repository were written by the same hand that wrote the modules. So each
mutant below is either the bug this phase actually had, or the exact inverse of a rule the document promises,
applied to a scratch copy of the tree — and then the thing that is supposed to notice (a gate check, or a test
file) is run there.

    python3 tools/p07-mutation-test.py                    # all mutants
    python3 tools/p07-mutation-test.py --only totp-leeway-wide,redact-key-names-inert
    python3 tools/p07-mutation-test.py --keep             # leave the copies to look at

A mutant the tree does not catch is reported `SURVIVED` and exits non-zero. Every one of those was worth the run:
in the P06 pass a survivor turned out to be a test whose assertion was inside `if service_op:`, i.e. skipped when
no route used the level — the mutation harness is a test-quality tool as much as a code-quality one.

Three rules about how this file itself is written, because all three were learned the hard way:

* Anchors are checked before anything runs. A mutant whose `find` text is not in the file would be *silently
  skipped*, and a skipped mutant inflates the kill rate — so an unfindable anchor is a hard error, not a warning.
* The baseline is run first, in the copy. If the clean copy's target does not pass, every `KILLED` below is a
  false positive, so the tool stops instead of reporting a 100 % rate for a broken tree.
* Each mutant restores its file from the pristine tree before applying, so no mutant inherits another's damage:
  a run where two edits combine is a run where a mutant can be killed by the wrong check.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
LINES: list[str] = []


def emit(text: str = "") -> None:
    """Print and keep, so `--record` can write the artifact without a shell pipe swallowing the exit code: a
    Makefile target whose `tee` hides a failing run is a target that turns red builds green."""
    LINES.append(text)
    print(text, flush=True)
SCRATCH = Path(tempfile.mkdtemp(prefix="p07-mutation-"))
SKIP_DIRS = {".git", "node_modules", "__pycache__", "var", ".tmp", ".pytest_cache", "dist", "build", ".venv",
             "docs/verification", "coverage", ".next", ".svelte-kit"}
SEC = "packages/polygm_core/security/"
CORE = "test:tests/test_security_core.py"
PLANE = "test:tests/test_security_plane.py"

# (name, file, find, replace, target, why-it-matters)
MUTANTS: list[tuple[str, str, str, str, str, str]] = [
    # --------------------------------------------------------------------- D3: passwords, TOTP, Telegram
    ("argon2-floor-removed", SEC + "passwords.py", "MIN_MEMORY_KIB = 19_456", "MIN_MEMORY_KIB = 1",
     CORE, "the floor is the published number, not a preference; with it gone a 1 MiB hash is 'Argon2id'"),
    ("argon2-floor-check-inert", SEC + "passwords.py", "    if p.memory_kib < MIN_MEMORY_KIB:", "    if False:",
     CORE, "the check that reads the floor and then does nothing is the most common shape of a fake control"),
    ("password-min-length-inert", SEC + "passwords.py", "    if len(pw) < MIN_LEN:", "    if False:",
     CORE, "12 characters is our floor because the threat model is a stolen hash table, not a shoulder surfer"),
    ("lockout-never-locks", SEC + "passwords.py", 'LOCK = {"max_failed": 10,', 'LOCK = {"max_failed": 100000,',
     CORE, "a lockout with a million attempts is a rate limit that only slows the honest user"),
    ("totp-leeway-wide", SEC + "totp.py", "LEEWAY_STEPS = 1", "LEEWAY_STEPS = 9",
     CORE, "one step either side is 90 seconds of acceptance; nine is fifteen minutes of reusable codes"),
    ("totp-attempt-cap-gone", SEC + "totp.py", "MAX_ATTEMPTS = 5", "MAX_ATTEMPTS = 5000",
     CORE, "6 digits with no attempt cap is a 5-minute brute force for anyone who phished the secret"),
    ("telegram-secret-derivation-drift", SEC + "telegram.py", 'WEBAPP_DATA = b"WebAppData"',
     'WEBAPP_DATA = b"WebAppDataV2"', CORE,
     "the HMAC key Telegram published; if we invent our own, every real login is a forged one and every forged"
     " one is real"),
    ("telegram-freshness-removed", SEC + "telegram.py", "LOGIN_MAX_AGE_S = 300", "LOGIN_MAX_AGE_S = 86_400_000",
     CORE, "a captured initData string is only useless once it is expired; 300 s is the whole replay defence"),
    ("telegram-replay-accepted", SEC + "telegram.py", 'return Verified(False, "replayed",',
     'return Verified(True, "replayed",', "gate:c5_telegram_known_answer_and_replay_across_a_restart",
     "the replay defence is the point of the nonce table; accepting a seen hash makes the table decoration"),
    # --------------------------------------------------------------------------------- D4: who may call what
    ("admin-forbidden-list-empty", SEC + "authz.py", "ADMIN_FORBIDDEN: tuple[str, ...] = (",
     "ADMIN_FORBIDDEN: tuple[str, ...] = (\n    )\n_UNUSED_FORBIDDEN: tuple[str, ...] = (", "gate:c20",
     "the two actions an admin may not do are the whole reason the admin surface is not a master key"),
    ("cooldown-zero", SEC + "authz.py", "COOLDOWN_MS: int = 24 * 60 * 60 * 1000", "COOLDOWN_MS: int = 0",
     "gate:c8", "the 24 h wait is what makes account takeover slow enough to notice; zero is no control"),
    ("access-token-ttl-month", SEC + "store.py", "ACCESS_TTL_MS = 15 * 60 * 1000",
     "ACCESS_TTL_MS = 30 * 24 * 60 * 60 * 1000", "gate:c7",
     "short access tokens are what make rotation and revocation mean anything; a month-long one is a password"),
    # ------------------------------------------------------------------- D2: keys, policy, break-glass
    ("break-glass-one-approver", SEC + "keys.py", "MIN_APPROVERS = 2", "MIN_APPROVERS = 1", "gate:c19",
     "one approver is not a second pair of hands, it is a person with a keyboard"),
    ("wrapped-length-trusted", SEC + "keys.py", "WRAPPED_LEN = KEY_BYTES + TAG_BYTES", "WRAPPED_LEN = 1",
     CORE, "the wrapped DEK's length is how a corrupt or truncated row is noticed before it is used to sign"),
    ("call-target-allowlist-open", SEC + "keys.py",
     "MAX_CALL_TARGETS: tuple[str, ...] = (CLOB_EXCHANGE, PUSD_TOKEN)",
     "MAX_CALL_TARGETS: tuple[str, ...] = ()", "gate:c9",
     "the prompt's disqualifying finding lives here: a key that may call anything is a key on a hot wallet"),
    ("revocation-rate-optimistic", SEC + "keys.py", "per_call_ms: int = 120, concurrency: int = 4",
     "per_call_ms: int = 0, concurrency: int = 4", CORE,
     "the 10,000-key plan is sized on this number; making it 0 prints '0.0 hours' next to an emergency procedure"),
    # ------------------------------------------------------------------- D5: untrusted metadata, money text
    ("trusted-resolution-hosts-empty", SEC + "sanitise.py",
     'TRUSTED_RESOLUTION_HOSTS = ("oracle.polymarket.com", "uma.project", "polymarket.com")',
     "TRUSTED_RESOLUTION_HOSTS = ()", "gate:c22",
     "the resolution source is the thing an attacker controls least — unless we do not check it at all"),
    ("http-urls-allowed", SEC + "sanitise.py", 'ALLOWED_SCHEMES = ("https",)', 'ALLOWED_SCHEMES = ("https", "http")',
     CORE, "a plain-http image or link inside market metadata is a downgrade the user cannot see"),
    # ------------------------------------------------------------------------ D6: what reaches a log
    ("redact-no-truncation", SEC + "redact.py", "MAX_FIELD = 2000", "MAX_FIELD = 10**9",
     CORE, "an unbounded field in a log line is a 2 MB order payload in a third-party retention window"),
    ("redact-key-names-inert", SEC + "redact.py", "SENSITIVE_KEYS: frozenset = frozenset({",
     "SENSITIVE_KEYS: frozenset = frozenset({\n    })\n_UNUSED_KEYS: frozenset = frozenset({", "gate:c12",
     "the key-name list is what catches `\"password\": \"...\"` in a body that matches no pattern"),
    # --------------------------------------------------------------------------- D8: abuse and the budget
    ("wash-freeze-nothing", SEC + "abuse.py", "WASH_FREEZE_BPS = 7_000", "WASH_FREEZE_BPS = 10**9",
     CORE, "a score that never reaches the threshold is a detector that only writes reports"),
    ("builder-code-never-disabled", SEC + "abuse.py", "DISABLE_AFTER_REJECTS = 5", "DISABLE_AFTER_REJECTS = 5**5",
     CORE, "if a rejected code is retried forever, the venue's decision is ours to undo"),
    ("per-user-budget-share-open", SEC + "abuse.py", "PER_USER_SHARE_CAP_BPS = 2_500",
     "PER_USER_SHARE_CAP_BPS = 10_000", "gate:c21",
     "without a share cap, one busy account exhausts the upstream budget for everyone, and the alarm blames"
     " the provider"),
    ("budget-alarm-at-the-cliff", SEC + "abuse.py", '"alarm_at_bps": 7_000}', '"alarm_at_bps": 10_000}',
     "gate:c21", "an alarm that fires only once the budget is gone is a post-mortem, not an alarm"),
    # ------------------------------------------------------------------- D9: incident response honesty
    ("drill-never-stales", SEC + "incident.py", "DRILL_MAX_AGE_MS = 90 * 24 * 3600 * 1000",
     "DRILL_MAX_AGE_MS = 10**12", CORE,
     "a drill from 2009 is not a control; the age bound is what makes the record mean anything"),
    ("forbidden-phrases-inert", SEC + "incident.py", "FORBIDDEN_PHRASES: tuple[str, ...] = (",
     "FORBIDDEN_PHRASES: tuple[str, ...] = (\n    )\n_UNUSED_PHRASES: tuple[str, ...] = (", CORE,
     "the phrase list is the control that keeps a breach notice from saying 'no funds were lost'"),
    ("drill-failure-needs-no-step", SEC + "store.py",
     '            raise ValueError("a failed drill must name the step that failed")',
     "            pass  # mutant: a failure with no step named is fine", PLANE,
     "the difference between a drill and a checkbox is that the failure says where it stopped"),
    ("append-only-check-inert", SEC + "store.py", '        if verdict == "fail" and not failed_step.strip():',
     "        if False:", PLANE, "same rule, the other half: the guard must actually be reached"),
]


def sh(cmd: list[str], cwd: Path, timeout: int = 1800) -> tuple[int, str]:
    env = dict(os.environ, TMPDIR=str(SCRATCH / "tmp"))     # /tmp fills up here; a full tmpdir fails the suite
    try:                                                    # with a confusing rc=120 that reads as a code bug
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def make_copy(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(ROOT.iterdir()):
        if item.name in SKIP_DIRS or str(item.relative_to(ROOT)) in SKIP_DIRS:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.db", "*.db-wal", "*.log",
                                                                        "node_modules", "__pycache__"))
        else:
            shutil.copy2(item, target)


def run_target(tree: Path, target: str) -> tuple[int, str]:
    kind, name = target.split(":", 1)
    if kind == "gate":
        return sh([PY, str(tree / "tools" / "p07-gate-check.py"), "--only", name], tree)
    return sh([PY, "-W", "ignore::ResourceWarning", "-m", "unittest", "discover", "-s", "tests",
               "-p", Path(name).name], tree, timeout=1500)


def main() -> int:
    ap = argparse.ArgumentParser(description="prove the P07 gate and tests can fail")
    ap.add_argument("--only", default="", help="comma-separated mutant names")
    ap.add_argument("--keep", action="store_true", help="leave the scratch copies on disk")
    ap.add_argument("--record", default="", help="write the transcript here (docs/verification/P07-mutation.txt)")
    a = ap.parse_args()
    wanted = {w.strip() for w in a.only.split(",") if w.strip()}

    bad_anchor = []
    for name, rel, old, new, target, _why in MUTANTS:
        path = ROOT / rel
        if not path.exists():
            bad_anchor.append((name, "missing file %s" % rel))
            continue
        if old not in path.read_text():
            bad_anchor.append((name, "anchor not found in %s" % rel))
    if bad_anchor:
        emit("MUTANT-CONSTRUCT ERRORS (fix these; a silent skip is not a pass):")
        for name, why in bad_anchor:
            emit("  %-32s %s" % (name, why))
        return 1

    tmp = SCRATCH
    (tmp / "tmp").mkdir(exist_ok=True)
    base = tmp / "baseline"
    emit("copying the tree to %s and checking the CLEAN targets first" % base)
    make_copy(base)
    targets = sorted({t for _n, _f, _o, _n2, t, _w in MUTANTS})
    for t in targets:
        rc, out = run_target(base, t)
        if rc != 0:
            emit("BASELINE DOES NOT PASS for %s - nothing here can be trusted:" % t)
            emit(out[-2500:])
            return 1
    emit("  baseline clean for %d target(s): %s\n" % (len(targets), ", ".join(targets)))

    caught, survived = [], []
    for name, rel, old, new, target, why in MUTANTS:
        if wanted and name not in wanted:
            continue
        dst_file = base / rel
        shutil.copy2(ROOT / rel, dst_file)                      # pristine, so mutants never combine
        dst_file.write_text(dst_file.read_text().replace(old, new, 1))
        rc, out = run_target(base, target)
        first = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith(("FAIL", "ERROR"))
                      and not ln.strip().startswith("FAILED")), "")
        if rc != 0:
            caught.append(name)
            emit("  %-32s KILLED   by %-14s %s" % (name, target, first[:70]))
        else:
            survived.append((name, target, why))
            emit("  %-32s SURVIVED by %-14s <-- %s" % (name, target, why[:64]))
        shutil.copy2(ROOT / rel, dst_file)                      # leave the copy as we found it

    total = len(caught) + len(survived)
    emit("\nmutation report: %d KILLED, %d survived of %d mutants" % (len(caught), len(survived), total))
    for name, target, why in survived:
        emit("  SURVIVED %s: nothing in %s notices it is gone - the rule is prose (%s)" % (name, target, why))
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        emit("copies left in %s" % tmp)
    emit("\nA survived mutant is not a product bug. It is a check that cannot fail, which is the same thing"
          " wearing a better shirt.")
    if a.record:
        path = Path(a.record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("P07 mutation run, recorded %s\ncommand: python3 tools/p07-mutation-test.py --record "
                        "%s\n\n%s\n" % (time.strftime("%Y-%m-%d"), a.record, "\n".join(LINES)))
        print("recorded %d lines -> %s" % (len(path.read_text().splitlines()), path))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
