"""Shared colour maths for PolyGM's design gates. ONE implementation, imported by:
tools/colour-audit.py, tools/build-contrast-table.py, tools/p02-gate-check.py, tools/palette-search.py.

Rationale: the P02 doc table and the gate initially carried two separate copies of the WCAG formula
with different rounding, and disagreed on dark `text.primary` (16.34 vs 13.94). Anything two pieces
of code can disagree about should live in exactly one place.
"""
from __future__ import annotations
import math

_M = [[0.31399022, 1.59507566, -0.09709400],
      [0.79764036, -0.78422044, 0.09709400],
      [0.01149310, 0.06651128, 0.09709400]]
_MI = [[5.47221101, -4.64196014, 1.69637041],
       [-1.12524583, 2.29317094, -0.16789520],
       [0.02980174, -0.19318073, 1.16364781]]


def to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(h: str) -> float:
    r, g, b = (lin(c) for c in to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def simulate(h: str, kind: str):
    """Brettel-Viénot-Mollon dichromatic appearance, in sRGB 0..1."""
    v = [lin(c) for c in to_rgb(h)]
    lms = [sum(_M[r][c] * v[c] for c in range(3)) for r in range(3)]
    if kind == "deuter":
        lms[0] = 2.02344 * lms[1] - 2.52581 * lms[2]
    elif kind == "protan":
        lms[1] = 0.494207 * lms[0] + 1.24827 * lms[2]
    o = [sum(_MI[r][c] * lms[c] for c in range(3)) for r in range(3)]

    def enc(c: float) -> float:
        c = max(0.0, min(1.0, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return tuple(enc(x) for x in o)


def lab(t) -> tuple[float, float, float]:
    r, g, b = t
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda v: v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str, cond: str = "none") -> float:
    one = (lambda h: simulate(h, cond)) if cond != "none" else to_rgb
    x, y = lab(one(a)), lab(one(b))
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(x, y)))


SIMS = ("none", "deuter", "protan")


def worst_separation(colors) -> float:
    return min(delta_e(a, b, c) for i, a in enumerate(colors) for b in colors[i + 1:] for c in SIMS)
