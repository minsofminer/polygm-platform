#!/usr/bin/env python3
"""P02 D1 name-collision probe.

P02 asks for a "domain availability assumption" per candidate. An assumption is cheap to make and
expensive to rely on, so this measures three free, public signals instead:

  1. DNS   — does <name>.com / <name>.app / <name>.io resolve at all (a negative result here is
             necessary, never sufficient: many registered domains have no A record)
  2. crt.sh — certificate transparency: an issued cert proves someone owns and uses the zone
  3. GitHub — repo-search hits for the bare word, a proxy for product collisions in our category

It does NOT check any trademark register. USPTO/EUIPO were not queryable from this sandbox
without auth, so trademark collision stays [UNVERIFIED] in the spec and is flagged per candidate.

    python3 tools/name-collision-probe.py [name ...]     # default: the 12 P02 candidates
"""
from __future__ import annotations

import json
import re
import socket
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "polygm-p02-name-probe/1.0"}
DEFAULT = ["tally", "vane", "oddspace", "aperture", "fathom", "resolve", "ledger",
           "signal", "calibrate", "parlay", "spread", "binary"]
TLD = ("com", "app", "io")


def http(url: str, timeout: int = 20):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return "ERR", repr(e)[:200]


def dns(host: str):
    try:
        return socket.gethostbyname(host)
    except Exception:  # noqa: BLE001
        return None


def crtsh_domains(name: str) -> list[str]:
    """Distinct registered domains in certs that contain the exact name as a label."""
    st, body = http("https://crt.sh/?q=" + urllib.parse.quote(f"%.{name}.%") + "&output=json")
    if st != 200 or not body.strip().startswith("["):
        return []
    try:
        rows = json.loads(body)
    except Exception:  # noqa: BLE001
        return []
    found: set[str] = set()
    pat = re.compile(r"(?:^|\.)(" + re.escape(name) + r"\.[a-z]{2,})$", re.I)
    for row in rows[:400]:
        for line in (row.get("name_value") or "").split("\n"):
            m = pat.search(line.strip().lower().lstrip("."))
            if m:
                found.add(m.group(1))
    return sorted(found)[:12]


def github_repos(word: str) -> dict:
    st, body = http("https://api.github.com/search/repositories?q=" +
                    urllib.parse.quote(word) + "&per_page=5")
    if st != 200:
        return {"error": st, "total": None}
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return {"error": "parse", "total": None}
    return {"total": d.get("total_count"),
            "top": [f"{i['full_name']}★{i['stargazers_count']}" for i in d.get("items", [])[:3]]}


def main() -> int:
    names = sys.argv[1:] or DEFAULT
    print(f"P02 name-collision probe — {len(names)} candidates\n")
    print(f"  {'name':11s} {' .com':>15s} {' .app':>15s} {' .io':>15s}  {'crt.sh zones':28s}  github")
    results = {}
    for n in names:
        ips = {t: dns(f"{n}.{t}") for t in TLD}
        crt = crtsh_domains(n)
        gh = github_repos(n)
        results[n] = {"dns": ips, "crtsh": crt, "github": gh}
        cells = " ".join(f"{(ips[t] or '-'):>15s}" for t in TLD)
        crt_s = (", ".join(crt[:3]))[:28] if crt else "-"
        print(f"  {n:11s} {cells}  {crt_s:28s}  {gh.get('total')} repos")
    print("\n  Reading guide:")
    print("   • no A record on all three TLDs + no crt.sh zone  => likely unclaimed (still not a legal clearance)")
    print("   • any A record or cert                            => the domain is in use")
    print("   • GitHub total is collision noise, not a trademark search — it is a *smell test*")
    print("\n  [UNVERIFIED for every candidate: USPTO/EUIPO registration, and any live product's")
    print("   actual trademark class. A real clearance search is a paid step before launch.]")
    out = "/tmp/name-probe.json"
    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"\n  json: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
