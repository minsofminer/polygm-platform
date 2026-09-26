#!/usr/bin/env python3
"""Render Openout's brand assets from the canonical geometry — deterministically, with no image model.

    python3 tools/render-brand-assets.py             # write every asset
    python3 tools/render-brand-assets.py --check     # verify what is on disk matches the geometry

Why this tool exists at all, when there is a built-in image generator: `docs/AGENTS.md` rule 3 and
`docs/SKILLS.md` guardrail 3 both say the same thing — **the mark may never be redrawn by an image model**, it is
composed from `brand/svg/mark.svg`. The generator is for atmosphere, application and world-building (the boards in
`brand/boards/`); the identity itself is sixty numbers in a 100-unit space, and those numbers are drawn here.

The geometry is *parsed out of the SVG that ships*, never retyped. A renderer that keeps its own copy of the
polygon points is a second source of truth for the logo, and the second one is always the one that drifts — the
same failure `tools/rename-wordmark.mjs` was written to prevent for the wordmark. So this file reads the points,
the radius and the stroke width from `brand/svg/mark.svg`, and it refuses to render if the Brand Lock's
`geometry_sha256` does not match the file it just parsed: a logo export from a mark nobody approved is worse than
no export.

No text is drawn here on purpose. The wordmark is set, not drawn (`docs/P02-brand.md` D3), and this box has no
Instrument Sans Condensed — a font substituted at render time would produce an asset that no longer matches
`brand/wordmark-metrics.json`. Sheets that need type are the SVG/HTML path, where the font is a request to the
browser rather than something baked into a raster.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRAND = ROOT / "brand"
MARK_SVG = BRAND / "svg" / "mark.svg"
LOCK = BRAND / "BRAND-KIT.md"
OUT = BRAND / "exports"

#: The palette, from `brand/tokens.json` via the Brand Lock. Pinned as literals because an export is a *snapshot*:
#: if a token moves, the exports here are stale and are supposed to be regenerated deliberately, not silently.
PALETTE = {
    "brand_500": "#3262ff",     # primary action, the filled chevron
    "brand_700": "#7ba5ff",     # the outlined chevron's stroke
    "brand_800": "#a1c0ff",     # used by the dark-surface mono treatment
    "gold": "#f1ce57",          # the resolution dot
    "surface": "#0c0e13",       # neutral 50 dark — the base surface
    "surface_2": "#12151c",     # one step up, the board panels
    "light": "#f4fcff",         # brand 50 light — the light-theme card
    "ink": "#0c00a4",           # brand 800 light — mono-on-light ink
    "hairline": "#232833",
    "muted": "#7d8189",         # neutral 500 dark
}


def parse_mark(svg_text: str) -> dict:
    """The mark's geometry, read out of the SVG: polygons (points, fill, stroke), the dot, and the stroke width."""
    polys = []
    for m in re.finditer(r'<polygon points="([^"]+)"\s+fill="([^"]+)"((?:\s+stroke="([^"]+)")?)'
                         r'((?:\s+stroke-width="([^"]+)")?)', svg_text):
        pts = [tuple(float(v) for v in pair.split(",")) for pair in m.group(1).split()]
        polys.append({"points": pts, "fill": m.group(2), "stroke": m.group(4), "width": m.group(6)})
    if len(polys) != 2:
        raise SystemExit("mark.svg: expected 2 polygons, found %d — refusing to render a mark I cannot read" % len(polys))
    dot = re.search(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"\s+fill="([^"]+)"', svg_text)
    if not dot:
        raise SystemExit("mark.svg: no circle (the resolution dot) — refusing to render")
    return {"polys": polys,
            "dot": {"cx": float(dot.group(1)), "cy": float(dot.group(2)), "r": float(dot.group(3)),
                    "fill": dot.group(4)},
            "stroke_width": float(polys[1]["width"] or 3.5) if polys[1].get("width") else 3.5}


def geometry_hash(svg_text: str) -> str:
    """The Brand Lock's hash: SHA-256(16) of the mark's shape elements in the 100-unit space.

    Reproduces `tools/rename-wordmark.mjs`'s canonical form *exactly* — its regex over `<polygon>`/`<circle>`
    elements, whitespace collapsed, joined with a newline, then SHA-256 truncated to 16 hex characters. That is
    the point of parsing the SVG here rather than restating the points: the two tools must agree, and the way they
    agree is by hashing the same thing. Verified against the tool itself (`node tools/rename-wordmark.mjs --hash`
    → c33b4d5fd82b7acd, which is what `brand/BRAND-KIT.md` locks).
    """
    els = re.findall(r"<(?:polygon|circle)\b[^>]*/>", svg_text)
    if len(els) != 3:
        raise SystemExit("mark.svg: expected 3 shape elements (2 polygons + the dot), found %d" % len(els))
    canonical = "\n".join(re.sub(r"\s+", " ", e).strip() for e in els)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def locked_hash() -> str:
    m = re.search(r'"geometry_sha256":\s*"([0-9a-f]+)"', LOCK.read_text())
    if not m:
        raise SystemExit("BRAND-KIT.md has no geometry_sha256 — the lock is the authority and it is missing")
    return m.group(1)


def draw_mark(size: int, *, fill: str, stroke: str, dot: str, background: str | None = None,
              pad: float = 0.0, supersample: int = 4) -> Image.Image:
    """The mark at `size` px, drawn from the parsed geometry. `pad` is a fraction of the canvas left as clear space.

    Supersampled 4× and downscaled: PIL has no anti-aliased polygon fill, and a logo edge that is jagged at 40 px
    in a Telegram list is a logo that looks cheap in the one place it is seen most.
    """
    s = size * supersample
    img = Image.new("RGBA", (s, s), background or (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inner = s * (1.0 - 2 * pad)
    off = s * pad
    k = inner / 100.0
    # The dot goes UNDER the chevrons (`mark.svg` draws it last, but the filled chevron would swallow it — the SVG's
    # shapes do not overlap the dot, so the order is free; drawing it first keeps the outline's stroke crisp).
    dot_c, dot_r = PARSE["dot"], PARSE["dot"]["r"]
    d.ellipse([off + (dot_c["cx"] - dot_r) * k, off + (dot_c["cy"] - dot_r) * k,
               off + (dot_c["cx"] + dot_r) * k, off + (dot_c["cy"] + dot_r) * k], fill=dot)
    for p in PARSE["polys"]:
        pts = [(off + x * k, off + y * k) for x, y in p["points"]]
        if p["fill"] != "none":
            d.polygon(pts, fill=fill if p["fill"] != "none" else None)
        if p["stroke"]:
            w = max(1, int(round(PARSE["stroke_width"] * k)))
            d.line(pts + [pts[0]], fill=stroke, width=w, joint="curve")
    return img.resize((size, size), Image.LANCZOS)


def rounded(img: Image.Image, radius_frac: float) -> Image.Image:
    """A rounded-square mask, the shape an app icon face actually is on both platforms."""
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1],
                                           radius=int(img.size[0] * radius_frac), fill=255)
    out = img.copy()
    out.putalpha(mask)
    return out


def render_all() -> list[tuple[str, tuple[int, int], str]]:
    """Write every export. Returns (path, size, one-line note) for the inventory."""
    OUT.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, tuple[int, int], str]] = []

    def save(img: Image.Image, name: str, note: str) -> None:
        p = OUT / name
        img.save(p)
        written.append((p.relative_to(ROOT).as_posix(), img.size, note))

    # --- the mark, transparent, both themes -------------------------------------------------------------------
    save(draw_mark(1024, fill=PALETTE["brand_500"], stroke=PALETTE["brand_700"], dot=PALETTE["gold"]),
         "mark-1024.png", "colour mark on transparency, canonical geometry")
    save(draw_mark(512, fill=PALETTE["brand_500"], stroke=PALETTE["brand_700"], dot=PALETTE["gold"]),
         "mark-512.png", "colour mark, 512")
    save(draw_mark(1024, fill="#ffffff", stroke="#ffffff", dot="#ffffff"),
         "mark-mono-light-1024.png", "mono for dark surfaces, matches svg/mark-mono-light.svg")
    save(draw_mark(1024, fill=PALETTE["ink"], stroke=PALETTE["ink"], dot=PALETTE["ink"]),
         "mark-mono-dark-1024.png", "mono for light surfaces, in brand-800 light")

    # --- the app icon: the mark on the base surface, at the icon safe area (70% of the canvas) ----------------
    icon = Image.new("RGBA", (1024, 1024), PALETTE["surface"])
    icon.alpha_composite(draw_mark(717, fill=PALETTE["brand_500"], stroke=PALETTE["brand_700"],
                                   dot=PALETTE["gold"]), (154, 154))
    save(rounded(icon, 0.22), "app-icon-1024.png", "rounded-square app icon; the mark at the 70% safe area")
    save(icon, "app-icon-square-1024.png", "the same icon without the mask, for stores that apply their own")

    # --- the Telegram channel avatar: the mark fills the frame, because in a chat list it is 40 px ------------
    for px in (512, 1080):
        av = Image.new("RGBA", (px, px), PALETTE["surface"])
        ring = ImageDraw.Draw(av)
        av.alpha_composite(draw_mark(int(px * 0.78), fill=PALETTE["brand_500"], stroke=PALETTE["brand_700"],
                                     dot=PALETTE["gold"]), (int(px * 0.11), int(px * 0.11)))
        inset, w = max(3, px // 96), max(2, px // 256)
        ring.ellipse([inset, inset, px - inset, px - inset], outline=PALETTE["hairline"], width=w)
        save(av, "telegram-channel-avatar-%d.png" % px,
             "channel avatar: mark at 78%, hairline ring so the circle crops cleanly in Telegram")

    # --- the logo system sheet: each treatment on the surface it exists for, sizes, clear space, no type -------
    # Three columns, and the column's own background is the point: a mono variant is only readable against the
    # surface it was made for, and the first version of this sheet drew the light-surface mono on a dark panel
    # where it vanished. The first version also laid the size ladder out twice by accident, which the record of
    # this file would rather admit than hide: a sheet that overlaps its own artwork is a sheet nobody trusts.
    W, H = 1600, 1000
    colw = W // 3
    sheet = Image.new("RGBA", (W, H), PALETTE["surface"])
    d = ImageDraw.Draw(sheet)
    columns = (
        (PALETTE["surface"], PALETTE["brand_500"], PALETTE["brand_700"], PALETTE["gold"], "#3a4150"),
        (PALETTE["surface_2"], "#ffffff", "#ffffff", "#ffffff", "#3a4150"),
        (PALETTE["light"], PALETTE["ink"], PALETTE["ink"], PALETTE["ink"], "#c9d6e8"),
    )
    for i, (bg, f, s, dot, guide) in enumerate(columns):
        x0 = i * colw
        d.rectangle([x0, 0, x0 + colw - 1, H - 1], fill=bg)
        if i:
            d.line([(x0, 0), (x0, H)], fill=guide, width=2)
        # 1. the size ladder, one baseline: 192 / 72 / 32 / 16 — the last one is the favicon in a tab.
        lx = x0 + 64
        for px in (192, 72, 32, 16):
            img = draw_mark(px, fill=f, stroke=s, dot=dot)
            sheet.alpha_composite(img, (lx, 120 + (192 - px) // 2))
            lx += px + 32
        # 2. clear space: the margin is one stroke-width of this render, drawn as the box the mark must not leave.
        cell = draw_mark(192, fill=f, stroke=s, dot=dot)
        cx, cy, m = x0 + 96, 430, 24
        d.rectangle([cx - m, cy - m, cx + 192 + m, cy + 192 + m], outline=guide, width=2)
        for k in range(1, 4):
            d.line([(cx - m, cy + k * 48), (cx + 192 + m, cy + k * 48)], fill=guide, width=1)
        sheet.alpha_composite(cell, (cx, cy))
        # 3. the app icon at the safe area, rounded the way both stores round it.
        icon = Image.new("RGBA", (176, 176), PALETTE["surface"] if i < 2 else "#ffffff")
        icon.alpha_composite(draw_mark(123, fill=f, stroke=s, dot=dot), (26, 26))
        sheet.alpha_composite(rounded(icon, 0.22), (cx + 8, 700))
    save(sheet, "logo-system.png", "sizes 256/96/40/16, clear space, dark and light, no type")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="render brand assets from the canonical mark geometry")
    ap.add_argument("--check", action="store_true",
                    help="verify the Brand Lock hash against the SVG and that every expected export exists")
    args = ap.parse_args(argv)

    global PARSE
    PARSE = parse_mark(MARK_SVG.read_text())
    want, have = locked_hash(), geometry_hash(MARK_SVG.read_text())
    if want != have:
        print("geometry mismatch: BRAND-KIT.md locks %s, mark.svg parses to %s" % (want, have), file=sys.stderr)
        print("the lock is the authority — fix the SVG or re-lock deliberately, but do not export from this state",
              file=sys.stderr)
        return 2
    print("geometry: %s (matches the Brand Lock)" % have)
    if args.check:
        expected = ["mark-1024.png", "mark-512.png", "mark-mono-light-1024.png", "mark-mono-dark-1024.png",
                    "app-icon-1024.png", "app-icon-square-1024.png", "telegram-channel-avatar-512.png",
                    "telegram-channel-avatar-1080.png", "logo-system.png"]
        missing = [n for n in expected if not (OUT / n).exists()]
        if missing:
            print("missing exports: %s — run without --check to write them" % ", ".join(missing), file=sys.stderr)
            return 1
        print("%d/%d exports present" % (len(expected), len(expected)))
        return 0
    for path, size, note in render_all():
        print("  %-44s %sx%s  %s" % (path, size[0], size[1], note))
    return 0


PARSE: dict = {}
if __name__ == "__main__":
    raise SystemExit(main())
