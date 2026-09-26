#!/usr/bin/env python3
"""Composite the canonical mark onto a generated board — the one permitted way to put the logo on artwork.

    python3 tools/compose-brand-board.py --in brand/boards/board-02-channel-world.png \
        --spec tools/brand-board-specs/board-02.json
    python3 tools/compose-brand-board.py --check        # verify every composed board is newer than its spec + mark

Why this exists, in the words of the repo's own rules: `docs/AGENTS.md` rule 3 — *never redraw the mark with an
image model, compose from `brand/svg/mark.svg`* — and `docs/SKILLS.md` guardrail 3, which says the same and adds
that the generator is for application and atmosphere. The built-in generator is very good at atmosphere and it
**will** draw a logo if a prompt mentions one, faithfully enough to look right in a thumbnail and wrong at 512 px:
its chevrons come out rounded, its resolution dot drifts off-centre, and the outlined chevron comes back filled.
That is not a style opinion, it is what the P16 launch boards came back with, and it is exactly why the mark is
composited here from the parsed geometry instead.

So the pipeline for any board is: **prompt without the mark** (atmosphere, layout, surfaces) → **clear the region**
the board reserves for identity → **paste the canonical render**. The clearing is a flat fill of the surface the
panel already is: a patch drawn over generated artwork is only honest when the region is a flat colour, which is
why each spec names the surface hex and the tool refuses if the region it is about to flatten is not actually flat.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from importlib.util import module_from_spec, spec_from_file_location

ROOT = pathlib.Path(__file__).resolve().parents[1]
MARK_SVG = ROOT / "brand" / "svg" / "mark.svg"
SPECS = ROOT / "tools" / "brand-board-specs"


def load_renderer():
    """The renderer, imported rather than re-implemented: one source of geometry, one source of drawing."""
    spec = spec_from_file_location("render_brand_assets", ROOT / "tools" / "render-brand-assets.py")
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PARSE = mod.parse_mark(MARK_SVG.read_text())
    return mod


def ring_stats(img: Image.Image, box: tuple[int, int, int, int], band: int = 24) -> tuple[int, tuple[int, int, int]]:
    """What the surface *around* a region looks like: its colour spread and its mean.

    The ring is the right thing to measure, and the first version of this tool measured the wrong thing — the
    inside of the box — which refused every placement it was given, including the good ones, because the inside of
    the box is where the generator's wrong logo already is. What decides whether a patch is honest is the surface
    the patch edge will touch.
    """
    x0, y0, x1, y1 = box
    pts = []
    for x in range(x0, x1, 3):
        for y in (y0 - band, y0 - band // 2, y1 + band // 2, y1 + band):
            if 0 <= y < img.size[1] and 0 <= x < img.size[0]:
                pts.append(img.getpixel((x, y)))
    for y in range(y0, y1, 3):
        for x in (x0 - band, x0 - band // 2, x1 + band // 2, x1 + band):
            if 0 <= x < img.size[0] and 0 <= y < img.size[1]:
                pts.append(img.getpixel((x, y)))
    if not pts:
        return 999, (0, 0, 0)
    g = [p[1] for p in pts]
    mean = tuple(int(statistics.mean(p[i] for p in pts)) for i in range(3))
    return max(g) - min(g), mean


def dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _homography(src_pts, dst_pts):
    """The 3x3 matrix taking `src_pts` to `dst_pts` (four correspondences, no three collinear)."""
    import numpy as np
    A, b = [], []
    for (u, v), (x, y) in zip(src_pts, dst_pts):
        A.append([u, v, 1, 0, 0, 0, -u * x, -v * x]); b.append(x)
        A.append([0, 0, 0, u, v, 1, -u * y, -v * y]); b.append(y)
    h = np.linalg.solve(np.array(A, float), np.array(b, float))
    return np.array([[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]])


def warp_quad(layer: Image.Image, quad: list) -> Image.Image:
    """Draw `layer` into the quadrilateral `quad` of an RGBA canvas: the photography-of-a-flat-object case.

    The first version of this solved the coefficients with a least-squares call whose sign convention I got
    backwards, and the result was an empty canvas — the failure mode of a perspective transform is silence, not
    an exception, which is why `compose()` now checks the warped layer's alpha box before pasting it. This version
    builds the source→destination homography from the four correspondences, inverts the *matrix* (not the problem),
    and hands PIL the eight coefficients in its own convention:
        x_src = (a·x + b·y + c) / (g·x + h·y + 1),   y_src = (d·x + e·y + f) / (g·x + h·y + 1)
    """
    import numpy as np
    w, h = layer.size
    src_rect = [(0, 0), (w, 0), (w, h), (0, h)]
    xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
    pad = 4
    ox, oy = max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad)
    size = (int(max(xs)) - ox + pad + 1, int(max(ys)) - oy + pad + 1)
    # The quad is expressed in the PATCH's own coordinates, because PIL's perspective transform samples the layer
    # in the coordinate space of the image it is producing. The first attempt handed it board coordinates while
    # asking for a patch-sized canvas, so every sample landed outside the layer and the transform returned
    # transparent pixels — an empty result with no error, which is why the caller now checks the alpha box.
    local = [(float(px) - ox, float(py) - oy) for px, py in quad]
    H = _homography(src_rect, local)
    Hinv = np.linalg.inv(H)
    c = (Hinv / Hinv[2, 2]).flatten()[:8].tolist()
    out = layer.transform(size, Image.PERSPECTIVE, c, resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))
    out.info["paste_at"] = (ox, oy)
    return out


def circle_ring_stats(img: Image.Image, cx: int, cy: int, r: int, band: int) -> tuple[int, tuple[int, int, int]]:
    """The surface just outside a disc: sampled as a ring, because that is the shape the patch edge is."""
    import math
    pts = []
    for deg in range(0, 360, 3):
        a = math.radians(deg)
        for rr in (r + band // 2, r + band):
            x, y = int(cx + rr * math.cos(a)), int(cy + rr * math.sin(a))
            if 0 <= x < img.size[0] and 0 <= y < img.size[1]:
                pts.append(img.getpixel((x, y)))
    if not pts:
        return 999, (0, 0, 0)
    g = [p[1] for p in pts]
    mean = tuple(int(statistics.mean(p[k] for p in pts)) for k in range(3))
    return max(g) - min(g), mean


def compose(board: pathlib.Path, spec: dict, out: pathlib.Path, renderer, spec_path=None) -> list[str]:
    img = Image.open(board).convert("RGBA")
    notes = []
    for i, place in enumerate(spec["placements"]):
        # A quad placement has no centre or pixel size: the object's own corners say where it goes, and the mark's
        # size is a fraction of the object's height. Everything else states a centre and a size.
        cx, cy, size = place.get("cx", 0), place.get("cy", 0), place.get("size", 0)
        d = ImageDraw.Draw(img)
        # 1. the region the generator drew its own logo into: cleared to the surface that surrounds it, after
        #    checking that the surrounding surface is actually flat and actually that colour.
        if place.get("clear_shape") == "circle":
            # A circular region cleared *as a circle*: inside a round tile, a rectangular patch is visible at its
            # corners however flat the surface is, and the tile's own ring (drawn at a fixed radius) is the edge the
            # patch must stay inside. So the region is a disc and the flatness sample is a ring at r+band.
            cx0, cy0, r = place["clear_circle"]
            spread, mean = circle_ring_stats(img, cx0, cy0, r, place.get("band", 12))
            want = place.get("background")
            if spread > place.get("ring_spread_max", 24):
                notes.append("placement %d: the disc's surrounding ring is NOT flat (spread %d) — a disc patch "
                             "would still be visible; regenerate the board without the mark" % (i, spread))
                continue
            if want and any(abs(int(want.lstrip("#")[kk:kk + 2], 16) - mean[kk // 2]) > 12 for kk in (0, 2, 4)):
                notes.append("placement %d: the spec says clear to %s but the surface is rgb%s" % (i, want, mean))
                continue
            d.ellipse([cx0 - r, cy0 - r, cx0 + r, cy0 + r], fill=place.get("background") or "#%02x%02x%02x" % mean)
            notes.append("placement %d: cleared a disc of radius %d at (%d,%d) to %s (ring spread %d, mean rgb%s)"
                         % (i, r, cx0, cy0, place.get("background") or "#%02x%02x%02x" % mean, spread, mean))
        elif place.get("clear"):
            cb = tuple(place["clear"])
            spread, mean = ring_stats(img, cb, place.get("band", 24))
            want = place.get("background")
            if spread > place.get("ring_spread_max", 24):
                notes.append("placement %d: the surface around the region is NOT flat (spread %d) — clearing it "
                             "would leave a visible rectangle; regenerate the board without the mark" % (i, spread))
                continue
            if want and any(abs(int(want.lstrip("#")[k:k + 2], 16) - mean[k // 2]) > 12 for k in (0, 2, 4)):
                notes.append("placement %d: the spec says clear to %s but the surrounding surface is rgb%s — "
                             "refusing to paint a colour that is not there" % (i, want, mean))
                continue
            d.rectangle(cb, fill=place.get("background") or ("#%02x%02x%02x" % mean))
            notes.append("placement %d: cleared %dx%d region to %s (ring spread %d, ring mean rgb%s)"
                         % (i, cb[2] - cb[0], cb[3] - cb[1], place.get("background") or "#%02x%02x%02x" % mean,
                            spread, mean))
        box = (cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2)
        # 2. the mark itself, at the canonical geometry
        variants = {"colour": (renderer.PALETTE["brand_500"], renderer.PALETTE["brand_700"], renderer.PALETTE["gold"]),
                    "mono_light": ("#ffffff", "#ffffff", "#ffffff"),
                    "mono_dark": (renderer.PALETTE["ink"], renderer.PALETTE["ink"], renderer.PALETTE["ink"])}
        fill, stroke, dot = variants[place.get("variant", "colour")]
        # The mark is drawn at the size the PLACEMENT needs, not at `place["size"]` — a quad placement has no pixel
        # size at all (its size is a fraction of the object), and drawing a 0x0 mark and then resizing it produced
        # an empty layer that the alpha check below caught. One size, computed once, used by every branch.
        draw_px = size
        if place.get("quad") and place.get("quad_mode") == "fit":
            q0 = place["quad"]
            draw_px = max(64, int((dist(q0[0], q0[3]) + dist(q0[1], q0[2])) / 2 * place.get("size_frac", 0.24)))
        mark = renderer.draw_mark(draw_px, fill=fill, stroke=stroke, dot=dot)
        if place.get("mask") == "rounded":
            mark = renderer.rounded(mark, 0.22)
        # Depth of field, matched by hand: an object in the background is soft, and a pin-sharp mark composited
        # onto it reads as a sticker floating in front of the photograph rather than print on the object.
        if place.get("blur"):
            from PIL import ImageFilter
            mark = mark.filter(ImageFilter.GaussianBlur(place["blur"]))
        # Ink does not sit at 100% opacity on a textured matte surface, and a mark that does looks pasted on. The
        # spec states the ink's opacity per object (print on metal is weaker than print on paper, and paint on a
        # ceramic mug weaker still when the light is raking): the alpha channel is scaled, alpha only, never colour.
        if place.get("opacity"):
            a = mark.getchannel("A").point(lambda v: int(v * place["opacity"]))
            mark.putalpha(a)
        if place.get("quad") and place.get("quad_mode") == "fit":
            # A flat object photographed at an angle: its surface is a quadrilateral, so the mark is warped into
            # it. The scale is a fraction of the quad's own mean height — a card's logo is a print size relative to
            # the card, not a number of pixels, so the spec states the ratio and this computes the pixels.
            q = place["quad"]
            w = draw_px
            fh = round(w * ((dist(q[1], q[2]) + dist(q[3], q[0])) / 2) /
                       max(1e-6, (dist(q[0], q[1]) + dist(q[2], q[3])) / 2))   # horizontal foreshortening
            quad_mark = warp_quad(mark.resize((fh, w)), q)
            if quad_mark.getbbox() is None:
                notes.append("placement %d: the warp produced NOTHING (empty alpha) — refusing to pretend it "
                             "worked" % i)
                continue
            img.alpha_composite(quad_mark, quad_mark.info.get("paste_at", (0, 0)))
            notes.append("placement %d: warped the canonical %s mark (%dx%d) into the object's quad %s; painted "
                         "%d px" % (i, place.get("variant", "colour"), fh, w, q,
                                    sum(1 for a in quad_mark.getchannel("A").convert("L").point(lambda v: 255 if v > 8 else 0).histogram()[255:])))
        elif place.get("ellipse"):
            # A circle seen at an angle, on a tilted sheet: squash, rotate to the sheet's tilt, paste.
            e = place["ellipse"]
            m = mark.resize((int(size), max(1, int(size * e["ry"] / e["rx"])),), Image.LANCZOS)
            m = m.rotate(e.get("rotate_deg", 0), expand=True, resample=Image.BICUBIC)
            img.alpha_composite(m, (cx - m.size[0] // 2, cy - m.size[1] // 2))
            notes.append("placement %d: composited the canonical %s mark squashed to %d:%d and rotated %.1f deg "
                         "onto the sticker at (%d,%d)" % (i, place.get("variant", "colour"), e["rx"], e["ry"],
                                                          e.get("rotate_deg", 0), cx, cy))
        else:
            img.alpha_composite(mark, (cx - size // 2, cy - size // 2))
            notes.append("placement %d: composited the canonical %s mark at %dpx, centre (%d,%d)"
                         % (i, place.get("variant", "colour"), size, cx, cy))
    img.convert("RGB").save(out)
    # Guardrail 3 says the mark is never redrawn: a board is canonical only if it was made from the geometry this
    # repo holds NOW. The renderer already refuses to export when mark.svg disagrees with the hash locked in
    # BRAND-KIT.md, so recording that same hash next to each board means a later `--check` fails every board the
    # moment the mark changes — the alternative is a folder of images that LOOK right and carry a mark from three
    # edits ago, which is exactly the drift the guardrail exists to prevent.
    ledger_path = out.parent / "COMPOSITES.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"boards": {}}
    ledger["mark_geometry_sha256"] = renderer.geometry_hash(MARK_SVG.read_text())
    ledger.setdefault("boards", {})[out.name] = {
        "base": spec.get("board"),
        "spec": (pathlib.Path(spec_path).name if spec_path else None),
        "placements": len(spec["placements"]),
        "variants": sorted({pl.get("variant", "colour") for pl in spec["placements"]}),
    }
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    return notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="composite the canonical mark onto a generated board")
    ap.add_argument("--in", dest="src", required=False)
    ap.add_argument("--spec", required=False)
    ap.add_argument("--out", required=False)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    renderer = load_renderer()

    if args.check:
        bad = 0
        for spec_path in sorted(SPECS.glob("*.json")):
            spec = json.loads(spec_path.read_text())
            board = ROOT / spec["board"]
            out = ROOT / (spec.get("out") or spec["board"].replace(".png", "-composed.png"))
            # A spec may only build on a base that was generated WITHOUT an identity mark. Every first-generation
            # board here contained the generator's own near-miss logo, and compositing onto one of those leaves the
            # original showing around the edges — the rule is cheap to state and it is the one that failed twice.
            if "-raw" not in board.stem:
                print("BASE    %s builds on %s, which is not a mark-free -raw base" % (spec_path.name, board.name))
                bad += 1
            newest_input = max(spec_path.stat().st_mtime, MARK_SVG.stat().st_mtime)
            if not out.exists():
                print("MISSING %s (spec %s)" % (out.relative_to(ROOT), spec_path.name)); bad += 1
            elif out.stat().st_mtime < newest_input:
                print("STALE   %s is older than its spec or the mark" % out.relative_to(ROOT)); bad += 1
            else:
                print("ok      %s" % out.relative_to(ROOT))
        # The provenance check: every composed board must carry the mark the repo holds now, not the mark it held
        # when the board was made. This is the half a timestamp cannot see.
        ledger_path = SPECS.parent.parent / "brand" / "boards" / "COMPOSITES.json"
        want = renderer.geometry_hash(MARK_SVG.read_text())
        if not ledger_path.exists():
            print("MISSING %s (no board is provably canonical)" % ledger_path.name); bad += 1
        else:
            ledger = json.loads(ledger_path.read_text())
            have = ledger.get("mark_geometry_sha256")
            if have != want:
                print("DRIFT   boards were composed from mark geometry %s; the repo holds %s"
                      % (have, want)); bad += 1
            else:
                print("ok      COMPOSITES.json: %d board(s) carry mark geometry %s"
                      % (len(ledger.get("boards", {})), want))
        print("%d problem(s)" % bad)
        return 1 if bad else 0

    if not (args.src and args.spec):
        ap.error("--in and --spec are required unless --check")
    spec = json.loads(pathlib.Path(args.spec).read_text())
    out = pathlib.Path(args.out) if args.out else ROOT / (spec.get("out") or args.src.replace(".png", "-composed.png"))
    for line in compose(pathlib.Path(args.src), spec, out, renderer):
        print("  " + line)
    print("wrote %s" % out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
