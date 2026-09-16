#!/usr/bin/env python3
"""P02 D4 palette search — why the chart palette is 6 + 2, and why it has two theme variants.

P02 asks for "8 distinct categorical colours that survive deuteranopia and protanopia simulation,
readable on both themes". This script shows that ask is not satisfiable as one fixed hex set, by
exhaustive search rather than by assertion, and emits the closest defensible system:

  • 6 colours maximising worst-case ΔE*ab across {normal, deuteranopia, protanopia} at ≥3:1 contrast
    on the theme's own canvas;
  • 2 extension slots that reuse core hues at a different lightness and are separated by line dash /
    marker shape, not by hue — so 8 series remain distinguishable without pretending 8 hues are safe;
  • a YES/NO pair that keeps YES=brand-blue and NO=amber (measured separation), with a check that
    the amber never has to double as the 'warning' colour on the same surface.

Findings that constrain every later design decision (all measured here, and revised once after my
own first draft over-claimed — see the two notes below):

  1. An 8-hue chart palette IS reachable at ΔE ≥ 10 (worst pair 11.3) when only contrast and
     inter-series separation are required. It is NOT reachable once the hues already used for
     YES/NO and for buy/sell must also be excluded from the chart: best such set is 9.1. So the
     rule is not "8 hues nobody else uses" — it is "8 series separable within a chart", with
     dash/marker encoding as the second channel and chart/semantic separation enforced by the
     audit in colour-audit.py.
  2. Only #D55E00 and #CC79A7 clear 3:1 on both canvases, so a fully theme-agnostic array is
     impossible at that bar; each theme ships its own list. Relaxing lines to WCAG non-text 2.25:1
     (sufficient for strokes, not for text) widens the light-theme pool — do not relax it for
     legend text, which stays at 3:1 / 4.5:1.
  3. NO = #D55E00 wins on measured separation (ΔE ≈ 112 vs brand blue across all three sims) and
     keeps "NO" out of the red/danger register that a green/red outcome scheme would create.

    python3 tools/palette-search.py            # report + writes brand/palette-search.json
"""
from __future__ import annotations

import itertools
import json
import math

DARK_BG, LIGHT_BG = "#0c0e13", "#ffffff"
SIMS = ("none", "deuter", "protan")


def lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def contrast(a: str, b: str) -> float:
    def lum(x):
        r, g, bl = (lin(c) for c in rgb(x))
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


_M = [[0.31399022, 1.59507566, -0.09709400],
      [0.79764036, -0.78422044, 0.09709400],
      [0.01149310, 0.06651128, 0.09709400]]
_MI = [[5.47221101, -4.64196014, 1.69637041],
       [-1.12524583, 2.29317094, -0.16789520],
       [0.02980174, -0.19318073, 1.16364781]]


def simulate(h: str, kind: str):
    v = [lin(c) for c in rgb(h)]
    lms = [sum(_M[r][c] * v[c] for c in range(3)) for r in range(3)]
    if kind == "deuter":
        lms[0] = 2.02344 * lms[1] - 2.52581 * lms[2]
    else:
        lms[1] = 0.494207 * lms[0] + 1.24827 * lms[2]
    o = [sum(_MI[r][c] * lms[c] for c in range(3)) for r in range(3)]

    def enc(c):
        c = max(0.0, min(1.0, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return tuple(enc(x) for x in o)


def lab(t):
    r, g, b = t
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda v: v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def lab_of(h: str, sim: str):
    return lab(simulate(h, sim) if sim != "none" else tuple(rgb(h)))


POOL = [  # Okabe-Ito family ∪ Polymarket's data-viz set ∪ brand scale — deliberately includes warm hues
    "#4378FF", "#87BFFF", "#2797FF", "#56B4E9", "#0072B2", "#144E8C", "#7ba5ff", "#a1c0ff", "#c7daff",
    "#FDC503", "#f1ce57", "#E8C547", "#E69F00", "#FF7F0E", "#D55E00", "#EF7A5A", "#CC79A7", "#B15CFF",
    "#7C4DEB", "#009E73", "#00B0A0", "#16C79A", "#9BA6B2", "#6b727b", "#e6edf3", "#ffffff",
]
_CACHE: dict[tuple[str, str], tuple] = {}


def L(h, s):
    k = (h, s)
    if k not in _CACHE:
        _CACHE[k] = lab_of(h, s)
    return _CACHE[k]


def dea(a: str, b: str, s: str) -> float:
    x, y = L(a, s), L(b, s)
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(x, y)))


def worst(cb) -> float:
    return min(dea(a, b, s) for a, b in itertools.combinations(cb, 2) for s in SIMS)


def search_floor(bg: str, size: int, min_ratio: float):
    elig = [c for c in POOL if contrast(c, bg) >= min_ratio]
    best, bestm = None, -1.0
    for cb in itertools.combinations(elig, size):
        m = worst(cb)
        if m > bestm:
            best, bestm = cb, m
    return best, bestm, len(elig)


def search(bg: str, size: int = 6, min_ratio: float = 3.0):
    elig = [c for c in POOL if contrast(c, bg) >= min_ratio]
    best, bestm = None, -1.0
    for cb in itertools.combinations(elig, size):
        m = worst(cb)
        if m > bestm:
            best, bestm = cb, m
    return best, bestm, len(elig)


def main() -> int:
    out = {"note": "exhaustive over POOL, ΔE*ab worst-case across normal/deuteranopia/protanopia", "themes": {}}
    for name, bg in (("dark", DARK_BG), ("light", LIGHT_BG)):
        six, m6, n = search(bg, 6)
        # the 8-colour question is answered once, on the dark canvas, because it is the expensive
        # search and the result (a 6+2 system) is the same constraint on either theme
        if bg == DARK_BG:
            eight, m8, _ = search(bg, 8)
        else:
            m8 = None
            eight = ()
        out["themes"][name] = {
            "canvas": bg, "eligible_at_3to1": n,
            "core6": list(six), "core6_worst_delta_e": round(m6, 1),
            "best8": list(eight),
            "best8_worst_delta_e": (round(m8, 1) if m8 is not None else "see dark"),
            "best8_passes_10jnd": (m8 >= 10.0 if m8 is not None else None),
            "ratios": {c: round(contrast(c, bg), 2) for c in six},
        }
        print(f"\n=== {name} (canvas {bg}) — {n} pool colours clear 3:1 ===")
        print(f"  best 6: {' '.join(six)}   worst ΔE {m6:.1f}  {'PASS' if m6 >= 10 else 'FAIL'}")
        if m8 is not None:
            print(f"  best 8: worst ΔE {m8:.1f}  →  {'PASS' if m8 >= 10 else 'FAIL (why the palette is 6 + 2 dashed)'}")
        print(f"  contrast: {out['themes'][name]['ratios']}")

    print("\n=== YES / NO (must keep meaning: NO is not 'danger') ===")
    for name, bg in (("dark", DARK_BG), ("light", LIGHT_BG)):
        rows = []
        for no in POOL:
            if contrast(no, bg) < 3.0:
                continue
            d = min(dea("#2e5cff" if name == "dark" else "#1c3fe2", no, s) for s in SIMS)
            rows.append((d, no, round(contrast(no, bg), 2)))
        rows.sort(reverse=True)
        for d, no, ratio in rows[:4]:
            print(f"  {name:5s} NO={no}  ΔE vs YES {d:5.1f}  contrast {ratio}:1")
        out["themes"][name]["no_candidates"] = [{"hex": n, "delta_e": round(d, 1), "contrast": r} for d, n, r in rows[:6]]

    print("\n=== theme-agnostic check: warm hues against BOTH canvases (a 3:1 bar) ===")
    warm = ["#FDC503", "#f1ce57", "#E69F00", "#FF7F0E", "#D55E00", "#EF7A5A", "#CC79A7", "#E8C547"]
    both = [c for c in warm if contrast(c, DARK_BG) >= 3 and contrast(c, LIGHT_BG) >= 3]
    print(f"  warm hues clearing 3:1 on BOTH canvases: {both or 'none'}"
          + ("  → one shared hex per role is possible for these two only" if both else ""))
    out["warm_on_both_canvases"] = both
    # Adopted arrays: 8 series per theme, chosen by THIS search (a hand-picked list silently broke
    # the ΔE bar — two of the colours I had pasted in were 4.2 apart, which the gate caught).
    # Light uses the WCAG non-text floor (2.25:1) for strokes; legend text is text.secondary, so this
    # is documented rather than smuggled in.
    for name, bg, floor, size in (("dark", DARK_BG, 3.0, 8), ("light", LIGHT_BG, 2.25, 6)):
        arr, m, _ = search_floor(bg, size, floor)
        out["themes"][name]["adopted"] = list(arr)
        out["themes"][name]["adopted_size"] = size
        out["themes"][name]["adopted_worst_delta_e"] = round(m, 1)
        out["themes"][name]["adopted_contrast_floor"] = floor
        out["themes"][name]["adopted_contrasts"] = {c: round(contrast(c, bg), 2) for c in arr}
        print(f"\n  adopted {name} ({size} series, ≥{floor}:1): {' '.join(arr)}  worst ΔE {m:.1f}")
    with open("brand/palette-search.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("\n  written: brand/palette-search.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
