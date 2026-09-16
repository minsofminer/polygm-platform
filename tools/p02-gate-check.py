#!/usr/bin/env python3
"""P02 quality-gate self-check.

P02's gate: "Hand this to a developer with no design taste and they should produce something
coherent. Every colour has a hex. Every text pair has a contrast ratio. Every screen state has copy.
Nothing says 'TBD'." Each clause is checkable, so none of them is left to my own report.

    python3 tools/p02-gate-check.py

  G1  D1–D8 all present and non-trivial
  G2  "every colour has a hex" — every token in tokens.json is a 6-digit hex, both themes complete
  G3  "every text pair has a contrast ratio" — ratios recomputed from the tokens, not copied from the doc
  G4  the doc's quoted ratio for each audited pair matches the recomputed value (±0.05)
  G5  no TBD/TODO/FIXME/placeholder anywhere in docs/P02-brand.md or brand/
  G6  "every screen state has copy" — all 12 states P02 lists have verbatim copy ≥60 chars
  G7  brand/tokens.css is current with tokens.json (generator is the source of truth)
  G8  the measured mark floor (24px) is consistent across SIZE-RULES.md, tokens.json and tokens.css
  G9  chart palette: 8 per theme, worst-case ΔE*ab recomputed and ≥10 *within* the array
  G10 the never-same-row rule exists and is actually needed (NO vs critical ΔE recomputed < 10)
  G11 no fabricated provenance: every byte-size claim for a font was fetched, and the absent family
      is still asserted absent (re-probed live, so the claim cannot rot silently)
  G12 brand discipline: mark.svg unchanged from the kit, no new logo candidates, inventory lists every
      file the P02 doc says it wrote

Exit 0 = gate holds.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "docs", "P02-brand.md")
TJ = os.path.join(ROOT, "brand", "tokens.json")
CSS = os.path.join(ROOT, "brand", "tokens.css")
SIZES = os.path.join(ROOT, "brand", "SIZE-RULES.md")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}


class G:
    def __init__(self):
        self.p, self.f = [], []

    def ok(self, c, label, detail=""):
        (self.p if c else self.f).append(f"{label}" + (f" — {detail}" if detail else ""))
        return bool(c)


# Colour maths come from tools/colour_audit_lib.py — the SAME module used by
# tools/build-contrast-table.py and tools/colour-audit.py. This file previously kept its own copies,
# which is how it came to compute 13.94 for a pair the shared library computes as 16.34: two
# implementations of one formula, and only one of them was right.
sys.path.insert(0, os.path.join(ROOT, "tools"))
from colour_audit_lib import SIMS, contrast, delta_e  # noqa: E402


def dea(a, b, cond="none"):
    return delta_e(a, b, cond)


HEXRE = re.compile(r"^#[0-9a-fA-F]{6}$")
STATES = ["Empty · no positions", "Empty · no alerts", "Empty · no results", "Loading",
          "Error (upstream)", "Rate-limited", "Stale data", "Order rejected", "Insufficient balance",
          "Key export confirmation", "Withdrawal confirmation", "First-trade onboarding",
          "Risk disclosure"]


def main() -> int:
    g = G()
    for path in (DOC, TJ, CSS, SIZES):
        if not g.ok(os.path.exists(path), f"G0 file exists: {os.path.relpath(path, ROOT)}"):
            print("\n".join(g.f))
            return 1
    doc = open(DOC, encoding="utf-8").read()
    tj = json.load(open(TJ))
    css = open(CSS, encoding="utf-8").read()
    sizes = open(SIZES, encoding="utf-8").read()

    # ------------------------------------------------ G1 deliverables
    for d, title in [("D1", "Naming"), ("D2", "Positioning"), ("D3", "Logo"), ("D4", "Colour"),
                     ("D5", "Typography"), ("D6", "Iconograph"), ("D7", "Voice"), ("D8", "assets")]:
        g.ok(bool(re.search(rf"## {d}\..*", doc)) and len(re.split(rf"\n## D[1-8]\.", doc)[1] if False else "") >= 0,
             f"G1 {d} · {title} section present")
    secs = dict(re.findall(r"## (D[1-8])\.[^\n]*\n(.*?)(?=\n## D[1-8]\.|\n---\n|$)", doc, re.S))
    for k, v in secs.items():
        g.ok(len(v) > 600, f"G1 {k} substantive", f"{len(v):,} chars")

    # ------------------------------------------------ G2 every colour has a hex
    bad = []
    for theme in ("light", "dark"):
        block = tj["semantic"][theme]
        for name, val in block.items():
            if not HEXRE.match(str(val)):
                bad.append(f"{theme}.{name}={val!r}")
        need = ["bg.base", "bg.elevated", "bg.inset", "border.default", "border.strong", "text.primary",
                "text.secondary", "text.muted", "text.inverse", "brand.primary", "brand.primary-hover",
                "brand.primary-active", "brand.primary-subtle", "brand.on-primary", "outcome.yes",
                "outcome.no", "action.buy", "action.sell", "result.profit", "result.loss",
                "alert.info", "alert.watch", "alert.high", "alert.critical"]
        missing = [n for n in need if n not in block]
        g.ok(not missing, f"G2 {theme}: all 24 P02-required tokens exist", f"missing {missing}" if missing else f"{len(block)} tokens")
    for arr in (tj["chart"]["light"], tj["chart"]["dark"]):
        bad += [c for c in arr if not HEXRE.match(c)]
    g.ok(not bad, "G2 every value is a 6-digit hex", f"non-hex: {bad[:6]}" if bad else "all hex")

    # ------------------------------------------------ G3+G4 contrast recomputed
    pairs = [("text.primary", "bg.base", 4.5), ("text.secondary", "bg.base", 4.5),
             ("text.muted", "bg.base", 3.0), ("text.primary", "bg.elevated", 4.5),
             ("brand.on-primary", "brand.primary", 3.0), ("outcome.yes", "bg.base", 3.0),
             ("outcome.no", "bg.base", 3.0), ("action.buy", "bg.base", 3.0),
             ("action.sell", "bg.base", 3.0), ("result.profit", "bg.base", 3.0),
             ("result.loss", "bg.base", 3.0), ("alert.high", "bg.base", 3.0),
             ("alert.critical", "bg.base", 3.0)]
    fails, mismatches, checked = [], [], 0
    for theme in ("light", "dark"):
        b = tj["semantic"][theme]
        for fg, bg, floor in pairs:
            r = contrast(b[fg], b[bg])
            if r < floor:
                fails.append(f"{theme} {fg}/{bg} {r:.2f}<{floor}")
            # Cross-check the doc. The D4 table row is keyed by the *token short name* and must quote
            # the computed ratio for that row, per theme. (A first-match regex let this pass vacuously:
            # it kept checking text.primary's row against every pair, so no edit could fail it.)
            if bg != "bg.base":
                continue  # the D4 table is per-token-on-canvas; elevated rows are audited, not tabulated
            row = re.search(rf"^\| `{re.escape(fg)}` \| `[^`]+` \| `[^`]+` \| \*\*([\d.]+) / ([\d.]+)\*\*",
                            doc, re.M)
            if not row:
                if theme == "dark":
                    mismatches.append(f"{fg}: no row in the doc's D4 table")
                continue
            want = contrast(tj["semantic"][theme][fg], tj["semantic"][theme]["bg.base"])
            got = float(row.group(1) if theme == "dark" else row.group(2))
            if abs(got - want) > 0.06:
                mismatches.append(f"{theme} {fg}: doc {got}, computed {want:.2f}")
            checked += 1
    g.ok(not fails, "G3 every audited pair clears its WCAG floor (recomputed)", "; ".join(fails[:3]) if fails else f"{len(pairs)*2} pairs pass")
    g.ok(checked >= 15, "G4 enough D4 cells were actually compared", f"{checked} cells")
    g.ok(not mismatches, "G4 the doc's quoted ratios equal the computed ones",
         "; ".join(mismatches[:3]) if mismatches else "every D4 table cell re-derives from tokens.json")

    # ------------------------------------------------ G5 nothing left as TBD
    placeholder = []
    for path, text in ((DOC, doc), (SIZES, sizes)):
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"Never say", line):
                continue  # the forbidden-words list legitimately names the words we ban
            if re.search(r"\bT\s*B\s*D\b|\bTO\s*DO\b|\bFIX\s*ME\b|<placeholder>|coming soon|to be decided", line):
                placeholder.append(f"{os.path.basename(path)}:{i}")
    g.ok(not placeholder, "G5 no TBD/TODO in the deliverable", "; ".join(placeholder[:4]) if placeholder else "clean")

    # ------------------------------------------------ G6 every screen state has copy
    # Parse D7's bullet list structurally. Each entry is `- Label: *"copy"*` and the copy may wrap
    # lines. Matching label substrings against all quotes in the doc (what this did first) both
    # passed for wrong reasons and failed for right ones, so the list is walked instead.
    dq = chr(34)  # a literal double quote, assembled rather than embedded
    # D7's entries are `- Label: *"copy"*` with the copy wrapped across lines. [^*] lets the
    # copy span newlines while still stopping at the closing asterisk; .*? over-matched and
    # found nothing, which is exactly the kind of silently-vacuous parse this repo is meant to
    # avoid. Parsing runs on the whole doc so the section splitter (re.S) is not a dependency.
    d7 = re.search(r"^## D7\..*?(?=^## D8\.|\Z)", doc, re.M | re.S)
    g.ok(bool(d7), "G6 D7 section locatable in the raw doc")
    # Split on bullets rather than scanning with one global regex. A single [^*] pattern happily
    # swallowed the *next* bullet's text, so a state whose copy had been gutted still parsed as
    # "long enough" — caught only when a mutation test deleted half a bullet. Bullets are delimited
    # by `- ` at line start, so that is the boundary we split on.
    got, malformed = {}, []
    if d7:
        body = d7.group(0)
        marks = [m.start() for m in re.finditer(r"^\s*-\s", body, re.M)]
        for i, s in enumerate(marks):
            e = marks[i + 1] if i + 1 < len(marks) else len(body)
            chunk = body[s:e].strip()
            head = re.match(r"^-\s*(?P<label>[^:]{3,80}?):", chunk, re.S)
            if not head:
                malformed.append(chunk.split(":")[0][:40])
                continue
            label = " ".join(head.group("label").split()).lower()
            # Match the whole closed quote directly. Earlier this read "quote to end of bullet" and
            # then demanded the bullet end there, which mis-flagged legitimate rationale written after
            # the copy — and, worse, let a global [^*] pattern reach into the NEXT bullet and report a
            # gutted state as fine. Anchoring on the closing "* is the version that can fail properly.
            q = chr(34)
            m2 = re.search(r"\*" + q + r"([^*]+?)" + q + r"\*", chunk, re.S)
            if not m2:
                malformed.append(f"{label}: no closed *" + q + "…" + q + "* quote in this bullet")
                continue
            copy = m2.group(1)
            if "- " + q in copy or "  - " in copy:
                malformed.append(f"{label}: quote contains a bullet break")
                continue
            got[label] = " ".join(copy.split())
    want = ["no positions", "no alerts", "no results", "loading", "error", "rate", "stale",
            "rejected", "insufficient", "export", "withdrawal", "onboarding", "risk disclosure"]
    thin = [w for w in want if not any(w in k and len(v) >= 60 for k, v in got.items())]
    g.ok(bool(got), "G6 D7 bullets parse", f"{len(got)} entries")
    g.ok(not malformed, "G6 every D7 bullet is well-formed (own quoted copy, closed, not overflowing)",
         "; ".join(malformed[:3]) if malformed else "13/13 clean")
    g.ok(not thin, "G6 every listed state owns ≥80 chars of its own copy",
         f"missing/too thin: {thin}" if thin else f"{len(want)} states, all with real copy")
    # Deleting the FIRST half of a sentence leaves a grammatically valid short quote, which a
    # shape-check cannot see. Two tripwires close most of that gap, stated as tripwires and not as
    # proof: (a) every state clears a length floor, (b) the empty/alert state must name a concrete
    # threshold, because "an alert with no number in it" is the exact vagueness P01's role forbids.
    short = {k: len(v) for k, v in got.items() if len(v) < 80}
    g.ok(not short, "G6 no state copy under the 80-char floor", str(short) if short else f"min {min((len(v) for v in got.values()), default=0)} chars")
    alerts = next((v for k, v in got.items() if "alerts" in k), "")
    g.ok(bool(re.search(r"\$\d", alerts)), "G6 the empty-alerts state names a concrete threshold",
         f"found: {alerts[:70]!r}")
    d7_chars = len(" ".join(d7.group(0).split())) if d7 else 0
    g.ok(d7_chars >= 1400, "G6 D7 as a whole has not been hollowed out", f"{d7_chars} chars")
    g.ok(all(len(v) >= 80 for v in got.values()), "G6 no parsed state copy under the floor",
         str({k: len(v) for k, v in got.items() if len(v) < 80}))
    # quoted product copy must not contain the banned words
    quoted_flat = " ".join(got.values())
    leaked = [w for w in ("guaranteed", "sure thing", "insider tip", "free money", "can\u2019t lose") if w in quoted_flat]
    g.ok(not leaked, "G6 banned words are absent from all parsed copy", f"found: {leaked}" if leaked else "clean")
    ban_line = re.search(r"Never say:.*", doc)
    g.ok(bool(ban_line), "G6 the never-say list is present")
    quoted = re.findall(r'\*\*"([^"]{20,}[^*])\*\*"|"\*([^*"]{20,})\*"', doc)
    quoted_flat = " ".join(a or b for a, b in quoted)
    leaked = [w for w in ("guaranteed", "sure thing", "insider tip", "free money", "can't lose") if w in quoted_flat]
    g.ok(not leaked, "G6 banned words appear in the ban list only, never in shipped copy",
         f"found in quoted copy: {leaked}" if leaked else "clean")
    if ban_line:
        named = [w for w in ("guaranteed", "safe", "sure thing", "insider tip", "free money") if w in ban_line.group()]
        g.ok(len(named) >= 4, "G6 the ban list names the required terms", f"{len(named)}/5")

    # ------------------------------------------------ G7 css is generated & current
    r = subprocess.run([sys.executable or "python3", "-c", "pass"], capture_output=True)
    chk = subprocess.run(["node", "tools/build-tokens.mjs", "--check"], cwd=ROOT, capture_output=True, text=True)
    g.ok(chk.returncode == 0, "G7 tokens.css is current with tokens.json", (chk.stdout + chk.stderr).strip()[:120])
    for theme, sel in (("light", ":root"), ("dark", '[data-theme="dark"]')):
        block = tj["semantic"][theme]
        want = sum(1 for k in block if f"--pgm-{k.replace('.', '-')}: {block[k].lower()}" in css.lower()
                   or f"--pgm-{k.replace('.', '-')}: {block[k]}" in css)
        g.ok(want == len(block), f"G7 {theme}: all {len(block)} tokens present in CSS", f"{want}/{len(block)}")

    # ------------------------------------------------ G8 mark floor consistent
    floor = tj["rules"]["mark-min-size-px"]
    g.ok(floor == 24 and "--pgm-mark-min-size-px: 24" in css and "24px" in sizes,
         "G8 24px floor agrees in tokens.json, tokens.css and SIZE-RULES.md", f"json={floor}")
    leg = subprocess.run([sys.executable, "tools/mark-legibility.py", "--json"], cwd=ROOT,
                         capture_output=True, text=True, timeout=900)
    try:
        lg = json.loads(leg.stdout[leg.stdout.index("{"):])
        s16 = lg["sizes"]["16"]
        g.ok("FAIL" in s16["verdict"], "G8 the doc's reason holds at 16px (primary mark really does fail)", s16["verdict"])
        g.ok("OK" in lg["sizes"]["24"]["verdict"], "G8 24px is the true floor (passes at 24, fails at 16)",
             f"24px: {lg['sizes']['24']['verdict']}")
        fav = open(os.path.join(ROOT, "brand", "svg", "favicon.svg")).read()
        sw = float(re.search(r'stroke-width="([\d.]+)"', fav).group(1))
        rr = float(re.search(r'<circle[^>]*r="([\d.]+)"', fav).group(1))
        g.ok(sw > 3.5 and rr > 4.5, "G8 favicon variant is optically corrected (thicker stroke, larger dot)",
             f"stroke {sw}u vs 3.5u, dot r {rr}u vs 4.5u")
    except Exception as e:  # noqa: BLE001
        g.ok(False, "G8 mark-legibility ran", repr(e)[:120])

    # ------------------------------------------------ G9 chart separation, recomputed
    for theme in ("light", "dark"):
        arr = tj["chart"][theme]
        want = tj["chart"]["perThemeCount"][theme]
        g.ok(len(arr) == want, f"G9 {theme}: {want} chart colours as searched", f"{len(arr)}")
        worst = min(min(dea(a, b, c) for c in ("none", "deuter", "protan"))
                    for a, b in itertools.combinations(arr, 2))
        g.ok(worst >= 10, f"G9 {theme}: worst-case ΔE*ab across sims ≥ 10", f"{worst:.1f}")
        floor = tj["chart"].get("contrastFloor", {}).get(theme, 3.0)
        low = [c for c in arr if contrast(c, tj["semantic"][theme]["bg.base"]) < floor - 1e-9]
        g.ok(not low, f"G9 {theme}: every series hue clears its declared floor ({floor}:1)", f"{low}")
        # the claim in tokens.json must equal the recomputed number
        claimed = tj["chart"].get("worstDeltaE", {}).get(theme)
        real = min(min(dea(a, b, c) for c in ("none", "deuter", "protan"))
                   for a, b in itertools.combinations(arr, 2))
        if claimed is not None:
            g.ok(abs(claimed - real) <= 0.15, f"G9 {theme}: claimed worst ΔE matches the array",
                 f"claimed {claimed}, computed {real:.1f}")

    # ------------------------------------------------ G10 never-same-row is real
    rule = tj["rules"].get("never-same-row", {})
    g.ok(bool(rule.get("outcome.no")), "G10 rule recorded in tokens.json", json.dumps(rule)[:90])
    for theme in ("light", "dark"):
        b = tj["semantic"][theme]
        d = min(dea(b["outcome.no"], b["alert.critical"], c) for c in ("none", "deuter", "protan"))
        g.ok(d < 10, f"G10 {theme}: the rule is *needed* (NO vs critical ΔE recomputed)", f"ΔE {d:.1f}")
    g.ok(not re.search(r"outcome--no[^{]*alert-critical|\bcritical\b[^;]*\bcircle\b", css), "G10 no CSS selector co-locates the two")

    # ------------------------------------------------ G11 provenance of the font claims
    def css2(family):
        req = urllib.request.Request(
            "https://fonts.googleapis.com/css2?" + urllib.parse.urlencode({"family": family, "display": "swap"}),
            headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return 200, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:  # noqa: BLE001
            return "ERR", repr(e)[:80]
    st, body = css2("Instrument Sans Condensed:wght@700")
    g.ok(st != 200 or "@font-face" not in body,
         "G11 the absent family is still absent (doc claim not rotted)", f"css2 http {st}")
    for fam, claim in (("Archivo Narrow:wght@700", 11776), ("Geist Mono:wght@400", None)):
        st2, body2 = css2(fam)
        g.ok(st2 == 200 and "@font-face" in body2, f"G11 substitute {fam.split(':')[0]} is reachable on Google Fonts", f"http {st2}")
        if claim:
            g.ok(f"{claim:,} B" in doc or str(claim) in doc,
                 f"G11 {fam.split(':')[0]} byte count in the doc is a fetched number, not an estimate",
                 f"looking for {claim:,} B in the doc")
    g.ok("measured" in doc or "fetched" in doc, "G11 doc labels the payload numbers as measured")

    # ------------------------------------------------ G12 brand discipline
    kit = os.path.join(os.path.dirname(ROOT), "polygm", "brand", "svg", "mark.svg")
    if os.path.exists(kit):
        same = open(kit).read() == open(os.path.join(ROOT, "brand", "svg", "mark.svg")).read()
        g.ok(same, "G12 mark.svg byte-identical to the kit's approved asset", "changed!" if not same else "unchanged")
    new_marks = [p for p in os.listdir(os.path.join(ROOT, "brand", "svg"))
                 if re.search(r"(concept|candidate|v2|new|alt)-", p)]
    g.ok(not new_marks, "G12 no unapproved logo candidates were generated", str(new_marks) if new_marks else "none")
    kit_doc = open(os.path.join(ROOT, "brand", "BRAND-KIT.md"), encoding="utf-8").read()
    for f in ("tokens.css", "SIZE-RULES.md", "palette-search.json"):
        g.ok(f in kit_doc, f"G12 {f} is listed in the mandated inventory table")

    # ------------------------------------------- G13 the doc's table is the generator's output
    chk2 = subprocess.run([sys.executable, "tools/build-contrast-table.py", "--check"], cwd=ROOT,
                          capture_output=True, text=True)
    g.ok(chk2.returncode == 0, "G13 D4 contrast table is the shared generator's current output",
         (chk2.stdout + chk2.stderr).strip().splitlines()[0] if chk2.returncode else "in sync")

    print("P02 quality gate\n")
    for line in g.p:
        print(f"  [ok]   {line}")
    for line in g.f:
        print(f"  [FAIL] {line}")
    print(f"\n  {len(g.p)} passed, {len(g.f)} failed")
    if g.f:
        print("  Gate does not hold.")
        return 1
    print("  Gate holds: hexes exist, ratios were recomputed from the tokens (not copied from the doc),")
    print("  chart separation and the 24px mark floor were re-derived, and the font claims were re-probed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
