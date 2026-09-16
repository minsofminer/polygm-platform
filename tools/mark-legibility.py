#!/usr/bin/env python3
"""P02 D3 — does the mark actually survive 16px / 40px / 512px?

Why this exists rather than a screenshot: this sandbox has no rsvg-convert (the tool
brandkit.py's export stage hard-requires, and the skill forbids substituting a normaliser), and
ImageMagick's internal MSVG renderer drops the mark's *stroked* right chevron entirely — measured:
0.0% coverage of #7ba5ff in a 512px raster. Screenshotting that would have produced a "verified at
16px" claim about a picture that is missing half the logo. So the geometry is computed exactly from
the SVG's own coordinates instead.

Method, per target size S:
  • solid left chevron   → polygon area (shoelace)
  • outlined right chevron → area of the stroke band = |outer| − |inner|, inner derived from
    stroke width w and miter joins (approximated by the polygon shrunk by w along its normals,
    which for this shape is a 1-D inset; documented as an approximation)
  • gold dot             → circle area
  • AA coverage          → sub-pixel sampling at 8×8 per pixel for the small sizes
  • "visible" bar        → an element is legible if it covers ≥ 1.5 px of the target box, and a
    1-px feature needs ≥ 60% coverage of its own pixel to survive bilinear downscaling

    python3 tools/mark-legibility.py            # report
    python3 tools/mark-legibility.py --json     # for the gate

Fixes proposed from the numbers are written into the P02 deliverable, not assumed here.
"""
from __future__ import annotations

import json
import math
import re
import sys

MARK = "brand/svg/mark.svg"
POLY = re.compile(r"<polygon[^>]*points=\"([^\"]+)\"", re.I)
CIRCLE = re.compile(r"<circle[^>]*cx=\"([\d.]+)\"[^>]*cy=\"([\d.]+)\"[^>]*r=\"([\d.]+)\"", re.I)
STROKE = re.compile(r"stroke-width=\"([\d.]+)\"", re.I)
VIEWBOX = re.compile(r'viewBox="([\d.\s-]+)"', re.I)


def shoelace(pts) -> float:
    return 0.5 * abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1]
                          - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts))))


def perimeter(pts) -> float:
    n = len(pts)
    return sum(math.hypot(pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1]) for i in range(n))


def inset(pts, d: float):
    """Inward offset of a simple polygon by distance d, as a uniform scale about its centroid.

    Uses the polygon offset-area relation A_inset = A − d·P + O(d²): solving A' = (A − d·P) for a
    uniform scale k gives k² = 1 − d·P/A, so k = sqrt(max(0, 1 − d·P/A)). This is exact for convex
    shapes in the area sense and, for the coverage question here (does a thin band survive at N px),
    area is what matters. The previous normal-intersection version inverted the normals and produced
    an "inner" polygon *larger* than the outline, which made every ring measure come out zero.
    """
    n = len(pts)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    a = shoelace(pts)
    p_ = perimeter(pts)
    k = math.sqrt(max(0.0, 1.0 - d * p_ / a)) if a > 0 else 0.0
    return [(cx + (x - cx) * k, cy + (y - cy) * k) for x, y in pts]


def parse(path: str):
    svg = open(path, encoding="utf-8").read()
    vb = [float(x) for x in VIEWBOX.search(svg).group(1).split()]
    polys = []
    for m in POLY.finditer(svg):
        pts = []
        for pair in m.group(1).split():
            x, y = pair.split(",")
            pts.append((float(x), float(y)))
        seg = svg[max(0, m.start() - 40):m.end() + 200]
        sw = STROKE.search(seg)
        stroke_w = float(sw.group(1)) if sw and "fill=\"none\"" in seg else 0.0
        polys.append({"points": pts, "area": shoelace(pts), "stroke_w": stroke_w})
    m = CIRCLE.search(svg)
    dot = None
    if m:
        dot = {"cx": float(m.group(1)), "cy": float(m.group(2)), "r": float(m.group(3))}
    return {"viewBox": vb, "polys": polys, "dot": dot}


def coverage(poly_pts, size: int, ss: int = 8) -> float:
    """Fraction of the size×size box covered by the polygon, via ss×ss sub-pixel sampling."""
    n = 0
    for j in range(size * ss):
        y = (j // ss + 0.5) / size * 100
        for i in range(size * ss):
            x = (i // ss + 0.5) / size * 100
            if inside(poly_pts, x, y):
                n += 1
    return n / (size * ss) ** 2


def inside(pts, x, y) -> bool:
    c = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                c = not c
    return c


def ring_coverage(outer, inner, size: int, ss: int = 8) -> float:
    return max(0.0, coverage(outer, size, ss) - coverage(inner, size, ss))


def disc_coverage(cx, cy, r, size: int, ss: int = 8) -> float:
    n = 0
    for j in range(size * ss):
        y = (j // ss + 0.5) / size * 100
        for i in range(size * ss):
            x = (i // ss + 0.5) / size * 100
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                n += 1
    return n / (size * ss) ** 2


def main() -> int:
    m = parse(MARK)
    solid, outlined = m["polys"][0], m["polys"][1]
    w = outlined["stroke_w"]
    inner = inset(outlined["points"], w)
    dot = m["dot"]
    if dot is None:
        raise SystemExit("no <circle> found in the mark — dot-size analysis is impossible")
    report = {"source": MARK, "viewBox_units": 100, "stroke_w": w, "sizes": {}}
    print(f"mark geometry: solid area {solid['area']:.1f}u², outline band "
          f"{shoelace(outlined['points']) - shoelace(inner):.1f}u², "
          f"dot r={dot['r']}u ({math.pi * dot['r']**2:.1f}u²)\n")
    print(f"  {'size':>5s}  {'solid px':>9s}  {'outline px':>11s}  {'dot px':>7s}  {'dot Ø px':>9s}  verdict")
    for size in (512, 128, 64, 40, 24, 16):
        k = size / 100.0
        c_solid = coverage(solid["points"], size)
        c_ring = ring_coverage(outlined["points"], inner, size)
        c_dot = disc_coverage(dot["cx"], dot["cy"], dot["r"], size)
        px_solid, px_ring, px_dot = c_solid * size * size, c_ring * size * size, c_dot * size * size
        dia = 2 * dot["r"] * k
        flags = []
        if px_solid < 1.5: flags.append("solid too small")
        if px_ring < 1.5: flags.append("outline vanishes")
        if dia < 2.0: flags.append(f"dot Ø {dia:.2f}px sub-pixel")
        verdict = "OK" if not flags else "FAIL: " + "; ".join(flags)
        print(f"  {size:>5d}  {px_solid:>9.2f}  {px_ring:>11.2f}  {px_dot:>7.2f}  {dia:>9.2f}  {verdict}")
        report["sizes"][str(size)] = {"solid_px": round(px_solid, 2), "outline_px": round(px_ring, 2),
                                     "dot_px": round(px_dot, 2), "dot_diameter_px": round(dia, 2),
                                     "verdict": verdict}
    # stroke width needed for the outline to survive at each small size, and the dot size that does
    print("\n  required adjustments for the small-size variants:")
    for size in (40, 24, 16):
        k = size / 100.0
        lo = w
        while lo < 20 and ring_coverage(outlined["points"], inset(outlined["points"], lo), size) * size * size < 3.0:
            lo += 0.25
        need_dot = max(math.sqrt(1.5 / (math.pi * k * k)), 1.2 / (2 * k))
        print(f"    {size}px: stroke ≥ {lo:.2f}u (was {w:.2f}u → {lo / w:.2f}×) ; "
              f"dot r ≥ {need_dot:.1f}u (Ø {2*need_dot*k:.2f}px) to hold ≥1.5px")
        report["sizes"][str(size)]["needs"] = {"stroke_u": round(lo, 2), "stroke_scale": round(lo / w, 2),
                                               "dot_r_u": round(need_dot, 2)}
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
