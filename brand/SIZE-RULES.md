# Mark size rules — measured, not eyeballed

Derived by `tools/mark-legibility.py`, which computes coverage from the SVG's own polygon and circle
geometry (8×8 sub-pixel sampling) instead of screenshotting. Screenshotting was not an option here:
ImageMagick's internal SVG renderer drops stroked paths entirely — it rasterised the mark's outlined
right chevron at **0.0% coverage** of `#7ba5ff`. A "verified at 16px" claim built on that would have
described a logo with half of it missing.

| target | solid chevron | outline band | dot Ø | use |
|---|---|---|---|---|
| 512 px | 16,810 px² | 13,406 px² | 46.1 px | `mark.svg` |
| 128 px | 1,050 | 834 | 11.5 px | `mark.svg` |
| 64 px | 286 | 242 | 5.8 px | `mark.svg` |
| 40 px | 108 | 69 | 3.6 px | `mark.svg` (Telegram chat list) |
| 24 px | 44 | 34 | 2.2 px | **smallest size for the primary mark** |
| 16 px | 14 | 12 | **1.44 px** ⚠ sub-pixel | **`favicon.svg` — mandatory** |

## Rules

1. **Floor.** The primary mark is never used below 24px. Enforced by `--pgm-mark-min-size-px: 24`
   in `brand/tokens.css`.
2. **Below 24px, `favicon.svg` is required, not optional.** Its stroke is 6u (vs 3.5u) and its dot is
   r=6u (vs 4.5u), which measures 19.4 px² per chevron at 16px and lifts the dot to 1.92px Ø. Both
   polygons clear the 1.5px legibility floor; the primary does not clear it for the third element.
3. **The gold dot is the first thing to die.** Any new small-size variant must keep dot Ø ≥ 2px at the
   render size, or drop the dot and widen the aperture gap to compensate. Never shrink-to-fit.
4. **Stroke.** Minimum rendered stroke is 1px: 3.5u survives at 24px (0.84px, rounded up by the AA
   path) and below 16px the favicon's 6u is what makes 0.96px survive. Do not thin the stroke for
   light backgrounds — switch to the mono variant.
5. **1-bit / fax / emboss.** The outline may only be reproduced where 1 device pixel ≥ 1/24 of the
   mark width — i.e. **≥ 28mm wide**. Under that, use `mark-mono-dark/light` and accept the dot as a
   solid square.
6. **Crop.** Avatar/app icon: mark inscribed in a 62%-diameter centred circle on `#0c0e13`, so a
   circular Telegram crop removes nothing and a squircle removes nothing either.
7. **Never** re-draw, re-proportion or re-colour the mark to fit a background. Compose the existing
   SVG (Brand Lock + `logo.md`: an image model may not redraw the mark for a background change).

## Reproduce

```bash
python3 tools/mark-legibility.py            # the table above
python3 tools/mark-legibility.py --json     # machine-readable, consumed by tools/p02-gate-check.py
```
