#!/usr/bin/env python3
"""Scan log output for secrets before it lands anywhere durable (P07 D6: "grep every log line").

The rule this exists to enforce is the one that is cheapest to skip: a secret in a *log* is a secret that has
already left the process, and it ends up in whatever retention the log platform has, which is longer than the
credential's lifetime and readable by more people than the database. So CI reads the logs the same way an
attacker would.

Three modes:
  stdin / --file            the log lines themselves (what a `make dev` run or a CI job produced)
  --sources                 the *rules* over tracked source, so a hard-coded secret cannot hide in a helper
  --built PATH              generated build output (a file or a tree), e.g. `web/.next`. A value the product
                            never intended to ship reaches a browser through a bundler, not through a log: the
                            P08 design's whole env discipline is about what survives `next build`. Minified
                            vendor code trips every heuristic that is merely *log-shaped* (that is why the
                            first version of this mode used LOG_RULES and reported 13 findings, all in
                            core-js), so a tree gets SOURCE_RULES: the shapes that are a secret anywhere.

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
ALLOW: list[re.Pattern[str]] = [
    re.compile(r"^\s*#"),                                   # prose in a source file
    re.compile(r"redact|REDACTED|\*\*\*|\[pem-key\]|\[jwt\]|\[bot-token\]"),
    re.compile(r"PGM_[A-Z_]+\s*=\s*$"),                      # an empty .env.example key
    re.compile(r"tools/ci-log-scan\.py"),                    # this file quotes the patterns it hunts
    re.compile(r"\b0x0{20,}\b|\bEXAMPLE\b|\byour-[-\w]+\b"),  # obvious placeholders
    re.compile(r"test_security|tests/"),                     # fixtures: the lint allowlist governs those
    re.compile(r"polygm:polygm@"),                           # the documented dev pair in docker-compose.yml
    re.compile(r'"X-Admin-Token": "[^"]*"'),                 # a header *name* with a dev value: lint owns this
    re.compile(r"tools/lint-rules.py|tools/p0\d-(gate|mutation|drill|chaos)"),
]

#: P14 D3: the *shared* allowlist (`tools/secret-scan-allowlist.json`) is loaded here as well, so an exemption
#: written once is in force for both the CI scanner and `tools/p14-appsec-scan.py`. Two lists of exemptions is how
#: a rule ends up enforcing something different from what the file says it enforces — the exact failure this
#: module's own docstring complains about.
_SHARED: list[tuple[str, str]] = []


def _load_shared() -> None:
    import json as _json
    path = Path(__file__).with_name("secret-scan-allowlist.json")
    if not path.exists():
        return
    data = _json.loads(path.read_text())
    for entry in list(data.get("strings", [])) + list(data.get("allow", [])):
        text = entry["string"] if isinstance(entry, dict) else str(entry)
        why = entry.get("why", "") if isinstance(entry, dict) else ""
        if text:
            ALLOW.append(re.compile(re.escape(text)))
            _SHARED.append((text[:60], why[:80]))
    for entry in data.get("paths", []):
        pat = str(entry["path"]).replace("**", "").rstrip("/")
        ALLOW.append(re.compile(re.escape(pat)))


_load_shared()


#: A value carrying a shell/python interpolation is not a literal, so it is not a secret in the file. This is the
#: rule that clears `https://x-access-token:$TOKEN@github.com/...`: the interpolation IS the safe way to write
#: that line, and flagging it would teach the wrong lesson at the worst possible moment (during an incident, when
#: somebody is rebuilding the remote). Log lines never contain `${VAR}`, which is why this guard is safe here.
INTERPOLATED = re.compile(r"\$\{?[A-Za-z_]|%s\b|\{[a-z_]+\}")


def findings(text: str, rules=None, where: str = "") -> list[tuple[int, str, str]]:
    r"""(line number, rule, redacted excerpt) for every line that looks like a secret.

    `where` is the file path, and it is part of the string the allowlist is matched against — that is the whole
    point of entries like `tools/ci-log-scan\.py` (this file quotes the shapes it hunts) and `tests/` (fixtures
    are planted on purpose). Matching the line alone made those entries decorative: the path never appeared in
    the text being tested, so the exemptions people *read* in the list were not the exemptions in force, and the
    first honest run of `--sources` lit up on our own self-test table.
    """
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        if any(a.search(where + ":" + line) for a in ALLOW):
            continue
        for name, rx in (rules or RULES):
            m = rx.search(line)
            if not m:
                continue
            if INTERPOLATED.search(m.group(0)):
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
    ap.add_argument("--built", action="append", default=[], type=Path,
                    help="a generated build-output file or tree to scan (e.g. web/.next)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        samples = {
            "private key in a log": "signer loaded key 0x" + "ab" * 32 + " for u_1",
            "postgres uri": 'connect failed for postgres://polygm:hunter2@db:5432/polygm after 3000ms',
            "jwt": "auth rejected eyJhbGciOi.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQ",
            "pem block": "-----BEGIN PRIVATE KEY-----\\nMIIEvQ\\n-----END PRIVATE KEY-----", # lint-allow: the redactor's own PEM test vector
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
        # And the two ways an allowlist is wrong, checked rather than asserted: an exemption that does not
        # match anything is a hiding place, so the path-scoped entries must actually silence the line they name.
        if findings("passphrase=hunter2xx", where="tests/test_x.py"):
            print("  ALLOWLIST does not honour a path-scoped exemption"); bad += 1
        if not findings("passphrase=hunter2xx", where="src/app.py"):
            print("  ALLOWLIST silences a real secret outside those paths"); bad += 1
        # The modes are wiring, and wiring breaks silently: `--built` once failed as `unrecognized arguments`
        # and the caller read that as "clean". So each mode is run for real over a temporary tree here, and a
        # mode that cannot run is a failure of the scanner, not of the code under it.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "chunk.js").write_text("var u={k:\"0x" + "ab" * 32 + "\"};\n")
            (d / "vendor.js").write_text('var a={token:"abc"};if(trimStart in String.prototype){}\n')
            for mode, expect in ((["--built", str(d)], 1), (["--file", str(d / "chunk.js")], 1),
                                 (["--file", str(d / "vendor.js")], None)):
                r = subprocess.run([sys.executable, str(Path(__file__).resolve())] + mode,
                                   capture_output=True, text=True)
                hit = r.returncode == 1
                if expect == 1 and not hit:
                    print("  MODE %s did not run or did not find the planted key" % mode[0]); bad += 1
                if expect is None and hit:
                    print("  MODE %s is noisy on ordinary code" % mode[0]); bad += 1
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
    elif a.built:
        # 25 MB is well above the largest chunk a Next build writes; past that a file is an image or an
        # artefact of another tool, and reading it to grep it is just slow.
        for root in a.built:
            for f in ([root] if root.is_file() else sorted(x for x in root.rglob("*") if x.is_file())):
                if f.suffix in (".js", ".mjs", ".cjs", ".css", ".html", ".json", ".txt", ".map") and \
                        f.stat().st_size <= 25_000_000:
                    texts.append((str(f), f.read_text(errors="replace")))
    elif a.file:
        texts = [(str(p), p.read_text(errors="replace")) for p in a.file]
    else:
        texts = [("<stdin>", sys.stdin.read())]

    total = []
    for name, text in texts:
        for no, rule, shown in findings(text, SOURCE_RULES if (a.sources or a.built) else None, where=name):
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
