# DESIGN.md — rules for the Openout front end

Condensed from `docs/P03-design-system.md`. Every rule below is **machine-checked**; if a check cannot run
in your environment, say so in the PR rather than silencing the rule. Do not "fix" a failing check by
loosening it — that is how P02's own contrast gate nearly shipped a 3.76:1 link colour.

## 1. Tokens, consumed — never redefined
| | |
|---|---|
| **Allowed** | `var(--pgm-*)` from `brand/tokens.css`, or the Tailwind class generated from the same token |
| **Forbidden** | a hex, px, ms, z-index, radius or font-size literal in `web/src/**`, `tailwind.config.*`, or a component stylesheet |
| **Source of truth** | `brand/tokens.json` → `tools/build-tokens.mjs` (CSS) → `tools/build-tailwind-preset.mjs` (Tailwind). Run both after any token edit. |
| **Check** | `python3 tools/p03-gate-check.py` (G1/G3), `node tools/build-tokens.mjs --check`, `node tools/build-tailwind-preset.mjs --check` |

`build-tokens.mjs` refuses to emit a custom property that has no home in `tokens.json`, and refuses to
generate at all if `tokens.json`, the Brand Lock name and the lockup wordmark disagree.

## 2. Colour semantics
* Outcomes are **not** good/bad. `outcome.yes` / `outcome.no` never mean profit or loss.
* Money direction is `action.buy` / `action.sell`. `action.buy` is *deliberately* the same hue as profit;
  the disambiguator is the glyph and the slot, never the colour.
* `alert.critical` is the same token as `action.sell`, so: a bare red dot and a SELL pill must never sit
  adjacent — the whale flag is a **badge with a word**.
* `outcome.no` / `action.sell` / `alert.critical` are within ΔE 3–5 of each other in deuteranopic and
  protanopic vision. They are one colour to many users. Layout separates them.
* Profit vs loss hues have ΔL 0.040 (dark) / 0.008 (light) and protan ΔE 9.0 / 6.9: **a coloured number is
  not a legible direction signal**. PnL magnitude is `text.primary`; direction is the +/− glyph + caret +
  fill-vs-outline.
* No alert hue inside `YesNoPair` or the book ladder — a warning next to two compared values reads as a
  third price.
* **Check** `python3 tools/component-colour-audit.py --gate` (its CONTROL rows are canaries: if a control
  *passes*, an exemption has grown — that is a bug in the rule, not a colour problem).

## 3. The brand mark is never redrawn
* Never generate the mark with an image model. Composite `brand/svg/mark.svg`.
* `brand/svg/lockup-horizontal.svg` is **generated** by `tools/rename-wordmark.mjs`; it rebuilds the mark
  geometry from `mark.svg` and refuses to write on any geometry/metric/fallback drift. Its hash is pinned
  in `brand/BRAND-KIT.md` (`brand_lock.geometry_sha256`); editing `mark.svg` and re-pinning in one change
  is also refused.
* Below 24px the `favicon.svg` geometry is mandatory (heavier stroke, bigger dot — measured, not styled).
* Never reproduce a Polymarket or gmgn logo, wordmark or marketing copy.
* **Check** `node tools/rename-wordmark.mjs --check`, plus `tools/p02-gate-check.py` G-rows.

## 4. Motion
* **Nothing animates a number.** No count-up, no per-digit roll, no crossfade between old and new value,
  no width animation on a numeric cell.
* Price change = background flash only: 90ms in / 260ms out (`--pgm-flash-*`), max one flash per cell per
  120ms, suppressed when the change is inside the displayed rounding window.
* UI ceiling 300ms (`--pgm-ui-ceiling`); only gesture-driven drawers may exceed it.
* Curves come from `motion.easing` (`ease-out 0.23,1,0.32,1` etc.). No `ease-in` on UI. No `transition: all`.
* Animate `transform`/`opacity` only; never `width`/`top`/`left`. Keep animated blur under 20px.
* Per-frame writes go to `ref.current.style`, not React state.
* Long lists are virtualised (tape DOM budget `--pgm-tape-rows`, hard cap 64 rows).
* Never `scale(0)`; start at 0.9–0.97 with `opacity: 0`. Popovers use origin-aware transform from the
  trigger; modals stay centred.
* `prefers-reduced-motion: reduce` → animations off, flash becomes a static start-border.
* **Check** `tools/p03-gate-check.py` G4 (duration/easing literals, forbidden properties) + review with
  `skills/emilkowalski-skills/skills/review-animations`.

## 5. Numbers and money
* Prices always use `.` as the decimal separator, in every locale. Precision comes from
  `minimum_tick_size` (0.001 → 3dp, 0.01 → 2dp) — never assume 2dp; both tick sizes occur in the same
  event today.
* Grouping uses a thin space (U+2009), never `,` or `.`.
* Money is integer cents at every boundary. `money/cents.ts` is the only parser/formatter; a float in the
  money path fails the build. No money value is ever rendered from an unrounded float.
* `REDEEM` payout comes from `usdcSize`, never `price × size` (price is 0 on 100% of 287 sampled rows).
* Every derived figure is labelled as ours where a Polymarket number also exists (PnL, rank, win rate,
  drawdown: `lb-api/pnl` does not exist and `lb-api/rank` returns 400).
* **Check** `tools/p03-gate-check.py` G5 (float-scan over money-formatting examples) + P01's gate for
  endpoint claims.

## 6. Live data honesty
* The tape is WebSocket-only: `data-api/trades` is Cloudflare-cached and a poller would show a confident,
  frozen feed. REST rows are tagged and never flash.
* Every surface declares a freshness threshold; past `stale` the value desaturates and `StaleIndicator`
  attaches; past `blocking`, order submission is disabled.
* Disconnected = **trading disabled app-wide** (client UX) and rejected by the risk gate (authority).
  Cancel-all stays available.
* Reconnect does snapshot-resync first, and shows the gap ("612 fills not shown"), never silently resumes.
* A one-sided or near-empty book is explained in words ("you can still SELL YES at 0.999 — that is buying
  NO at 0.001"), never rendered as "no liquidity".
* **Check** `tools/datasource-probe.py` (upstream contract) + `tools/p03-gate-check.py` G2 (every field
  cites a probed endpoint).

## 7. Accessibility
* WCAG 2.2 AA, keyboard-complete, visible focus (2px ring, 2px offset, never removed without replacement).
* No focus traps: `Esc` works everywhere except while a request is in flight, and that is announced.
* The live tape is `aria-live="off"` with a separate summarising `role=status` region (≤1 announcement
  per 5s). `polite` on a 20/sec region makes the product unusable.
* Colour is never the only channel: every badge has a word, every direction a glyph.
* 44px minimum touch target at every density; density scales text, never hit areas.
* **Check** `tools/p03-gate-check.py` G6 + the storybook state list (component × 11 states).

## 8. Copy
* Weak-copy length floors from P02 apply; no "no liquidity!" alarmism, no promises, no "guaranteed", no
  financial-advice phrasing, no accusation words in the suspicious-behaviour panel (metrics + thresholds
  only). Risk disclosure is one click from the trade ticket, permanently visible.
* i18n keys `screen.component.element#variant.state`; missing translation fails the build, not the runtime.

## If you are an agent building from this file
Read `docs/P03-design-system.md` (full spec), `brand/BRAND-KIT.md` (Brand Lock) and `docs/AGENTS-BUILD.md`
(build order) first. Run, in order: `python3 tools/p03-gate-check.py` then `python3 tools/p02-gate-check.py`
then `python3 tools/p01-gate-check.py`. A green suite is the definition of done, not a progress report.

## 9. Quoting a violation (spec only, never product code)

A spec must be able to *show* the bug it forbids. Two markers exist, both exact whole lines, both scoped to
`docs/*.md`:

* `<!-- P03: anti-example -->` immediately above a fence — the fence is quoted, not shipped.
* `<!-- P03: nocode -->` above a prose block — the block may name a forbidden form. It **cannot** cover a
  code block: a `nocode` marker in front of a fence is a gate failure (G5.2c).

Neither marker is recognised in `web/src/**`, `server/**`, or any stylesheet: in product code, "this line
demonstrates the bug" is not a defence. An orphan marker (no fence under an anti-example, nothing after a
nocode) is also a failure, so forgetting one reports rather than hides.
