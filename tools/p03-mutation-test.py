#!/usr/bin/env python3
"""Mutation test for tools/p03-gate-check.py.

The kit's rule 7 says a test that cannot fail is not a test. P01's gate was validated this way (9/9
caught); this does the same for P03's eight check groups. Each mutation breaks ONE invariant in a COPY of
the workspace, and the gate must go red for the right reason. A mutation the gate misses is printed as a
MISS and reported in the exit code — this file is only useful if it can embarrass me.

  python3 tools/p03-mutation-test.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
GATE = "tools/p03-gate-check.py"
TOKENS = "brand/tokens.json"
SPEC = "docs/P03-design-system.md"
CSS = "brand/tokens.css"
SPECIMEN = "brand/specimen.html"
LOCKUP = "brand/svg/lockup-horizontal.svg"
KIT = "brand/BRAND-KIT.md"

# (id, group, description, path, mutate(text)->text)
MUTATIONS: list[tuple[str, str, str, str, ...]] = [
    ("M1", "G1", "a D1 foundation section is deleted from tokens.json", TOKENS,
     lambda s: json.dumps({k: v for k, v in json.loads(s).items() if k != "elevation"}, indent=2)),
    ("M2", "G1", "density switch removed from the stylesheet", CSS,
     lambda s: re.sub(r'\[data-density="compact"\]\s*\{[^}]*\}', "", s)),
    ("M3", "G2", "spec cites an endpoint that was never probed", SPEC,
     lambda s: s + "\n`userScore ← https://gamma-api.polymarket.com/users?address=…`\n"),
    ("M4", "G2", "a dead endpoint is presented as a data source without its status", SPEC,
     lambda s: s.replace("lb-api/pnl` 404s", "lb-api/pnl` exists")
              .replace("`lb-api/pnl` 404s and `lb-api/rank` returns 400 even with `rank` supplied",
                       "`lb-api/pnl` is available for PnL")),
    ("M5", "G3", "a domain component section is deleted", SPEC,
     lambda s: re.sub(r"#### `StaleIndicator`.*?(?=\n---)", "", s, flags=re.S)),
    ("M6", "G3", "a primitive loses its accessibility clause", SPEC,
     lambda s: re.sub(r"(\| `Checkbox`.*?\| )[^|\n]*\|$", r"\1— |", s, count=1, flags=re.M)),
    ("M7", "G4", "a motion duration breaks the skill's ceiling", TOKENS,
     lambda s: json.dumps(_set(json.loads(s), ["motion", "duration_ms", "small", "max"], 900), indent=2)),
    ("M8", "G4", "an easing curve drifts from the vendored skill's value", TOKENS,
     lambda s: json.dumps(_set(json.loads(s), ["motion", "easing", "ease-out"], "cubic-bezier(0.4, 0, 0.2, 1)"), indent=2)),
    ("M9", "G4", "the number-animates ban is weakened to a suggestion", TOKENS,
     lambda s: json.dumps(_set(json.loads(s), ["motion", "number_policy", "rule"], "numbers may animate when idle"), indent=2)),
    ("M10", "G5", "float arithmetic is introduced in a money example", SPEC,
     lambda s: s + "\n```js\nconst pnl = parseFloat(row.price) * parseFloat(row.size) * 100;\n```\n"),
    ("M11", "G5", "the decimal-separator rule is removed", SPEC,
     lambda s: re.sub(r"\*\*Rule: prices and tick sizes always use[^.]*\.", "", s)),
    ("M12", "G6", "the specimen redefines a token instead of consuming it", SPECIMEN,
     lambda s: s.replace(".pgm-sr-only {", ".drift { --pgm-space-2: 7px; }\n.pgm-sr-only {")),
    ("M13", "G6", "a colour literal appears in the specimen CSS", SPECIMEN,
     lambda s: s.replace(".panel {\n  background: var(--pgm-bg-elevated);", ".panel {\n  background: #101010;")),
    ("M14", "G6", "the specimen uses an undefined custom property", SPECIMEN,
     lambda s: s.replace("var(--pgm-emphasis)", "var(--pgm-font-huge)")),
    ("M15", "G6", "the PnL number is coloured (the exact P03 finding)", SPECIMEN,
     lambda s: s.replace(".pnl { color: var(--pgm-text-primary); }", ".pnl { color: var(--pgm-action-buy); }")),
    ("M16", "G7", "the WCAG conformance claim is deleted", SPEC,
     lambda s: re.sub(r"\*\*Normative target:.*?\*\*", "", s)
                    .replace("WCAG 2.2 Level AA", "the accessibility target").replace("WCAG 2.2 AA", "the accessibility target")),
    ("M17", "G8", "the lockup is hand-edited (wordmark changed directly)", LOCKUP,
     lambda s: s.replace(">Open<tspan", ">POLY<tspan").replace(">out</tspan>", "GM</tspan>")),
    ("M18", "G8", "the pinned geometry hash no longer matches the master", KIT,
     lambda s: s.replace("c33b4d5fd82b7acd", "aaaaaaaaaaaaaaaa")),
    ("M19", "G1", "breakpoints lose the terminal definition", TOKENS,
     lambda s: json.dumps(_drop(json.loads(s), ["breakpoints", "values"], lambda b: b.get("name") != "xl"), indent=2)),
    ("M20", "G3", "a story-state pair is dropped from the list", TOKENS,
     lambda s: json.dumps(_set(json.loads(s), ["components", "states"],
                               [x for x in json.loads(s)["components"]["states"] if x != "insufficient"]), indent=2)),
]


def _set(obj, path, value):
    cur = obj
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value
    return obj


def _drop(obj, path, pred):
    cur = obj
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = [x for x in cur[path[-1]] if not pred(x)]
    return obj


def run_gate(root: Path) -> tuple[int, str]:
    p = subprocess.run([sys.executable, GATE], cwd=root, capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    base_rc, base_out = run_gate(SRC)
    print(f"baseline gate on the real workspace: exit={base_rc}")
    if base_rc != 0:
        print("  fix the gate's failures before mutation testing; a red baseline hides misses")
        print("  " + "\n  ".join(base_out.splitlines()[-6:]))
        return 2
    caught, missed = [], []
    for mid, group, desc, rel, fn in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="p03mut-") as td:
            root = Path(td)
            # copy the trees that carry code AND their subdirs in one pass, then re-copy the mutated
            # targets explicitly so a stale mtime can never make a check read the pre-mutation file
            for d in ("tools", "docs", "brand", "web", "server", "skills"):
                shutil.copytree(SRC / d, root / d, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.pyc"))
            # NOTE: the tree copy above already contains every file the gate reads. Re-copying the
            # originals here (as an earlier version did) silently UNDOES the mutation, which produced
            # a terrifying-looking "0/20 caught" — 20 false misses caused by the harness, not the gate.
            path = root / rel
            text = path.read_text()
            try:
                mutated = fn(text)
            except Exception as e:  # a mutation that cannot be applied is not a test
                missed.append((mid, group, desc, f"could not apply: {e}"))
                print(f"  {mid:4s} {group:3s} SKIP  {desc}  (mutation error: {e})")
                continue
            if mutated.strip() == text.strip():
                missed.append((mid, group, desc, "mutation changed nothing — vacuous"))
                print(f"  {mid:4s} {group:3s} MISS  {desc}  (no-op mutation)")
                continue
            path.write_text(mutated)
            rc, out = run_gate(root)
            if rc not in (0, 1):
                print("     gate output:", out.strip()[:400].replace("\n", " | "))
            tag = [l.strip() for l in out.splitlines() if "[FAIL]" in l]
            hit_group = any(group in t or mid.replace("M", "G") in t for t in tag)
            if rc != 0 and tag:
                caught.append(mid)
                print(f"  {mid:4s} {group:3s} caught {desc}  →  {tag[0][:96]}")
            else:
                missed.append((mid, group, desc, f"exit={rc}, failures={'none' if not tag else tag[0][:60]}"))
                print(f"  {mid:4s} {group:3s} MISS  {desc}  (gate exit={rc})")
    print(f"\n{len(caught)}/{len(MUTATIONS)} mutations caught")
    if missed:
        print("NOT CAUGHT:")
        for m in missed:
            print(f"  ✗ {m[0]} {m[1]}: {m[2]} — {m[3]}")
    print("\nA miss means the gate cannot see that class of breakage. Either fix the check, or say plainly")
    print("in the report that the invariant is unenforced — never pretend it is covered.")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
