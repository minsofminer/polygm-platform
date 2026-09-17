#!/usr/bin/env python3
"""P04 Quality Gate. Every rule here is EXECUTED, and each one reports what it counted.

Format of a check: (label, ok, detail). `detail` is required even on success where a number is meaningful,
because "a check that only speaks when it fails is indistinguishable from one that never ran" (00-SHARED-
CONTEXT rule, learned in P02). Run with `--list` to print the rules without executing them, `--fast` to skip
the steps that spawn servers (used by the mutation harness).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TRACEABLE = (".py", ".yaml", ".yml", ".sql", ".json", ".md", ".ts", ".tsx", ".js", ".mjs", ".css", ".sh",
             ".toml", ".env", ".example", ".txt")
CRED_RE = re.compile(r"ghp_[A-Za-z0-9]{20}|github_pat_[A-Za-z0-9_]{20}|-----BEGIN [A-Z ]*PRIVATE KEY|"
                     r"sk_live_[A-Za-z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}")
CONFIG_RE = re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\\-]{16,}['\"]")
PY = sys.executable


def sh(argv: list[str], cwd: Path | None = None, env: dict | None = None, timeout: int = 900):
    e = dict(os.environ)
    e["PYTHONPATH"] = "%s:%s:%s:%s" % (ROOT / "packages", ROOT / "services" / "api",
                                        ROOT / "services" / "executor-mock", ROOT / "tools")
    e.update(env or {})
    return subprocess.run(argv, cwd=str(cwd or ROOT), env=e, capture_output=True, text=True, timeout=timeout)


def read(rel: str) -> str:
    return (ROOT / rel).read_text()


# --------------------------------------------------------------------------- checks
def g1_tests(fast: bool) -> list[tuple[str, bool, str]]:
    r = sh([PY, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-q"])
    out = r.stdout + r.stderr
    m = re.search(r"Ran (\d+) tests", out)
    n = int(m.group(1)) if m else 0
    ok = r.returncode == 0 and n >= 145 and "OK" in out
    return [("suite: %d tests, exit %d, tail %r" % (n, r.returncode, out.strip().splitlines()[-1][:60]),
             ok, out[-500:])]


def g2_money() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    lint = sh([PY, "tools/lint-rules.py"])
    out.append(("lint-rules clean (%s)" % (lint.stdout.splitlines() or [""])[0][:70],
                lint.returncode == 0, lint.stdout[-400:]))
    canary = sh([PY, "tools/lint-rules.py", "--canary"])
    fired = re.search(r"(\d+)/(\d+) rules fire", canary.stdout)
    out.append(("every lint rule fires on a planted violation (%s)" % (fired.group(0) if fired else "no count"),
                bool(fired) and fired.group(1) == fired.group(2), canary.stdout[-300:]))
    # Column definitions only, never prose: an earlier draft matched the sentence "never NUMERIC for money"
    # and reported a column called `never`. SQL is read line-by-line with comments stripped, the same rule
    # tools/build-sqlite-migrations.py had to learn the hard way.
    mig = ""
    for f in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        for ln in f.read_text().splitlines():
            if not ln.strip().startswith("--"):
                mig += ln.split("--")[0] + "\n"
    floats = [ln.strip() for ln in mig.splitlines()
              if re.match(r"^\s*\w+_micro\s+(NUMERIC|REAL|FLOAT|DOUBLE)", ln, re.I)]
    out.append(("no float/NUMERIC money column in Postgres migrations (%d found)" % len(floats),
                not floats, "; ".join(floats)))
    numeric_ok = set(re.findall(r"^\s+(\w+)\s+NUMERIC", mig, re.M))
    allowed = {"minimum_tick_size", "minimum_order_size", "risk_latency_ms"}
    out.append(("NUMERIC only for the three metadata columns (%s)" % (sorted(numeric_ok) or "none"),
                numeric_ok <= allowed, "unexpected: %s" % sorted(numeric_ok - allowed)))
    out.append(("the three NUMERIC metadata columns are actually declared (%d/3)" % len(numeric_ok & allowed),
                numeric_ok == allowed, "found %s" % sorted(numeric_ok)))
    cents = read("packages/polygm_core/money/cents.py")
    calls = [ln.strip() for ln in cents.splitlines()
             if "float(" in ln and not ln.strip().startswith(("#", '"', "'")) and "`" not in ln.split("float")[0]]
    code_calls = [ln for ln in calls if re.match(r"^\w+\s*=\s*float\(", ln) or re.match(r"^return float\(", ln)]
    out.append(("the float boundary is exactly ONE assignment, inside to_float_for_sdk (%s)" % len(code_calls),
                cents.count("def to_float_for_sdk") == 1 and len(code_calls) == 1
                and "cannot round-trip through float" in cents,
                "; ".join(code_calls)))
    contract = read("contracts/openapi.yaml")
    out.append(("contract: Price is a string pattern, not a number",
                bool(re.search(r"Price:\n(?:.*\n)*?\s+type: string", contract)), ""))
    return out


def g3_gate() -> list[tuple[str, bool, str]]:
    gate = read("packages/polygm_core/risk/gate.py")
    body = gate[gate.index("def evaluate"):]
    def pos(needle: str) -> int:
        i = body.find(needle)
        return i if i >= 0 else 10 ** 6
    order = [pos('kill_switch') , pos('accepting_orders'), pos('snap_age'), pos('side not in'),
              pos('UNKNOWN_TICK'), pos('OFF_TICK'), pos('ZERO_SIZE'), pos('BELOW_MIN_SIZE'),
              pos('BAD_AMOUNT'), pos('OVER_ORDER_CAP'), pos('PRICE_FAR_FROM_MID')]
    out = [("kill switch is the first check, and the order is %s" % ("ascending" if order == sorted(order) else "WRONG"),
            order == sorted(order), "positions: %s" % order)]
    # Denials are built by one helper, so the invariant is checkable at the helper rather than by counting
    # occurrences of `notional_micro=0` (which the first draft did, and which reported FAIL on correct code:
    # Decision's field DEFAULT is 0 and deny() simply never passes one).
    deny_src = gate[gate.index("def deny("):gate.index("def deny(") + 700]
    default_zero = bool(re.search(r"notional_micro:\s*int\s*=\s*0", gate))
    out.append(("Decision.notional_micro defaults to 0 and deny() never sets it (a refusal must not spend "
                "the 24h cap)",
                default_zero and "notional_micro" not in deny_src.split(")")[0],
                "default=%s deny-sets-notional=%s" % (default_zero, "notional_micro=" in deny_src)))
    outs = []
    outs.append(("every denial path goes through deny() (no hand-rolled Decision(False, ...) that could "
                 "forget the zero)",
                 gate.count("return deny(") >= 12 and "Decision(False" not in gate,
                 "deny() uses: %d" % gate.count("return deny(")))
    out.append(("limits come from flags at request time, not a module constant",
                "limits = Limits(" in read("services/api/app.py")
                and "limits=LIMITS," not in read("services/api/app.py"), ""))
    return out


def g4_idempotency() -> list[tuple[str, bool, str]]:
    idem = read("packages/polygm_core/risk/idempotency.py")
    app = read("services/api/app.py")
    handler = app[app.index("def place_order"):app.index('@app.get("/v1/orders/intents')]
    outs: list[tuple[str, bool, str]] = []

    outs.append(("begin() answers 'did I create this row?' with RETURNING (sqlite) and xmax=0 (postgres)",
                 "RETURNING" in idem and "xmax = 0" in idem, ""))

    # The properties must EXIST on the class, not merely appear in the text: correctness rests on `rec.busy`
    # being distinguishable from `rec.replay`, and a rename would otherwise pass here and 409 every first
    # order in production.
    sys.path.insert(0, str(ROOT / "packages"))
    from polygm_core.risk.idempotency import Record
    bad = [a for a in ("replay", "busy", "mismatch") if not isinstance(getattr(Record, a, None), property)]
    outs.append(("Record exposes replay/busy/mismatch as properties (state alone cannot tell the owner from "
                 "a waiter)", not bad, "not properties: %s" % bad))

    # The precise rule, not a ratio. IDEM_CONFLICT and IDEM_IN_PROGRESS must NOT release the key (it belongs
    # to another request) and the replay returns a JSONResponse, not err(); every other denial after begin()
    # has an abandon() within the lines above it, or the user's retry is a 409 against a dead row.
    lines = handler.splitlines()
    start = next(k for k, l in enumerate(lines) if "idem.begin" in l)
    exempt = {"IDEM_CONFLICT", "IDEM_IN_PROGRESS"}
    unpaired, checked = [], 0
    for k in range(start, len(lines)):
        m = re.search(r'return err\("([A-Z_]+)"', lines[k])
        if not m:
            continue
        code = m.group(1)
        checked += 1
        if code in exempt:
            continue
        window = "\n".join(lines[max(start, k - 8):k + 1])
        if "idem.abandon(" not in window:
            unpaired.append("%s@line%d" % (code, k))
    outs.append(("every denial after begin() releases the key (%d returns audited, %d unpaired)"
                 % (checked, len(unpaired)), not unpaired, "; ".join(unpaired)))

    outs.append(("body shape is checked BEFORE idem.begin (a 422 must not burn a key)",
                 handler.index("_check_body") < handler.index("idem.begin"), ""))

    up = app[app.index("def _upsert_intent"):app.index('@app.post("/v1/orders"')]
    outs.append(("a rejected intent is REVISED, not re-inserted (one key, one row; a bare INSERT 500s the "
                 "retry against UNIQUE(user_id, idempotency_key))",
                 "UPDATE order_intents" in up and "INSERT INTO order_intents" in up
                 and "WHERE user_id=? AND idempotency_key=?" in up, ""))
    return outs


def g5_contract() -> list[tuple[str, bool, str]]:
    r = sh([PY, "tools/check-openapi.py"])
    m = re.search(r"(\d+) passed, (\d+) failed", r.stdout)
    passed, failed = (int(m.group(1)), int(m.group(2))) if m else (0, -1)
    st = sh([PY, "tools/check-openapi.py", "--self-test"])
    m2 = re.search(r"(\d+) passed, (\d+) failed", st.stdout)
    selfp, selff = (int(m2.group(1)), int(m2.group(2))) if m2 else (0, -1)
    return [("openapi audit: %d checks passed, %d failed" % (passed, failed), r.returncode == 0 and passed >= 60,
             r.stdout[-400:]),
            ("openapi audit self-test fires: %d/%d" % (selfp, selfp + max(selff, 0)),
             st.returncode == 0 and selfp >= 10, st.stdout[-300:])]


def g6_end_to_end(fast: bool) -> list[tuple[str, bool, str]]:
    if fast:
        return [("envelope demo SKIPPED (--fast; CI and make gate run it)", True, "")]
    r = sh([PY, "tools/envelope-demo.py"], timeout=240)
    return [("envelope demo over real sockets (12 steps)", r.returncode == 0,
             (r.stdout + r.stderr)[-600:])]


def g7_migrations() -> list[tuple[str, bool, str]]:
    chk = sh([PY, "tools/build-sqlite-migrations.py", "--check"])
    outs = [("generated SQLite subset is current", chk.returncode == 0, chk.stdout[-300:])]
    seed = read("db/seed.sql")
    abs_ts = re.findall(r"\b17[0-9]{11}\b", seed)
    outs.append(("seed.sql stamps with {{NOW_MS}}, never an absolute timestamp (%d literals found)" % len(abs_ts),
                 "{{NOW_MS}}" in seed and not abs_ts,
                 "a frozen timestamp makes a fresh dev database read as STALE_QUOTE — found by envelope-demo"))
    runsql = read("tools/run-sql.py")
    outs.append(("run-sql records a ledger and refuses drift on an applied file",
                 "schema_migrations" in runsql and "MIGRATION DRIFT" in runsql, ""))
    outs.append(("dollar-quote-aware statement splitter exists (naive split on ';' breaks triggers)",
                 "def split_statements" in runsql and "in_dollar" in runsql, ""))
    return outs


def g8_schema_rules() -> list[tuple[str, bool, str]]:
    core = "".join(f.read_text() for f in sorted((ROOT / "db" / "migrations").glob("*.sql")))
    append_only = ["cash_ledger", "flag_audit", "kill_switch_state", "audit_log"]
    outs = []
    for t in append_only:
        trig = re.search(r"CREATE TRIGGER[^;]*BEFORE\s+(?:UPDATE|DELETE)[^;]*ON\s+%s\b" % t, core) is not None
        grants = re.search(r"REVOKE[^;]*%s[^;]*(?:UPDATE|DELETE)" % t, core, re.I) is not None
        outs.append(("%s is append-only by trigger%s" % (t, " and grant" if grants else ""), trig,
                      "trigger=%s grant=%s" % (trig, grants)))
    outs.append(("kill_switch reason length is a DB CHECK, not a code comment",
                  "length(reason) BETWEEN 4 AND 400" in core, ""))
    outs.append(("cash_ledger's uniqueness is (ref_table, ref_id, kind, user_id)",
                  "UNIQUE (ref_table, ref_id, kind, user_id)" in core or
                  "UNIQUE (ref_table, ref_id, kind, user_id)" in read("db/migrations/0002_money.sql"), ""))
    idx = re.findall(r"CREATE (?:UNIQUE )?INDEX[^;]*;", core, re.S)
    bad_idx = [i.split()[3] for i in idx if "book_levels" in i and "updated_ms" in i]
    outs.append(("no index leads with book_levels.updated_ms (%d such index statements checked)" % len(idx),
                 not bad_idx, "; ".join(bad_idx)))
    outs.append(("there is no payout_micro column (a payout is a ledger kind, never a mutable field)",
                 "payout_micro" not in core, ""))
    return outs


def yaml_strict(text: str) -> tuple[bool, str]:
    """Parse YAML and REJECT duplicate mapping keys.

    `yaml.safe_load` silently keeps the last value where Compose (go-yaml) aborts with "mapping key already
    defined", so a compose file validated with safe_load can still be a compose file that never comes up.
    This check exists because a duplicated `restart:` under the `seed` service sat in the file for a day,
    invisible to every check that used safe_load — which is the same class of hole as a `[UNVERIFIED]` label
    standing in for "I did not look".

    Keys are compared as their scalar text and merge keys (`<<: *env`) are skipped: PyYAML has no constructor
    for the merge tag (it is special-cased inside flatten_mapping), so constructing one here would raise on a
    file that Compose is perfectly happy with.
    """
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def construct_checked(loader, node, deep=False):
        seen = set()
        for k, _v in node.value:
            if k.tag == "tag:yaml.org,2002:merge":
                continue
            key = k.value if isinstance(k, yaml.ScalarNode) else "<complex key>"
            if key in seen:
                raise yaml.YAMLError("duplicate key %r near line %d (safe_load forgives it; "
                                     "`docker compose up` aborts on it)" % (key, k.start_mark.line + 1))
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_checked)
    try:
        return bool(yaml.load(text, Loader=Strict)), ""
    except (yaml.YAMLError, AttributeError) as e:
        return False, str(e)[:200]


def g9_ops(fast: bool) -> list[tuple[str, bool, str]]:
    import yaml
    outs: list[tuple[str, bool, str]] = []
    compose_txt = read("docker-compose.yml")
    strict_ok, strict_why = yaml_strict(compose_txt)
    if not strict_ok:
        # No point continuing to the safe_load assertions: they would pass on a document Compose refuses.
        return [("compose parses with NO duplicate mapping keys (%d lines)" % compose_txt.count("\n"),
                 False, strict_why)]
    doc = yaml.safe_load(compose_txt)
    svcs = doc.get("services") or {}
    missing = []
    for name, svc in svcs.items():
        dfile = (svc.get("build") or {}).get("dockerfile")
        ctx = (svc.get("build") or {}).get("context") or "."
        if dfile:
            path = (ROOT / ctx / dfile).resolve() if ctx != "." else (ROOT / dfile)
            if not path.is_file():
                missing.append("%s -> %s" % (name, path.relative_to(ROOT) if str(path).startswith(str(ROOT)) else path))
    outs.append(("every compose build target exists (%d services, %d missing)" % (len(svcs), len(missing)),
                 not missing, "; ".join(missing)))
    outs.append(("compose strict-parses on the first pass (%d lines, 0 duplicate keys)"
                 % compose_txt.count("\n"), strict_ok, strict_why))
    outs.append(("compose parses and names the P04 surface",
                 {"api", "executor-mock", "migrate", "postgres", "pgbouncer", "redis"} <= set(svcs),
                 str(sorted(svcs))))
    env_file = json.dumps(svcs.get("api", {}).get("env_file") or doc.get("x-env", {}).get("env_file"))
    outs.append((".env is optional-at-boot for services (required: false), so a fresh clone starts",
                 "required" in env_file or "false" in env_file, env_file[:80]))
    mk = read("Makefile")
    piped = [ln for ln in mk.splitlines() if re.match(r"^\t.*\| *(tail|head|grep)", ln)]
    outs.append(("no make recipe pipes a check into tail (a pipe hides the exit status) (%d found)" % len(piped),
                 not piped, "; ".join(piped)))
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", mk, re.M))
    needed = {"dev", "dev-core", "test", "lint", "typecheck", "openapi", "gate", "gate-mutate", "check",
              "migrate", "seed", "doctor", "envelope"}
    gap = sorted(needed - targets)
    outs.append(("every promised target exists (%s)" % ("all %d" % len(needed) if not gap else "missing: %s" % gap),
                 not gap, "; ".join(gap)))
    # a target's referenced files must exist, or `make gate` is a 404 with a nice name
    refs = set(re.findall(r"\$\(PY\) (tools/[\w./-]+\.py)", mk))
    gone = [r for r in refs if not (ROOT / r).is_file()]
    outs.append(("every tool a make target runs is on disk (%d referenced, %d missing)" % (len(refs), len(gone)),
                 not gone, "; ".join(gone)))
    if not fast:
        d = sh([PY, "tools/doctor.py"])
        outs.append(("make doctor runs and reports the tooling honestly", d.returncode == 0 and "MISSING" in d.stdout,
                     d.stdout.splitlines()[0][:80]))
    return outs


def g10_flags() -> list[tuple[str, bool, str]]:
    flags = read("packages/polygm_core/config/flags.py")
    app = read("services/api/app.py")
    readyz = app[app.index("def readyz"):app.index("@app.get(\"/v1/markets/{market_id}\"")]
    outs = [("readiness REFRESHES the store, so a pod recovers without a restart", "STORE.current()" in readyz, ""),
            ("boot read happens at import, so a fresh pod is not stuck 'not ready'",
             "STORE.current()" in app[:app.index("def readyz")], ""),
            ("numeric flags never carry `on` (bool(1234) would enable a feature)",
             'is True]' in flags and '"on": bool(value)' in flags and "kind == \"bool\"" in flags, ""),
            ("a flag write needs a reason >= 4 chars and lands in flag_audit",
             "flag_audit" in flags and "not a flag change" in flags, ""),
            ("Flags has no boot_defaults_used field (it lives on the store, where it is observable)",
             "boot_defaults_used" not in flags[flags.index("class Flags"):flags.index("class FlagStore")], "")]
    return outs


def g11_docs() -> list[tuple[str, bool, str]]:
    d = ROOT / "docs" / "P04-backend-architecture.md"
    if not d.is_file():
        return [("docs/P04-backend-architecture.md exists", False, "not written yet")]
    t = d.read_text()
    heads = re.findall(r"^## (D\d+)", t, re.M)
    outs = [("decision sections D1-D8 present (%s)" % ",".join(heads),
             all(("D%d" % i) in heads for i in range(1, 9)), str(heads)),
            ("the compose path is marked [UNVERIFIED], not silently assumed",
             "[UNVERIFIED]" in t and "docker" in t.lower(), ""),
            ("D2 states the measured SDK fact that decided the stack (V2 args are float)",
             "float" in t and "1.1.0" in t, ""),
            ("D4 carries the four measured precision numbers",
             all(k in t for k in ("6/99", "126/999", "499,999", "2**53")), ""),
            ("the environment's missing tools are listed (docker/psql/sqlite3 CLI)",
             all(k in t for k in ("docker", "psql", "sqlite3")), ""),
            ("every check in this gate is claimed with a number, not an adjective",
             bool(re.search(r"\b\d+ tests\b", t)) and bool(re.search(r"\b\d+ passed\b", t)), "")]
    return outs


def g12_secrets() -> list[tuple[str, bool, str]]:
    """Scan the WORKING TREE, not the git index.

    The first draft used `git grep`, which only searches tracked files — so while the whole P04 surface sat
    uncommitted it scanned the P01-P03 files and reported "clean" about a phase that had not been added yet.
    `git ls-files --cached --others --exclude-standard` is the honest set: committed, staged and unstaged,
    still honouring .gitignore.

    A fixture that must LOOK like a secret (the lint rule's own canary) is exempted by exact line, through
    tools/secret-scan-allowlist.json, and every entry there must still match a real line — an exemption that
    stops matching cannot quietly grow into a hiding place.
    """
    listed = sh(["git", "ls-files", "--cached", "--others", "--exclude-standard"])
    if listed.returncode != 0:
        return [("the working tree can be enumerated with git ls-files", False, listed.stderr.strip()[:120])]
    ALLOWLIST = "tools/secret-scan-allowlist.json"
    # The allowlist is skipped by the scan itself: an exemption must LOOK like the secret it exempts, so the
    # file can never be scanned without tripping on its own contents. What keeps it honest is the second check
    # below - an entry that does not mirror a live line in a real file is reported as stale - so the file
    # cannot be used as a hiding place for a key that exists nowhere else.
    files = [f for f in listed.stdout.splitlines() if f.endswith(TRACEABLE) and f != ALLOWLIST]
    allow: dict[str, set[str]] = {}
    allow_path = ROOT / "tools" / "secret-scan-allowlist.json"
    if allow_path.is_file():
        allow = {k: set(v) for k, v in json.loads(allow_path.read_text()).items()}
    hits, stale = [], []
    used = {f: set() for f in allow}
    for f in files:
        try:
            txt = (ROOT / f).read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for ln in txt.splitlines():
            if CRED_RE.search(ln):
                if ln.strip() in allow.get(f, ()):
                    used[f].add(ln.strip())
                else:
                    hits.append("%s: %s" % (f, ln.strip()[:70]))
            elif CONFIG_RE.search(ln) and "env.example" not in f:
                if ln.strip() in allow.get(f, ()):
                    used[f].add(ln.strip())
                else:
                    hits.append("%s: %s" % (f, ln.strip()[:70]))
    for f, lines in allow.items():
        for want in lines:
            if want not in used.get(f, set()):
                stale.append("%s: %s" % (f, want[:50]))
    return [("no credential-shaped string in %d working-tree files (%d patterns, allowlist enforced by exact "
             "line)" % (len(files), 2), not hits, "; ".join(hits[:3]) or "0 hits"),
            ("every secret-scan exemption still matches a line (a dead exemption would hide the next real key)",
             not stale, "; ".join(stale[:3]) or "all %d entries live" % sum(len(v) for v in allow.values())),
            ("the scan covers the working tree, not just what git has tracked (a phase in progress is exactly "
             "when a key gets pasted)",
             len(files) >= 40, "%d files" % len(files)),
            (".env is ignored by git", sh(["git", "check-ignore", "-q", ".env"]).returncode == 0, ""),
            ("the env example has no values an attacker can use",
             not re.search(r"(?m)^(?:PGM_|POLYGM_|TELEGRAM_|STRIPE_)\w*=\S*(?:[A-Za-z0-9]{20,})", read(".env.example")),
             "")]


CHECKS = [g2_money, g3_gate, g4_idempotency, g5_contract, g7_migrations, g8_schema_rules, g10_flags,
          g11_docs, g12_secrets]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="skip the steps that start servers (mutation harness)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for f in [g1_tests, *CHECKS, lambda: g9_ops(a.fast), lambda: g6_end_to_end(a.fast)]:
            print(f.__name__)
        return 0
    results: list[tuple[str, bool, str]] = []
    results += g1_tests(a.fast)
    for fn in CHECKS:
        results += fn()
    results += g9_ops(a.fast)
    results += g6_end_to_end(a.fast)
    ok = sum(1 for _l, c, _d in results if c)
    print("P04 gate: %d/%d checks passed" % (ok, len(results)))
    for label, cond, detail in results:
        print("  %s %s" % ("PASS" if cond else "FAIL", label))
        if not cond and detail:
            for ln in str(detail).strip().splitlines()[-6:]:
                print("        |", ln[:160])
    print("gate is a floor: any FAIL here means the phase is not done, whatever the prose elsewhere says")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
