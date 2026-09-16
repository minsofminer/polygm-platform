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
