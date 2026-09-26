#!/usr/bin/env python3
"""P04 lint: mechanical enforcement of the phase's hard constraints.

Rules live here rather than in prose because a constraint nobody can run is a preference. Every rule has
already caught something real during this phase, or it is not in the file.

Two design decisions that keep this usable, and one that keeps it honest:
  * An escape hatch exists (`# lint-allow: <reason>`), because a rule with no exit gets deleted the first
    time it cries wolf. The reason text makes the exemption reviewable.
  * Findings are judged on CODE, not on string literals or comments: half of the money-path noise in an
    earlier draft of this file was a raised error message mentioning `1.0`. A rule that fires on prose is a
    rule people mute.
  * `--canary` plants one violation per rule and fails if any rule cannot see it. A rule that matches
    nothing is decoration.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = ["packages", "services", "db", "tests", "contracts"]
SKIP_DIR = {"__pycache__", "node_modules", "var", ".git", "dist", "build"}
SELF = Path(__file__).resolve()
Finding = tuple[str, int, str, str]

# ---------------------------------------------------------------- code-only line filter
def code_lines(text: str) -> list[tuple[int, str, str]]:
    """(lineno, code, raw) per line: `code` has string literals and comments blanked, `raw` does not.

    Shape-of-value rules (is there a literal on the right of this assignment?) must read `raw`; structure
    rules (does this line import V1?) must read `code`. Using one for the other is how a linter ends up
    matching its own regex source or a docstring that mentions a float.

    Blanking, not deleting: line numbers and column widths must survive so a finding points at the right
    place. Quotes are walked, not regexed, because `"don't"` inside a triple-quoted docstring breaks every
    naive approach (this exact mistake cost an hour earlier in the phase, on the SQL comment stripper).
    """
    out: list[tuple[int, str, str]] = []
    in_doc: str | None = None
    for no, raw in enumerate(text.splitlines(), 1):
        line, i, n = [], 0, len(raw)
        while i < n:
            if in_doc:
                j = raw.find(in_doc, i)
                if j < 0:
                    i = n
                else:
                    i = j + 3
                    in_doc = None
                continue
            ch = raw[i]
            if ch == "#":
                break
            if ch in "\"'":
                trip = raw.startswith('"""', i) or raw.startswith("'''", i)
                q = raw[i:i + 3] if trip else ch
                i += len(q)
                if trip:
                    j = raw.find(q, i)
                    if j < 0:
                        in_doc = q
                        i = n
                    else:
                        i = j + 3
                else:
                    while i < n:
                        if raw[i] == "\\":
                            i += 2
                            continue
                        if raw[i] == q:
                            i += 1
                            break
                        i += 1
                line.append("STR")
                continue
            line.append(ch)
            i += 1
        out.append((no, "".join(line), raw))
    return out


def allow(line: str) -> bool:
    return "lint-allow:" in line


def rel_of(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- rules
MONEY_RE = re.compile(r"(^|/)(money|risk|ledger|executor|config)/|/cents\.py$|/gate\.py$|/ledger\.py$|"
                      r"/executor\.py$|/idempotency\.py$|/flags\.py$")


def r_float_in_money(files: list[Path]) -> list[Finding]:
    """No float arithmetic in the money path. `to_float_for_sdk` is the single, checked crossing."""
    out: list[Finding] = []
    pat = re.compile(r"\bfloat\s*\(|[*+/]\s*\d+\.\d+|\b\d+\.\d+\s*[*+/]")
    exc = re.compile(r"^to_float_for_sdk$")  # the one sanctioned crossing, and it asserts the round trip
    for p in files:
        rel = rel_of(p)
        if "/tests/" in "/" + rel or not MONEY_RE.search(rel) or not rel.endswith(".py"):
            continue
        in_exempt = False
        for no, code, raw in code_lines(p.read_text(errors="replace")):
            m = re.match(r"^(?:async )?def\s+(\w+)", code)
            if m:                                   # exemption is scoped to the function, not the file:
                in_exempt = bool(exc.match(m.group(1)))   # a whole-file pass would hide the next offender
            if code and not code.startswith((" ", "\t")) and not m:
                in_exempt = False
            if pat.search(code) and not in_exempt and not allow(raw):
                out.append((rel, no, "money-no-float", raw.strip()[:110]))
    return out


def r_money_column_type(files: list[Path]) -> list[Finding]:
    """Every *_micro column is an integer type; NUMERIC money columns are the precision illusion we avoided."""
    out: list[Finding] = []
    bad = re.compile(r"\b(\w+_micro)\s+(NUMERIC|DECIMAL|REAL|FLOAT|DOUBLE PRECISION|DOUBLE)\b", re.I)
    for p in files:
        if not p.name.endswith(".sql"):
            continue
        for no, code, raw in code_lines(p.read_text(errors="replace")):
            m = bad.search(code)
            if m:
                out.append((rel_of(p), no, "money-col-integer", "%s is %s" % (m.group(1), m.group(2))))
    return out


SECRET_NAME = re.compile(r"(?i)\b(private_key|privkey|secret_key|api_key|apikey|auth_token|access_token|"
                         r"bot_token|mnemonic|seed_phrase|signing_key|webhook_secret)\b")
# A secret-named argument or field with a literal value, seen one fragment at a time. `STR` is what
# `code_lines` leaves behind for any quoted body, and `environ`/`getenv` stay clean because indirection is the
# mechanism this repo uses for every real secret.
FRAG_SECRET = re.compile(r"\b(" + "|".join(
    ("private_key", "privkey", "secret_key", "api_key", "apikey", "auth_token", "access_token", "bot_token",
     "mnemonic", "seed_phrase", "signing_key", "webhook_secret", "session_token", "refresh_token", "admin_token",
     "init_data", "initdata", "totp_secret", "kek", "wrapped_dek")) + r")\b\s*(?::\s*[\w\[\]]+)?\s*=\s*STR\s*$",
    re.I)


SECRET_SHAPE = re.compile(r"\bghp_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
                          r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bsk_(live|test)_[A-Za-z0-9]{16,}\b|"
                          r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\beyJ[A-Za-z0-9_-]{20,}\.eyJ")


def r_secrets(files: list[Path]) -> list[Finding]:
    """A secret assigned to a name, or any token-shaped literal. `key = os.environ[...]` is fine by design:
    indirection through the environment is the mechanism, not the leak."""
    out: list[Finding] = []
    raw_assign = re.compile(r"(?i)\b(private_key|secret_key|api_key|apikey|auth_token|access_token|bot_token|"
                            r"mnemonic|seed_phrase|webhook_secret)\b\s*[:=]\s*[\"'][^\"']{12,}[\"']")
    for p in files:
        rel = rel_of(p)
        text = p.read_text(errors="replace")
        for no, code, raw in code_lines(text):
            if allow(raw) or not code.strip():
                continue                       # a docstring line is not an assignment
            if SECRET_SHAPE.search(raw):
                out.append((rel, no, "no-secrets", "token-shaped literal: " + raw.strip()[:70]))
                continue
            if raw_assign.search(raw):
                out.append((rel, no, "no-secrets", "literal assigned to a secret name: " + raw.strip()[:70]))
                continue
            # Per-argument, not per-line. `code_lines` has already replaced every string body with STR, so the
            # shape to look for is "a secret NAME and its own literal, in the same comma-separated fragment".
            # Scanning the whole line instead made `def verify(query: str, bot_token: str, *, at: int,
            # purpose: str = "login")` a finding — a parameter *named* bot_token is the correct way to take a
            # secret, and a rule that calls that a leak gets `lint-allow:` sprayed over the codebase until the
            # marker stops meaning anything. `api_key: str = STR` still trips it, which is the case that matters:
            # a Pydantic field default is a secret someone committed.
            for frag in code.split(","):
                if FRAG_SECRET.match(frag.strip()):
                    out.append((rel, no, "no-secrets", "secret-named field given a literal: " + raw.strip()[:70]))
                    break
    return out


def r_gate_order(files: list[Path]) -> list[Finding]:
    """An order reaches the venue ONLY through packages/polygm_core/executor, which owns signing, the
    idempotency key and the UNCERTAIN state. A web-facing service that posts to the CLOB directly bypasses the
    risk gate, the ledger and crash recovery in one line, and it compiles happily."""
    out: list[Finding] = []
    bad = re.compile(r"\b(create_order|create_and_post_order|post_order|post_orders|place_order_at_venue|"
                     r"cancel_orders)\s*\(")
    for p in files:
        rel = rel_of(p)
        if not rel.startswith("services/api/") and rel != "services/webhook/app.py":
            continue
        for no, code, raw in code_lines(p.read_text(errors="replace")):
            if bad.search(code) and not allow(raw):
                out.append((rel, no, "order-via-gate", raw.strip()[:100]))
    return out


def r_v1_client(files: list[Path]) -> list[Finding]:
    """CLOB V2 only; V1 is a dead protocol whose orders are rejected. Silent until an order fails."""
    out: list[Finding] = []
    bad = re.compile(r"^\s*(?:from|import)\s+py_clob_client(?!\w*_v2)\b|MarketOrderArgsV1|OrderArgsV1|"
                     r"\bpy_clob_client\b(?!\w*_v2)")
    for p in files:
        rel = rel_of(p)
        if p.resolve() == SELF or not rel.endswith(".py"):
            continue
        for no, code, raw in code_lines(p.read_text(errors="replace")):
            if bad.search(code) and not allow(raw):
                out.append((rel, no, "clob-v2-only", raw.strip()[:100]))
    return out


# `statistics` joined for P05's z-score rule: the alternative is hand-rolled mean/pstdev inside the pure core,
# which is more code to audit for no gain, and the rule's actual subject is third-party dependencies.
# The point of this list is "no third-party imports in the core", so it has to be the *whole* stdlib surface a
# reviewer would expect to see. P07 added base64/struct/html/urllib (TOTP secrets, the code truncation, and the
# untrusted-metadata parser) after the rule told it to move working code to the services tree: a lint rule that
# pushes logic away from where it belongs is a rule that needs its allowlist finished.
# P14's webhook transport added `socket` and the lint caught it, which is the rule working: the guard that refuses
# a stored URL pointing at loopback is only a guard if the *hostname* is resolved before the send (the rebinding
# shape its tests cover), and the resolver is `socket.getaddrinfo`. Moving that call out of this module would move
# a security decision into whichever caller happened to remember it — the same argument P07 made for base64,
# struct and urllib, and the list is finished the same way: with the reason written next to the name.
CORE_STDLIB = {"polygm_core", "__future__", "dataclasses", "typing", "decimal", "math", "statistics", "time",
               "socket",
               "base64", "struct", "binascii", "secrets", "html", "urllib", "ipaddress", "codecs",
               "json",
               "hashlib", "hmac", "os", "sys", "re", "enum", "collections", "itertools", "functools",
               "argparse", "sqlite3", "pathlib", "random", "heapq", "bisect", "copy", "abc", "contextlib",
               "dataclasses", "datetime", "string", "unicodedata"}


def r_core_dep_free(files: list[Path]) -> list[Finding]:
    """The core is imported by five services and by the test suite; a third-party import there means the
    pure logic cannot be run, reviewed or fuzzed on its own."""
    out: list[Finding] = []
    bad = re.compile(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)")
    for p in files:
        rel = rel_of(p)
        if not rel.startswith("packages/") or not rel.endswith(".py"):
            continue
        for no, code, raw in code_lines(p.read_text(errors="replace")):
            m = bad.match(code)
            if not m or allow(raw):
                continue
            top = m.group(1).split(".")[0]
            if top not in CORE_STDLIB:
                out.append((rel, no, "core-dep-free", "imports %s" % m.group(1)))
    return out


def r_money_field_types(files: list[Path]) -> list[Finding]:
    """A request model with `price: float` is a silent precision loss before our code ever runs: pydantic
    accepts 0.1 and hands us 0.1000000000000000055511151231257827. Only model annotations are checked, not
    internal helpers - the mock's fill(price: float) mirrors the venue's own API, and pretending otherwise
    would be a lint lie."""
    out: list[Finding] = []
    bad = re.compile(r"^\s{2,}(price|size|price_micro|size_micro|notional\w*|amount\w*)\s*:\s*"
                     r"(float|number)\b")
    for p in files:
        rel = rel_of(p)
        if not rel.startswith("services/") or not rel.endswith(".py"):
            continue
        text = p.read_text(errors="replace")
        in_model = False
        for no, code, raw in code_lines(text):
            if re.match(r"^class\s+\w+\(.*(BaseModel|Schema).*\)", code):
                in_model = True
            elif code and not code.startswith((" ", "\t")):
                in_model = False
            if in_model and bad.match(code) and not allow(raw):
                out.append((rel, no, "money-as-string", raw.strip()[:100]))
    return out


def r_append_only(files: list[Path]) -> list[Finding]:
    """cash_ledger / flag_audit / kill_switch_state / audit_log have UPDATE+DELETE blocked by trigger AND by
    the missing GRANT. Application code must not try anyway: it would pass in dev (sqlite) and fail in
    production, or worse, pass because someone ran the migration as superuser."""
    out: list[Finding] = []
    bad = re.compile(r"\b(UPDATE|DELETE\s+FROM)\s+(cash_ledger|flag_audit|kill_switch_state|audit_log)\b",
                     re.I)
    lit = re.compile(r"[\"']\s*(?:UPDATE|DELETE\s+FROM)\s+(cash_ledger|flag_audit|kill_switch_state|audit_log)",
                     re.I)
    for p in files:
        rel = rel_of(p)
        if rel.startswith("db/") or not rel.endswith(".py"):
            continue
        if rel.startswith("tests/"):
            continue  # the tests exercise the trigger on purpose
        text = p.read_text(errors="replace")
        for no, ln in enumerate(text.splitlines(), 1):
            if (bad.search(ln) or lit.search(ln)) and not allow(ln):
                out.append((rel, no, "append-only", ln.strip()[:100]))
    return out


RULES: list[tuple[str, "object"]] = [
    ("money-no-float", r_float_in_money),
    ("money-col-integer", r_money_column_type),
    ("no-secrets", r_secrets),
    ("order-via-gate", r_gate_order),
    ("clob-v2-only", r_v1_client),
    ("core-dep-free", r_core_dep_free),
    ("money-as-string", r_money_field_types),
    ("append-only", r_append_only),
]


def collect() -> list[Path]:
    out: list[Path] = []
    for d in SCAN:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if any(part in SKIP_DIR for part in p.parts):
                continue
            if p.suffix in (".py", ".sql") and p.is_file():
                out.append(p)
    return out


# ---------------------------------------------------------------- canary
CANARY = {  # one planted violation per rule, in RULES order
    "float-in-money": ("packages/polygm_core/money/probe.py", "usd = raw * 1.0\n"),
    "money-col-integer": ("db/migrations/9999_probe.sql", "  amount_micro NUMERIC(20,6) NOT NULL\n"),
    # Deliberately not token-shaped. The rule has two branches - a secret NAME holding a literal, and a
    # credential SHAPE anywhere - and the canary only needs to be found, so it trips the name branch with an
    # ordinary string. A real-looking `sk_live_…` here got the branch rejected by GitHub push protection, which
    # is the correct outcome for a repo that also holds migrations and docs: a planted fake and a real leak are
    # indistinguishable to a scanner, and to the person who has to rotate something at 3am.
    "no-secrets": ("services/api/probe.py", 'API_KEY = "a literal assigned to a secret-named field"\n'),
    "order-via-gate": ("services/api/probe.py", "    return client.post_order(signed)\n"),
    "clob-v2-only": ("services/api/probe.py", "from py_clob_client.client import ClobClient\n"),
    "core-dep-free": ("packages/polygm_core/probe.py", "import requests\n"),
    "money-as-string": ("services/api/probe.py", "class OrderIn(BaseModel):\n    price: float = 0.5\n"),
    "append-only": ("services/api/probe.py", 'cur.execute("DELETE FROM cash_ledger")\n'),
}


def canary() -> list[tuple[str, bool]]:
    """Plant one violation per rule in a scratch tree and confirm THAT rule sees it.

    ROOT is retargeted so the rules' relative-path logic runs unchanged: the canary exercises the real code
    path, not a copy of it. A canary that re-implements the matcher is how a dead rule keeps passing.
    """
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="lint-canary-"))
    saved_root = globals()["ROOT"]
    globals()["ROOT"] = tmp
    results: list[tuple[str, bool]] = []
    try:
        for (name, fn), (rel, body) in zip(RULES, CANARY.values()):
            f = tmp / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
            hits = [h for h in fn([f]) if h[2] == name]
            results.append((name, bool(hits)))
    finally:
        globals()["ROOT"] = saved_root
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canary", action="store_true", help="verify every rule can fire on a planted violation")
    a = ap.parse_args()

    if a.canary:
        res = canary()
        fired = sum(1 for _, ok_ in res if ok_)
        for name, ok_ in res:
            print("  %-22s %s" % (name, "fires" if ok_ else "NEVER FIRES - decoration, not a rule"))
        print("lint canary: %d/%d rules fire" % (fired, len(res)))
        return 1 if fired != len(res) else 0

    files = collect()
    counts: dict[str, int] = {}
    all_findings: list[Finding] = []
    for name, fn in RULES:
        hits = fn(files)
        counts[name] = len(hits)
        all_findings += hits
    print("lint-rules: %d files scanned, %d findings" % (len(files), len(all_findings)))
    for rule in sorted(counts):
        print("  %-20s %s" % (rule, ("%d finding(s)" % counts[rule]) if counts[rule] else "clean"))
    by: dict[str, list[Finding]] = {}
    for f in all_findings:
        by.setdefault(f[2], []).append(f)
    for rule in sorted(by):
        for rel, no, _, text in by[rule][:15]:
            print("  FAIL %s:%d [%s] %s" % (rel, no, rule, text))
        if len(by[rule]) > 15:
            print("  ... %d more %s" % (len(by[rule]) - 15, rule))
    return 1 if all_findings else 0


if __name__ == "__main__":
    sys.exit(main())
