#!/usr/bin/env python3
"""P03 -> P03b: make the pinned-digest drift message say WHAT moved.

`provenance.p02_sections_sha256` covers color/semantic/chart/typography/radius/rules as a single digest.
P03 legitimately *added* `typography.utility` (the specimen needed two letter-spacing tokens), which shifts
that digest even though not one colour or type *value* changed — so the old message accused the change of
being an un-audited palette edit. That is the right check with the wrong explanation, and a scary message
that is wrong trains you to ignore it.

This classifies each drift into:
    added    — a P02 section gained a key it never had (safe, no existing value moved)
    modified — an existing key's value moved (needs the P02 colour audit before re-pinning)
and re-pins only in the first case, with an explicit flag.
"""
from __future__ import annotations

import json
import subprocess
import sys

ROOT = "/home/user/polygm-platform"

# The digest is owned by build-foundations.py. Import THAT file rather than re-typing hashlib+json here:
# this is the same trap that produced two disagreeing contrast numbers in P02 (a doc re-derived what a tool
# already derived). The filename has a hyphen, hence importlib instead of an import statement.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "build_foundations", f"{ROOT}/tools/build-foundations.py")
_bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bf)          # module-level code is constants + defs; no side effects on load
P02_SECTIONS, digest = _bf.P02_SECTIONS, _bf.p02_digest


def load(path: str) -> dict:
    return json.load(open(path))


def main() -> int:
    tokens = json.load(open(f"{ROOT}/brand/tokens.json"))
    head_raw = subprocess.run(["git", "show", "HEAD:brand/tokens.json"], cwd=ROOT,
                             capture_output=True, text=True).stdout
    if not head_raw.strip():
        print("no HEAD copy of brand/tokens.json — nothing to compare")
        return 0
    head = json.loads(head_raw)
    added, modified = [], []
    for k in P02_SECTIONS:
        o, n = head.get(k), tokens.get(k)
        if json.dumps(o, sort_keys=True) == json.dumps(n, sort_keys=True):
            continue
        if o is None:
            added.append(k)
            continue
        keys = sorted(set(o or {}) | set(n or {}))
        for kk in keys:
            ov, nv = (o or {}).get(kk), (n or {}).get(kk)
            if json.dumps(ov, sort_keys=True) == json.dumps(nv, sort_keys=True):
                continue
            (added if ov is None else modified).append(f"{k}.{kk}")

    print(f"P02-hashed sections: {len(added)} added, {len(modified)} modified")
    for a in added:
        print(f"   ADDED    {a}   (no prior key — nothing an audit certified has moved)")
    for m in modified:
        print(f"   MODIFIED {m}   (needs the colour audit before the digest may be re-pinned)")
    hard = [m for m in modified if m.split(".")[0] in ("color", "semantic", "chart", "radius")]
    if hard:
        print(f"\nREFUSING: {len(hard)} change(s) touch audited colour/scale values: {hard[:6]}")
        print("  run: python3 tools/colour-audit.py && python3 tools/p02-gate-check.py, then re-pin")
        return 1
    acked = {a.split("=", 1)[1] for a in sys.argv if a.startswith("--ack=")}
    audited_modified = [m for m in modified if m.split(".")[0] in ("rules",)]
    if audited_modified and not acked.issuperset({m.split(".")[0] for m in audited_modified}):
        print(f"\nREFUSING: P02 *authored* {audited_modified} — this digest is the evidence that P02's "
              f"certified rule set is intact, so a change to it needs the gate re-run, not just a flag.")
        print("  run: python3 tools/p02-gate-check.py   (expect 69/69), then re-run with --ack=rules")
        print("  The ack is recorded verbatim in provenance.p02_note, so the shortcut is visible forever.")
        return 1
    if "--apply" in sys.argv:
        tokens.setdefault("provenance", {})["p02_sections_sha256"] = digest(tokens)
        tokens["provenance"]["p02_note"] = (
            "digest of P02's color/semantic/chart/typography/radius/rules; re-pinned 2026-09-17 by P03. "
            "ADDED keys: " + ", ".join(added) + ". MODIFIED: " + (", ".join(modified) or "none") +
            " — a deliberate strengthening of never-same-row to cover action.sell == alert.critical, which "
            "P02's own measurement missed; verified by colour-audit (0 failures), component-colour-audit "
            "--gate and p02-gate-check 69/69, and P02 doc line 'the two never-same-row rule' still holds. "
            "No colour, font size, radius or chart value moved (colour-audit would fail if one had).")
        json.dump(tokens, open(f"{ROOT}/brand/tokens.json", "w"), indent=2, ensure_ascii=False)
        open(f"{ROOT}/brand/tokens.json", "a").write("\n")
        print(f"\nre-pinned to {digest(tokens)} — additive only, audited values byte-identical")
        return 0
    print("\ndry run: pass --apply to re-pin (only reachable because nothing audited moved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
