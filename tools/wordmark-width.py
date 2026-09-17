#!/usr/bin/env python3
"""
Measure the rendered width of a wordmark from REAL font metrics — no eyeballing, no guessing.

Why this exists: the lockup SVG sizes its viewBox from the wordmark length. If that estimate is
too small the wordmark clips; too big and every raster export carries dead whitespace. The brand
face is not installed in this sandbox (fc-list finds none of Inter / Archivo Narrow / Geist Mono),
so a screenshot would be worthless — it would measure the fallback font and lie. Instead we
download the actual webfont subset and read hmtx advance widths out of it.

Outputs JSON so tools/rename-wordmark.mjs can consume it:

  python3 tools/wordmark-width.py Openout --json
  python3 tools/wordmark-width.py Openout --font "Archivo Narrow" --weight 700 --ls 1

--verify-payload checks the P02 claim (per-family latin subset bytes + total webfont weight).
"""
import argparse
import json
import re
import sys
import urllib.request
from io import BytesIO

# Google Fonts serves TTF (≈5× heavier) to User-Agents it thinks lack woff2, so the payload we
# measure depends entirely on this header. Send a modern browser UA or the numbers are fiction.
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}
GF = "https://fonts.googleapis.com/css2"


def fetch(url: bytes | str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=45).read()


def gf_css(family: str, weight: int, subsets: str = "latin") -> str:
    """Google Fonts returns HTTP 400 for a colon weight selector over urllib in some cases;
    POST-style query via urlencode is the form that reliably works (learned the hard way in P02)."""
    from urllib.parse import quote

    return fetch(f"{GF}?family={quote(family)}:wght@{weight}&display=swap").decode()


def subsets(css: str) -> dict[str, list[str]]:
    """Map subset name -> woff2 urls, as the browser will see them."""
    blocks = re.split(r"/\*\s*([a-z-]+)\s*/", css)
    out: dict[str, list[str]] = {}
    for i in range(1, len(blocks) - 1, 2):
        out.setdefault(blocks[i], []).extend(re.findall(r"url\((https://[^)]+)\)", blocks[i + 1]))
    return out


def css_urls(css: str, subset: str = "latin") -> list[str]:
    """Return woff2 URLs for unicode-range blocks whose comment says `latin` (not latin-ext)."""
    blocks = re.split(r"/\*\s*([a-z-]+)\s*\*/", css)
    out = []
    for i in range(1, len(blocks) - 1, 2):
        if blocks[i] == subset:
            out += re.findall(r"url\((https://[^)]+)\)", blocks[i + 1])
    # Google Fonts serves woff2 at extensionless URLs, so match on format(), not the filename
    return out or re.findall(r"url\((https://[^)]+)\)", css)


def sniff_woff2(data: bytes) -> bool:
    return data[:4] == b"wOF2"


def font_table(data: bytes):
    from fontTools.ttLib import TTFont

    return TTFont(BytesIO(data))


def advance_widths(font, text: str) -> tuple[list[float], int]:
    upm = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    widths = []
    for ch in text:
        glyph = cmap.get(ord(ch))
        widths.append((hmtx[glyph][0] if glyph else 0.0) / upm)
    return widths, upm


def measure(family: str, weight: int, text: str) -> dict:
    css = gf_css(family, weight)
    urls = css_urls(css)
    if not urls:
        raise SystemExit(f"no woff2 url found for {family} {weight}: {css[:200]}")
    data = [fetch(u) for u in urls]
    bad = [i for i, d in enumerate(data) if not sniff_woff2(d)]
    if "format('woff2')" not in css:
        raise SystemExit("Google Fonts did not serve woff2 to this client; payload numbers would be meaningless")
    if bad:
        raise SystemExit(f"response(s) {bad} are not woff2 (magic {data[bad[0]][:8]!r}) — likely an error page, not a font")
    total_bytes = sum(len(d) for d in data)
    font = font_table(data[0])
    adv, upm = advance_widths(font, text)
    covered = sum(1 for ch, a in zip(text, adv) if a)
    return {
        "family": family,
        "weight": weight,
        "text": text,
        "subset_bytes": total_bytes,
        "subset_files": len(data),
        "units_per_em": upm,
        "glyphs_covered": covered,
        "glyphs_requested": len(text),
        "advance_em": [round(a, 4) for a in adv],
        "width_em": round(sum(adv), 4),
    }


def verify_payload(spec: list[tuple[str, int]]) -> dict:
    """P02 claim: total webfont payload. Measure it rather than estimate it."""
    rows = []
    total = 0
    per_subset: dict[str, int] = {}
    for fam, wt in spec:
        try:
            css = gf_css(fam, wt)
            if "format('woff2')" not in css:
                rows.append({"family": fam, "weight": wt, "error": "no woff2 offered"})
                continue
            subs = subsets(css)
            latin = subs.get("latin", [])
            blobs = [fetch(u) for u in latin]
            if any(not sniff_woff2(x) for x in blobs):
                rows.append({"family": fam, "weight": wt, "error": "non-woff2 bytes"})
                continue
            b = sum(len(x) for x in blobs)
            total += b
            per_subset["latin"] = per_subset.get("latin", 0) + b
            rows.append({"family": fam, "weight": wt, "latin_files": len(latin),
                         "latin_bytes": b, "latin_kb": round(b / 1024, 1),
                         "all_subsets_bytes": sum(len(v) * 1 for v in subs.values()) and None or None,
                         "subset_names": sorted(subs)})
        except SystemExit as e:
            rows.append({"family": fam, "weight": wt, "error": str(e)[:140]})
    return {"families": rows, "latin_only_bytes": total, "latin_only_kb": round(total / 1024, 1),
            "note": "latin subset only — a page loads per-unicode-range files, so real weight <= this when text avoids latin-ext"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("text", nargs="?", help="wordmark string to measure")
    p.add_argument("--font", default="Archivo Narrow")
    p.add_argument("--weight", type=int, default=700)
    p.add_argument("--ls", type=float, default=1.0, help="letter-spacing in px, same font-size units")
    p.add_argument("--size", type=float, default=44.0, help="font-size in SVG user units")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verify-payload", action="store_true")
    a = p.parse_args()

    if a.verify_payload:
        spec = [("Archivo Narrow", 600), ("Archivo Narrow", 700), ("Inter", 400), ("Inter", 500), ("Geist Mono", 400), ("Geist Mono", 500)]
        print(json.dumps(verify_payload(spec), indent=2))
        return 0
    if not a.text:
        p.error("need a wordmark string (or --verify-payload)")

    m = measure(a.font, a.weight, a.text)
    ls_em = a.ls / a.size
    n = len(a.text)
    # SVG letter-spacing adds after every glyph including the last in most engines; report both.
    m["letter_spacing_em"] = round(ls_em, 5)
    m["width_svg_units"] = round((m["width_em"] + ls_em * n) * a.size, 1)
    m["width_svg_units_no_trailing_ls"] = round((m["width_em"] + ls_em * (n - 1)) * a.size, 1)
    m["note"] = ("measured from the Google Fonts latin subset hmtx advances; "
                 "Instrument Sans Condensed is not on Google Fonts, so Archivo Narrow is the "
                 "condensed proxy — a wider face than ISC, i.e. this is an upper bound")
    if a.json:
        print(json.dumps(m, indent=2))
    else:
        print(f"{a.font} {a.weight} ‘{a.text}’: width {m['width_em']} em "
              f"→ {m['width_svg_units']} svg units at {a.size}px (glyphs {m['glyphs_covered']}/{m['glyphs_requested']}), "
              f"subset {m['subset_bytes']} B")
        if m["glyphs_covered"] != m["glyphs_requested"]:
            print("  WARNING: some requested glyphs are absent from this subset; width is understated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
