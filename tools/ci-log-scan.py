#!/usr/bin/env python3
"""Scan log output for secrets before it lands anywhere durable (P07 D6: "grep every log line").

The rule this exists to enforce is the one that is cheapest to skip: a secret in a *log* is a secret that has
already left the process, and it ends up in whatever retention the log platform has, which is longer than the
credential's lifetime and readable by more people than the database. So CI reads the logs the same way an
attacker would.

Two modes:
  stdin / --file            the log lines themselves (what a `make dev` run or a CI job produced)
  --sources                 the *rules* over tracked source, so a hard-coded secret cannot hide in a helper

Exit 1 with the file:line and the rule name. The line text is re-printed through `redact.redact_text`, never
raw: a tool that prints the secret it found is a tool that puts the secret in a new place.

`--self-test` proves the scanner can fail. A check that cannot fail is worse than no check, because it buys the
false confidence that the check was the control.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))
from polygm_core.security import redact                                        # noqa: E402

# Things that are *shape* secrets even when they are fake in a test file. The `no-secrets` lint rule in
# tools/lint-rules.py owns the allowlist for dev fixtures; this list owns the "it looked like a key in a log".
EXTRA = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key_pem", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("keystone_style_key", re.compile(r"\bsk-(live|test|ant)[A-Za-z0-9_-]{12,}\b")),
    ("postgres_uri_with_password", re.compile(r"://[^\s/]+:[^\s@]{3,}@")),
    ("jwt_like", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("telegram_initdata_hash", re.compile(r"initData[^&\n]{0,20}[?&]hash=[0-9a-f]{32,}", re.I)),
)
LOG_RULES = tuple((name, pattern) for (name, pattern, _repl) in redact.PATTERNS) + EXTRA
# Source scanning is a different question than log scanning. The log rules include heuristics that are correct on
# a line of runtime text (`key: <value>`, an email in a support note, an address in a refusal) and wrong on a
# source file, where `SEED = {...}` and `password="demo"` are the code *about* those things — the first repo-wide
# run of this tool found 4,326 of them, which is how a scanner gets muted. So `--sources` gets only the shapes
# that are a secret wherever they appear: a PEM block, a provider token, a 32-byte hex key, a URI with a
# password in it. Everything else about source secrets is `tools/lint-rules.py`'s `no-secrets` rule, which owns
# the allowlist for dev fixtures.
_HIGH_CONFIDENCE = ("pem_key", "private_key_hex", "aws_access_key", "github_token", "slack_token",
                    "keystone_style_key", "postgres_uri_with_password", "telegram_initdata_hash")
SOURCE_RULES = tuple(r for r in EXTRA if r[0] in _HIGH_CONFIDENCE) + tuple(
    (name, pattern) for (name, pattern, _repl) in redact.PATTERNS if name in _HIGH_CONFIDENCE)
RULES = LOG_RULES

# Lines that are *about* a secret without being one. Kept short and named, because an allowlist nobody can read
# is how a scanner gets ignored.
ALLOW = (
    re.compile(r"^\s*#"),                                   # prose in a source file
    re.compile(r"redact|REDACTED|\*\*\*|\[pem-key\]|\[jwt\]|\[bot-token\]"),
    re.compile(r"PGM_[A-Z_]+\s*=\s*$"),                      # an empty .env.example key
    re.compile(r"tools/ci-log-scan\.py"),                    # this file quotes the patterns it hunts
    re.compile(r"\b0x0{20,}\b|\bEXAMPLE\b|\byour-[-\w]+\b"),  # obvious placeholders
    re.compile(r"test_security|tests/"),                     # fixtures: the lint allowlist governs those
    re.compile(r"polygm:polygm@"),                           # the documented dev pair in docker-compose.yml
    re.compile(r'"X-Admin-Token": "[^"]*"'),                 # a header *name* with a dev value: lint owns this
    re.compile(r"tools/lint-rules.py|tools/p0\d-(gate|mutation|drill|chaos)"),
)


def findings(text: str, rules=None) -> list[tuple[int, str, str]]:
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        if any(a.search(line) for a in ALLOW):
            continue
        for name, rx in (rules or RULES):
            m = rx.search(line)
            if not m:
                continue
            # The excerpt is a *fingerprint*, not the line. A scanner that prints the secret it found has put
            # the secret somewhere new - usually the CI log, which is public on a public repo and retained for
            # 90 days on a private one. `redact_text` handles the shapes it knows; for the ones it does not
            # (an AWS key is not something this product logs), show only enough to find the line.
            shown = redact.redact_text(line.strip())[:200]
            if m.group(0) in shown:
                shown = "%s...<%d chars, matched by %s>" % (m.group(0)[:4], len(m.group(0)), name)
            out.append((no, name, shown))
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", default=[], type=Path)
    ap.add_argument("--sources", action="store_true", help="scan tracked source files instead of stdin")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        samples = {
            "private key in a log": "signer loaded key 0x" + "ab" * 32 + " for u_1",
            "postgres uri": 'connect failed for postgres://polygm:hunter2@db:5432/polygm after 3000ms',
            "jwt": "auth rejected eyJhbGciOi.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQ",
            "pem block": "-----BEGIN PRIVATE KEY-----\\nMIIEvQ\\n-----END PRIVATE KEY-----",
            "github token": "pushed with ghp_" + "T" * 36,
        }
        bad = 0
        for label, s in samples.items():
            f = findings(s)
            print("  %-22s -> %s" % (label, ("%s (line %d)" % (f[0][1], f[0][0])) if f else "MISSED"))
            bad += 0 if f else 1
        clean = [
            "user u_123 signed in from ip_hash a1b2c3d4e5f6 status 200",
            "the destination 0x1234abcd\u2026cdef is in its cooldown",
            'POST /v1/orders 202 dur_ms=41 rid=deadbeef',
        ]
        for s in clean:
            if findings(s):
                print("  FALSE POSITIVE on: %s -> %s" % (s[:50], findings(s)[0][1]))
                bad += 1
        print("log-scan self-test: %d pattern(s) failed" % bad)
        return 1 if bad else 0

    texts: list[tuple[str, str]] = []
    if a.sources:
        r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
        for rel in r.stdout.splitlines():
            p = ROOT / rel
            # `.json` and `.md` are out for source mode: the P05 fixtures are *captured upstream payloads*, where
            # a 64-hex `conditionId` is a market identifier and indistinguishable from a private key by shape.
            # A log line with a 64-hex value is still redacted (that part is not negotiable); a fixture full of
            # them is data, and a scanner that cries over 2,694 market ids gets muted by the third CI run.
            if not p.is_file() or p.suffix not in (".py", ".yml", ".yaml", ".sh", ".toml", ".ts", ".tsx",
                                                   ".js", ".env"):
                continue
            if "/fixtures/" in "/" + rel or rel.endswith((".min.js", ".min.css")):
                continue
            texts.append((rel, p.read_text(errors="replace")))
    elif a.file:
        texts = [(str(p), p.read_text(errors="replace")) for p in a.file]
    else:
        texts = [("<stdin>", sys.stdin.read())]

    total = []
    for name, text in texts:
        for no, rule, shown in findings(text, SOURCE_RULES if a.sources else None):
            total.append({"file": name, "line": no, "rule": rule, "excerpt": shown})
    if a.json:
        import json
        print(json.dumps({"scanned": len(texts), "findings": total}, indent=2))
    else:
        for f in total:
            print("%s:%d [%s] %s" % (f["file"], f["line"], f["rule"], f["excerpt"]))
        print("ci-log-scan: %d file(s) scanned, %d finding(s)" % (len(texts), len(total)))
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
