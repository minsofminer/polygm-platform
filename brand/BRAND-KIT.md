# Brand Kit — PolyGM (working name)

Identity for the Polymarket trading terminal + Telegram bot defined in `../prompts/`.
Brand methodology follows the vendored `skills/higgsfield-ai-skills/higgsfield-brandkit`
(MIT, © Higgsfield AI): Brand Lock → palette → logo mechanisms → typography → applications.
All assets here are **original work**: the mark, palette roles, and copy are ours.
The palette *values* are derived from Polymarket's public design tokens as inspiration —
we deliberately do **not** reproduce Polymarket's logo, wordmark, or marketing assets,
and nothing here should be mistaken for an official Polymarket product.

Rename note: `PolyGM` is the working name. `../prompts/P02-branding.md` defines the
renaming procedure; the mark is abstract, so a rename only touches wordmark text.

---

## Brand Lock

```json
{
  "version": 1,
  "brand_context": {
    "name": "PolyGM (working)",
    "offering": "Non-custodial prediction-market trading terminal + Telegram bot on Polymarket's order book",
    "industry": "Fintech / prediction markets / crypto tooling",
    "positioning": "The gmgn-grade terminal, for prediction markets, inside Telegram",
    "audience": "Crypto-native retail traders, global (heavy India/SEA/LatAm/CIS), mid-range Android",
    "tone": ["dense", "precise", "unbothered"]
  },
  "concept": {
    "name": "Binary Aperture",
    "visual_premise": "A prediction is a question held open until it resolves. The mark is a filled position (solid chevron) facing an open question (outlined chevron); the gold dot is the moment of resolution.",
    "avoid": ["candlestick cliches", "rockets", "crystal balls", "dice", "chart-arrow-through-a-letter", "Polymarket/gmgn logos or wordmarks", "gradients on chrome"]
  },
  "visual_axes": { "restrained_expressive": 25, "geometric_organic": 10, "familiar_experimental": 40 },
  "authoritative_assets": {
    "logo": {
      "origin": "brandkit_generated",
      "source": "svg/mark.svg (canonical vector); logo/concept-b-binary.png (approved raster direction)",
      "variants": {
        "color": "svg/mark.svg",
        "mono_light": "svg/mark-mono-light.svg",
        "mono_dark": "svg/mark-mono-dark.svg",
        "favicon": "svg/favicon.svg",
        "lockup": "svg/lockup-horizontal.svg",
        "app_icon": "app-icon-512.png",
        "avatar": "avatar-512.png"
      },
      "status": "fixed"
    }
  },
  "palette": {
    "origin": "derived from polymarket.com compiled CSS tokens, roles original",
    "primary": ["#2e5cff", "#7ba5ff", "#0c00a4"],
    "accent": ["#f1ce57"],
    "neutral_dark": ["#0c0e13", "#1c1f27", "#2d3037", "#62646a", "#8b929b", "#e6edf3"],
    "neutral_light": ["#ffffff", "#f4f5f7", "#e6e8ec", "#999ea7", "#1f2937"],
    "semantic_roles": {
      "action": "#2e5cff",
      "action_hover": "#5485ff",
      "yes_or_up": "#16a34a",
      "no_or_down": "#ef4444",
      "warning": "#f1ce57",
      "whale": "#f1ce57"
    },
    "forbidden": ["Polymarket wordmark blue #144E8C as a brand colour", "any gradient on chrome"]
  },
  "typography": {
    "display": { "family": "Instrument Sans Condensed", "weights": [600, 700] },
    "body": { "family": "Inter Variable", "weights": [400, 500, 600, 700] },
    "mono": { "family": "Geist Mono", "weights": [400, 600] },
    "fallbacks": ["system-ui", "Segoe UI", "Roboto"]
  },
  "layout": {
    "grid": "12-col desktop terminal, single-col mobile Mini App",
    "spacing_scale": [4, 8, 12, 16, 24, 32, 48],
    "density": "dense default, comfortable opt-in",
    "alignment": "left; numbers right-aligned tabular"
  },
  "shape_language": {
    "corner_radii": ["11.2px base (--radius .7rem)", "8px cards on dense", "999px chips"],
    "borders": "1px #2d3037 (dark), no shadows except overlays",
    "forms": ["horizontal bars (tape, depth)", "diamond aperture (mark, bullets)", "thin rules"]
  },
  "unknowns": ["final product name pending trademark check (P2 D1)", "light-theme launch timing (dark ships first)", "P02: typography.display name 'Instrument Sans Condensed' has no verifiable distribution (Google Fonts css2 -> HTTP 400, repo InstrumentType/Instrument-Sans -> 404, GitHub code search -> 0 hits). Proposed substitute: Archivo Narrow 600/700, OFL, latin subset measured 11,776 B. This is a PROPOSED field, so it may be revised; awaiting human approval before the lock is rewritten."]
}
```

---

## Contrast checks (dark theme, primary surfaces)

| Pair | Ratio | Verdict |
|---|---|---|
| `#e6edf3` on `#0c0e13` | ≈ 16.8:1 | AAA body |
| `#8b929b` on `#0c0e13` | ≈ 5.1:1 | AA body / AA small |
| `#ffffff` on `#2e5cff` (on-primary) | ≈ 4.6:1 | AA (bold/large OK) |
| `#04150c` on `#16a34a` | ≈ 5.4:1 | AA |
| `#0c0e13` on `#f1ce57` | ≈ 10.9:1 | AAA |
| `#7ba5ff` on `#0c0e13` | ≈ 6.9:1 | AA large / data |

Re-run these on any palette edit — `../prompts/P02-branding.md` requires the table.

---

## Asset inventory

| File | What it is | Status |
|---|---|---|
| `svg/mark.svg` | canonical production vector mark | fixed |
| `svg/mark-mono-light.svg` / `mark-mono-dark.svg` | monochrome variants | fixed |
| `svg/favicon.svg` | 16px-safe favicon geometry | fixed |
| `svg/lockup-horizontal.svg` | mark + wordmark, type set in code | fixed (text swaps on rename) |
| `logo/concept-a-orderflow.png` | rejected candidate (order-book ladders) | reference |
| `logo/concept-b-binary.png` | **approved** direction (raster) | reference |
| `logo/concept-c-signal.png` | rejected candidate (step line) | reference |
| `app-icon-512.png` | app icon, rounded-square | fixed |
| `avatar-512.png` | Telegram/Discord avatar, circular-crop safe | fixed |
| `og-1200x630.png` | social sharing card | fixed |
| `brandboard.png` | identity board for reviews / decks | fixed |
| `tokens.css` | **new (P02)** generated CSS custom properties, `:root` light + `[data-theme=dark]` | proposed — regenerate with `node tools/build-tokens.mjs` |
| `SIZE-RULES.md` | **new (P02)** measured size floor (24px), favicon switch, 1-bit and crop rules | proposed |
| `palette-search.json` | **new (P02)** exhaustive ΔE*ab search evidence for chart palette + NO hue | proposed — evidence, not an asset |
| `tokens.json` | extended: `semantic.{light,dark}`, per-theme `chart` arrays, `rules` incl. never-same-row | palette roles `proposed` (values audit-verified); mark/typography `fixed` fields untouched |

---

## Usage rules (short version — full rules in `../prompts/P02-branding.md` D3–D8)

1. Minimum mark size 16px; clear space = the gold dot's diameter on all sides.
2. Never redraw the mark with an image model. Compose with the SVGs.
3. Gold `#f1ce57` is a **point** colour: resolution, whale flag, alerts. Never a fill for large areas.
4. `yes/no` use `#16a34a` / `#ef4444`; the brand blue never means "yes".
5. Every number in UI uses the mono face, tabular figures.
6. No "guaranteed / safe / free money" language anywhere.

---

## Regenerating or extending (for your agent)

To produce more branded graphics from this kit, follow the vendored brandkit workflow:
`skills/higgsfield-ai-skills/higgsfield-brandkit/SKILL.md` and its `references/`
(`brand-lock.md`, `palette.md`, `logo.md`, `mockups.md`, `social-templates.md`, `brandbook.md`).

Substitution rule for environments without the Higgsfield CLI: use the workspace's
built-in image generator with prompts that paste the Brand Lock's palette hexes and
the mark description verbatim (see `../SKILLS.md` for the exact adapter). Never let the
image model free-hand the mark on top of an existing layout — regenerate from `svg/mark.svg`
and composite deterministically.
