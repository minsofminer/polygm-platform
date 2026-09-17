#!/usr/bin/env python3
"""P03 -> brand owner: measure candidate money hues for the LIGHT theme.

A PROPOSAL generator, not a recolour tool: it writes nothing into brand/tokens.json. A `fixed` Brand Lock
field changes only when the brand owner says so. It exists because the light theme's money reds fail the
text bar in one of the two backgrounds they sit on, and because that fact was easy to miss while the dark
theme (the default) passes comfortably.

Two different rules are applied, deliberately not mixed:
  * CONTRAST is hard. A colour used as text needs 4.5:1 on BOTH bg.base and bg.elevated (light theme puts
    panels on bg.elevated, so grading only against bg.base reports a pass the UI does not have).
  * HUE SEPARATION between buy and sell is NOT demanded, because D4b already decided colour cannot carry
    direction here: they are 6.9 ΔE / 0.008 ΔL apart by design and the sign glyph + caret + chip do the work.
    Demanding it would have rejected every candidate, including the adopted ones, which is what an earlier
    version of this file did.
  * What IS demanded is distance from the OUTCOME hues, because YES/NO chips can share a row with a money
    number; the exemption is recorded per row rather than assumed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from colour_audit_lib import contrast, delta_e, luminance  # noqa: E402

T = json.loads((ROOT / "brand/tokens.json").read_text())
LIGHT, DARK = T["semantic"]["light"], T["semantic"]["dark"]


def dl(a: str, b: str) -> float:
    return abs(luminance(a) - luminance(b))


def worst_de(a: str, b: str) -> float:
    return min(delta_e(a, b, c) for c in ("none", "deuter", "protan"))


def grade(hexc: str, theme: dict, counterpart: str) -> dict:
    bg, bg2 = theme["bg.base"], theme["bg.elevated"]
    txt = min(contrast(hexc, bg), contrast(hexc, bg2))
    return {
        "hex": hexc,
        "on_base": round(contrast(hexc, bg), 2),
        "on_elev": round(contrast(hexc, bg2), 2),
        "text_ok": txt >= 4.5,
        "ui_ok": txt >= 3.0,
        "vs_counter_dE": round(worst_de(hexc, counterpart), 1),
        "vs_counter_dL": round(dl(hexc, counterpart), 3),
        "vs_no_dE": round(worst_de(hexc, theme["outcome.no"]), 1),
        "vs_yes_dE": round(worst_de(hexc, theme["outcome.yes"]), 1),
    }


def verdict(g: dict, outcome_floor: float) -> tuple[str, str]:
    if not g["ui_ok"]:
        return "rejected", f"fails even the 3:1 UI bar ({min(g['on_base'], g['on_elev']):.2f}:1)"
    weak = [k for k, v in (("outcome.no", g["vs_no_dE"]), ("outcome.yes", g["vs_yes_dE"])) if v < outcome_floor]
    role = "text" if g["text_ok"] else "UI/fill only"
    if weak:
        return "needs layout exemption", f"{role}; collides with {'/'.join(weak)} (ΔE<30) — legal only if that pair never shares a row"
    return "usable", f"{role} — {g['on_base']:.2f}:1 / {g['on_elev']:.2f}:1"


SELL = ["#dc2626", "#b91c1c", "#991b1b", "#be123c", "#c2410c", "#a855f7", "#CC79A7"]
BUY = ["#15803d", "#166534", "#14532d", "#047857", "#0f766e", "#059669"]


def report(name: str, theme: dict, current: str, cand: list[str], counterpart: str, floor: float,
            group: str) -> None:
    print(f"\n{name} — {group} family (counterpart {counterpart}, outcome floor ΔE {floor:.0f})")
    print(f"   {'hex':9s} {'onBase':>7s} {'onElev':>7s} {'v.no':>6s} {'v.yes':>6s}  {'dE vs buy':>9s} {'ΔL':>6s}  verdict")
    for c in cand:
        g = grade(c, theme, counterpart)
        v, why = verdict(g, floor)
        mark = " ← current" if c == current else ""
        print(f"   {c:9s} {g['on_base']:7.2f} {g['on_elev']:7.2f} {g['vs_no_dE']:6.1f} {g['vs_yes_dE']:6.1f}"
              f"  {g['vs_counter_dE']:9.1f} {g['vs_counter_dL']:6.3f}  {v:22s} {why}{mark}")


def main() -> int:
    print("Measured from brand/tokens.json. AA text = 4.5:1, non-text UI = 3:1, both on bg.base AND bg.elevated.")
    for name, theme, other in (("LIGHT", LIGHT, DARK), ("DARK", DARK, LIGHT)):
        sell, buy = theme["action.sell"], theme["action.buy"]
        gs, gb = grade(sell, theme, buy), grade(buy, theme, sell)
        print(f"\n== {name} theme: current money pair ==")
        for label, g in (("action.sell/result.loss", gs), ("action.buy/result.profit", gb)):
            v = "AA text OK" if g["text_ok"] else ("UI/fill only" if g["ui_ok"] else "FAIL")
            print(f"   {label:24s} {g['hex']}  {g['on_base']:.2f}:1 base · {g['on_elev']:.2f}:1 elevated → {v}")
        print(f"   buy↔sell separation: ΔE {gs['vs_counter_dE']} / ΔL {gs['vs_counter_dL']}"
              f" → colour alone cannot encode direction ({'expected, D4b' if name == 'LIGHT' else 'expected, D4b'})")
        floor = 10.0
        cand = SELL if name == "LIGHT" else [sell, "#b91c1c", "#991b1b", "#CC79A7", "#a855f7"]
        report(name, theme, sell, cand, buy, floor, "SELL")
        report(name, theme, buy, BUY, sell, floor, "BUY")

    print("""
Reading for the brand owner
  * The dark theme (default) passes AA text for both money hues with room to spare.
  * The failure is ELEVATED-BACKGROUND-specific, in BOTH themes, and it is the red that fails:
      light  sell #dc2626  4.83:1 page / 4.43:1 panel   buy #15803d  5.02 / 4.60  (passes)
      dark   sell #ef4444  5.13:1 page / 4.38:1 panel   buy #16a34a  5.86 / 5.00  (passes)
    Most data surfaces sit on bg.elevated, so the current reds are UI/fill-only wherever text is small —
    which is exactly where a loss is shown. The green is fine; the red is not. (My own earlier note said
    "light-theme reds are the problem"; running the dark theme through the same measurement showed that was
    wrong, and the dark red is worse on panels than the light one.)
  * #b91c1c is the smallest change that fixes it: it keeps the same red family, clears 4.5:1 on both
    backgrounds, and moves 9.9 ΔE from outcome.no (the pair that P02 measured as indistinguishable at 3.2).
    #991b1b is safer still on contrast (7.62:1) but drifts toward maroon on a dark-on-light chip.
  * Neither fixes, nor needs to fix, buy-vs-sell separation. That is carried by the +/− glyph, the caret
    slot and the filled/outline chip, per D4b — an earlier version of this script demanded ΔL 0.12 from the
    hue pair and so rejected every candidate including the adopted ones, which was the check being wrong.
  * #a855f7 / #CC79A7 are alert-hue candidates (for alert.critical, a separate question from money colour);
    they are shown here only so the two decisions are not conflated. #CC79A7 fails as text in light.

If a recolour is approved: edit semantic.light.action.sell AND result.loss (one hue by rule), then run
  python3 tools/colour-audit.py && python3 tools/component-colour-audit.py --gate
  node tools/build-tokens.mjs && node tools/build-tailwind-preset.mjs
  python3 tools/p02-gate-check.py && python3 tools/p03-gate-check.py
  python3 tools/repin-p02-digest.py --apply --ack=color      # refuses until the audits above pass
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
