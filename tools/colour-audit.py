#!/usr/bin/env python3
"""P02 D4 colour audit — WCAG contrast and colour-blind separation, computed.

P02's quality gate says "Every text pair has a contrast ratio. Show the numbers." Doing that by
eyeball is how a brand kit ships an unreadable tape, so this measures it:

  • WCAG 2.1 relative luminance + contrast ratio for every declared pair (AA body 4.5:1,
    AA large/UI 3:1, AAA 7:1) — the same formula the Brand Kit's existing table used.
  • CIE Lab ΔE*ab separation under none / deuteranopia / protanopia using the Brettel-Viénot
    1-D simulation, so the 8-colour chart palette is audited against the pair of deficiencies
    P02 names, not against a vibe.
  • The YES/NO vs BUY/SELL vs PROFIT/LOSS collision the prompt asks us to resolve: are the two
    axes distinguishable for a deuteranope at all?

    python3 tools/colour-audit.py            # report
    python3 tools/colour-audit.py --json     # for the gate script / CI

Deltas below 10 JND are treated as "will be confused in a dense 11px table". That threshold is a
convention (a colour pair ~10 apart in ΔE*ab is generally held to be distinguishable), not a law.
"""
from __future__ import annotations

import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- tokens (single source: brand/BRAND-KIT.md)
import json as _json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from colour_audit_lib import SIMS, contrast, delta_e, worst_separation  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Tokens are LOADED from brand/tokens.json. This file previously carried its own hardcoded copy of
# the palette, which is how it survived P02 reviewing while still asserting `no: #a1c0ff` — the exact
# value the ΔE measurement rejected (it sits 1.5 ΔE from the other outcome colour for deuteranopes).
# An auditor reading a stale snapshot audits nothing.
_TOK = _json.load(open(os.path.join(ROOT, "brand", "tokens.json")))
DARK, LIGHT = _TOK["semantic"]["dark"], _TOK["semantic"]["light"]
CHART = {"dark": _TOK["chart"]["dark"], "light": _TOK["chart"]["light"]}

_PAIR_FLOORS = {  # WCAG floor per role; mirrors build-contrast-table.py's ROWS
    "text.primary": 4.5, "text.secondary": 4.5, "text.muted": 3.0,
    "brand.primary": 3.0, "brand.primary-text": 4.5,   # brand.on-primary pairs with brand.primary only (see below), "outcome.yes": 3.0, "outcome.no": 3.0,
    "action.buy": 3.0, "action.sell": 3.0, "result.profit": 3.0, "result.loss": 3.0,
    "alert.info": 3.0, "alert.watch": 3.0, "alert.high": 3.0, "alert.critical": 3.0,
}
SURFACES = ["bg.base", "bg.elevated", "bg.inset"]
PAIRS = [
    (fg, "bg.base", _PAIR_FLOORS[fg]) for fg in _PAIR_FLOORS
] + [
    (fg, "bg.elevated", _PAIR_FLOORS[fg]) for fg in ("text.primary", "text.secondary")
] + [
    ("text.inverse", "alert.high", 4.5),          # label on a warning banner
    ("text.inverse", "brand.primary", 3.0),       # button label
]


def dea(h1, h2, cond="none"):
    return delta_e(h1, h2, cond)


def audit() -> dict:
    out: dict = {"themes": {}, "chart": {}, "axes": {}}
    for theme, toks in (("dark", DARK), ("light", LIGHT)):
        rows = []
        for fg, bg, need in PAIRS:
            if fg not in toks or bg not in toks:
                continue
            ratio = contrast(toks[fg], toks[bg])
            rows.append({"fg": fg, "bg": bg, "hex": (toks[fg], toks[bg]), "need": need,
                         "ratio": round(ratio, 2), "pass": ratio >= need,
                         "grade": ("AAA" if ratio >= 7 else "AA" if ratio >= 4.5
                                   else "AA-large" if ratio >= 3 else "FAIL")})
        out["themes"][theme] = rows
    for theme, arr in CHART.items():
        out["chart"][theme] = {"n": len(arr),
                              "worst_delta_e": round(worst_separation(arr), 1),
                              "floor": _TOK["chart"].get("contrastFloor", {}).get(theme, 3.0),
                              "separable": worst_separation(arr) >= 10.0}
    for cond in SIMS:
        out["axes"][cond] = {
            "yes_vs_no": round(dea(DARK["outcome.yes"], DARK["outcome.no"], cond), 1),
            "no_vs_critical": round(dea(DARK["outcome.no"], DARK["alert.critical"], cond), 1),
            "buy_vs_profit": round(dea(DARK["action.buy"], DARK["result.profit"], cond), 1),
            "buy_vs_sell": round(dea(DARK["action.buy"], DARK["action.sell"], cond), 1),
        }
    return out


def main() -> int:
    r = audit()
    if "--json" in sys.argv:
        print(json.dumps(r, indent=2))
        return 0
    fails = 0
    print(f"tokens audited: brand/tokens.json ({len(DARK)} dark / {len(LIGHT)} light roles)")
    for theme, rows in r["themes"].items():
        print(f"\n=== {theme} — WCAG contrast (foreground / background) ===")
        for x in rows:
            mark = "ok  " if x["pass"] else "FAIL"
            fails += not x["pass"]
            print(f"  [{mark}] {x['fg']:15s} on {x['bg']:12s} {x['hex'][0]} / {x['hex'][1]}"
                  f"  {x['ratio']:>5.2f}:1  need {x['need']:>3.1f}  {x['grade']}")
    print("\n=== chart palette separation (ΔE*ab worst pair, across normal/deuter/protan) ===")
    for theme, v in r["chart"].items():
        print(f"  {theme:5s} n={v['n']}  worst ΔE={v['worst_delta_e']:5.1f}  "
              f"{'separable' if v['separable'] else 'TOO CLOSE'}  (contrast floor {v['floor']}:1)")
    print("\n=== semantic axes (dark theme, ΔE*ab) ===")
    for cond, v in r["axes"].items():
        name = {"none": "trichromatic", "deuter": "deuteranopia", "protan": "protanopia"}[cond]
        print(f"  {name:14s} yes↔no {v['yes_vs_no']:6.1f} | no↔critical {v['no_vs_critical']:6.1f}"
              f" | buy↔profit {v['buy_vs_profit']:5.1f} | buy↔sell {v['buy_vs_sell']:6.1f}")
    print(f"\n  contrast failures: {fails}")
    print("  buy↔profit ΔE≈0 is intentional: the same hue, different context. The disambiguator is")
    print("  the glyph (+/−, ▲/▼), never the colour — see D4's colour-independence rules.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
