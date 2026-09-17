#!/usr/bin/env python3
"""P04 pre-work: find money-red candidates that clear the 4.5:1 text bar on BOTH backgrounds.

The P03 addendum left one thing open: `action.sell`/`result.loss` (which `alert.critical` shares in dark)
measures 4.43:1 on light panels and 4.38:1 on dark panels, i.e. under the body-text floor the app's own
audit now enforces. The light side had candidates; the dark side had "none found" written in a report
without a search behind it. This searches, over both directions (darker for light theme, lighter for dark),
and prints the full set of constraints each candidate would have to satisfy.

Read-only: prints a table, changes nothing.
"""
from __future__ import annotations

import colorsys
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from colour_audit_lib import contrast, delta_e, luminance  # noqa: E402

T = json.loads((ROOT / "brand/tokens.json").read_text())


def to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def from_hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def candidates(base_hex: str, direction: str) -> list[str]:
    """Keep hue/saturation, walk lightness in the given direction at ~1% steps."""
    r, g, b = from_hex(base_hex)
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    out = []
    for i in range(1, 100):
        step = i / 100.0
        nv = min(1.0, max(0.0, v + step if direction == "up" else v - step))
        nr, ng, nb = colorsys.hsv_to_rgb(h, s, nv)
        out.append(to_hex(round(nr * 255), round(ng * 255), round(nb * 255)))
        if nv in (1.0, 0.0):
            break
    return out


def dl(a, b):
    return abs(luminance(a) - luminance(b))


def worst_de(a, b):
    return min(delta_e(a, b, c) for c in ("none", "deuter", "protan"))


def main() -> int:
    for theme in ("light", "dark"):
        sem = T["semantic"][theme]
        bg, bg2 = sem["bg.base"], sem["bg.elevated"]
        sell, buy = sem["action.sell"], sem["action.buy"]
        yes, no = sem["outcome.yes"], sem["outcome.no"]
        cur = contrast(sell, bg), contrast(sell, bg2)
        direction = "down" if bg == "#ffffff" else "up"
        print(f"\n=== {theme.upper()}  (current sell {sell}: {cur[0]:.2f}:1 base / {cur[1]:.2f}:1 elevated)"
              f"  searching {direction} in lightness ===")
        print(f"{'hex':9s} {'base':>6s} {'elev':>6s} {'vsBuyΔE':>8s} {'vsBuyΔL':>8s} {'vsNo':>6s} "
              f"{'vsYes':>6s} {'vsAlertH':>9s}  status")
        found = []
        for c in candidates(sell, direction):
            cbase, celev = contrast(c, bg), contrast(c, bg2)
            if min(cbase, celev) < 4.5:
                continue
            vbuy, vbdl = worst_de(c, buy), dl(c, buy)
            vno, vyes = worst_de(c, no), worst_de(c, yes)
            vh = worst_de(c, sem["alert.high"])
            # P03's separation rule: fail if dE<10 OR (dL<0.12 AND dE<25); the buy pair is exempted by
            # shape+word per D4b, but the outcome collision is NOT exempted where they can share a row.
            sep_ok = not (vbuy < 10 or (vbdl < 0.12 and vbuy < 25))
            outc_ok = vno >= 10 and vyes >= 10
            alert_ok = vh >= 10
            status = []
            if not sep_ok:
                status.append("buy-sep")
            if not outc_ok:
                status.append("outcome-collision")
            if not alert_ok:
                status.append("alert.high-collision")
            tag = "CLEAN" if not status else "needs exemption: " + ",".join(status)
            if not found and (not status or status == ["buy-sep"]):
                found.append(c)
            print(f"{c:9s} {cbase:6.2f} {celev:6.2f} {vbuy:8.1f} {vbdl:8.3f} {vno:6.1f} {vyes:6.1f} "
                  f"{vh:9.1f}  {tag}")
        print(f"  first clean candidate: {found[0] if found else 'NONE'}")
        if found:
            print(f"  ΔL to buy at that candidate: {dl(found[0], buy):.3f}")
        else:
            print("  no candidate in this hue family clears both bars")
    print("\nConstraint source: 4.5:1 = WCAG AA body text (tokens.rules.hue-never-small-text);")
    print("separation = the adopted ΔE<10 OR (ΔL<0.12 AND ΔE<25) rule; outcome pairs are NOT layout-exempt")
    print("in PositionRow, so an outcome collision there is a real finding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
