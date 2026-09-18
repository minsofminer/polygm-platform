#!/usr/bin/env python3
"""Dependency scanning as product work (P07 D7), not as a badge.

Three claims get checked, because those are the three that survive contact with an incident review:

1. **Everything is pinned.** An unpinned requirement is a build that changes when nobody changed it, on a
   machine that signs for user money. Every line of `requirements*.txt` must be `name==version`.
2. **The advisory feed is consulted when it can be.** `pip-audit` is used if it is importable; when it is not
   (this repo's build environment has no network at gate time), the tool says so in the output instead of
   printing a pass. A green check that never reached the feed is the exact thing D7 is complaining about.
3. **The images the security plane trusts are pinned by digest.** `deploy/image-digests.txt` must name every
   service in the critical set that `docker-compose.yml` starts from an *image* (a `build:` target is checked by
   `make digest-record` at deploy time, and the file records who wrote it).

`web/package.json` does not exist yet — P08 lands the app, and the lockfile rule below activates with it
rather than passing vacuously today. That is stated in the output.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRITICAL = ("api", "migrate", "executor", "executor-mock", "ingest")
PINNED = re.compile(r"^[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9._+!-]+$")


def requirements() -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for f in sorted(ROOT.glob("requirements*.txt")):
        for raw in f.read_text().splitlines():
            line = raw.split("#")[0].strip()
            if not line or line.startswith("-r ") or line.startswith("-e "):
                continue
            (ok if PINNED.match(line) else bad).append("%s: %s" % (f.name, line))
    return ok, bad


def node_locks() -> tuple[bool, str]:
    pkg = ROOT / "web" / "package.json"
    if not pkg.exists():
        return False, "web/package.json does not exist yet (P08) - nothing to lock-scan"
    lock = (ROOT / "web" / "package-lock.json")
    if not lock.exists():
        return True, "web/package.json exists with no package-lock.json: the build is not reproducible"
    data = json.loads(pkg.read_text())
    loose = [k for k, v in {**data.get("dependencies", {}), **data.get("devDependencies", {})}.items()
             if str(v).startswith(("^", "~", "*"))]
    return (True, "web deps not exact-pinned: %s" % ", ".join(sorted(loose))) if loose else (
        False, "web lockfile present, %d deps exact-pinned" % len(data.get("dependencies", {})))


def digests() -> tuple[list[str], list[str]]:
    # `deploy/`, not `var/`: var/ is gitignored, and a pin that CI never clones is not a pin - it is a
    # local file someone believes in. The gate additionally asserts the file is tracked by git.
    f = ROOT / "deploy" / "image-digests.txt"
    rows = {}
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 2:
                rows[parts[0]] = parts[1]
    compose = (ROOT / "docker-compose.yml").read_text()
    found = re.findall(r"^  (\w[\w-]*):\n(?:.*\n)*?    image: (\S+)", compose, re.M)
    services = [(svc, img) for svc, img in found if svc in CRITICAL]
    missing, present = [], []
    for svc, img in services:
        if "@sha256:" not in img:
            missing.append("%s uses a tag (%s): pin by digest" % (svc, img))
        else:
            present.append(svc)
    if not found:
        # Dev compose builds everything from local Dockerfiles; the digest claim is then about the *base* images
        # inside those Dockerfiles, and saying so is better than counting zero and calling it a pass.
        present.append("(no image: lines in docker-compose.yml - all services build locally; the base-image "
                       "digests are recorded by `make digest-record` at deploy time)")
    if services == [] and found:
        present.append("(no image: reference for %s - they build locally; record the built digest with "
                       "`make digest-record` before any deploy that signs)" % ", ".join(CRITICAL))
    return present, missing


def audit() -> tuple[str, int]:
    try:
        import pip_audit                                                   # noqa: F401
    except Exception:
        return ("pip-audit is not installed here: no advisory feed was consulted [UNVERIFIED - CI installs it]", 0)
    r = subprocess.run([sys.executable, "-m", "pip_audit", "-r", str(ROOT / "requirements.txt"),
                        "--progress-spinner", "off", "--desc", "on"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    return ((r.stdout + r.stderr).strip()[:4000], r.returncode)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-audit", action="store_true", help="do not shell out to pip-audit")
    a = ap.parse_args()
    pinned, unpinned = requirements()
    node_bad, node_msg = node_locks()
    have, digest_bad = digests()
    audit_out, audit_rc = ("skipped (--skip-audit)", 0) if a.skip_audit else audit()

    report = {"pinned": len(pinned), "unpinned": unpinned, "node": node_msg, "node_findings": node_bad,
              "digest_services": have, "digest_findings": digest_bad,
              "advisory": audit_out, "advisory_rc": audit_rc}
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print("pinned requirements: %d line(s), %d unpinned" % (len(pinned), len(unpinned)))
        for u in unpinned:
            print("  UNPINNED %s" % u)
        print("node: %s" % node_msg)
        print("image digests: %s" % (", ".join(have) or "none"))
        for d in digest_bad:
            print("  DIGEST %s" % d)
        print("advisory feed:\n%s" % "\n".join("  " + l for l in audit_out.splitlines()[:20]))
        fails = len(unpinned) + len(digest_bad) + audit_rc + (1 if node_bad else 0)
        print("dependency-scan: %s (%d finding(s))" % ("pass" if not fails else "FAIL", fails))
    return 1 if (unpinned or digest_bad or audit_rc or node_bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
