# Skills — how this repo uses them

Two skill libraries are vendored under `skills/` (both MIT-licensed, attribution preserved
in each copy). They are **methodology for an AI agent to follow**, not code we execute.

| Vendored copy | Upstream | What it is for here |
|---|---|---|
| `skills/higgsfield-ai-skills/` | https://github.com/higgsfield-ai/skills.git | Brand system methodology (`higgsfield-brandkit`): Brand Lock, palette, logo mechanisms, mockups, social templates, brandbook |
| `skills/emilkowalski-skills/` | https://github.com/emilkowalski/skills.git | Frontend craft: `animate`, `animation-vocabulary`, `find-animation-opportunities`, `improve-animations`, `review-animations`, `apple-design`, `mobile-native`, `emil-design-eng`, `prototype` |

---

## If your agent has the Higgsfield CLI

Follow `skills/higgsfield-ai-skills/INSTALL_FOR_AGENTS.md`, authenticate
(`higgsfield auth login`), and run the brandkit workflow verbatim:

```
skills/higgsfield-ai-skills/higgsfield-brandkit/SKILL.md
```

The project's **Brand Lock is already complete** — do not redo intake. Start from
`brand/BRAND-KIT.md` (it is the canonical lock, in the skill's shape), treat its
`fixed` fields as immutable, and only extend `proposed`/`unknown` fields.

## If your agent does NOT have the CLI (e.g. this sandbox) — the adapter

The brandkit skill's paid-generation steps assume the Higgsfield API. Substitute the
workspace's built-in image generator and keep the rest of the workflow identical:

1. **Palette stage** — skip generation; the palette is approved in the Brand Lock.
2. **Logo stage** — never regenerate the approved mark with an image model.
   `brand/svg/mark.svg` is canonical. If a *new candidate* is explicitly requested,
   generate exactly three symbol-only prompts per `references/logo.md`, paste the
   Brand Lock hexes (`#2e5cff #7ba5ff #0c00a4 #0c0e13 #f1ce57`) into each prompt,
   forbid text/letters, and present all three for a human choice.
3. **Application stages (mockups, social, posters, brandbook)** — call the built-in
   image generator with prompts composed from:
   - the exact palette hexes,
   - the mark description: "solid vivid-blue left chevron and lighter-blue outlined
     right chevron forming a diamond negative space with a small gold dot at centre,
     on deep navy #0c0e13",
   - the constraints from `references/brand-lock.md` (preserve exact copy, never invent
     claims, never redraw the mark for background changes — composite the SVG instead).
4. **Deterministic outputs** (HTML/SVG/PPTX brandbook pages, logo exports, spec sheets):
   use the skill's bundled scripts where they are dependency-light
   (`scripts/brandkit.py` is local tooling; its state file lives in a `brandkit/`
   workdir that the skill creates, not in this repo).

## Frontend phases (P3, P8–P11) — apply the Emil Kowalski skills

When implementing the UI prompts in `prompts/`:
- `find-animation-opportunities` before writing any motion.
- `animation-vocabulary` for naming durations/easings in the design system.
- `improve-animations` + `review-animations` on the flash-on-change price cells,
  the tape coalescing, sheet/sheetbox transitions, and tab switches.
- `mobile-native` for the Telegram Mini App (haptics, safe areas, dvh, back-button).
- `emil-design-eng` as the review lens for every merged UI PR.
- `prototype` for the read-only Phase-0 build.
- `performance-cheatsheet.md` (repo root of that library) before choosing any
  animation technique — the tape renders 20+ updates/sec.

## Guardrails that override any skill

1. The Brand Lock's `fixed` fields are immutable without an explicit human approval.
2. No asset may reproduce Polymarket's or gmgn's logo, wordmark, or copy.
3. The image generator may never redraw `brand/svg/mark.svg`; composite it.
4. Anything the skills generate for this project lands in `brand/` and gets listed in
   the inventory table of `brand/BRAND-KIT.md`.

## The image pipeline that actually works — empty regions in, canonical renders out
*(added after three boards were generated the other way and all three came back wrong)*

The built-in image generator **redraws the mark** even when the prompt describes it
exactly and pastes the Brand Lock hexes. It comes back with rounded chevrons, an
off-centre dot, and a filled right chevron at small sizes — close enough to survive a
glance and wrong enough to ship a fake logo. Do not prompt the generator with the mark.

What works, in order:

1. **Ask for empty identity surfaces.** The prompt names a *place* for the mark (an
   empty panel, a blank disc, four blank stickers) and forbids the mark in the negative
   list — `no logos, no emblems, no chevrons, no icons, no symbols, no text`. Include
   `no letters, no numbers` too: a generator asked for a "terminal" will otherwise add
   invented tickers.
2. **Clear a flat region only if the base already has a mark** (older boards). The region
   is cleared only when the *ring around it* — not its interior — is flat and matches the
   target colour; if the ring crosses a panel rule or a highlight, no patch is honest and
   the board must be regenerated instead. Widening the tolerance is how a visible rectangle
   gets shipped.
3. **Composite from `brand/svg/mark.svg`** with `tools/compose-brand-board.py`, never by
   asking a model to place a logo. Placements support a centre + size (colour, mono_light,
   mono_dark), a quad (`quad_mode: "fit"`, sized as a *fraction of the object*, for a card
   seen at an angle), an ellipse (a sticker on a tilted sheet), per-placement ink `opacity`
   and a depth-matching `blur`.
4. **Verify.** `python3 tools/compose-brand-board.py --check` fails on a missing or stale
   board, on a spec that builds on a base which is not a mark-free `-raw` file, and on
   provenance drift: every composed board is recorded in `brand/boards/COMPOSITES.json`
   against the locked mark hash, so a board made from an older mark fails loudly.

The renderer and the compositor agree on the hash by construction — both compute
`geometry_hash(mark.svg)` the way `tools/rename-wordmark.mjs` does, and the renderer
refuses to export when it disagrees with the lock in `brand/BRAND-KIT.md`.

### Vendored for this pipeline

| Library | Repo | Commit | Why |
|---|---|---|---|
| `vercel-agent-skills` | vercel-labs/agent-skills | `063bee94` | web-design-guidelines, react-best-practices, vercel-optimize |
| `taste-skill` | Leonxlnx | `ce26fc25` | image-to-code direction for the boards |
| `awesome-design-md` | VoltAgent | `f6961238` | design-system reference writing |
| `playwright-cli` | microsoft | `74354ecc` | browser automation for the visual passes |

Provenance for all four is machine-readable in `skills/VENDOR.json`.

## Motion audit (Emil Kowalski skills, applied 2026-09-26)

`find-animation-opportunities` + `improve-animations` were run against the Mini App
surfaces. The result was **no changes needed**, which is worth recording as a result
rather than as a skipped step:

- durations and easings are tokens (`--pgm-flash-out`, `--pgm-dur-press`, `--pgm-ease-out`),
  never literals — a px/ms literal fails the P08 gate, so the discipline is enforced;
- no `transition-all` anywhere: transitions name the property they change;
- `prefers-reduced-motion` is honoured in `web/src/globals.css`, in `tma/TradeSheet.tsx`,
  and in `tma/haptics.ts`, and the reduced-motion path keeps the *information* (the flash
  becomes a static edge) rather than only removing the animation.
