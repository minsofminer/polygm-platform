# Web interface guidelines — review of the product, and what it changed

Method: `vercel-labs/agent-skills`' `web-design-guidelines` skill, which fetches Vercel's Web Interface Guidelines
and reviews files against every rule, one `file:line` per finding. This is the record of that pass over the web
app, in the same shape as `plans/animation-audit.md`: what was found, what was done, what was **refused with a
reason**, and what is left for the owner.

The skill's own format is terse (`file:line`). That is right for a review and wrong for a repository: six months
from now the useful thing is not the finding, it is *why this product does or does not do it that way*. So each
entry carries the reasoning, and the ones that turn out to be deliberate are recorded as refusals rather than
quietly deleted.

---

## Findings, by severity

### F1 — the rail handles were keyboard-inaccessible (HIGH · **fixed, and now browser-verified**)

> Verified on 2026-09-27 by `e2e/shell-fixes.spec.ts` in a real chromium: one ArrowRight adds 0.005 to
> `pgm.rails.v1` (the same store the pointer drag writes), Shift adds ×5, Home/End reach the bounds, the announced
> `aria-valuenow` follows, and Enter collapses the rail. The run that earned this is
> `plans/browser-verification.md`.

`web/src/shell/Shell.tsx` rendered both resize handles as `role="separator"` with `tabIndex={0}` and a pointer
handler. A focusable `separator` is a **window splitter**: WAI-ARIA requires `aria-valuenow` / `aria-valuemin` /
`aria-valuemax`, and the guidelines require interactive elements to answer the keyboard. These had neither. Tab
reached a control that did nothing, Home/End meant nothing, and a keyboard-only user could not resize a rail at
all — on a product whose whole layout is three resizable columns.

The terminal's own handles (`TerminalLayout.tsx`) have answered `ArrowLeft/ArrowRight` (0.005 per press, ×5 with
Shift) and `Home` since P10. The shell, three phases later, had the pointer half only. That is the worst kind of
inconsistency: not a style choice, but the same product disagreeing with itself about what a control is.

Fixed: `RAIL_STEP` + `nudgeFraction` exported from `src/shell/rails.ts` (the same numbers the terminal uses, so the
two layouts cannot drift again), `applyRail`/`nudge` in `src/shell/useRails.ts`, `handleKeys` in the shell, and the
values a splitter owes — `aria-valuenow`/`min`/`max` in percent plus an `aria-valuetext` that names the keys,
because a percentage announced alone is a number with no affordance attached. `Enter`/`Space` perform the same
collapse the rail's own button does. Every keyboard resize passes the same `railsFit` gate as the drag: the keyboard
cannot reach a layout the pointer would be refused.

Tests (`src/shell/rails.test.ts`, 12 cases) cover the step arithmetic and the wiring — including that the ARIA
values are present, because the first draft of this fix added the handler and **forgot the values**, which is an
incomplete splitter rather than a fixed one.

### F2 — `color-scheme` was never declared (HIGH · **fixed**)

The product is dark-first (`data-theme="dark"` on `<html>`, written server-side). Nothing declared
`color-scheme`. So the browser painted its own furniture — scrollbars, the text caret, the `<select>` popup,
autofill backgrounds — in **light**, inside dark cards. This is the class of bug that makes an application feel
wrong without anyone being able to name why.

Fixed in `src/globals.css` on `[data-theme="dark"]` / `[data-theme="light"]`: the attribute the server already
writes, so it cannot disagree with the theme of the first paint.

### F3 — the handle could not be dragged by touch (HIGH on mobile · **fixed**)

No `touch-action` on `.handle`. On a touch device a finger drag is a *scroll*: the browser claims the gesture, the
element receives `pointercancel`, and the rail is **not resizable at all** by touch. The Mini App's sheet grabber
sets `touch-action: none`; the desktop shell never did.

Fixed: `touch-action: none` — `none` and not `manipulation`, because the handle has exactly one gesture and it is
the drag.

### F4 — the modal layer chained its scroll (MEDIUM · **fixed**)

`.overlay` scrolled but did not contain its scroll: reaching the end of a long dialog kept scrolling the page
underneath, so the reader closed the dialog to find the market they were on had moved. The ladder's own scroll
container already set `overscroll-behavior: contain` — the rule was known and the newest layer did not have it.

### F5 — no skip link (MEDIUM · **fixed**)

The shell renders the topbar and **both rails** before `<main>` in document order, so a keyboard user tabbed
through an entire rail before reaching the content, on every navigation. Fixed with a `.skip` link that is the
first focusable element and is visible when focused (`<main id="content" tabIndex={-1}>` is its target, so the
caret lands inside rather than on the body).

### F6 — four loading strings ended in three periods (LOW · **fixed**)

`en.terminal.ts` had three strings with `…` and four with `...`. Inconsistency inside one file, so: all seven now
end in `…`. (The guideline's curly-quote rule was checked against `en.ts` and the copy is already clean of straight
quotes — the em dashes and the `—` in `shell.rail.width` are intentional and the skeleton rule in `P03` is why.)

---

## Checked and already correct (the review's negative results)

Recorded so a future reader does not re-litigate them:

| Rule | State |
|---|---|
| `user-scalable=no` / `maximum-scale=1` | **absent, deliberately** — `app/layout.tsx` says so in a comment: pinch-zoom is an accessibility feature, not a bug |
| `<img>` without dimensions, missing `alt` | no `<img>` elements in the product; every icon is an inline `<svg aria-hidden>` |
| `transition: all` | none anywhere (asserted in `rails.test.ts`) |
| `outline: none` without a replacement | none; one `:focus-visible` rule with a token-drawn outline, 48 consumers of that pattern |
| Icon-only buttons without `aria-label` | all labelled (`Dialog` close, palette, rail asides) |
| `<div onClick>` | none — actions are `<button>`, navigation is `<Link>` |
| Form controls without labels | `src/ui/Field.tsx` renders a real `<label>`; 81 controls all go through it or carry `aria-label` |
| `tabular-nums` on number columns | set globally on `.num` and on the ladder/tape cells |
| `prefers-reduced-motion` | honoured, and now **behaviourally** (see `plans/animation-audit.md`) |
| Autoplay/looping media | none — no video or audio in the product |
| Hardcoded date/number formats | `Intl.*` throughout the public and terminal views |
| `theme-color` meta | present, both schemes, from generated `theme-colors.json` |
| Deep-linkable state | tabs and filters are routes or query params; the palette is the keyboard path |

---

## Refused, with the reason

These are the guidelines' rules this product deliberately does not follow. A review that only lists fixes is a
review that will be re-run forever; these are the parts where the answer is "no, because".

**R1 — "URL reflects state: expanded panels in query params" (rail widths).** The rail fractions are stored per
browser (`localStorage`, with the account's own preference deliberately not involved) because they are a *device*
property — a 13" laptop and a 27" monitor want different rails, and one user has both. Putting them in the URL
would make every navigation carry a layout nobody typed and would let one device's layout be shared into another's.
Reason recorded at the storage layer itself.

**R2 — "Virtualize lists >50 items" (the ladder).** The ladder renders up to 200 price levels, deliberately, and
the requirement is that the reader keeps their place while it re-folds. Virtualization would trade a bounded,
measurable render cost for a scroll-anchoring problem the product already solved once (`src/lib/anchor.ts`). The
scroll container is `max-block-size`-bounded rather than the whole page, which is the part of the rule that
matters. `content-visibility: auto` is *not* used: it changes layout timing on rows that are already re-laddered
20 times a second.

**R3 — "Disable `autoFocus` on mobile" (command palette).** The palette opens *because* the user asked for it, by
keyboard, on a device with a keyboard; focusing the input is the entire point of the interaction. The rule is
about page loads stealing focus, which this is not.

**R4 — "Title Case for headings and buttons."** The product's copy is sentence case, set in `web/DESIGN.md §8`
along with the weak-copy floors and the refusal wording rules. One style guide owns the copy; Vercel's guideline is
right that *some* guide must, and this repository has one.

**R5 — "Active voice / numerals for counts."** Already true in the shipped copy ("Reading the market…", "8 levels",
"62 checks passed"), so there is nothing to change — but it is recorded here because the rule is one a reviewer
will check again, and the answer is "yes, already".

---

## Deliberately left: `[UNVERIFIED]`

Two things this review could not do on this machine, in line with how the rest of the platform records its
unearned items:

* **The touch fix is reasoned, not felt.** `touch-action: none` on the handle was verified in a browser as
  computed style (`e2e/shell-fixes.spec.ts`, and in the touch-emulating webview project) — but no finger has
  dragged the rail on a real device. See `plans/browser-verification.md`.
* **Contrast of the hover/active states** was verified by the token pair (the ladder's own
  `--pgm-text-*`/`--pgm-bg-*` combinations are checked in P03's gate), **not** by rendering. The guideline's
  "interactive states increase contrast" holds by token construction; a human should still look.

## What this pass did *not* cover

`react-best-practices` (render behaviour, effects, memoization, server/client boundaries) and `vercel-optimize`
(bundle and delivery) are separate skills with separate criteria, and neither was applied here. The animation
pass that preceded this one is `plans/animation-audit.md`.
