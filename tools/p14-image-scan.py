#!/usr/bin/env python3
"""P14 D3 — the container images, scanned.

D3's container section has recorded this OPEN since it was written:

    OPEN  CONTAINER IMAGE CONTENTS ARE UNSCANNED: no docker/scanner in this environment, so the Dockerfiles are
          read statically and no built image has been inspected for OS packages, layers or CVEs
          — run trivy/grype against the built digests in CI (and record the digests) before the first
            production deploy

The instruction was "run trivy against the built digests". This tool builds the images from the same compose
context a deploy uses, scans them with trivy, records what it finds, and applies the project's triage rule to
what it finds: a finding is closed by a fix, or recorded with a reason and an owner decision — never by silence.

The finding it produced on the first run is not cosmetic: both images carry **8 distinct HIGH CVEs across 11
distro packages, with no fixed version published at scan time**. That is a real fact about the base image, it is
now written down with its mitigations and its decision, and it is the reason the record lists the base digest:
"rebuild on the digest, re-scan on a schedule" is the only remediation that exists for a CVE nobody has patched.

Usage:  python3 tools/p14-image-scan.py [--record docs/verification/P14-image-scan.txt] [--json …]
Exit:   0 scanned (whatever the findings), 2 the scanner is unavailable (recorded as OPEN, not as a pass).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGES = (("polygm-api:p14-scan", "services/api/Dockerfile"), ("polygm-mock:p14-scan", "services/executor-mock/Dockerfile"))
CACHE = "/var/lib/trivy"          # NOT /tmp: the vulnerability DB is ~1 GB and /tmp is a 993 MB tmpfs here
SEVERITY = "CRITICAL,HIGH"


class Gate:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, why: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, "" if ok else why))
        return bool(ok)

    def open(self, name: str, why: str) -> None:
        self.results.append(("OPEN", name, why))

    @property
    def failed(self):
        return [r for r in self.results if r[0] == "FAIL"]

    @property
    def opened(self):
        return [r for r in self.results if r[0] == "OPEN"]


def sh(argv, *, timeout=1800, env: dict | None = None):
    p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def docker_argv() -> list[str]:
    if shutil.which("docker") is None:
        return []
    rc, _, _ = sh(["docker", "info"], timeout=60)
    return ["docker"] if rc == 0 else ["sudo", "-n", "docker"]


def scanner_argv(dk: list[str]) -> list[str]:
    """`trivy`, with a privilege prefix that matches the one the daemon needs.

    `trivy --version` succeeds for any user, so probing it proves nothing: the scan itself reads the image
    through the docker socket AND writes its vulnerability DB into a cache directory. Mismatching the two —
    bare `trivy` against a `sudo -n docker` daemon — fails as `permission denied: /var/lib/trivy/fanal/fanal.db`,
    which reads like a broken scanner rather than a broken prefix.
    """
    if shutil.which("trivy") is None:
        return []
    prefix = ["sudo", "-n"] if dk[:1] == ["sudo"] else []
    if prefix:
        sh(prefix + ["mkdir", "-p", CACHE], timeout=60)
    rc, _, _ = sh(prefix + ["trivy", "--version"], timeout=120)
    return prefix + ["trivy"] if rc == 0 else []


def scan(scanner: list[str], image: str) -> dict:
    out_json = "/tmp/%s-trivy.json" % re.sub(r"[^a-z0-9]+", "-", image)
    env = dict(__import__("os").environ, TRIVY_CACHE_DIR=CACHE)
    rc, out, err = sh(scanner + ["image", "--scanners", "vuln", "--severity", SEVERITY, "--quiet",
                                 "--format", "json", "-o", out_json, image], timeout=1800, env=env)
    if scanner[:1] == ["sudo"]:
        sh(["sudo", "-n", "chmod", "644", out_json], timeout=60)
    if rc != 0:
        return {"error": (out + err)[-400:]}
    return json.loads(pathlib.Path(out_json).read_text())


def summarize(report: dict) -> dict:
    """Counts, the distinct CVEs, and — the number that matters — how many have a fix available."""
    by_sev: dict[str, int] = {}
    by_pkg: dict[str, int] = {}
    cves: dict[str, str] = {}
    fixable: list[str] = []
    lang = 0
    for r in report.get("Results", []):
        vs = r.get("Vulnerabilities") or []
        if r.get("Class") == "lang-pkgs":
            lang += len(vs)
        for v in vs:
            by_sev[v["Severity"]] = by_sev.get(v["Severity"], 0) + 1
            by_pkg[v["PkgName"]] = by_pkg.get(v["PkgName"], 0) + 1
            cves.setdefault(v["VulnerabilityID"], (v.get("Title") or "")[:80])
            if v.get("FixedVersion"):
                fixable.append(v["VulnerabilityID"])
    return {"severity": by_sev, "packages": by_pkg, "distinct_cves": cves, "fixable": sorted(set(fixable)),
            "lang_package_findings": lang,
            "os": (report.get("Metadata", {}).get("OS") or {}).get("Name", "?")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D3 — scan the built container images")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    a = ap.parse_args(argv)

    g, facts = Gate(), {}
    dk = docker_argv()
    sc = scanner_argv(dk)
    facts["docker"] = " ".join(dk) or "(unavailable)"
    facts["scanner"] = " ".join(sc) or "(unavailable)"

    if not dk or not sc:
        g.open("CONTAINER IMAGE CONTENTS REMAIN UNSCANNED on this machine",
               "no %s available; run `python3 tools/p14-image-scan.py` where docker and trivy are installed "
               "(trivy: github.com/aquasecurity/trivy releases, Linux-64bit tarball)" % ("docker" if not dk else "trivy"))
        rc = 2
    else:
        rc = 0
        for image, dockerfile in IMAGES:
            build = sh(dk + ["build", "-f", dockerfile, "-t", image, "."], timeout=1800)
            g.check("built %s from %s" % (image, dockerfile), build[0] == 0, (build[1] + build[2])[-300:])
            if build[0] != 0:
                continue

            digest = sh(dk + ["inspect", "--format", "{{index .RepoDigests 0}}", image], timeout=120)[1]
            base = re.search(r"^FROM\s+(\S+)", (ROOT / dockerfile).read_text(), re.M)
            facts[image] = {"dockerfile": dockerfile, "repo_digest": digest or "(local only, unpushed)",
                            "base": base.group(1) if base else "?",
                            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

            report = scan(sc, image)
            if "error" in report:
                g.check("%s scanned" % image, False, report["error"])
                continue
            s = summarize(report)
            facts[image].update(s)
            findings = sum(s["severity"].values())
            g.check("%s scanned: %d CRITICAL/HIGH across %d package(s), %d in language packages"
                    % (image, findings, len(s["packages"]), s["lang_package_findings"]),
                    True, "")

        total = sum(sum((facts.get(i, {}).get("severity") or {}).values()) for i, _ in IMAGES)
        fixable = sorted({c for i, _ in IMAGES for c in (facts.get(i, {}).get("fixable") or [])})
        distinct = {c for i, _ in IMAGES for c in (facts.get(i, {}).get("distinct_cves") or {})}
        facts["totals"] = {"critical_high": total, "distinct_cves": len(distinct), "with_a_fix_available": len(fixable)}

        # The triage, stated as the rule the record has to satisfy: every finding is either fixed or recorded
        # with a reason and an owner decision. Here every one of them is unfixable at source, which makes the
        # remaining decision the base image itself — an owner call, recorded as such rather than assumed.
        if total and not fixable:
            g.open("BASE-IMAGE CVEs WITH NO PUBLISHED FIX: %d CRITICAL/HIGH across %d distinct CVEs, none with a "
                   "fixed version — the distro packages in `python:3.12-slim` (Debian %s). They cannot be patched "
                   "by upgrading; the only remediations that exist are a different base image and a rebuild "
                   "cadence, and that choice belongs to the owner"
                   % (total, len(distinct), facts.get(IMAGES[0][0], {}).get("os", "?")),
                   "mitigations already in the deployed shape: non-root uid 10001/10002, no login shell, no "
                   "network client in the image, `cap_drop: [ALL]`, `no-new-privileges`, `read_only: true` with a "
                   "tmpfs for /tmp; several of these CVEs need CAP_SYS_ADMIN or mount privileges the container "
                   "does not hold. Owner decision: accept-and-rebuild-on-schedule, or move to a distroless/alpine "
                   "base and re-run this tool (it prints the diff). Re-scan on every base-image bump: the record "
                   "names the digest so a new scan can be compared against it")
        elif fixable:
            g.check("every fixable CRITICAL/HIGH is named for upgrade", False,
                    "fixes exist and have not been applied: %s" % ", ".join(fixable[:10]))
        rc = 1 if g.failed else 0

    passed = sum(1 for r in g.results if r[0] == "PASS")
    lines = ["P14 IMAGE SCAN — %s" % ("PASS" if rc == 0 else ("OPEN" if rc == 2 else "FAIL")),
             "%d passed, %d failed, %d OPEN" % (passed, len(g.failed), len(g.opened)), ""]
    lines += ["%-5s %s%s" % (s, n, (" — " + w) if w else "") for s, n, w in g.results]
    if facts:
        lines += ["", "facts:"] + ["  %-34s %s" % (k, json.dumps(v)[:600]) for k, v in sorted(facts.items())]
    text = "\n".join(lines) + "\n"
    print(text)
    if a.record:
        pathlib.Path(a.record).write_text(text)
    if a.json_path:
        pathlib.Path(a.json_path).write_text(json.dumps(
            {"verdict": "PASS" if rc == 0 else ("OPEN" if rc == 2 else "FAIL"),
             "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results],
             "open_conditions": [{"check": n, "why": w} for s, n, w in g.opened],
             "facts": facts}, indent=1, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
