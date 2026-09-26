# P3 — Design System & Screen Specifications · Openout

> Built from `docs/prompts/P03-design-system.md`, on top of P01 (`docs/P01-product-spec.md`) and P02
> (`brand/BRAND-KIT.md`, `brand/tokens.json`). Every colour, size, duration and breakpoint here is a
> **reference into `brand/tokens.json`**, never a re-typed number; `tools/p03-gate-check.py` proves it.
> Every data field cites an endpoint shape that exists in `docs/verification/P01-probe.json`
> (`tools/datasource-probe.py --json --save …` regenerates that evidence).

**Status of the two prompt claims P03 re-measured** (report first, then deviate — see D0):

| Prompt claim | Re-measured 2026-09-17 | Verdict |
|---|---|---|
| "a Fed market with 94 ask levels and zero bids on YES", "one-sided happens constantly" | 22 books sampled: **0 fully one-sided**; 8/22 have ≤5 levels on the near side; deeper side up to 137 levels; Fed book was 63 asks / 3 bids | claim's *shape* is real, its *absolutes* are stale. The UI must handle **near-empty side** (frequent) and **fully one-sided** (rare but reachable), not just the second |
| `PriceCell` flash = "green/red background decay over 600ms" | 600ms breaks P02's own adopted ceiling (300ms for UI motion); green/red as *outcome* colour breaks `rules.outcome-never-green-red` | **both numbers changed**: 90ms in / 200ms out (290ms total), and the flash reuses the `action.buy`/`action.sell` hues (which are the *money* semantics, not outcome). Recorded as a §12 amendment |

---

## D0. Deviations from the prompt, and why

1. **Flash duration 600ms → 90ms in / 200ms out (290ms total).** `skills/emilkowalski-skills/…/STANDARDS.md` (which `SKILLS.md`
   makes binding for frontend decisions): *"Rule: UI animations stay under 300ms. A 180ms dropdown feels
   more responsive than a 400ms one."* A 600ms decay on the tape at 14.7–33.3 fills/sec means ~10–20
   cells flashing simultaneously. Split into a fast spike (attention) and a longer fade (legibility) —
   perceived intensity of 600ms, cost of 290ms.
2. **Flash colour: "green/red" → `action.buy` / `action.sell`.** These *are* green `#16a34a` and red
   `#ef4444`, so the prompt's intent is honoured; what is forbidden is using *outcome* colours here.
   `outcome.yes #7ba5ff` / `outcome.no #D55E00` never encode direction (P02 D4), and `PositionRow` must
   not read a flash hue as a verdict.
3. **"nothing animates a number" is strengthened, not just kept.** No tween, no per-digit roll, no
   crossfade, and no width animation on the cell. See `motion.number_policy` in tokens.json.
4. **`OrderBook` gets a *third* state the prompt didn't ask for: asymmetric-but-present.** Measured above
   as the common case. Designing only for "zero bids" would ship a terminal that looks broken 8/22 of the
   time instead of 0/22.

---

## D1. Foundations

All values live in `brand/tokens.json` (written by `tools/build-foundations.py`, emitted to
`brand/tokens.css` by `tools/build-tokens.mjs`). What is *generated* here is the numbers, not the prose:
`python3 tools/build-contrast-table.py --foundations` prints the D1.1 off-grid, D1.4 density and D1.5
breakpoint tables from the tokens, and `--foundations --check` fails if this document's figures (or the
density table's header order) drift from them. `tools/p03-gate-check.py` G1.6 runs that check, so it cannot
be forgotten. Prose stays authored — a sentence cannot be diffed against JSON, and pretending otherwise
would make the check meaningless. Hand-edit a *number* instead of the JSON and the gate fails.

### D1.1 Spacing — 4px base

`spacing.base = 4`, steps `0 1 2 3 4 5 6 8 10 12 16 20` (0/4/8/12/16/20/24/32/40/48/64/80 px).
Rule: **every** gap, pad and margin in the app is one of those twelve. Three off-grid values are
sanctioned for optical work only, each with a named token so "off-grid" is still auditable:

| Token | px | Only for |
|---|---|---|
| `--pgm-space-off-1` | 1 | hairline borders, the OrderBook depth-bar 1px track |
| `--pgm-space-off-2` | 2 | icon→label gap inside 11px text, where 4px reads disconnected |
| `--pgm-space-off-3` | 3 | stacked yes/no chip gutter at dense density only |

### D1.2 Borders

`hair` 1px (row/column separators, card edges) · `strong` 2px (focus rings, active tab underline,
selected-row left rule) · `heavy` 3px (drag targets, resize handles). Colour is always
`border.default` / `border.strong` — a hard-coded grey is a P02 audit failure.

### D1.3 Elevation — four levels, used almost never

Elevation 0 page · 1 cards/panels/sticky headers (**a 1px bottom hairline, not a shadow**) ·
2 dropdown/popover/command palette · 3 modal/toast/TradeTicket sheet. Nothing else gets elevation.
Forbidden: glassmorphism on data surfaces, elevation on rows or cells, inner shadows on inputs
(use `bg.inset`), animated blur above 20px. On light theme the black alphas drop to .12 and the 1px
ring carries the separation.

### D1.4 Density

`dense` (default) / `compact` / `comfortable`, user setting, per device, no account required.
Dense is the default because a terminal showing 14 rows is a terminal you scroll, and the median fill
is $5–6 (P01).

| px | dense | compact | comfortable |
|---|---|---|---|
| `table_row` | 22 | 28 | 36 |
| `tape_row` | 20 | 26 | 34 |
| `book_level` | 16 | 20 | 26 |
| `list_card` | 44 | 56 | 72 |
| `cell_x` pad | 6 | 8 | 12 |
| `panel` pad | 8 | 12 | 16 |
| font size | 11 | 12 | 13 |

**Density never scales hit areas.** `min_touch_target = 44px` is constant; at dense on a touch
breakpoint the row's *padded box* stays ≥44px while its text stays 11px. `topbar 40`, `tab_bar 32`,
`trade_ticket_row 28` are structural constants — same at every density, so switching density mid-session
never moves the primary actions.

### D1.5 Breakpoints

| name | trigger | layout |
|---|---|---|
| `xs` | <640 | single column, one panel at a time, ticket = bottom sheet, Book/Tape/Positions in a tab bar. **This is the majority client: mid-range Android.** |
| `sm` | ≥640 | single column + persistent topbar; ticket becomes an inline panel |
| `md` | ≥768 | two columns: chart+book left, tape right |
| `lg` | ≥1024 | three columns (`--pgm-cols: 3`), watchlist rail |
| `xl` | ≥1280 | **full terminal** — watchlist \| market+chart \| book+tape, ticket docks right; `--pgm-tape-rows: 16` |
| `2xl` | ≥1536 | as xl, wider tape, 12 book levels |
| `3xl` | ≥1680 | four columns; watchlist may collapse to icons; DepthChart gets a second pane; `--pgm-tape-rows: 24` |

Rules: layout is fluid between breakpoints and **only column count changes at them**; nothing is added
above 1680 (a 4k user gets bigger gutters, not a 5th column); the ≥1280 and <640 layouts are the only
two *designs*, everything between is a reflow.

### D1.6 Motion

Easing and duration values are **quoted from** `skills/emilkowalski-skills/skills/review-animations/STANDARDS.md`
(not invented here): `ease-out cubic-bezier(0.23, 1, 0.32, 1)`, `ease-in-out cubic-bezier(0.77, 0, 0.175, 1)`,
`ease-drawer cubic-bezier(0.32, 0.72, 0, 1)`. Decision order: entering/exiting → `ease-out`; moving or
morphing on screen → `ease-in-out`; hover/colour → `ease`; constant motion → `linear`; default `ease-out`.
`ease-in` on UI is banned. `transition: all` is banned — list properties.

| token | ms | range | for |
|---|---|---|---|
| `dur-press` | 100 | 100–160 | `:active scale(0.97–0.98)` on any pressable |
| `dur-micro` | 80 | 80–120 | flash-in, tooltip after the first |
| `dur-small` | 125 | 125–200 | popover, chip toggle |
| `dur-medium` | 150 | 150–250 | dropdown, select, disclosure, tab indicator slide |
| `dur-large` | 200 | 200–300 | modal, sheet settle |
| `dur-drawer` | 300 | 300–450 | drag-to-dismiss, `ease-drawer`, springs — the only surface past the ceiling, and only because it is gesture-driven and interruptible |

`ui_ceiling = 300ms` for anything on a data surface. Never `scale(0)`: start 0.9–0.97 with `opacity: 0`.
Popovers use **origin-aware animation** (`transform-origin` from the trigger); modals are exempt and stay
 centred. Spring config for drag only: `{type:'spring', duration:0.5, bounce:0.2}`, bounce 0.1–0.3.

**Vocabulary** (terms from `animation-vocabulary/SKILL.md`; do not coin synonyms in specs or comments):
used — *Stagger*, *Origin-aware animation*, *Crossfade*, *Clip-path*, *Rubber-banding*.
Banned as product design — *Morph*, *Shared element transition*, *Layout animation*, *Pop in*, *Bounce*:
each means the same pixels briefly represent two prices, which in a trading UI is a data-integrity bug.

**Motion decisions were filtered through `find-animation-opportunities` (its Gate: frequency → purpose →
interruptibility → cost). Rejected, with the answer that killed each:**

| candidate | freq. | verdict |
|---|---|---|
| stagger-in Explore cards | every nav | **reject** — data, not marketing; adds perceived latency |
| count-up on portfolio value | every refresh | **reject** — violates `number_policy`, and hides *that* a number changed |
| tape row slide-in from right | 20+/sec | **reject** — 20 concurrent animations, and it destroys the vertical scan |
| depth-chart path morph per tick | 20+/sec | **reject** — re-rendering an interpolated path at 20Hz is how mid-range Android drops frames. Data appends; no interpolation |
| hover lift on rows | tens/day | **reject** — transform shifts 1px and reads as jitter at 22px rows; background only |
| skeleton shimmer | every load | **reject the motion, keep the state** — linear motion on a surface whose message is "data is NOT arriving"; static opacity instead |

Accepted: sheet/drawer enter+exit, **modal/dialog enter+exit**, popover open, toast, command palette, tab
indicator, copy-confirmation.

**Where that acceptance list stood for four phases (added by the motion pass in `plans/animation-audit.md`).** The
list above is a *permission*, and three of its entries had no implementation on the desktop shell: the dialog
appeared from nothing, the toast stack jumped, and the tab indicator swapped a `box-shadow` with no transition while
`duration_ms.medium`'s own text promised a slide. The permission was written in P03 and shipped in P12 only inside
the Mini App block — every motion consumer in the product was in the webview. The audit that found this also found
two rules that contradicted the ladder (a `translateY` press where `brand/tokens.json` says
`:active scale(0.97–0.98)`, and `--pgm-dur-micro` with zero consumers), and all of it is fixed in
`tools/build-tokens.mjs` + `web/src/globals.css` with the values read from the tokens rather than typed.

### D1.7 Layers

`dropdown 1000` · `sticky_header 1100` · `drawer_scrim 1200` · `drawer 1300` · `modal 1400` ·
`trade_ticket_sheet 1450` · `toast 1500`. Only these seven exist; needing an eighth is a layout bug.

---

## D2. Component library

### D2.0 The state model (applies to everything)

The prompt lists 8 states. A trading terminal needs **11**, because three of them are the difference
between an order going out and one going out by accident. Every component below declares its behaviour
for all eleven; "inherits D2.0" in the table means the row is identical to this contract.

| # | state | trigger | visual | interaction |
|---|---|---|---|---|
| 1 | `default` | — | tokens only | — |
| 2 | `hover` | pointer over, **pointer-capable devices only** | `bg.elevated` fill, no transform, no elevation change | — |
| 3 | `active` | pressed | `scale(0.97)` 100ms `ease-out`; `bg.inset` | — |
| 4 | `focus-visible` | keyboard/focus-visible | 2px ring `border.strong`, offset 2px, **never `outline:none` without a replacement** | — |
| 5 | `disabled` | not now | 45% opacity, `text.muted`, cursor `not-allowed` | not focus-reachable via keyboard **but must remain discoverable** → use `aria-disabled` + kept in tab order for destructive/monetary actions so a screen-reader user learns *why* it is off |
| 6 | `loading` | request in flight | skeleton (static opacity, no shimmer) or `Spinner` ≤ 14px; label text preserved so width doesn't jump | repeats ignored (single-flight) |
| 7 | `error` | 4xx/5xx/throw | `alert.critical` 1px border + message in `text.primary`, never colour alone (icon + words) | retry offered where idempotent |
| 8 | `empty` | 0 results, service healthy | `EmptyState` — what is missing + the one action that fills it | action, not a shrug |
| 9 | **`stale`** | age > freshness threshold (D5) | `StaleIndicator` attached to the figure; value stays visible, desaturates to `text.secondary`, timestamp surfaces | **non-trading surfaces keep working; anything that sends an order is blocked** |
| 10 | **`disconnected`** | WS down / `clob.polymarket.com/ok` failing | top-bar badge + `alert.watch` hairline across the data region; last-known values annotated "as of {time}" | **all order submission disabled app-wide** (D5.4) |
| 11 | **`insufficient`** | balance < min-order or < ticket total | inline, next to the offending field, with the exact shortfall in USD | submit disabled; the *fill-the-gap* action is offered, not just the error |

Sizes are `xs 11 / sm 12 / md 13 / lg 16` px font with paired row heights from D1.4; controls are
`sm 24 / md 28 / lg 32 / xl 40` px tall and **the touch breakpoint floors any control at 44px**
regardless of declared size. Radius always from `--pgm-radius-*`, chips are pills (`999px`).

### D2.1 Primitives (31)

`a11y` is the *additional* requirement beyond correct semantics; a `<button>` that is a `<button>` and a
`<label>` that is a `<label>` are assumed, not listed.

| component | anatomy | variants | states beyond D2.0 | sizes | do / don't | a11y |
|---|---|---|---|---|---|---|
| `Button` | label, optional leading icon, trailing slot (kbd hint / spinner) | `primary` (brand.primary-text), `secondary` (border.hair), `ghost`, `danger`, `buy`, `sell` | — | sm/md/lg | **do** keep label ≥4 words for money actions ("Buy 12 YES"); **don't** use `brand.primary` (#2e5cff, 3.76:1) for text | `aria-keyshortcuts` on every shortcut-bearing button; disabled money buttons need `aria-describedby` → the reason |
| `IconButton` | icon + 44px hit area, label is `sr-only` | as Button | — | 24/28/32/40 | **do** render the name on hover as Tooltip; **don't** put two adjacent without a divider | accessible name mandatory; `aria-pressed` for toggles |
| `Icon` | 16px box, 1.5px stroke, currentColor | `size`, `tone` | — | 12/14/16/20 | **do** set `focusable=false aria-hidden` when decorative; **don't** encode a state only in an icon without a name | `aria-hidden` + `focusable=false` when decorative; a **non**-decorative icon needs an `aria-label`, and the 1-bit legibility at 12px on `bg.base` is verified via the P02 contrast table |
| `Input` | label, field, helper, inline error | `text`, `search`, `password`, `number` (→ `NumberInput`) | `insufficient`, `stale` (read-only styling) | sm/md/lg | **do** keep helper text in the `aria-describedby` chain; **don't** shrink label into placeholder | every field needs a real `<label for>` (placeholder is never the label); never `type=number` for money (scroll-wheel value theft) |
| `NumberInput` | value, unit suffix, stepper pair, step hint | `price` (tick-aware), `size` (shares), `usd` | `insufficient` | md default | **do** respect `minimum_tick_size` and clamp; **don't** accept a float string — parse to integer cents/shares in the money path | steppers are real buttons (keyboard repeat works); `role=spinbutton` with `aria-valuetext` reading the unit aloud |
| `Select` | trigger, menu, optional search | `native` (≤7 options), `menu` | — | sm/md/lg | **do** use native below 8 options; **don't** animate height (animate opacity + `Clip-path` only) | listbox pattern with typeahead; `aria-expanded`/`aria-controls` |
| `Checkbox` | 16px box, 2px radius, check glyph | `default`, `indeterminate` | — | 16 | **do** put label in `<label>`; **don't** use for binary trade side — that's a `SegmentedControl` | space toggles; indeterminate announced as "mixed" |
| `Toggle` | 32×18 track, 14 knob, optional inline label | `on/off` | `stale` (a setting fed by stale state is not actionable) | 32/40 | **do** apply instantly (no Save); **don't** animate track width, translate the knob only | `role=switch` + `aria-checked`; label not "On"/"Off" alone |
| `Slider` | track, fill, handle, value bubble | `single`, `range`, `stepped` | `insufficient` (fill turns `alert.critical` past the cap) | 24 | **do** snap to the token steps; **don't** make the handle the only size affordance — the numeric field is the source | `aria-valuenow/text/min/max`; arrow keys ±1 step, PageUp/Down ±10 |
| `Tooltip` | bubble, arrow, optional kbd line | `info`, `warn`, `danger` | — | sm | **do** skip delay+animation after the first in a group; **don't** put interactive content in one | `role=tooltip`, `aria-describedby`; shows on focus as well as hover; never on touch-hold |
| `Badge` | text, optional glyph, optional count | `outcome.yes/no`, `action.buy/sell`, `alert.*`, `classification` (sniper/whale/bot/smart), `plan` | — | 11/12 | **do** glyph + word always (colour never alone); **don't** put `outcome.no` and `alert.critical` in one row — P02 rule, ΔE 4.1 dark / 3.2 light | `sr-only` expansion for glyph-only badges ("No, losing side") |
| `Tag` | removable chip, filter count | `filter`, `category`, `wallet` | `loading` on removal | 11/12 | **do** show what removing it costs ("× removes liquidity filter"); **don't** exceed 6 in a row — collapse to "+n" | remove button is a real focus target with the tag's name |
| `Avatar` | image, fallback initials, presence dot, size ring | `wallet` (identicon), `user` (upload), `bot` (glyph) | `stale` presence | 16/20/24/32/48 | **do** always pair with a name string; **don't** let the identicon be the only identity | decorative `alt=""` when adjacent name exists; else name is the accessible name |
| `Skeleton` | block/row/list presets | `text`, `row`, `chart` | — | matches target size | **do** match the real element's box exactly (zero layout shift); **don't** shimmer (D1.6) | `aria-hidden` + a live region announcing "loading {thing}" once |
| `Spinner` | 14px arc, 600ms linear | `xs–md` | — | 12/14/16 | **do** use only for <2s waits with known target; **don't** spin on the tape | `role=status` + text, not the spinner alone |
| `Progress` | track, fill, % label | `determinate`, `indeterminate`, `segments` (daily cap) | `error` | 4/8 | **do** show the absolute next to the % (cap = $, not %); **don't** animate width per tick | `role=progressbar` + `aria-valuetext="$1,240 of $5,000 daily cap"` |
| `Divider` | hairline, optional centre label | `h`, `v`, `section` | — | — | **do** separate data groups; **don't** use as row separators (that's the row's border) | `role=separator` + `aria-orientation` where focusable |
| `Modal` | panel, header, body, footer, scrim | `confirm`, `form`, `detail` | `disconnected` | 480/640/880 | **do** lock scroll, restore focus on close; **don't** animate `scale` from 0, from 0.96 | focus trap **with an escape hatch** (Esc always closes unless the action is in-flight), `aria-modal`, labelled by title |
| `Sheet` | grabber, header, content, footer CTA | `bottom` (mobile), `right` (desktop ticket) | `disconnected` | 40/72/100 % | **do** support drag-to-dismiss with velocity (spring); **don't** dismiss on drag if a ticket is mid-submit | Esc closes; grabber is focusable and acts as a button (Enter expands/collapses) |
| `Popover` | bubble, arrow, content | `info`, `menu`, `picker` | — | 240/320 | **do** animate from the trigger (origin-aware); **don't** render into a portal that loses `aria-labelledby` | focus returns to trigger; outside-click closes without trapping |
| `Menu` | items, groups, kbd hints, danger group last | `context`, `select`, `action` | — | item 28 | **do** keep kbd hints right-aligned in tabular numerals; **don't** nest deeper than one submenu | roving tabindex, Home/End, typeahead, `role=menuitem` |
| `Tabs` | list, indicator, panels | `underline` (2px strong), `segmented` | `stale` badge on a tab | sm/md | **do** keep panel heights stable; **don't** animate the indicator's width *and* the panel content | `role=tablist/tab/tabpanel`, arrow-key roving, `aria-controls`; panels lazy but `keepMounted` for data |
| `SegmentedControl` | track, segments, selection fill | `side` (BUY/SELL), `window` (1d/7d/30d/all), `view` (list/grid) | `disabled` per segment (e.g. `/pnl` window that doesn't exist upstream) | 24/28/32 | **do** use for 2–5 mutually exclusive views; **don't** use for 6+ → `Select` | `role=radiogroup` with `aria-checked`, arrow keys; selection announced with its meaning ("7 day window") |
| `Accordion` | header, chevron, disclosure | `settings`, `grouped` | — | — | **do** keep `height: auto` transitions off (animate opacity + `Clip-path`); **don't** nest accordions inside accordions | `aria-expanded` + `aria-controls`; content unmounted only when cheap |
| `Toast` | icon, title, body, action, timer | `info`, `success`, `warn`, `critical` | — | 320/420 | **do** keep `critical` sticky (no auto-dismiss) — a lost order notice must not vanish; **don't** stack >3 (collapse with count) | `role=status` for routine, `role=alert` for money-affecting; focusable action; pause on hover |
| `EmptyState` | glyph, title, why, primary action, secondary | `no-data`, `no-results`, `no-permission`, `no-connection` | — | — | **do** name the missing thing precisely ("no fills ≥ $2,000 in 24h"); **don't** use it for an error | title is the region's accessible name; action reachable by Tab, not mouse-only |
| `ErrorState` | glyph, what happened, upstream status, retry, support link | `inline`, `block`, `full` | — | — | **do** surface the endpoint + status code (engineers need it, users get a plain sentence first); **don't** leak secrets, tokens or full URLs with keys | `role=alert`; retry button receives focus if the error interrupted an action |
| `DataTable` | head, rows, cells, sticky cols, sort, row actions | `virtual` (>50 rows), `flat` | `stale` (per column), `partial` (per row) | density-driven | **do** right-align all numerics, tabular-nums always; **don't** sort a money column client-side without re-summing from integer cents | `<table>` semantics kept even when virtualised (announce "showing 40 of 315"); header `aria-sort`; row focusable with `aria-rowindex` |
| `VirtualList` | spacer, window, overscan | `uniform`, `variable` (needs measured cache) | — | — | **do** overscan 8 rows, cap DOM nodes (D5.3); **don't** animate entry (tape rows appear instantly) | scroll region gets `role=list`/`listitem` (or table), announced total count, and keyboard PageUp/Down wired |
| `Pagination`/`InfiniteScroll` | page buttons or sentinel + loaded count | `pages` (known total), `cursor` (Gamma's 100-row cap) | `loading` (append, keep old rows visible) | — | **do** use explicit paging for Explore (Gamma caps 100/page and `offset` works); **don't** infinite-scroll a dataset that needs a stable row count for comparison | sentinel has `role=status`; "loaded N of M" is in the DOM, not only visible |
| `CommandPalette` | input, grouped results, kbd footer, recents | `global`, `scoped` (in-market) | `disconnected` (order commands greyed **with the reason**) | 640 | **do** open instantly (no animation on the input itself); **don't** fuzzy-match prices | `role=combobox` + `listbox`; `aria-activedescendant`; Esc closes; ↑/↓ wraps at 25 results |

### D2.2 Domain components

These carry real data, so each also declares **where its numbers come from**. A field whose source is
not in the table below does not exist and must not be invented (this is what the gate checks).

#### `PriceCell`
The atomic unit of the terminal: one price, always legible, never lying about its age.

*Anatomy* `value` (tabular numerals, integer-cent derived string) · `caret` (▲/▼, 8px, direction only) ·
`flash` layer (background, behind the glyphs) · `stale` dot (when age > threshold) · optional `delta`
(“+1.2¢”). *Variants* `size` sm/md/lg (11/13/16px) · `align` left/right (right for grids) · `emphasis`
`quiet` (muted) / `normal` / `loud` (weight 700 for the price you are about to pay) · `decimals`
**derived, never assumed**: `minimum_tick_size = 0.001` → 3dp, `0.01` → 2dp, read per-market from
`clob.polymarket.com/markets/{condition_id}` → both values occur in the top-12 events today, so a fixed
2dp layout is wrong.

*States beyond D2.0* — `stale` is the whole point of this component: value desaturates to
`text.secondary`, `StaleIndicator` attaches, and if the cell belongs to a ticket the field goes read-only.
`disconnected` freezes the last value with an explicit “as of {HH:MM:SS}” and the caret stops flashing.
A flash is only emitted when the *displayed* precision actually changes: an update inside the rounding
window must not fire it (otherwise a 0.001 book fires 3× more than the user can read).

*Motion* flash = background only, `--pgm-flash-in` 90ms spike → `--pgm-flash-out` 200ms fade (290ms total, inside the 300ms ceiling),
`ease-out`; up = `--pgm-flash-bg-up` (`action.buy`), down = `--pgm-flash-bg-down` (`action.sell`).
No digit animation whatsoever (D1.6 `number_policy`). At most **one** flash per cell per 120ms
(coalesced, D5.3); direction shown is the net of the coalesced batch.

*a11y* the cell is a `<td>`/`<span>` with `aria-live="off"` — **never** `polite` on a 20Hz surface;
`aria-label` reads “63.5 cents, up 1.2 cents, updated 3 seconds ago”. Screen-reader users get the value
on demand, not pushed. `@media (prefers-reduced-motion: reduce)` → animation none.

*Source* `clob.polymarket.com/last-trade-price` · `clob.polymarket.com/spread` · WS
`last_trade_price` / `book` (D5.1) · backfill `data-api.polymarket.com/trades` (REST is Cloudflare-cached;
`cache-hit-stale` in the probe proves a poll cannot drive a live cell).

#### `YesNoPair`
Two sides of one contract, and the *relationship* is the information: `NO = 1 − YES`.

*Anatomy* YES cell (left, always left) · NO cell (right, always right) · gutter showing the
complement `1 − yes − no = residual` (fees/tick rounding) · optional `mid` marker line between them.
*Variants* `compact` (single row, “63.5 / 36.5”) · `stacked` (mobile, two rows) · `pair-loud`
(terminal header, 16px, `emphasis: loud` on the side the user is about to buy) · `event` (a
negRisk outcome row, 11px, no mid marker).
*Rules that make it correct* — fixed L/R slots so colour is redundant; each side carries the words
“YES”/“NO”; `outcome.yes #7ba5ff` / `outcome.no #D55E00`, **never** green/red. **The hue is the chip, not
the word:** the YES/NO label and both prices are `text.primary`, and the outcome hue appears on the chip's
1px outline and its tint behind the label. Reason, measured: `#D55E00` on `bg.base` light is 3.87:1 and on
`bg.elevated` dark 4.26:1, i.e. it fails the 4.5:1 body-text bar everywhere this UI renders text (the largest
text token is 13px, so WCAG's large-text exemption is never available), while as a border/tint it clears the
3:1 non-text bar. `component-colour-audit.py` enforces this by class. The pair is
`1 − yes ± residual` and if `residual > 1 tick` that is rendered as a *warning* (“book is 2 ticks wide —
yes+no ≠ $1”) because it means the sides are separately mispriced and a naive arbitrage display would
be a lie. When one side has no book at all, its cell renders `—` plus its `stale`-style reason, never
`0` (a 0 would read as “free YES”, i.e. infinite profit).
*a11y* the pair is a `role=group` labelled “Yes/No prices”; each cell announces price + side word; the
residual warning is `role=status` on change (politeness **off** while streaming, `polite` when the user
opens the market).

#### `OrderBook`
*Anatomy* header (spread row) · asks ladder (up) · best-price band (mid + spread in **ticks**) ·
bids ladder (down) · per-level depth bar behind price (**the only place an outcome hue appears in the
ladder** — level prices and sizes are `text.primary`, because `#D55E00` as text measures 3.87:1 light /
4.26:1 dark) · `aggregate` control · footer (total levels,
tick size, “click a level to fill the ticket”).
*Levels* `--pgm-row-book-level`: dense 16px, 10 levels/side desktop, 6 on mobile, 12 at 2xl; scroll
within the panel rather than growing the page.
*Spread must be shown in ticks, not cents.* Measured across 22 top books: median spread **71 ticks**,
max **998 ticks**, tick sizes `{0.001, 0.01}`. A 46%-of-mid spread (best bid `None` vs ask `0.999`)
is normal near resolution, not an error.
*Controls* aggregate by level ↔ by price step (default **by level**, because the deeper side had a
median 58 levels and max 137 — unaggregated it is wallpaper); side filter (both/bids/asks);
“hide dust < $N” (default $50, token-step slider); click-a-level → writes price+side into the ticket
and focuses the size field (never auto-submits).
*States* — all D2.0 plus the three that matter, specified in **D4a**: `near-empty side`,
`one-sided`, `locked` (0.999/0.001 no crossing). `loading` shows the ladder skeleton at final height;
`stale` greys the whole ladder and the click-to-fill affordance is **disabled** (you would be sending a
limit price into a book you cannot see); `disconnected` = frozen last snapshot + age, no click-to-fill.
*a11y* a real `<table>` with `aria-rowcount` = total levels; each level row’s `aria-label` =
“ask 64.0¢, 12,400 shares, $7,936, level 3 of 10”; click-to-fill levels are `<button>`s inside cells, not
click handlers on `<tr>`; the spread row is the region’s `aria-describedby`.

#### `DepthChart`
Cumulative size vs price, both sides, mirrored around mid; notional axis (USD, not shares) because a
$5 fill and a $50k fill are different worlds at the same share count.
*Rules* dark theme uses the adopted 8-hue chart array (worst ΔE\*ab 11.3), light uses 6 (13.4) and
series 7+ reuses hues at a different lightness **plus dash + marker** — never a 7th colour in light.
Legend text is `text.secondary` (≥4.5:1). No path animation between updates (D1.6). Empty/one-sided:
a flat side is drawn as a zero-height area with a caption, **not** as a missing series — the difference
between “no liquidity” and “no data” is the product.
*Source* `clob.polymarket.com/book` per `token_id` (both outcomes’ arrays), aggregated client-side.

#### `TapeRow`
One fill. Row height `--pgm-row-tape-row` 20px dense; ≤24 rows in the DOM (D5.3).
*Anatomy* `Avatar` 16 + `name` (from the trade row itself — `E5-trades-carry-identity` proves
`name/pseudonym/bio/profileImage` are embedded, so no profile join exists; `data-api/profile` 404s) ·
`classification` badge (sniper/whale/bot/smart, P01 rules) · `side` badge (buy/sell) ·
`outcome` chip (yes/no, worded) · `price` (`PriceCell` sm) · `size` (shares) · `usd` notional ·
`age` · `market link` (slug) · `whale flag` when notional ≥ the saved threshold.
*Rules* numerics right-aligned, tabular-nums, prices in the market's tick precision; `REDEEM`/
`CONVERT` rows render differently (their `price` is **0 on 100% of rows** and the payout is in
`usdcSize` — 287 rows across 10 wallets; showing “0.0¢” for a redeem is the single most misleading thing
this table could do, so it shows the payout and a “redeem” glyph instead of side+price).
New rows **append instantly**; no slide-in (rejected, D1.6). Flash on the notional only when the row is
new *and* ≥ the user's whale threshold, so the eye is drawn to money rather than noise.
*a11y* the container is `aria-live="off"` + `role=log` with `aria-relevant="additions"`; a separate
visually-hidden `role=status` announces “14 new fills, largest $3,100 buy YES — Fed decision” at most
once per 5s (see D6.2 for why `off` is the only survivable choice).

#### `TradeTicket`
The only component whose states can cost money; it is also the one that must go dark when data goes stale.
*Anatomy* side `SegmentedControl` (BUY/SELL) · outcome selector (`YesNoPair`, `pair-loud`) ·
`order type` (limit / market) · `NumberInput price` (tick-stepped) · `NumberInput size` (shares) +
quick-picks 25/50/75/100% of the *relevant* balance · `fee breakdown` (platform + builder, each line +
total) · `all-in` (market buys cap at total cost, not size) · `submit` (Button `buy`/`sell`, label
includes the outcome and USD amount) · `risk link` (one click to D3.20, always visible, never in a tooltip) ·
`StaleIndicator` (a ticket with a stale book cannot submit).
*Validation ladder* — evaluated in this order, first failure wins, each with its own inline message:
1. `accepting_orders == true` (Gamma/CLOB market object) → else “market is not accepting orders”.
2. `size ≥ minimum_order_size` (**5** measured) → else shortfall in USD, state `insufficient`.
3. price within `(0,1)` and on a `minimum_tick_size` multiple (**0.001 / 0.01** measured) → else “price must be a multiple of 0.001”.
4. balance/capacity from `data-api.polymarket.com/positions` + our own ledger → `insufficient`.
5. `seconds_delay` (0 measured) and any live risk-gate verdict → blocks with the reason, never silently.
6. confirm step above the user's threshold (default $250): modal restating side, outcome, size, all-in,
   fee and **the two prices it is being compared against** (best bid, best ask) so a market order is not
   a surprise. *Nothing in this component may compute with floats in the money path* — integer cents
   in, integer cents out (kit rule; `no-floats-in-money-path` in `tokens.json.rules`).
*States* `disabled` (no wallet) · `loading` (submit in flight, button keeps its width and shows a
14px spinner) · `error` (upstream rejection with the endpoint status) · `stale` + `disconnected`
(**hard block**, D5.4) · `insufficient` · `partial-fill` (post-submit: filled vs resting, with the
resting portion's cancel affordance inline).
*a11y* field order = visual order; submit is `aria-describedby` the all-in line; the risk link is in the
focus order *before* submit; confirm modal traps focus with an Esc escape hatch only when nothing is in
flight.

#### `PositionRow`
*Anatomy* `MarketCard`-lite (title + category) · `outcome` chip · `size` shares · `avg entry` ·
`mark` · `unrealised PnL` (signed, colour-coded by sign) · `pnl %` · `size of portfolio` bar ·
`quick-exit` (market-sell this position, pre-filled ticket).
The full colour-collision analysis and the deuteranopia test are in **D4b**; the summary rule:
**PnL carries a sign glyph and the words “profit”/“loss”; the outcome chip carries the word “Yes”/“No”;
and the two hues that collide (`outcome.no #D55E00` vs `alert.critical #ef4444`, ΔE 4.1 dark / 3.2 light)
are never in the same row** — enforced by `rules.never-same-row`, which is why the severity badge lives on
the *market* cell, not beside the PnL.
*Source* `data-api.polymarket.com/positions?user=` (size, avg, cur, cashPnl, percentPnl — reconciled
against our ledger) · `data-api/value?user=` for total portfolio value (free ground truth) ·
`clob.polymarket.com/spread` for mark. Never `price*size` for a redeem leg (D2.2 TapeRow).

#### `TraderCard`
avatar+name · 7D/30D PnL (**our estimate**, labelled as such — `lb-api/pnl` 404s and `/rank` returns 400
even with `rank` supplied) · win rate · volume (`lb-api/volume?window=`) · category specialisation
(computed from the trader's own `activity` rows) · max drawdown (ours) · classification badges ·
follow + copy-trading actions (both disabled while `disconnected`).
*Rules* every computed number shows its window and its source class (ours vs Polymarket's) — a P01
compliance requirement, not decoration. “Smart money” is never asserted without the underlying criterion
visible on hover.

#### `AlertRuleBuilder`
condition rows (`[field] [op] [threshold] [window]`, AND within a group, OR between ≤3 groups) ·
channel selection (in-app, Telegram, email — email requires verified address) · `test-alert` (fires the
rule against the last 24h of *stored* data and shows what it would have matched, including “0 matches —
this rule is too tight”, which is the answer users actually need) · saved views.
*Fields it may offer* are the ones the probe proves exist: price, spread-in-ticks, size, notional USD,
outcome, market volume24hr, liquidity, seconds-to-end, one-sided-ness, tick size. Anything else is
unsupported and must not appear in the picker.

#### `CopyConfigPanel`
target wallet (paste/QR/recent) · multiplier (0.1×–5×, stepped) · per-trade cap USD · daily cap USD
(`Progress` segments) · category filter (multi-`Tag`) · TP/SL (percent, tick-aware) · min/max trade size
filter (median fill is $5–6, so a 0 minimum floods the copier) · `confirm-with-consequences`: a modal
that types out the daily cap and states, in plain words, “this places real orders with your money without
asking you each time” + a live count of the target's last-24h fills the rule would have followed.
*States* `stale` (source wallet data older than threshold → pauses new copies, does not close positions) ·
`disconnected` (pauses) · `paused` (a real, visible state with a reason).

#### `WalletPanel`
balance (pUSD, integer cents) · deposit address + QR (address is monospace, copy-verified: the QR and
the string must match, checked by the component) · network selector · key export behind typed
confirmation · withdrawal behind password + email code.
*Rules* the address is never truncated in the export path (truncation is a display affordance only, and
the copy button copies the full string); key export shows a red `danger` Button and a 10s countdown
before the reveal; every money figure in this panel is `tabular-nums` and formatted from integer cents.
*No endpoint here is Polymarket's* — custody is ours; the panel's *reflected* balance reconciles against
`data-api.polymarket.com/value?user=` and a mismatch is an error state, never a silent rounding.

#### `MarketCard`
title · category · YES price (left slot) · 24h volume · liquidity · `ends in` countdown · sparkline
(48×16, from stored closes, no animation) · optional `negRisk` outcome count.
*Rules* countdown switches format at thresholds (7d → “3d”, 48h → “14h 02m”, 1h → “09:12”, <60s →
“settling”); the sparkline's axis is *never* auto-scaled silently — an unlabelled y-axis on a price chart
is a design bug, so the card shows min/max as micro-text or omits the chart at `xs`.
*Source* `gamma-api.polymarket.com/events?limit=…` / `…/markets?order=…` (`volume24hr`,
`liquidityNum`, `endDate`, `outcomes`, `clobTokenIds`, `negRisk`, `accepting_orders`,
`feeType` — all measured fields; feeType varies per market: `crypto_fees_v2`, `politics_fees`,
`sports_fees_v2`, `economics_fees`, `culture_fees`, `finance_prices_fees`, `sports_fees_v3`).

#### `StaleIndicator` — a safety component
*Anatomy* 6px dot or a `14px` inline label, `age` (”4s”, “2m”, “since 14:02”), severity from D5.2,
optional `retry`. Placement contract: top-left of whatever it stale-ifies; if a region has more than one
stale source it renders once, for the *worst*.
*Rules* it never hides the number — staleness is additive information, never a replacement; it appears
with no transition (a fade would mean the value was briefly *not* marked stale, which is the dangerous
half-second); `prefers-reduced-motion` is irrelevant because it does not move; colour is
`alert.watch` → `alert.high` → `alert.critical` with a **word** at each step
(“slightly old” / “stale” / “stale — trading off”).
*a11y* `role=status` when it first appears, then silent; must not re-announce on each age tick.

---

## D3. Screen specifications

Conventions for this section, so each screen stays short:
**fields** are listed as `name ← endpoint#path`. If a field is computed by us it says `(ours: how)` and,
where it looks like a Polymarket number, it must be labelled “our estimate” in the UI (P01 compliance).
**states** lists only what deviates from D2.0's 11-state contract. **mobile** is the `xs` adaptation.
Every screen renders `<title>` + `h1` + a breadcrumb on `xl`, and every one is reachable by keyboard from
the top bar. Auth-required screens render their shell (skeleton, not blank) before the auth check resolves
so the layout never jumps.

### 1. Landing / marketing — public, SEO
*purpose* convert on one screen: what it is, what it is not (non-custodial; not Polymarket), and the risk.
*route* `/`
*layout* `<1280` single column, 5 stacked sections (hero · terminal screenshot · 4 proof points · pricing ·
risk+FAQ). `≥1280` two columns: copy left 46%, product visual right 54% (the visual is a **real**
terminal render or an SVG mock composed from `brand/svg/*` — never an AI raster, per P02).
*fields* static copy; live market ticker strip (optional, 3 rows) ← `gamma-api/events?limit=…#markets[
volume24hr, question, bestBid/bestAsk via clob]`; sign-up CTA; pricing table ← our own plans (no endpoint).
*states* `loading` ticker omitted (never a skeleton hero); `error` ticker falls back to no strip — the page
must render with zero network after first paint (it is the SEO surface); `stale` ticker appends “(delayed)”
rather than disappearing; no `unauthenticated` state (public).
*seo* one `h1`, `article`/`FAQPage` JSON-LD, `og:image` = `brand/og-1200x630.png` — **blocked until the
1731×909 → 1200×630 derivative is regenerated and re-rendered with the current wordmark** (P02 addendum).
Do not ship the landing page pointing at the oversized card; do not re-render the mark with an image model.
*a11y* `skip to content`; all sections `aria-labelledby` their heading; contrast already AA via P02, and
the hero must not rely on `brand.primary` for body text (3.76:1, `rules.brand-primary-is-not-body-text`).
*mobile* everything single column, CTA sticky bottom 44px, ticker becomes one scrolling line
(`linear` motion is allowed here — marketing, not data).

### 2. Sign up
*purpose* account first, wallet second — explicitly, because a wallet before an account makes recovery impossible.
*route* `/signup`
*layout* centred card 480px (`Modal` shell, but routed). Three blocks in order: email+password → Telegram
OAuth → Google. Below the fold: “we create a wallet for you **after** this step” + a link to D3.20.
*fields* email, password (strength meter = `Progress`, thresholds from our own policy, not a lib default),
Telegram OAuth (`/telegram` widget callback), Google OAuth, terms checkbox (unchecked by default). No
seed-phrase entry — ever, on any screen.
*states* `error` per-field inline + one form-level summary `role=alert`; `loading` button keeps its label;
`disabled` submit until terms checked (with the reason, not silence); no `empty`/`stale` (no live data).
*keyboard* Enter in any field = submit when valid; `Tab` order = visual; `Esc` routes back to `/`.
*mobile* full-bleed card, no max-width, input font-size ≥16px (iOS zoom suppression).
*after* the wallet-creation step is its own screen (D3.22 step 2) — signup must not become a custody flow.

### 3. Sign in + 2FA + recovery
*route* `/signin`, `/signin/2fa`, `/recovery`
*layout* same 480px shell; 2FA is the *same* card with the second factor replacing the password (not a new
page — the user must not lose their place); recovery is a third step of the same shell.
*fields* email, password, TOTP `NumberInput` (6 discrete boxes, `inputmode=numeric`, **no** decimal
formatting), backup-code link, “email me a sign-in link”. Recovery: email + a signed link, then password
reset; key export is *not* part of recovery (documented in D3.17).
*states* `error` on bad TOTP must not say which factor failed beyond “code not accepted”; rate-limit
`error` shows the retry countdown as text, not a disabled spinner; `loading`; `stale` n/a.
*a11y* TOTP boxes are one `role=combobox`-style group with `aria-label` per box and paste support (paste
of 6 digits distributes); focus moves to the first empty box.
*mobile* same; passkey/Touch-ID entry point when `window.PublicKeyCredential` exists.

### 4. Markets (discovery home)
*purpose* the front door; must be useful on a 3G phone in one second.
*route* `/markets`, filters are query params (`?cat=politics&minliq=50000&sort=vol24&end=48h&q=fed`)
*layout* `xs` list + a filter `Sheet`. `md` filter rail (240px) + list. `xl` filter rail + list + a right
preview panel (hover/selection shows the `MarketCard` enlarged + top-of-book).
*fields* per row: `question ← gamma/events?limit=…#markets[].question` · `category ← …#events[].tags` ·
`yes price ← clob/spread#mid` (per selected `token_id`, batched ≤20 rows) · `volume24hr` ·
`liquidityNum` · `endDate` · `negRisk` + outcome count · `accepting_orders` · `feeType`.
Sorting: `volume24hr`, `liquidityNum`, `endDate`, `createdAt` (`new`), and **(ours)** 5-min volatility
from our own tape store — never labelled as a Polymarket metric.
Pagination is **explicit**: Gamma caps at 100 rows/page (`E1-gamma-row-cap` measured) and `offset` works.
Default `limit=100`, page buttons show “100 of ~N”; infinite scroll is disallowed here because the row
count is a comparison surface.
*states* `loading` 8 skeleton rows at final height; `empty` when filters exclude everything → names the
filter that did it, one-click remove; `error` upstream non-200 → retry + the endpoint; `partial` when the
CLOB price batch fails for some rows (price cell shows `—` + `stale` dot, row still renders);
`stale` whole-list banner if >30s; `unauthenticated` — list fully readable, prices visible, ticket CTA says
“sign in to trade” (never a login wall on read data).
*keyboard* `/` focuses search; `j/k` rows; `Enter` opens; `f` filters; `s` cycles sort; `?` shortcut sheet.
*mobile* list only, filter button in the top bar with an active-count badge, 44px rows.

### 5. Event detail (multi-outcome)
*purpose* read an event with N outcomes without losing the money picture.
*route* `/event/{slug}`
*layout* `xs` outcome list (one column, each row: outcome, yes price, vol, liquidity). `md` list + sticky
summary. `xl` list **left** (up to `--pgm-tape-rows` tall, virtualised) + selected outcome's terminal
preview **right** (book top-5 + `YesNoPair` loud + ticket shortcut).
*Hard case* outcome count. The prompt's example (Republican Nominee 2028, 128 markets) verified
**exactly 128 with `enableNegRisk=true`** — but the real top-100 maximum is **315** (`nfl-det-buf-2026-09-18`),
median 17. So: virtualise from 24 rows, paginate the remainder, and the header states
“315 outcomes · showing 24 · sorted by volume” rather than rendering a 315-row DOM.
*fields* `title/description/slug/tags ← gamma/events?slug=…` · per outcome
`question, outcomes, clobTokenIds, volume24hr, liquidityNum, endDate, negRisk, accepting_orders,
bestBid/bestAsk ← same object` · `yes price ← clob/spread#bid/ask/mid` ·
`order-gate fields ← clob/markets/{condition_id}#minimum_order_size, minimum_tick_size, accepting_orders,
seconds_delay` · `total event volume (ours: Σ volume24hr of markets)` — labelled ours.
For negRisk, the sum of YES prices is *not* $1 across 128 outcomes; that must be stated, not implied,
because “buy the underpriced side” is the classic wrong read.
*states* `loading` shell + first 24 rows skeleton; `empty` (event with 0 open markets → “all outcomes
settled” + a results view, not an empty state); `error`; `partial` (some outcomes have no book → per-row
`—` + reason); `stale` (>30s on the price batch); `unauthenticated` read-only, identical to D3.4.
*keyboard* `j/k` outcomes; `Enter` opens the market terminal; `[` `]` prev/next outcome; `t` toggles
table/graph.
*mobile* list only; tapping an outcome opens the market sheet with a “open terminal” link.

### 6. Market detail / terminal ★ the product
*purpose* see, decide, act in under two seconds.
*route* `/market/{condition_id}?tab=tape`
*layout (≥1280)* three columns: left rail 240 (watchlist + this event's outcomes) · centre
(`DepthChart` 320px tall + tabbed panel: Tape / Book / Orders / Positions / Info) · right rail 320
(`MarketCard` + `YesNoPair` loud + `TradeTicket` + `StaleIndicator`). Below 1280 the right rail becomes a
bottom sheet opened by a sticky “Trade 63.5¢ YES” bar; below 768 the chart and book are tabs.
*fields* `question, description, outcomes, clobTokenIds, endDate, category/tags, volume24hr, liquidityNum,
negRisk, accepting_orders, feeType ← gamma/markets?order=… (also /events?slug=…)` ·
`bid/ask/mid/spread ← clob/spread` · `last price ← clob/last-trade-price` · `book ladder ← clob/book?token_id=…`
(+ WS `book`, `price_change`, `last_trade_price`, `tick_size_change`) · `tape ← WS last_trade_price; backfill
data-api/trades (REST is Cloudflare-cached — poll only for history)` · `tick/size limits ←
clob/markets/{condition_id}` · `positions/PMNs (ours)`
*states* every one of the 11 applies, and the terminal is the reference implementation for `stale`,
`disconnected` and `partial`. `unauthenticated` — full read access, ticket shows “sign in to trade” where
the submit button would be. `loading` — panel order matters: prices before chart before tape (the user's
first question is “what is it now?”).
*keyboard* `b` buy / `s` sell (side toggle), `1`–`9` quick-pick size %, `Enter` submit (only when focus is
inside the ticket), `Esc` dismiss sheet, `.` toggles the book's aggregate mode, `,` opens settings,
`r` retry stale source, `p` pins the ticket. Global shortcuts are suppressed while typing in a field.
*mobile* one panel at a time; the trade bar is permanent; book/tape alternate via `Tabs`; DepthChart is
optional and off by default on `xs` (it is the most expensive thing on a mid-range Android).

### 7. Live tape (full page)
*route* `/tape?min=2000&side=buy&cat=crypto&cls=whale&w=0xabc…`
*layout* filter bar (44px) + full-width `DataTable` of `TapeRow`, virtualised, `--pgm-tape-rows` budget on
`xl`; a right 320 “inspector” panel opens on row click (wallet → `TraderCard` + that wallet's last 20 fills).
*fields* per row: `proxyWallet, side, outcome, price, size, usdcSize, timestamp, title, eventSlug, slug,
name, pseudonym, profileImage, icon ← data-api/trades (identity is embedded; data-api/profile 404s)` ·
`usd notional = size×price (ours, integer cents)` · `age (ours)` · `classification (ours)` ·
`whale flag (ours: notional ≥ threshold, rate-capped — median fill $5–6 and 0.2–1.2% of fills ≥ $1k, so a
$1k threshold fires 5–15/min; default threshold $2,000 with a per-user cap of 6 alerts/min)`.
*states* `loading` 10 rows; `empty` “no fills match these filters in the last 15 min — loosen the size
filter” (with the button); `error` WS down → `disconnected` (frozen + age + reconnect countdown, **not**
an empty table); `partial` REST backfill still arriving (rows append, marked “backfill”);
`stale` per D5.2. Filter edits never blank the table (results swap in place).
*keyboard* `space` pause/resume (pausing is a real state: buffer keeps filling, `paused — 128 new`);
`↑/↓` rows; `Enter` open inspector; `m` cycle min-size; `x` clear filters.
*mobile* 20px rows, 6 visible, filters behind a sheet, inspector becomes a full sheet.

### 8. Traders / leaderboard
*route* `/traders?window=7d&sort=vol`
*layout* window `SegmentedControl` (1d/7d/30d/all — exactly these, per `lb-api/volume?window=`
support; there is no other window) + `DataTable`: rank, avatar, name, volume, PnL (ours), win rate (ours),
trades (ours), followed-by-you. `xl` adds a right `TraderCard` preview on hover.
*fields* `name, amount ← lb-api/volume?window=…` · `name, amount ← lb-api/profit?window=…` ·
`pnl, winrate, drawdown (ours: computed from data-api/activity + positions; labelled “our estimate” —
lb-api/pnl 404s and lb-api/rank returns 400 even when rank is supplied, so neither is usable)`.
*states* `loading`, `empty` (window with no rows), `error`, `partial` (PnL column unavailable for a wallet
whose activity fetch failed → “—” + `stale` dot, never 0), `stale` (leaderboards are polled 60s, so
staleness is the normal state: the header says “hourly” instead of alarming anyone).
*keyboard* `j/k`, `Enter` profile, `w` cycle window, `f` follow.
*mobile* 3 columns only (rank, name+avatar, volume); PnL hidden behind a tap.

### 9. Trader profile (gmgn-equivalent dossier)
*route* `/trader/{address}`
*layout* header card (`TraderCard` full) · PnL curve (ours, stored) · open positions table · recent fills
(filterable) · category breakdown (ours) · suspicious-behaviour panel · follow/copy CTAs.
*fields* `data-api/trades?user=` (identity, fills) · `data-api/positions?user=` (size, avg, cur, cashPnl,
percentPnl) · `data-api/activity?user=` (TRADE/REDEEM/… — REDEEM payout read from `usdcSize`) ·
`data-api/value?user=` (current portfolio value) · `data-api/traded?user=` (lifetime volume, reconciliation
ground truth) · `lb-api/volume|profit?window=` (only for the leaderboard's own numbers) ·
`(ours)` win rate, max drawdown, holding-time distribution, category specialisation, “front-runs
resolution”, “fills within 1 tick of book extremes”, “bot cadence” (inter-fill interval variance).
*Rules* the suspicious-behaviour panel is **metrics, not accusations**: every badge names its threshold and
its window, and there is no “scam”/“fraud” wording anywhere. PnL for a REDEEM leg must never be computed as
`price×size` (price is 0 on 100% of REDEEM rows).
*states* `loading` header first; `empty` (new wallet: “first fill recorded {date}”); `error`;
`partial` (positions OK but activity fetch failed → curve hidden with a reason); `stale` (profile data is
polled 60–300s — must say so next to “today”); `unauthenticated` fully readable, CTAs become “sign in”.
*keyboard* `f` follow, `c` copy-config, `j/k` fills, `1/7/3/a` windows.
*mobile* stacked sections, curve collapses to a 48px sparkline, table → cards.

### 10. Wallet Radar (multi-market intersection scanner)
*purpose* find the wallet that is simultaneously positioned across related markets (the actual edge in
prediction markets: one outcome set, many framings).
*route* `/radar?m=cond_a&cond_b&min=1000&depth=2h`
*layout* market picker (2–6 markets, each a `MarketCard`-sm with a remove `Tag`) + result `DataTable`
(address, matched markets, net exposure per side, aggregate USD, first/last seen) + a right panel for the
selected wallet's per-market breakdown.
*fields* per market `clobTokenIds, outcomes ← gamma/markets?order=…` · per wallet
`size, avgPrice, asset ← data-api/positions?user=` **for each market's token**, intersected client-side ·
`data-api/trades?market=…` for the fill stream of a market · `(ours)` net exposure = Σ signed notional in
integer cents; “matched on 4/4 markets you selected”.
*Note* there is no endpoint that answers “who holds both A and B”; this screen is our scan, so it has a
cost and a rate. Specify it: ≤6 markets, ≤2h lookback, cached 15 min, and the scan's progress is a real
`Progress` with counts, never an idle spinner.
*states* `loading` = scanning progress; `empty` “no wallet holds ≥$1,000 on 4/4 in 2h — widen lookback”
(with the control); `error` (upstream rate limit mid-scan → resumes from the last offset);
`stale` (cached result older than 15 min → “rescan” button); no trading from this screen (link out).
*mobile* market picker stacks; results become cards; scan runs are not startable on `xs` (battery/data).

### 11. Whale tracker
*route* `/whales?min=2000&window=15m&saved=true`
*layout* threshold + window controls (44px bar) · live feed of `TapeRow` (dense, 20px) · saved-views rail
(`Tag`s) · per-wallet aggregation toggle (“collapse to net $ per wallet per 5 min”, default **on** —
at 14.7–33.3 fills/sec with 0.2–1.2% ≥ $1k, an uncollapsed feed is 5–15 alerts/minute, which is noise).
*fields* `data-api/trades?limit=…` (backfill) + WS `last_trade_price` (live) · `(ours)` aggregation
bucket, whale flag, net-per-wallet · `TraderCard` lite on the wallet.
*states* `disconnected` freezes with age + “reconnecting in 4s” (D5.4) · `stale` · `empty` (threshold too
high → “raise the tape window; nothing ≥ $50k in 15 min”) · `error` · `loading` 6 rows.
*keyboard* `m` threshold stepper, `w` window, `c` toggle collapse, `space` pause.
*mobile* collapsed-by-default, 6 rows, pull-to-refresh for backfill only (the live path is WS).

### 12. Portfolio
*route* `/portfolio?tab=positions|orders|history`
*layout* value header (48px number + 24h delta + `StaleIndicator`) · PnL curve (`DepthChart`-class chart,
320px) · `DataTable` of `PositionRow` · orders tab (open/resting/filled/cancelled) · history tab
(fills + redeems + fees) · export button.
*fields* `size, avgPrice, curPrice, cashPnl, percentPnl ← data-api/positions?user=` ·
`portfolio value ← data-api/value?user=` · `lifetime volume ← data-api/traded?user=` ·
`fills, redeems, fees ← data-api/activity?user=` (REDEEM payout from `usdcSize`) ·
`mark prices ← clob/spread` · `(ours)` PnL curve series, realized cost basis, fee totals, and the
**reconciliation** row: our ledger vs `value`/`traded` — a mismatch > 1¢ is an `error` state, not a note.
*rules* never display a PnL that mixes our basis with Polymarket's `cashPnl` without saying which is which;
export is CSV with the same integer-cent strings the UI shows (no re-formatting through a float).
*states* `unauthenticated` → `EmptyState` “sign in to see positions” (this screen has no public view);
`empty` (no positions yet → “your first trade will appear here; you can also import your Polymarket history
by connecting your address” — that CTA is the address field, since `positions` is public per address) ·
`loading` · `error` · `partial` (curve unavailable, positions fine) · `stale` (marks polled 15s).
*keyboard* `j/k` rows; `e` export; `t` toggle tab; `x` close a position (opens a pre-filled ticket, never
instant-sells).
*mobile* value header, then tabbed list; chart 48px sparkline; export moves into a sheet.

### 13. Copy trading (discovery + config + monitor)
*routes* `/copy`, `/copy/{trader}/configure`, `/copy/{trader}/monitor`
*layout* discovery = `TraderCard` grid (3-up `xl`, 1-up `xs`), pre-filtered to wallets we can actually copy
(≥20 fills/30d) · config = `CopyConfigPanel` in a 640 card · monitor = status header + followed wallet's
tape + your mirrored positions + a **big pause button** (the most important control on the screen).
*fields* discovery `lb-api/volume?window=`, `lb-api/profit?window=` + our metrics (win rate, drawdown) ·
config our own stored settings (POST to our API) · monitor `data-api/trades?user={target}` for the source
fills (WS for live) and our ledger for your copies · `data-api/positions?user=` for both, side by side.
*rules* the monitor shows *latency* per copy (“source filled 3.2s ago; you filled at 63.1¢ vs 63.4¢”) —
copy trading without slippage visibility is a lie by omission. Every row states whether the copy was
skipped and why (cap, filter, price band, disconnected).
*states* `unauthenticated` · `empty` (no traders pass the filter → show the leaderboard with a note) ·
`error` · `partial` (source feed down, your positions still live → banner, and **copies pause**) ·
`stale` (>5s of source silence while its market is still trading = a missed-fill risk, so this is
`alert.high`, not `watch`) · `disconnected` (all copies paused, visibly).
*keyboard* `p` pause all; `space` pause one; `c` configure; `j/k`.
*mobile* cards stack; monitor's pause becomes the sticky bottom bar in place of the trade bar.

### 14. Automation (rules + builder + history)
*routes* `/automation`, `/automation/new`, `/automation/{id}/runs`
*layout* rule list (`DataTable`: name, trigger, action, status, last run, hit-rate) · builder (`Sheet` on
`xs`, right panel ≥`md`) · run history (per-run row: condition snapshot, decision, order result, latency,
error).
*fields* our own stored rules + runs (our API) with the market inputs each rule reads
(`clob/spread`, `clob/book?token_id=`, `gamma/markets?order=` `volume24hr`/`liquidityNum`/`endDate`,
`data-api/trades?market=`) — the builder only offers those fields, because nothing else is readable.
*rules* a rule that submits a real order must show a `risk` strip: max size, daily cap, and “this can spend
$X without asking” with a confirm-each-time toggle that defaults **on**. Run history is append-only and
exportable, because the user's first question after a bad fill is “what did it see?”
*states* `unauthenticated` · `empty` (no rules → one-click two templates, both conservative) ·
`loading` · `error` (a rule that failed to run 3× shows `error` inline with the upstream status and is
auto-paused — a silently retrying bot is worse) · `partial` (a rule's data source stale → rule is marked
`stale` and skipped, logged as skipped) · `disconnected` (all rules pause; the list says so).
*keyboard* `n` new rule; `p` pause; `j/k`; `Enter` edit.
*mobile* list + full-screen builder; the run-history diff is text, not a table.

### 15. Alerts (rules, history, channels)
*route* `/alerts?tab=rules|history|channels`
*layout* like D3.14 with `AlertRuleBuilder` inline; channels tab = Telegram / in-app / email (verified) with
a per-channel rate and a `test-alert` button each.
*fields* our stored rules + `data-api/trades?user=` (wallet alerts) / `gamma/markets?order=` (market
alerts) / `clob/spread` (price alerts) · `channel status` (our own delivery ledger: sent/queued/failed).
*rules* the same rate reality as whales: a $1k tape threshold fires 5–15/min, so any rule with a tape
trigger gets a mandatory **per-window cap** in the builder, prefilled, and the UI states the expected fire
rate for the current settings (“~2–6/day on today's volume”) — a real number computed from our stored tape,
not marketing copy.
*states* as D3.14 plus `empty` on history (“nothing has fired — this rule is tighter than the market”).
*keyboard* `n`, `t` test, `j/k`.
*mobile* rules list, builder in a sheet; delivery log as plain text rows.

### 16. Profile & settings
*route* `/settings?tab=account|security|api|notifications|display|referral`
*layout* left nav (tabs, `xs` = `SegmentedControl` scroll) + right form column, 640 max width, single
column, generous `comfortable` density (settings are not a trading surface).
*fields* account (email, Telegram handle link state, timezone, currency display) · security (password,
TOTP enrol, backup codes, sessions, “export keys” link → D3.17) · API keys (create/revoke, last-used,
scopes) · notifications (per-channel toggles + quiet hours) · **display (density mode, theme, tape row
budget, whale threshold, price precision override)** · referral (code, link, invited count, builder share —
P01's revenue split must be shown in plain words, including that it is 0% where the market's own builder
fee is 0).
*rules* `density` and `theme` apply **instantly** (no Save) and are previewed live on a sample `PriceCell` +
`TapeRow` inside the settings card — the only way a user can choose between dense and compact meaningfully.
`prefers-reduced-motion` is honoured and the UI says so (“system setting” with no override, or an explicit
override labelled as such).
*states* `unauthenticated` redirect with a return path (this screen has no read-only mode) · `error` per
save with retry · `loading` per-field, not full-page · `stale` never (no live data) · `empty` for API keys.
*keyboard* `⌘/Ctrl+S` saves the focused tab; unsaved changes block navigation with a confirm.
*mobile* one tab full-screen with a back arrow; instant-apply controls stay 44px.

### 17. Wallet / deposit / withdraw / key export
*route* `/wallet?tab=overview|deposit|withdraw|keys`
*layout* balance header (48px, `tabular-nums`, integer cents) · actions row (deposit / withdraw / send) ·
recent movements table · per-tab panel.
*fields* balance and movements from **our** custody/ledger API (no Polymarket endpoint exists for our
wallet) · `data-api/value?user=` is shown as “reflected on Polymarket” for reconciliation · network from our
chain registry · deposit address + QR (our generated data).
*rules* the address is never truncated in the copy path (display may truncate; the copy button copies the
full string and the QR must match the string, verified in-component); every withdrawal is
amount → address → password → email code → confirm, each step restating the amount; **key export** requires
a typed confirmation string *and* shows a red danger button *and* logs the event to the security tab; any
UI text that describes custody must be one of the approved strings (non-custodial-adjacent wording is
forbidden — P01 legal section), and a `link to D3.20` sits in the header of this screen permanently.
*states* `loading` skeleton at final width · `error` (chain RPC down: “we can see your balance as of {time},
but cannot broadcast — retry”) · `stale` (RPC head behind by >3 blocks → `alert.high` + disable withdraw,
keep deposit open) · `disconnected` (n/a — this screen is our API) · `empty` (zero balance: “deposit to
trade”; never a scary empty state for a new account).
*keyboard* `d` deposit, `w` withdraw (opens a confirm), `Esc` closes sheets.
*mobile* QR is the primary deposit UI; address copy is one button; no seed phrase is ever shown on screen
by default (reveal requires the typed confirmation).

### 18. Billing
*route* `/billing?tab=plan|invoices|telegram`
*layout* plan comparison (3 columns, current plan highlighted, feature rows with ✓/✗ **plus words**),
Stripe checkout embedded, Telegram Stars path as a separate card, invoice table.
*fields* our plans (static) · Stripe session state (our API) · invoices (our API: number, date, amount,
status, PDF link) · Stars balance (Telegram payment API).
*rules* the annual/monthly toggle shows the effective monthly price *and* the annual total (no
“only $9/mo” with a hidden $108). The downgrade path states what is lost (rule count, tape history depth,
builder share) in the same screen, not in a support article.
*states* `unauthenticated` → full plan table is public, checkout CTA prompts sign-in · `loading` skeleton
rows for invoices · `error` (Stripe decline reasons surfaced verbatim but sanitised of card digits) ·
`empty` invoices (“your first invoice appears here”) · `stale` never.
*mobile* plan cards stack, current plan first, sticky checkout bar.

### 19. Referrals
*route* `/referrals`
*layout* link + code card (copy, share to Telegram) · invited list (handle, joined, first trade, volume,
your earned share) · share-of-revenue explainer.
*fields* our own referral ledger · earned share per invitee's trading volume (ours) · builder-share
context, because P01's compliance rule caps what we keep: the explainer shows the actual split of a
market's builder fee, and shows 0% honestly where the market's builder fee is 0.
*rules* never promise earnings; show the current month's real number, even if it is $0.00, with the
formula next to it.
*states* `unauthenticated` sign-in card only · `loading` · `empty` (“no one has used your link yet” + the
share button) · `error` · `stale` (earnings compute hourly → shown as “as of {time}”).
*mobile* stacked cards; the share button uses the Web Share API when available.

### 20. Risk disclosure & terms
*route* `/risk`, `/terms`, `/privacy`
*layout* single column, 720 max, `comfortable` density, 15px text, no data components at all. Contents:
what a prediction market is · total-loss cases (resolution against you, spread, fees, delay, oracle/UMA
dispute risk) · “we are not Polymarket” · non-custodial/key custody · no financial advice · jurisdiction
and eligibility disclaimer (P01 legal) · support and complaint route.
*reachability* a permanent, visible link in the `TradeTicket` footer and in the wallet screen header — one
click, never inside a tooltip, never behind a modal that can be dismissed without reading on the *first*
trade (first-trade confirm shows the summary + link).
*states* `loading` none (static) · `error` none · everything else n/a. Print stylesheet is part of this
screen (users print terms).
*mobile* full-bleed text, sticky “back to trading”.

### 21. 404 / 500 / maintenance / rate-limited
*route* any unmatched path · server-rendered fallbacks
*layout* one shell, four variants: a `glyph`, a one-sentence cause, **what happens next**, and one action.
* 404 — “No market at that address” + search box + three live markets from `gamma/markets?order=`
  (so the page is never a dead end); the market slug is often just stale, so offer a search.
* 500 — no details on screen; a short error id, support link, and retry. Nothing from the response body
  is rendered (no stack, no SQL, no key — kit rule).
* maintenance — planned banner + ETA, read-only cache if we hold it (prices greyed with their age), and
  **trading disabled** with the reason; the WebSocket surfaces stay open for status only.
* rate-limited — the retry delay as an explicit countdown, plus “we back off automatically; you don't need
  to refresh”; this state applies to *our* API too, and the UI must distinguish “Polymarket is throttling
  us” from “you are refreshing too much”.
*states* these *are* the states; they must render without auth, without JS data, and without CSS
  (inline the critical CSS so a 500 can't be caused by the stylesheet).
*a11y* each is `role=alert` for 500/maintenance, `role=status` for 404/rate-limit; focus lands on the
  action.

### 22. Onboarding — 4 steps, ending in a trade
*route* `/start/{wallet|connect|paper|first}`
*layout* a `Sheet` on `xs`, a centred 640 `Modal`-style flow ≥`sm`, with a 4-step progress rail. Never a
`<div>`-only wizard: it must be escapable at every step and resumable.
*steps* 1 **account** (already done at signup — this step only confirms email + timezone) ·
2 **wallet created** (we generated it; shows the address, the one-phrase explanation of what non-custodial
means here, and a “not now” path that still lets the user browse) ·
3 **paper trade** (a real ticket against a real live book, fake money, with the same validation ladder and
the same stale/disconnected blocks — the point is to teach the UI, not to flatter the user) ·
4 **first real trade** (deposit + optional amount; if they decline, the terminal opens with a
`Toast` “paper mode is on, switch it off in settings”).
*fields* live data in step 3 from `clob/spread`, `clob/book?token_id=`, `gamma/markets?order=` (pick a
liquid market automatically: highest `volume24hr` with `accepting_orders=true` and
`liquidityNum` above a floor) — never a mock price, because a fake number teaches the wrong reflex.
*rules* no step may be skippable into a *money* action; step 2's “not now” is allowed precisely because it
leads to reading, not trading. The risk link (D3.20) sits in the footer of every step.
*states* `loading` between steps, with the destination visible · `error` (wallet creation failed → retry,
and browsing still opens) · `disconnected` (step 3 blocks with the reason; it must not paper-trade against
a frozen book) · no `empty`.
*keyboard* `Enter` next; `Esc` exit → “you can finish later from settings” toast; focus stays on the
primary action at each step.
*mobile* full-screen sheet, one control per screen, 44px targets, no drag gestures.

---

## D4. The two hardest UI problems — solved with measurements

### D4a. One-sided and near-empty books

**What the prompt asserted** vs what was measured today across 22 books from the top-12 events:

| | prompt | measured 2026-09-17 |
|---|---|---|
| "I observed a Fed market with 94 ask levels and zero bids" | absolute | Fed-October book: **63 asks / 3 bids** on YES, mirrored **64 bids / 3 asks** on NO; deeper side max in the sample **137** levels |
| "this happens constantly" | frequency | **0/22** books were literally one-sided; **8/22 (36%)** had ≤5 levels on the near side |
| implied: treat as one state | design | it is **four** states, each needing a different message |
| — | — | spread is **median 71 ticks, max 998 ticks**; tick sizes present in the same sample: `{0.001, 0.01}` |

The last line is the reason a naive book "looks broken" even when nothing is wrong: a **46%-of-mid spread**
(best bid absent, best ask `0.999`) is *normal* for a market everyone agrees on. So the book must explain
itself, not just render what it received.

**The four states and their exact rendering** (component: `OrderBook`, `book.state`):

| state | condition | ladder | header | ticket interaction |
|---|---|---|---|---|
| `balanced` | both sides ≥ 6 levels | normal 10/side | `spread 71t · 0.071¢` | click-to-fill on |
| `near-empty` | min side ≤ 5 levels (the common case: 8/22) | full side gets **more rows** (10 → 14, the empty side is 1 placeholder line, not 10 blank rows) | `spread 996t` **+** “thin side: 3 bids” | click-to-fill on the **populated** side only |
| `one-sided` | min side = 0 levels | one ladder, other side replaced by a stated explanation row (below) | `no bids — only asks rest` + `best ask 0.999` | **buy at market allowed** (there is an ask), sell disabled with “nothing to buy from” |
| `locked` | best bid 0.999 / best ask 0.001 crossing or one side at a bound | compressed ladder + settlement strip | “settled-price zone — quotes here are residual” | submit blocked above 1¢ from the bound |

**The explanation row is the actual fix.** It states, in the UI's own words, the arithmetic the user cannot
see from a one-sided ladder:

> `No bids on YES · 63 asks rest · you can still SELL YES at 0.999 (that is buying NO at 0.001) · spread shown in ticks`

Why that sentence and not an empty state: with `NO = 1 − YES`, "no bids on YES" *is* "asks on NO at 0.001".
The liquidity exists — on the complement. A UI that shows “no liquidity” here is **wrong**, and a user who
believes it will not place the trade that is actually available. This is the single most common way a
prediction-market terminal lies to its user.

Additional hard rules:
* Never render an absent side as `0.00`/`—` alone; always `— (no resting bids)`. A dash is ambiguous
  between “zero”, “no data” and “not supported”, and all three need different actions.
* Never treat `levels == 0` as an error unless **both** sides are 0 (that is `no data` → retry, distinct message).
* Aggregate by level by default (median deeper side 58 levels, max 137 — unaggregated it is wallpaper).
* The `spread` figure is **always** in ticks with cents in parentheses; a bare `0.999 − None` is a bug report waiting to happen.
* DepthChart mirrors this: a flat side is drawn as a zero-height area **with a caption**, not a missing series.

### D4b. YES/NO vs BUY/SELL colour collision — measured, and it changed my own P02 answer

The prompt's example: a user holding a **profitable NO** sees green PnL next to a burnt-orange outcome chip.
I re-measured that composition with `tools/component-colour-audit.py` (pairwise ΔE\*ab in normal,
deuteranopic and protanopic vision, plus ΔL, on the actual row backgrounds) and **P02's analysis was
incomplete in two ways that matter**:

| pair | P02 said | measured in the row | consequence |
|---|---|---|---|
| `outcome.no` ↔ `alert.critical` | ΔE 4.1 / 3.2, “never share a row” | deuter **4.9 / 3.2**, protan 4.1 / 4.9 | confirmed — and `alert.critical` is *literally the same token* as `action.sell` |
| profit ↔ loss (`action.buy` ↔ `action.sell`) | (not checked as a pair) | **ΔE 9.0 (dark) / 6.9 (light) under protanopia; ΔL 0.040 / 0.008** | these are the hues P02 **disqualified from the chart palette** for exactly this reason, then used as the app's directional encoding anyway |
| `outcome.no` ↔ `alert.high` (warning inside `YesNoPair`) | (not checked) | ΔE **2.7** under protanopia in light theme | a gold warning inside a yes/no pair reads as a third price |

**Decisions, each with the measurement that forces it** (all recorded in `tokens.json.rules`):

1. `pnl-never-coloured-text` — the PnL **number** is always `text.primary`; direction is the +/− glyph
   (mandatory, never omitted), a leading caret in a fixed slot, and fill-vs-outline on the chip. Colour
   reinforces, never *is*. (Chips tinted 22% were tried first and measured **1.59:1** against the row —
   below the 3:1 non-text floor — with ΔL 0.006 between them. Rejected on its own numbers.)
2. `direction-never-colour-alone` — every binary direction signal must survive colour removal. Applies to
   buy/sell, profit/loss, up/down, everywhere.
3. `no-alert-hue-inside-a-compared-pair` — alert colours may not appear inside `YesNoPair` or the book
   ladder; a warning there is `text.primary` + icon + words.
4. `whale-flag-is-a-badge-not-a-dot` — since `alert.critical` **is** `action.sell`, a bare red dot next to a
   SELL pill is ΔE 0.0 in normal vision. The whale flag is a badge with a word and an area. Enforced as a
   *structural* check, because gold-vs-orange is genuinely separable in dark (ΔE 27) yet still forbidden.
5. `hue-is-owned-by-a-column` — the side cell owns `action.*`, the outcome cell owns `outcome.*`, the PnL
   cell owns `action.*` (it *is* a money direction). Same-column pairs must clear the strict tier;
   cross-column pairs are reported but not failed, because layout already prevents them meaning one thing.

**Proposed for the brand owner (not applied — P02's palette was approved as a set).** `action.sell` and
`alert.critical` sharing a token is the root of item 4. Measured candidates for a dedicated alert hue,
worst-case ΔE against `outcome.no`/`action.buy` across both themes and all vision models:

| candidate | worst ΔE | contrast dark / light | verdict |
|---|---|---|---|
| `#CC79A7` | 31.2 | 5.38 / 2.81 | **only candidate ≥3:1 in both themes**; magenta, currently the chart's “smart” hue — would need its own slot |
| `#a855f7` | 51.8 | 4.16 / 3.63 | best separation; 4.16:1 in dark fails for body text (fine for a badge) |
| `#f1ce57` | 14.2 | 10.75 / 1.40 | light-theme failure |
| `#f97316` | **4.0** | 5.88 / 2.57 | unusable — collides with `outcome.no` |

Recommendation: keep the layout rules (they are cheap and enforced), and adopt `#CC79A7` for `alert.whale`
only if a P02 palette-search re-run keeps it out of the chart arrays. Until then the audit tool's
CONTROL rows assert the rules hold; they are canaries — if a control ever *passes*, an exemption has grown.

**PositionRow, final anatomy for a colour-blind user** (this is the answer to “test against deuteranopia”):
`[▲ +$1,240]` renders as caret+sign+number where the caret+sign are the whole message; `No` appears as a
**word** in a chip with a ● glyph; the severity/warning badge is on the market cell, never beside the PnL;
quick-exit is a bordered button, not a red glyph. Remove colour from a screenshot of this row and it still
reads correctly — that is the acceptance test, and it is checked by the audit's structural rules.

---

## D5. Real-time behaviour

### D5.1 Transport per surface — WS or poll, and why

| surface | transport | cadence | source |
|---|---|---|---|
| Live tape (market, global, whale) | **WebSocket only** | push (measured **14.7–33.3 fills/sec** on the global firehose) | `wss://ws-subscriptions-clob.polymarket.com/ws/market` → `last_trade_price` |
| Order book / depth | **WebSocket** `book` + `price_change` + `tick_size_change`; REST for the initial snapshot | push, coalesced 100ms | `clob.polymarket.com/book?token_id=…` |
| Best bid/ask/mid/spread | WS-derived; REST `spread`/`last-trade-price` on open and after resync | push + on-demand | `clob.polymarket.com/spread`, `…/last-trade-price` |
| Market metadata (question, liquidity, volume24hr, endDate, feeType, accepting_orders) | **REST poll** | 60s (markets) / 300s (discovery lists) | `gamma-api.polymarket.com/markets?order=…`, `…/events?limit=…&offset=…` |
| Positions & balance | REST poll | 15s while a ticket is open, 60s idle | `data-api.polymarket.com/positions?user=`, `…/value?user=` |
| Fill history (backfill, exports) | REST, offset paging | on demand | `data-api.polymarket.com/trades`, `…/activity?user=` |
| Leaderboard | REST poll | 60s, and the UI says “hourly” | `lb-api.polymarket.com/volume?window=`, `…/profit?window=` |
| Our own PnL / rank / classification | our API (we compute; Polymarket offers neither) | 60s | our ledger |

**Why the tape is never polled.** `data-api.polymarket.com/trades` is served from Cloudflare's cache
(`cf-cache-status: HIT`, byte-identical 8s apart — `tools/datasource-probe.py`, check
`E2-trades-is-cached`). A poller would show a *confident, frozen* tape, which is worse than an obviously
stopped one. REST is therefore for backfill and history only, and the UI must distinguish them: rows
arriving over REST are tagged “backfill” and never flash.

### D5.2 Freshness thresholds, and what renders past them

Age is measured from the last event *for that token*, not from the last WS frame.

| data | `watch` | `stale` | `blocking` (trading off) | renders past `stale` |
|---|---|---|---|---|
| tape fill stream | >3s | >10s | >30s | `StaleIndicator` “no fills for 12s · market may be quiet or disconnected” + last-fill age; **no** spinner (a spinner promises arrival) |
| book ladder | >3s | >5s | >10s | ladder greyed to `text.secondary`, click-to-fill disabled, “frozen at 14:02:11” |
| price cell | >3s | >5s | >10s | desaturate + dot; flash suppressed (a flash on stale data is a lie with good timing) |
| market metadata | >120s | >300s | never | “data 4m old” micro-text; `accepting_orders=false` is **not** staleness, it is a fact — separate message |
| positions/value | >30s | >90s | >180s | PnL greyed; ticket's “% of balance” quick-picks disabled (they divide by this number) |
| leaderboard | >180s | — (hourly by design) | — | label only; no warning chrome, or the user learns to ignore warnings |

Every threshold above is a **user-visible number** in settings → “data & freshness”, with the current age
next to it. A hidden threshold is a support ticket.

### D5.3 Backpressure — the tape does ~21 fills/sec

Ordered by cost, each one measured or justified rather than assumed:

1. **Coalesce before render.** Buffer WS frames and flush on a 100ms boundary (`dur-micro`). At 33/sec that
   is ~3 rows per flush; at 15/sec ~2. Flushing per-frame is what makes 60Hz impossible on a mid-range phone.
2. **One flash per cell per 120ms**, direction = net of the batch; updates inside the rounding window do not
   flash at all (a 0.001-tick market otherwise flashes 3× more often than the user can read).
3. **Virtualise.** DOM row budget `--pgm-tape-rows` (4 at `xs`, 16 at `xl`, 24 at `3xl`) + 8 overscan.
   Hard cap: **never more than 64 tape rows in the DOM**, regardless of viewport.
4. **Cap the buffer, then drop from the middle.** 500 rows retained for scroll-back; beyond that drop
   oldest-with-a-count, and say so: “3,204 fills earlier this session are not in the DOM”. Silent loss on a
   tape is a data-integrity problem, so the number is shown.
5. **Write to refs, not state.** Row content updates go through `ref.current.style` / direct text nodes for
   the flash and value; React state updates only for row *insertion* (the virtualiser needs it). Per the
   performance cheatsheet: “React re-renders every frame → write to `ref.current.style`, not state.”
6. **Animate only `transform` and `opacity`** (plus `background-color` for the flash, which is the one
   non-composited property we accept, on ≤64 elements). No `width`/`left` animation, no `transition: all`,
   no animated blur.
7. **`content-visibility: auto` + `contain: layout paint`** on tape/book rows so off-screen rows cost nothing.
8. **No per-row animation entry.** New rows appear; they do not slide (D1.6).
9. **`paused — N new`.** Scrolling up or pressing `space` pauses *rendering*, not *ingestion*: the buffer keeps
   filling, the header shows a live count, and resuming applies the batch in one flush (never re-animating).
   The pause state is a real URL-able UI state, mirrored in a `Toggle`, because “am I seeing live data?” must
   never be a guess.

### D5.4 Connection state — and the trade block

```
connecting → open → (lagging) → degraded → reconnecting → … → disconnected
```
* **Backoff:** 500ms base, ×2 per attempt, ceiling 30s, ±20% jitter, and after 5 failures a visible
  “we keep retrying, you don't need to refresh” with the countdown. Never a modal.
* **On every reconnect, resync-then-stream:** REST `book` + `spread` snapshots first; the tape shows
  “gap 14:02:11–14:03:02 · 612 fills not shown” rather than silently continuing — a hidden gap in a tape is
  how a user “sees” a trade that never happened.
* **Trading is disabled while disconnected.** Non-negotiable and enforced in three places: the ticket's
  submit is `disabled` with the reason (`insufficient`-style inline message, not a tooltip), the server
  rejects `disconnected` submissions independently (the client check is UX, not safety — the risk gate is
  the authority), and copy-trading and automation pause visibly with a logged “paused: disconnected” row.
  A resting **cancel-all** stays available while disconnected: being unable to *exit* is worse than being
  unable to enter, and cancel is idempotent.
* The top bar's connection state is one of exactly four strings: `live` · `delayed` · `reconnecting 4s` ·
  `offline — trading off`. Nothing else, and no colour-only signal.

### D5.5 Tab visibility

Hidden: stop the render flush (the buffer keeps filling, capped as above), stop non-essential polling
(discovery/leaderboard → paused; positions → 60s, because a margin change can matter), and set
`document.title` to the one number worth interrupting for (“$1,240 ▲ · Openout”) only when the user enabled
title alerts. Visible: resync snapshots, flush the backlog, and mark the gap. No thundering herd —
re-poll is jittered per panel (0–2s).

---

## D6. Accessibility & internationalisation

**Normative target: WCAG 2.2 Level AA**, at every density, in both themes, for every screen in D3. That is
a conformance claim, not an aspiration: it is verified by (a) `tools/colour-audit.py` for contrast,
(b) `tools/component-colour-audit.py` for colour-independence and non-text contrast, (c) the 11-state
contract in D2.0 for focus/disabled/error affordances, and (d) the story list in D7.3, which is the test
surface a manual audit would run. The 2.2-specific criteria this product leans on hardest are
**1.4.1 Use of Colour** (a prediction market is nothing but colour-coded values — see D4b),
**2.5.8 Target Size (Minimum)** (dense rows make this the riskiest criterion; hence 44px floors that
density cannot shrink), **2.4.11 Focus Not Obscured** (sticky headers + a docking ticket are built to
overlap), **2.4.13 Focus Appearance** (the 2px ring at 3:1 against its background) and
**3.2.6 Consistent Help** (the risk link sits in the same place on every screen that can spend money).

### D6.1 Keyboard
Every action is reachable; `Tab` order is DOM order is visual order; focus is a 2px `border.strong` ring
with a 2px offset that is never removed without a replacement. Shortcuts are namespaced: `?` lists them,
`⌘K` opens the palette, and typing in an input suppresses globals (except `⌘`-chords).
No focus traps, with one carve-out: **modals hold focus but `Esc` always works** — including the trade
confirm, where closing mid-submit is allowed only before the request leaves (after that, the modal shows
“sending…”, `Esc` is inert, and that fact is announced). Escaping a confirm is *not* a way to abort a
broadcast order; the risk gate is.

### D6.2 The live tape and `aria-live` — the decision that makes it usable at all
* The container is `aria-live="off"` + `role=log` + `aria-relevant="additions"`.
  **`polite` on a 20/sec region makes the product unusable** — the queue runs seconds behind the screen and
  the user cannot reach any control while it drains; `assertive` is worse.
* A separate visually-hidden `role=status` region announces **at most once per 5s**, summarising:
  “14 new fills, largest $3,100 buy YES — Fed decision”. Summary, not stream.
* Row-level values are read **on demand** (the row is focusable; `aria-label` carries the whole row). A
  screen-reader user auditing a tape wants to read it, not to be read to.
* Whale alerts (user-triggered, low frequency) are the only tape-adjacent thing allowed `aria-live="polite"`.
* `prefers-reduced-motion` and the user's own density/freshness settings are announced as they change
  (“Density: dense”), so a keyboard-only user learns what the mouse would have shown them.

### D6.3 Numbers — decimal separators are **not** localised
**Rule: prices and tick sizes always use `.` as the decimal separator and never a comma.** They are
identifiers in a market protocol (`0.999`, `0.001`, `minimum_tick_size`), and a user who pastes a price into
a venue, a support chat or a bot command must be able to paste what they saw. Grouping is locale-aware and
uses a thin space (`U+2009`), never `,` or `.` — so `1 234 567` in every locale, and `0.999` everywhere.
Two formatters exist and the split is deliberate:
* `fmtPrice(tickSize, cents)` — `.` always, precision from `minimum_tick_size`, no grouping;
* `fmtMoney(intCents)` — locale grouping with a thin space, `$` prefix, sign as `+`/`−` (U+2212).
`lang="en"` is set on price elements inside a non-Latin document so an AT reads them in a digit-capable
voice. Prices are never spoken as “point nine nine nine yen”-style currency.

### D6.4 RTL
The app is logical-property CSS (`inset-inline-start`, `margin-inline`, `padding-inline-end`) with two
explicit **LTR islands that never flip**: the order-book ladder (bids below / asks above is a *convention*
that traders map to position, not a reading direction) and numeric columns. LTR flips: side-of-outcome words,
icons, and the ticket's outcome selector, whose “YES left / NO right” rule becomes “YES first / NO second”.
`direction: rtl` sets `--pgm-dir`, and `PriceCell`'s caret direction is bound to the sign, not to `inline-start`.
Bidirectional text (a market title in Arabic inside an English slug list) is wrapped in `unicode-bidi: isolate`
— a title with a trailing colon must not drag the price column with it.

### D6.5 i18n keys
`screen.component.element#variant.state` — e.g. `ticket.submit.label#sell.stale`,
`tape.paused.count`, `book.state.oneSided.hint`, `common.freshness.blocking`.
Rules: (1) no string concatenation of translated fragments — plural via `Intl.PluralRules`;
(2) money/price tokens are placeholders (`{price}`, `{usd}`), never interpolated into a sentence with a
hard-coded space; (3) a key with no translation in a shipped locale fails the build, not the runtime;
(4) copy stays under the P02 weak-copy limits (a translated string is audited for length too, since German
inflates ~30%); (5) nothing in the risk disclosure is auto-translated — those strings are reviewed per
locale or ship in English only.

### D6.6 Reduced motion
`prefers-reduced-motion: reduce` → all transitions/animations 1ms except the flash, which becomes a static
1px start-border in the same hue that fades out (no movement, no full-area pulse), and the depth chart's
data-append which was never animated anyway. The user's setting wins over the in-app “motion” toggle, and
the settings row says which one is in effect.

---

## D7. Implementation package

### D7.1 Tailwind config — generated, so it cannot drift
`web/tailwind.preset.cjs` is **generated** by `node tools/build-tailwind-preset.mjs` from
`brand/tokens.json` (the same source as `brand/tokens.css`), so a Tailwind class and a CSS variable can
never disagree. It maps: `spacing.{0,1,2,…,20}` ← `spacing.scale`; `borderWidth.{hair,strong,heavy}`;
`boxShadow.{0,1,2,3}`; `fontSize.{dense,compact,comfortable}` + the P02 type scale; `screens` ← `breakpoints`;
`transitionDuration`/`transitionTimingFunction` ← `motion`; `zIndex` ← `layers`; colours ← `semantic.*`
(flattened to `bg-base`, `outcome-yes`, `alert-critical`, …). `extend` only — a colour added by hand to
`tailwind.config` instead of `tokens.json` is the P03 gate's “redefined, not consumed” failure.

### D7.2 File structure & naming
```
web/src/
  tokens/            generated: tailwind preset, tokens.css import, contrast table
  ui/<Name>/<Name>.tsx, <Name>.module.css, <Name>.test.tsx, <Name>.stories.tsx
  market/<Name>/…    domain: PriceCell, YesNoPair, OrderBook, DepthChart, TapeRow,
                       TradeTicket, PositionRow, TraderCard, AlertRuleBuilder,
                       CopyConfigPanel, WalletPanel, MarketCard, StaleIndicator
  realtime/          ws.ts (transport, backoff, resync), coalesce.ts (100ms flush),
                     freshness.ts (D5.2 thresholds), feed-store.ts (integer cents only)
  money/             cents.ts (parse/format integer cents), tick.ts (tick-aware price rounding)
  features/<screen>/ routes, panels, hooks
```
Rules: PascalCase directories; one component per directory with its test and story beside it;
`ui/` may not import from `market/` (a primitive must not know what a fill is); no component reads
`fetch`/`WebSocket` directly (only `realtime/` does) — that is what makes `stale`/`disconnected` universal
instead of per-screen; every money value crosses into components as `{cents: number}` or a formatted string,
never as a float (`kit` rule; `money/` is the only place that parses).

### D7.3 Storybook story list
**Component × every applicable state from D2.0**, so no state can be “implemented later”.
31 primitives × 11 + 13 domain × 11, minus n/a (a `Divider` has no `loading`), **plus** the cross-cuts that
break in combination and are therefore stories of their own:
`PositionRow × {dense,compact,comfortable} × {dark,light} × {deuter-sim,protan-sim}` (a colour-blind
simulator filter is a story *control*, not a one-off check), `OrderBook × {balanced, nearEmpty, oneSided, locked}`,
`YesNoPair × {normal, residualWarn, noBookOnOneSide}`, `TradeTicket × the six validation rungs`,
`TapeRow × {trade, redeem, convert}`, `StaleIndicator × {watch, stale, blocking}`, every screen shell ×
`{loading, empty, error, partial, stale, disconnected, unauthenticated}`. Total: **1,062 named stories**
(66 roots; the dense ones are `PositionRow`×73, `PriceCell`×72, `OrderBook`×70 because each carries a
density × theme matrix), enumerated in `docs/verification/P03-storybook-list.txt` and regenerated by
`python3 tools/build-storybook-list.py`. The count is computed from `tokens.json.components`, not typed
into this document — `tools/p03-gate-check.py` recomputes it and fails on disagreement, because "we
covered every state" should be arithmetic, and my first draft of this line claimed ~468 by hand.

### D7.4 `DESIGN.md` — the rules a developer must not break
Written to `web/DESIGN.md`; the must-not-break list, each one machine-checked:
1. No colour literal in **authored** code except `brand/tokens.json`; regenerate with `build-tokens.mjs`.
   `brand/tokens.css` and `web/tailwind.preset.cjs` are *generated* from that JSON and legitimately contain
   hexes — excluding them is not a loophole, it is what "one source of truth" means (they are checked for
   drift by `build-tokens.mjs --check` / `build-tailwind-preset.mjs --check`, and P03's G1/G6 compare the
   generated CSS against the JSON). Enforced by G6.7, which audits `web/src/**`, `server/**`, `tools/**`
   and `brand/specimen.html`, and keeps a **named** exemption list rather than silently scoping them out.
   Current named exemption: `server/public/index.html` (the P01 throwaway probe page, pre-brand, own
   `:root` palette) — recorded debt, superseded when P10 builds the real landing page on the tokens.
2. No number typed into an authored stylesheet or a Tailwind config — spacing/radius/motion/z-index come
   from tokens. (Same generated-file allowance as rule 1.)
3. Nothing animates a number. Background flash only; ≤300ms on any data surface.
4. Outcome hues never encode good/bad; PnL numbers are `text.primary`; direction always has a glyph + a slot.
5. `outcome.no`/`alert.critical` and `action.sell`/`alert.critical` never share a row (`component-colour-audit.py` fails otherwise).
6. The mark is never redrawn. Composites reference `brand/svg/mark.svg`; the lockup is generated
   (`tools/rename-wordmark.mjs`) and its geometry is hash-pinned in `brand/BRAND-KIT.md`.
7. Prices use `fmtPrice` (always `.`); money uses `money/cents.ts`; a float in the money path fails CI.
8. No screen fetches data directly, so no screen can skip `stale`/`disconnected`.
9. Trading is disabled when the book is stale or the socket is down — a UI that allows submit there is a bug
   report, not a preference.
10. `REDEEM` payouts read `usdcSize`, never the price-times-size product (price is 0 on 100% of rows measured).

---

## D8. Verification — what was actually run, and what the gate found in itself

The prompt's quality gate is one sentence ("never has to invent a spacing value, guess a state, or ask
what a stale price should look like"). It is not checkable as written, so `tools/p03-gate-check.py`
operationalises it as **62 checks / 8 groups**, and `tools/p03-mutation-test.py` proves those checks can
fail. Running the document is not the same as admiring it.

| group | what it refuses to let be vague |
|---|---|
| G1 | D1 sections exist in `tokens.json`; every token is emitted to a CSS var; density switches by one `[data-density]` attribute; all 7 breakpoints defined; **G1.6 delegates to `build-contrast-table.py --foundations --check`, so the doc's D1 numbers are the tokens' numbers** |
| G2 | every host/path the spec cites exists in `docs/verification/P01-probe.json`; every mention of a known-dead endpoint carries its measured status; the probe is fully green; tape is WS-with-REST-backfill, never a REST polling design |
| G3 | 31 primitives + 13 domain components each specified; every primitive row has a do/don't **and** an accessibility clause; all 11 states defined and covered; the 1,062-story list is in sync with tokens (not a snapshot) |
| G4 | easing curves are the vendored skill's exact strings; no duration above the ceiling; the drawer exception is bounded and justified; flash is background-only; banned forms absent from all code blocks; **the number ban is asserted as a rule in both tokens and prose (G4.4b/G4.4c)** |
| G5 | no unqualified float arithmetic and no price-times-size product in any code block or the specimen |
| G6 | the specimen consumes tokens (every `var(--pgm-*)` is defined) and redefines none; no colour/size literals; hard states renderable; tape is `aria-live="off"`; PnL uncoloured; no NO-chip+critical+SELL row; **G6.8 audits hexes repo-wide with a named-exemption debt register** |
| G7 | the 10 accessibility obligations that must be paid before the first payable screen |
| G8 | brand name coherent across tokens/Brand Lock/rendered lockup; **the pinned geometry hash is compared to `mark.svg` right now (G8.2b), not merely declared**; GENERATED headers present |

### What the gate caught in my own work (all fixed in the artefact, none by relaxing a check)

G5 judges fenced code in this document as if it were product code, which is the point — but it also judged
this *retrospective*'s anti-example. The exemption is a marker, not a location: an HTML comment reading
"P03: anti-example" on the line immediately above the opening fence. Unmarked fences fail, so forgetting the
marker reports a violation instead of hiding one.

1. **Flash 90 + 260 = 350ms** violated the ≤300ms ceiling the same commit claimed to enforce → retokened
   to 90/200 = 290ms with a `ceiling_note`. Three prose sites still said 600ms/350ms afterwards; grep now
   returns zero.
2. **`--pgm-font-lg` did not exist.** The specimen reached for an undefined var, so the "specimen is
   buildable" claim was false → added `typography.utility` (`emphasis`, `letter-spacing-brand`,
   `letter-spacing-caps`) and registered it in `build-tokens.mjs`, which had refused the unregistered name.
3. **The spec never stated the normative WCAG 2.2 AA claim** its own prompt demands → added the D6
   "Normative target" paragraph naming 1.4.1 / 2.5.8 / 2.4.11 / 2.4.13 / 3.2.6 and which tool verifies each.
4. **D7.4 rule 1 said "no colour literal anywhere" while the check only read the specimen.** That is the
   false-claim shape: `server/public/index.html` carries ~30 literals and its own `:root` palette. The rule
   now states the real scope (authored files, with generated `tokens.css`/`tailwind.preset.cjs` exempt by
   construction) and G6.8 enforces it repo-wide, with the prototype as a *named* exemption whose check fails
   if the reason or the file disappears.
5. **A vacuous check of my own**: G2.3 originally matched full `lb-api.polymarket.com/pnl` URLs, which the
   spec never writes (it writes the short route form). Zero sentences matched, so "all labelled" passed on nothing.
   Mutation M4 exposed it. Now keyed on the route token, with **G2.3b failing if a dead endpoint is never
   mentioned at all** — a check that inspects nothing is now itself a failure.
6. **P01's C9 went red after the deep probe refresh**: the spec cited `finance_prices_fees`, observed in an
   earlier pull whose JSON the refresh overwrote. Re-testing it was not a disproof — `tag_slug` is **ignored
   by Gamma** (`tag_slug=zzzznotatag` returns the same 40 rows as `politics`), so no tag query can refute it.
   The honest fix was neither to keep a stale citation nor to delete an inconvenient value: the claim now
   reads "the four values in the retained sample" plus "this is a sample, not a closed taxonomy — read
   `feeType` per market, never hard-code an allow-list". P01's gate is back to 33/33.

### Mutation testing: what broke, in the checker and in the test of the checker

`tools/p03-mutation-test.py` breaks one invariant per check in a throwaway copy of the tree. Final state:
**20/20 caught**, baseline **62/62**. Getting there took four rounds, and *every* round's failure was worth
recording because in three of them the result looked like a verdict and was actually a bug in the machinery:

* **The harness undid its own work.** It re-copied pristine files *after* applying each mutation, so all 20
  mutations were reverted before the gate ran → "0/20 caught", which reads as "your gate is worthless" and
  meant "your test is broken". Tell: 20 identical `exit=1, failures=none`. Identical output across 20
  different mutations is never a property of the mutations.
* **The gate crashed and printed nothing.** With the vendored `skills/` tree absent it raised
  `FileNotFoundError` mid-group — exit 1, no verdict, which the harness scored as a miss. Now an absent
  reference is an **explicit skip** with a reason (60 → 59 passed, 1 skipped) and an unrunnable group
  reports `FAILED: aborted: <exception>`. A gate that dies quietly is worse than one that fails loudly.
* **5 of the first 20 escaped for real.** Those became G2.3b, G4.4b, G4.4c, G8.2b, G3.8b. M4 needed three
  attempts: proximity (too loose), then sentence-scoped, then `\b404\b` — which cannot match `404s`, since
  `s` is a word character, so a *correctly labelled* line was reported unlabelled while the mutation still
  slipped. Finally absolute-per-mention, and the vacuity guard G2.3b (a dead route the spec never mentions is
  a failure, not a pass).
* **G7.1 was scoping-free.** It searched the whole 1,218-line document for "WCAG 2.2 AA", so deleting D6's
  normative sentence still passed because the string survives in a sentence about a *tool* that verifies
  contrast. Now the claim must appear inside D6 **and** in `web/DESIGN.md`. M16 also had to be fixed: its
  `[^*]*` regex deleted only part of the bold span, making the mutation a near-no-op — a mutation that
  changes nothing is not a test.
<!-- P03: nocode -->
* **G5.2 could not see its own subject.** The pattern was `price\s*\*\s*size|price × size`, which matches
  neither `price * row.size` (an operand prefix sits between the `*` and `size`) nor the doc's spaced
  `×`. The anti-example I added to prove the marker mechanism was exempted *and* undetected: the check was
  blind in both directions. Replaced with a real operand-tolerant pattern, which immediately failed on the
  unmarked case.

The marker mechanism that came out of that is deliberately narrow, and was tested in five states — marked
fence passes; marker deleted, blank line between marker and fence, orphan marker with no fence, and an
unmarked genuine bug all fail:

### The second round, which was worse and quieter

The first round's bugs were all found by mutation testing. The second round's were found by **running the
tools in the order the build actually runs them**, and they were more serious because none of them produced
a red light:

* **The 290ms fix had only been applied to the output.** `brand/tokens.json` said `out_ms: 200`; the
  generator `tools/build-foundations.py` still said `260`. The phase gate stayed green, because G4.2 reads
  `tokens.json` — which is the *file the generator writes*. Editing a generated file is editing nothing.
  Now the gate shells out to `build-foundations.py --check` (G1.7), so an output that is not reproducible
  from its generator is a failure by itself.
* **Regenerating then deleted a token.** Running the builder overwrote `density.constants`, and
  `shell-max-width` had been added to `tokens.json` only — so `--pgm-shell-max-width` vanished from
  `tokens.css` and `specimen.html` read an undefined var. G6.1 caught *that* one (it compares every var the
  specimen reads against the emitted stylesheet), which is the only reason it is a finding and not a bug in
  the landing page. Fixed in the generator, so the next regeneration cannot undo it.
* **Two of my "checks" could only ever pass.** G5.2's regex matched neither spelling of the thing it bans,
  and G5.2c only existed inside an `if abuse:` branch — a check that is not reported when it passes is
  indistinguishable from a check that never ran. Both now report in both states.
* **The prose exemption I added to fix a false positive became an escape hatch:** `P03: nocode` exempted the
  following *block*, which included fenced code, so one comment line disabled the money rules for a real code
  block. Caught by deliberately trying that (test 3 above), not by reading the code. Markers now exempt prose
  only; a marker in front of a fence is itself a failure, and `web/src/**` and `server/**` get no marker
  handling at all.

Recorded so the pattern is visible: **every one of these was a check pointed at a derived artifact, or a
check that only spoke when it failed.** The fix each time was to point at the generator or the source, and to
make silence count as a result.

### An anti-example, kept on purpose

G5.2 treats fenced code in this document as product code. So that a *quoted* bug can be shown without
becoming a violation, exactly one block here is marked:

<!-- P03: anti-example -->
```js
const payout = row.price * row.size;   // 0 × anything is 0: a REDEEM row pays nothing
```

The gate exempts that fence only because the marker is the whole line above it — an inline mention, a blank
line in between, or a deleted marker all leave the block as a failure. The mechanism is deliberately not
extended to `web/src/**` or the server, where "this is an example of the bug" would be an excuse to ship it.

**Unenforced, stated plainly:** no pixel/browser verification exists here (no rasteriser or headless
browser), so nothing in this phase is confirmed to *look* right — only to be *specified* consistently.
Rendering checks are `review-animations` + the P08/P10 browser pass. Raster products (`.png` brand
exports) remain from P02 and cannot be regenerated in this environment; `node tools/rename-wordmark.mjs`
owns the SVG lockup. The `vercel` and `supabase` CLIs are absent from the workspace, so no deployment
claim is made in P03 — as none should be.
