# The desktop shell's motion: an audit, and the plans it produced

Method: `skills/emilkowalski-skills/skills/improve-animations` (recon → audit → vet), with the rule catalog from
that skill's `AUDIT.md` and the exact values from `skills/emilkowalski-skills/skills/review-animations/STANDARDS.md`.
Read-only analysis first; the plans below are the executable half, and they were executed in the commit that adds
this file. Every finding this document keeps was re-read at its `file:line` before being written down, per the
skill's Phase 3 — and three findings the first pass produced were **rejected** as by-design, listed at the end so
the rejection is on the record rather than in somebody's memory.

## Recon (the motion surface, as it actually is)

| Fact | Evidence |
| --- | --- |
| Stack | Next.js App Router + React; **no motion library** (`grep framer/motion/gsap` → none): plain CSS, which is the right default here and is what makes these fixes cheap |
| Where motion lives | `web/styles/tokens.css` (tokens + keyframes), `web/src/globals.css` (shell surfaces), components only reference classes |
| The token layer is already the vendored skill's numbers | `brand/tokens.json` cites `review-animations/STANDARDS.md` for every curve and duration; P03's gate **G4** re-reads that file and fails on drift (G4.1), on a duration over the skill's 300 ms ceiling (G4.2), and if the ladder ever stops being sourced rather than invented |
| Personalities | crisp dashboard, money product. Motion exists to explain state changes (a fill landed, a refusal happened), never to decorate a data surface |
| Frequency map | command palette + shortcuts: **100+/day → never animate** · rail handle, chips, toggles, tab bar: tens/day · dialog, toasts: occasional → standard animation allowed |
| Animated classes today | `--pgm-rise-sm: 8px; /* the sheet, the card, the confirm row, the skeleton and the refusal nudge — no other class animates */` — including that clause, which this pass has to answer rather than step over |

**The finding underneath the findings:** every motion consumer in the product is in the **Mini App** block
(`pgm-card-in`, `pgm-sheet-in/out`, `pgm-ack`, `pgm-reject-nudge`, `pgm-soft-pulse`, `pgm-scrim` — tokens.css
279–297). The desktop shell animates exactly two things: a number flash and a button press. The design system
already contains the durations for the desktop's missing motion — `--pgm-dur-large` "modals, sheet settle" has one
consumer on desktop (a skeleton), `--pgm-dur-micro` has **zero**, `--pgm-dur-medium`'s own comment promises a "tab
underline slide" that no rule implements.

## Findings, ordered by leverage

| # | Sev | Category | Location | Finding | Fix (plan) |
| --- | --- | --- | --- | --- | --- |
| 1 | HIGH | Purpose/Physicality | `web/src/ui/Dialog.tsx:52,63` → `globals.css:312–330` | The dialog appears from nothing and vanishes instantly: no `animation`, no `transition`, no transform. Occasional surface → "Standard animation" (STANDARDS frequency); `scale(0)`-equivalent appearance, i.e. the exact physicality the skill names | **P1** |
| 2 | HIGH | Purpose/Interruptibility | `web/src/ui/Toast.tsx:38` (`dismiss` filters synchronously) → `globals.css:341–362` | The toast stack jumps in and out. Toasts are the surface `AUDIT.md` names by name ("keyframes that make toasts jump"); dismissal is a hard removal, so nothing can be interrupted or read on the way out | **P2** |
| 3 | HIGH | Physicality/Performance | `globals.css:275–278` | Press feedback is `translateY(--pgm-space-off-1)` while the design system says `scale(0.97)`: `docs/P03-design-system.md:169` (`active │ pressed │ scale(0.97) 100ms ease-out`) and the token table's own `:active scale(0.97–0.98) on any pressable` (`brand/tokens.json` `duration_ms.press`). Worse, `transition` is declared **inside** `:active`, so the *release* has no transition at all and snaps back | **P3** |
| 4 | MED | Cohesion/Missed opportunity | `web/styles/tokens.css:187` vs `grep → 0 consumers` | `--pgm-dur-micro` (80 ms, documented "flash-on-change in, tooltip appear after first") is used by nothing. A token with no consumer is a ladder rung nobody stands on; the controls hit tens of times a day are its honest job | **P4** |
| 5 | MED | Cohesion/Physicality | `tokens.css:190` vs `globals.css:215–218` | The ladder's `--pgm-dur-medium` comment promises a "tab underline slide"; the active-tab marker is an untransitioned `box-shadow` swap, so the mobile tab bar pops | **P5** |
| 6 | LOW | Cohesion | `web/styles/tokens.css:201` | `--pgm-rise-sm`'s comment ends `no other class animates` — a **contract**, and this pass changes it. Contract updates get reasons, not quiet violations | **P6** |

### Rejected on vetting (do not re-report)

* **The command palette has no open/close animation.** Correct: 100+/day, keyboard-initiated — STANDARDS' strongest
  rule ("Never animate keyboard-initiated actions"). Left alone deliberately.
* **`transform-origin: center` behaviour on the dialog.** Modals are *exempt* from the trigger-origin rule; a plan
  that made a centred modal scale from a button would be wrong, not clever.
* **The rail's drag handle (`globals.css:158`) snapping on hover.** Tens/day, and the "remove or drastically reduce"
  branch of the frequency table permits a fast colour change; it becomes plan **P4**'s consumer rather than a
  delete-the-motion finding.

## Plans

Executed in this commit; each is self-contained per `PLAN-TEMPLATE.md` (exact file, exact values, no context from
this conversation required).

### P1 — the dialog enters and exits (~200 ms, centred, `--pgm-rise-sm`)

* `tokens.css`: `pgm-panel-in` (opacity 0 → 1, `translateY(var(--pgm-rise-sm))` → none) and `pgm-panel-out`
  (the reverse), then `.pgm-panel-in { animation: pgm-panel-in var(--pgm-dur-large) var(--pgm-ease-out) 1; }`,
  `.pgm-panel-out { … 1 forwards; }` and a scrim fade at `--pgm-dur-small`.
* `globals.css`: `.overlay__panel` gets the entrance class; `.overlay` fades.
* `Dialog.tsx`: a `closing` state; `requestClose()` plays the exit then calls `onClose`. **`onAnimationEnd` never
  fires under `prefers-reduced-motion`** (the reduced-motion block sets `animation: none !important`), so the exit
  path must short-circuit to `onClose()` when the media query matches — otherwise the dialog becomes unclosable
  for exactly the users the media query protects. That case has a test.

### P2 — the toast stack rises in and leaves instead of vanishing

* `tokens.css`: `pgm-toast-in` / `pgm-toast-out` at `--pgm-dur-small` (125 ms, "popovers, small overlays") with
  `translateY(var(--pgm-rise-sm))`, `ease-out`, exit `forwards`.
* `Toast.tsx`: `dismissing: boolean` per row and `requestDismiss(id)`; `dismiss(id)` stays the only remover, called
  from `onAnimationEnd` (or immediately, reduced motion).
* **A correction the planning pass needed:** `ttlMs` is carried by every row and **no timer reads it** — there is no
  `setTimeout` anywhere in `Toast.tsx`, so today's toasts live until they are dismissed. Auto-dismiss is therefore a
  product *behaviour* change and is deliberately **not** in this pass: the leaving state is built and documented so
  that the timer, when somebody decides a toast should expire, calls `requestDismiss` and gets the exit for free.
  The field's docstring now says exactly that, because a comment implying an auto-dismiss the product does not have
  is worse than no comment.
* Dedupe is unchanged: a repeat bumps the counter in place, which is what stops the waterfall the file's header
  describes from becoming an animated waterfall.

### P3 — press feedback obeys the documented physicality, and both directions animate

* `globals.css`: `.button:active` → `transform: scale(0.97)`; the `transition: transform var(--pgm-dur-press)
  var(--pgm-ease-out)` moves to the **base** `.button` rule so the release eases back (the current placement
  animates only the press).
* `0.97` is the design system's own value (`P03 §D3`), not a taste call; the press duration stays `--pgm-dur-press`.

### P4 — `--pgm-dur-micro` gets its documented consumer

* `globals.css`: the hover-colour changes on the controls hit tens of times a day (`.handle:hover`, `.tab`, the
  chips/toggles pressed state) transition at `var(--pgm-dur-micro) var(--pgm-ease-out)`. No transform, no
  movement — the frequency table's "drastically reduce" read literally: a colour that changes over 80 ms reads as
  responsive; the same change over 300 ms reads as lag.

### P5 — the tab indicator moves at `--pgm-dur-medium`

* `globals.css`: the active marker becomes a pseudo-element with `transform: scaleX()` transitioning over
  `var(--pgm-dur-medium)` (150 ms), growing from the leading edge, instead of an instant `box-shadow` swap.
* **A true cross-tab slide is deferred, with the reason:** the marker lives on each tab and the bar is a
  `grid-auto-flow: column` with equal fractions, so sliding one marker between them requires measuring each tab's
  offset with JS and re-measuring on resize/rotation — a `ResizeObserver` and a positioned indicator for a bar that
  is only visible under 768 px. The token's comment is updated to describe what exists rather than what was hoped.

### P6 — the contract comment is updated, not quietly broken

* `tokens.css:201`: `--pgm-rise-sm`'s comment names its consumers, and it now includes the dialog panel and the
  toast. Same for `--pgm-dur-medium`'s "tab underline slide" (P5) — the ladder's comments are read by the P03 gate
  and by people; one that promises motion nobody shipped is worse than no comment.

## What "verified" means for a motion change in this repo

Not a screenshot. The rules that can be checked by machine are checked by machine: `web/src/ui/motion.test.ts`
reads `tokens.css` and `globals.css` and asserts the values (durations are tokens, `ease-out` on entrances, never
`ease-in`, never `scale(0)`, `scale(0.97)` on press, an exit path that exists for both surfaces and a
reduced-motion short-circuit in both components); the component tests assert the lifecycle (entrance class present,
`onAnimationEnd` → `onClose`/removal, TTL goes through the leaving state, reduced motion closes immediately); and
the phase gates that police the stylesheet (P03 G4 + P08 c5's token/px-ms-literal scan) re-run because a stylesheet
change is never only a stylesheet change.
