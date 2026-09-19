# P09 — Frontend: markets, event detail, order book, charts

The trading surfaces. P08 built the frame; this phase fills it with the four screens a prediction-market
terminal is actually for, and it is the first phase where a wrong number is a wrong *trade* rather than a
wrong pixel. Three rules run through everything below and are worth stating once:

- **no float in the money path.** Prices and sizes arrive as decimal strings (`.001`, `232978723.404255`) and
  are parsed into integer micro-units by `src/lib/depth.ts`, which mirrors the API's `_micro_of`, including its
  refusal to round. Every sum, cumulative, spread and percentage in this phase is integer arithmetic.
- **no raw number on screen.** Every figure goes through `src/num/Number.tsx`, which owns precision (a market's
  tick decides its decimals, not a component's taste), the SI suffixes, the sign, the flash and the stale
  marker. A screen that formats its own number has two owners for one contract.
- **one source per value.** The book's payload, the market's metadata and the candles come from the contract
  (`contracts/openapi.yaml` → `src/api/schema.gen.ts`, via `npm run gen:api`). Nothing in `web/src` hand-types
  an API body; `tools/p08-gate-check.py` c2 fails the build on one, and it caught three in this phase.

## 1. What the phase is made of

```
web/src/lib/depth.ts        integer ladder arithmetic: micro-units, aggregation, cumulative, spread, imbalance
web/src/lib/ladders.ts      candle intervals (server 1m/5m/15m/1h, derived 6h/1d) and the event invariant
web/src/lib/upstream.ts     sanitiseUpstreamText / safeSourceUrl / flattenMarkdownLinks for venue text
web/src/lib/anchor.ts       where the ladder is looking, preserved across a re-ladder
web/src/screens/MarketCard.tsx        one row/card, incl. the 128-outcome summarisation
web/src/screens/MarketsClient.tsx     D1: SSR first page, then filters, sorts, typeahead, view, long tail, cursor
web/src/screens/OrderBook.tsx         D3: the ladder, aggregate control, one-sided banner, spread row
web/src/screens/PriceChart.tsx        D5: probability axis, candles, volume histogram, markers, annotations
web/src/screens/EventTable.tsx        D2: the negRisk outcome table and the probability-sum invariant
web/src/screens/MarketRail.tsx        D6: info rail, resolution text, holders with their provenance
web/src/screens/MarketView.tsx        the binary/few-outcome composition (chart + book + ticket + rail)
web/src/screens/EventView.tsx         the negRisk composition (outcome table drives the book)
web/app/markets/page.tsx              SSR of the first page, then hand-off to MarketsClient
web/app/market/[market_id]/page.tsx   SSR detail; 404 → notFound()
web/app/event/[event_id]/page.tsx     SSR event + first outcome's book
```

The dependency direction is one-way and deliberate: the four `lib/` modules are pure and know nothing about
React; the screens own layout and copy; `app/` owns data fetching. That is what makes the arithmetic testable
without a DOM (`vitest run src/lib` is 45 tests in ~4 s) and it is why the two bugs in §2.8 were caught by
tests rather than by looking at a screen.

## 2. Controls

Each control has an owner and a test. `[owner: … · test: …]` is the format the gate parses; a control whose
test is a document is not a control. Owner names are roles (`frontend-owner`, `design-owner`, `backend-owner`,
`product-owner`, `ops-ani`) that the launch review replaces with a person.

### 2.1 D1 — discovery

- **The first page is server-rendered and then hydrated, not re-fetched.** `app/markets/page.tsx` reads
  `/v1/markets?limit=50&sortBy=volume24h` and hands the payload to `MarketsClient` as initial state; a client
  that re-fetches what the server just rendered is a double-load on every navigation. A failed SSR read renders
  the same component with `initial={null}`, and the client fetches through the same-origin proxy, because the
  browser's path to the API is not the server's. `[owner: frontend-owner · test: web/app/markets/page.tsx]`
  `[UNVERIFIED — confirm before launch: seo-crawl]`
- **Filters, sorts and the query are server parameters, not client-side array surgery.** Category, volume /
  liquidity / open-interest floors, `endsWithin`, `new` and `negRisk` all travel as query parameters to the
  same endpoint the SSR used, so the filtered list is the same object the crawler would see.
  `[owner: frontend-owner · test: web/src/screens/MarketsClient.tsx]`
- **The long tail is opt-in, and its default is stated.** The endpoint hides low-notional markets unless
  `includeLongTail=true`; the list says how many were hidden and by what threshold rather than showing an
  unexplained short list. `[owner: product-owner · test: web/src/lib/ladders.ts::tailCopy]`
- **New markets arriving mid-scroll do not shift what you are reading.** A poll that returns a newer `asOf`
  than the top of the list raises a "new rows" affordance instead of prepending rows under the reader's thumb;
  the cursor is the page, so a market created while you scroll appears at the top on the next intentional load.
  `[owner: frontend-owner · test: web/src/screens/MarketsClient.tsx]`
- **List or grid is per user.** `pgm.markets.view` and `pgm.markets.longTail` are read after mount, never during
  render: reading localStorage while rendering is how a server-rendered list disagrees with the first client
  paint. `[owner: frontend-owner · test: web/src/screens/MarketsClient.tsx]`
- **128 outcomes are summarised, not listed.** A card for a market inside a large event shows the top three
  outcomes by 24h volume plus the count it left out (`SUMMARY_AT = 5`, so events up to five outcomes still
  render in full). `[owner: design-owner · test: web/src/screens/MarketCard.tsx]`
- **A dead market and a live market are different cards.** Accepting-orders, closed and no-price states are
  distinct; "no price" is a market that never traded, not a fetch that failed.
  `[owner: design-owner · test: web/src/screens/MarketCard.tsx]`

### 2.2 D2 — event detail

- **Binary and negRisk are two layouts, one route contract.** `/event/[event_id]` lists outcomes; selecting a
  row swaps the book that sits beside it, so the page has one selected market at a time and never renders 128
  books. `[owner: frontend-owner · test: web/src/screens/EventView.tsx]`
- **The probability sum is the invariant, and the page states it with the tolerance it used.** Outcomes'
  prices are summed in integer micro-units by `eventInvariant`, compared against the venue's tick-implied
  tolerance, and the deviation is printed next to whether it is inside that tolerance — a sum of 1.002 with a
  tolerance of 0.128 is not an error, and saying so is the point. `[owner: product-owner · test:
  web/src/lib/ladders.test.ts::eventInvariant]`
- **Resolution text is sanitised plain text, and the source is linked, never embedded.** Whoever created the
  market wrote that string, so it is the one attacker-influenced field on the page: `sanitiseUpstreamText`
  strips markup (strip → decode → strip, idempotent), and `safeSourceUrl` admits only http(s) with a dotted
  host, so a `javascript:` URL cannot be dressed as a citation.
  `[owner: security-owner · test: web/src/lib/upstream.test.ts::sanitiseUpstreamText]`
- **The outcome table is capped and says so.** `MAX_ROWS = 400` renders at once; beyond that the table prints
  how many rows it did not render. Virtualisation is *not* in this phase — see §3 — and the frame-trace that
  would justify it is launch item 2. `[owner: frontend-owner · test: web/src/screens/EventTable.tsx]`
  `[UNVERIFIED — confirm before launch: virtualised-128]`

### 2.3 D3 — order book

- **Aggregation is directional, because a basket is a promise.** Bids round **down** and asks round **up**
  (`aggregateLevels`), so an aggregated level never displays a price the reader could not have obtained. The
  control defaults to the raw tick and offers 1¢ and 5¢, because a 0.001-tick market has a thousand levels per
  dollar. `[owner: frontend-owner · test: web/src/lib/depth.test.ts::priceUnitsOf]`
- **Both sides share one depth scale.** `maxCumulative` is computed across bids and asks and handed to
  `withDepth`, so a bar is comparable between sides; two scales would make a thin side look deep.
  `[owner: design-owner · test: web/src/lib/depth.test.ts]`
- **A one-sided book is a state, not a broken feed.** The banner carries the level count, the notional and an
  explicit sentence that there is no bid at any price, and it refuses the "cheap asks" reading: a market with 94
  ask levels at 0.001 totalling $21.9M and no bids is the P01 measurement this treatment exists for.
  `[owner: product-owner · test: web/src/screens/OrderBook.test.tsx]`
- **The spread row says mid, cents and bps, or says there is no two-sided quote.** `spreadOf` returns nulls
  rather than zeros when one side is empty, because a spread of zero is a claim the book does not support.
  `[owner: frontend-owner · test: web/src/lib/depth.test.ts::spreadOf]`
- **A re-ladder keeps the reader's place.** Changing the tick or the aggregate step rebuilds every row; the
  price under the top edge of the panel stays under it, including when that bucket merged into a neighbour
  (`src/lib/anchor.ts`). The panel is bounded (`--pgm-book-max-block`: 560px ≈ 12 rows at the 44px touch
  target, the book the design system's `2xl` breakpoint promises), so the browser's own document anchoring does
  not apply and this is ours to hold. `[owner: frontend-owner · test: web/src/lib/anchor.test.ts]`
- **A changed size flashes; a re-ladder does not.** The size cell compares against the previous paint of the
  same (aggregate, side, price) bucket, and the flash policy itself lives in `src/num/flash.ts`. Today the book
  arrives by REST poll, and the policy refuses to flash a REST row — a cache can be minutes old, and flashing it
  is a confident announcement of stale news. `source="rest"` is therefore not a missing feature: the ladder
  passes the source through and the flash wakes up the day the live topic ships (launch item 5).
  `[UNVERIFIED — confirm before launch: ws-book]`
  `[owner: frontend-owner · test: web/src/num/flash.test.ts]`
- **A stale book says so over the numbers, not instead of them.** When the poll's `freshness` is not `live` the
  ladder dims and a sentence appears above it; the levels stay readable, because "we cannot see" and "there is
  nothing" are different decisions. `[owner: design-owner · test: web/src/screens/OrderBook.test.tsx]`
  `[UNVERIFIED — confirm before launch: reduce-motion-ladder]`
- **Clicking a level is the only way the ladder hands a price to the ticket.** `onPick` carries the venue's own
  price string, so an off-grid price cannot be submitted from here — and the ladder follows the same connection
  gate as the ticket, so a disconnected reader cannot populate an order from a dead book.
  `[owner: security-owner · test: web/src/screens/OrderBook.test.tsx]`
- **200 levels at 10 Hz is a claim about work per frame, and the work is bounded.** The book is a REST poll
  (`POLL_MS = 2000`, tagged `rest`) with a 400-level cap per side, not a per-frame socket: one user must not be
  able to spend the shared venue rate budget, which is the rule P04 exists to enforce. The 10 Hz wiring is
  launch item 5. `[UNVERIFIED — confirm before launch: frame-trace-ladder]` `[owner: backend-owner · test: web/src/screens/MarketView.tsx]`

### 2.4 D4 — trade ticket

- **The ticket is not rebuilt here.** P08's `TradeTicket` already owns the stepper, the tick snapping, the
  validation order, the fee breakdown with its estimate label, the max-loss figure, the confirm threshold with
  its one-click opt-in, the submit states including `unknown`, and the plain-language rejections. P09 wires it to
  a real selection (the ladder, the outcome table) and otherwise leaves it alone — a second ticket would be a
  second fee model. `[owner: frontend-owner · test: web/src/screens/TradeTicket.tsx]`
- **A price picked from the ladder arrives as a string and is snapped by the ticket.** The ladder's `onPick`
  hands over the venue's price; the ticket's own snapping is what refuses an off-tick value, and both are
  integer paths. `[owner: frontend-owner · test: web/src/screens/OrderBook.test.tsx]`
- **No paywall in a trade path.** The fee breakdown names the fee type and the estimate, and there is no
  upsell, gate or interstitial between a user and an order. `[owner: product-owner · test:
  tools/p08-gate-check.py::c10]`

### 2.5 D5 — price history and charts

- **The server owns 1m/5m/15m/1h; the client derives 6h and 1d from the 1h candles.** Deriving upward is a
  bucketing of what we already have; deriving *downward* would be inventing candles. Gaps stay absent — a
  bucket with no fills does not exist in the array and is not drawn flat, because a candle across a gap is a
  chart making up prices. `[owner: frontend-owner · test: web/src/lib/ladders.test.ts]`
- **The axis is probability, not price-in-dollars.** Prediction markets are priced in probability; the label
  says so, and the caption prints the low/high of the window as `microToDecimal(min, 3)` so a 0.001 market still
  reads as 0.001. `[owner: design-owner · test: web/src/screens/PriceChart.tsx]`
- **Volume is a histogram under the candles, and it is omitted on a short-life market.** Below `SHORT_LIFE_MS =
  30 min` of visible history one bucket of volume is all of it, and a histogram with one bar is decoration.
  `[owner: design-owner · test: web/src/screens/PriceChart.tsx]`
- **Trade markers are filterable by class, and our own position is a line.** Markers carry `t`, a micro price
  and a side; the average-cost line is drawn from the position, not from the tape, so "my cost" and "the
  market's price" are visually distinct objects. `[owner: frontend-owner · test: web/src/screens/PriceChart.tsx]`
- **Event annotations are drawn from the event's own record** (created, resolved, dispute) rather than from a
  second opinion about what happened. `[owner: product-owner · test: web/src/screens/PriceChart.tsx]`
- **At most four charts on one screen.** A page of twelve charts is a page nobody scrolls; the fourth is the
  cap and the rest are linked. `[owner: design-owner · test: web/src/screens/PriceChart.tsx]`
  `[UNVERIFIED — confirm before launch: chart-a11y]`

### 2.6 D6 — info rail

- **One request, one stamp.** The rail renders category, resolution text, liquidity, three volume windows, open
  interest, last price, 24h change and the holder count from a single `/v1/markets/{market_id}` read — five
  endpoints would produce five `asOf` stamps for one panel, and the freshness indicator answers "how old is what
  I am looking at", singular. `[owner: frontend-owner · test: web/src/screens/MarketRail.tsx]`
- **The holder list says where it comes from.** Holders are wallets *we have seen trade* (our tape), labelled
  "seen trading"; the venue does not publish a holder list for every market, and a number that silently means
  two different things is worse than a number that admits which one it is.
  `[owner: product-owner · test: web/src/screens/MarketRail.tsx]`
  `[UNVERIFIED — confirm before launch: holder-labels]`
- **Labels are published labels only.** `insider_suspect` is never publishable and so cannot appear; the rail
  renders the six that can. `[owner: product-owner · test: web/src/lib/upstream.ts]`

### 2.7 D7 — state table, per screen

Every screen has these states, and each one is reachable in dev. Anything missing here is a state a reader
would meet for the first time in production.

| screen | loading | empty | stale | error / refuse | degraded | special |
|---|---|---|---|---|---|---|
| `/markets` | skeleton rows + `markets.state.loading` | "filters returned nothing" + reset | `markets.freshness.book` text from `asOf` | SSR-failed → `markets.state.retry` + client retry | `limited` (429) keeps the rows | long tail hidden with its count; new-rows watermark |
| `/market/[id]` | book `unknown` until the first poll lands | market with no book → `markets.book.noBook` + history only | book dimmed + overlay sentence; rail stamped separately | 404 → `notFound()`; other → error block | closed / `acceptingOrders:false` / `secondsDelay` refusals | one-sided banner with levels + notional; flash suppressed on REST |
| `/event/[id]` | table skeleton, one book | event with one outcome | outcome table stamped once for 128 rows | 404 → `notFound()` | deviation outside tolerance is *stated*, not hidden | outcome count and excluded rows printed |
| ladder | `markets.book.noBook` | `no bids at any price` / `no asks at any price` | overlay above dimmed levels | `canTrade` false → rows disabled, not hidden | aggregate change keeps the reader's place | one-sided, re-ladder anchor, flash policy |
| chart | caption while `candles` empty | `markets.chart.empty` | axis caption carries the window | gap-split segments, never interpolated | short-life notice under 30 min | ≤4 charts, marker classes, average-cost line |
| ticket | P08 states unchanged | — | refuses to submit when the quote is stale | `unknown` submit state | — | selection from the ladder or the table |

`[owner: frontend-owner · test: web/src/screens/MarketsClient.tsx]`
`[owner: design-owner · test: web/src/screens/OrderBook.tsx]`
`[UNVERIFIED — confirm before launch: storybook-p09]`
`[UNVERIFIED — confirm before launch: touch-ladder]`

### 2.8 What the phase's own tools caught

Five defects that a screenshot would have shipped, in the order they were found. Each one is why the rule it
belongs to is a check rather than a paragraph.

1. **Tick units vs micro-units.** `Number kind="price"` takes **tick units**; the ladder was feeding it micro.
   On a 0.001-tick market that renders `.001` as **`1.000`** — a plausible price, ten times the size, on the
   market whose whole difficulty is its tick. Found by the first render test of the ladder
   (`OrderBook.test.tsx`), fixed by `priceUnitsOf`, which delegates the parse to `src/money/cents.ts` so there
   is still exactly one price parser in the app.
2. **The same mistake in the size column**, where shares arrived already scaled: 232,978,723 shares rendered
   as `232978.7B`. Fixed by `shareUnitsOf`, which drops the fraction toward zero — a displayed size may never
   be larger than the size that is there.
3. **A spread in cents multiplied by 100 on the way into a money cell**, printing a 0.2¢ spread as `$20.00`.
   `spreadOf` already returns integer cents; the bug was a conversion applied out of habit.
4. **`SuccessBody` unioned every documented response**, so the body type of the book was
   `{bids, asks, …} | {error: …}` and `book.bids` was a type error on a successful response. Fixed by
   extracting 2xx only — and by realising `Extract<keyof R, \`2${string}\`>` matches neither `200` nor `"200"`,
   which is how a body type silently becomes `unknown` while every check still passes.
5. **The contract was missing what the API serves.** `cumShares` on every level and six fields on the market
   detail were read by the client and absent from `contracts/openapi.yaml`. The contract is the source of the
   client's types, so the fix was the contract, not a local type: a field the ladder cannot work without is not
   optional documentation.

The P08 gate was re-run over all of it and is green: see §5.

## 3. Cost that was not paid

Three things this phase deliberately did not build, each with the reason and the item that carries it:

- **No virtualised outcome table.** 128 rows render as 128 rows. Virtualising without a browser to trace is a
  performance claim with no evidence behind it, and the honest alternative — a documented cap (`MAX_ROWS = 400`)
  plus a launch item that measures the frame — is worth more than an unmeasured `react-window`. Launch item 2.
- **No WebSocket book.** The ladder polls REST at 2 Hz and never flashes, by the flash policy's own rule. A
  second high-frequency REST loop would spend the rate budget the ingest exists to protect (P04 rule 2), and
  the live topic is a backend phase's work. Launch item 5.
- **No client-side aggregation of the raw ladder.** Bucketing decimal strings in the browser is float
  arithmetic in the money path; the endpoint aggregates directionally, in integers, before the limit is applied.
  The client control therefore chooses a bucket width the server already supports (`raw`, `1c`, `5c`).

## 4. Launch checklist — every `[UNVERIFIED]` in this document

Numbers are what the gate pairs the `[UNVERIFIED — confirm before launch: …]` slugs against; the items are
owned by roles the launch review replaces with people.

1. **`lighthouse-routes` — Lighthouse ≥ 90 on `/markets`, `/market/[market_id]` and `/event/[event_id]`, with
   LCP < 2.5 s on a mid-range Android over throttled 4G.** The budget has 2 KB of room; the commit that pushes
   `/markets` over 200 KB is the moment this becomes a blocker. `[owner: frontend-owner]`
2. **`frame-trace-ladder` — a DevTools trace of the book at 10 Hz for 60 s with 200 levels per side**, plus a
   recorded trace of a re-ladder under scroll, and of the outcome table at 128 and 400 rows. This is the item
   that decides whether virtualisation is needed at all. `[owner: frontend-owner]`
3. **`virtualised-128` — outcome-table virtualisation** if and only if the trace in item 2 shows dropped frames,
   with the scroll-position behaviour of the anchor written down. `[owner: frontend-owner]`
4. **`touch-ladder` — the ladder on real iOS and Android hardware**: 44px targets at `dense`, no accidental
   level selection while scrolling, and the aggregate control reachable one-handed.
   `[owner: design-owner]`
5. **`ws-book` — `/v1/live/<topic>` for the book**, so the ladder's `source` becomes `ws` and the flash policy
   starts applying. Until then the ladder is labelled as a poll and nothing flashes.
   `[owner: backend-owner]`
6. **`storybook-p09` — the 11 states of §2.7 for each of the four screens in the story runner**, with visual
   regression on the ladder, the chart and the outcome table. `[owner: design-owner]`
7. **`seo-crawl` — a real crawl of the SSR market and event pages**: canonical URLs, the title and description
   they render, and whether the first page of `/markets` is indexed as data or as an empty shell.
   `[owner: product-owner]`
8. **`chart-a11y` — a screen-reader pass over the chart and the ladder**: the SVG's `role="img"` and its alt
   text, the table's headers, and whether the ladder reads as a table or as 200 buttons.
   `[owner: design-owner]`
9. **`reduce-motion-ladder` — `prefers-reduced-motion` in a real browser**: the depth bar and the flash must
   degrade to a static edge, and the re-ladder must not animate a scroll position.
   `[owner: frontend-owner]`
10. **`holder-labels` — the publishing review for the holder labels** (`whale`, `smart_money`, `cluster`,
    `wash_like`): what each one claims, what a reader is meant to do with it, and the data behind it.
    `[owner: product-owner]`
11. Owner names in these markers are roles, not people: `frontend-owner`, `design-owner`, `backend-owner`,
    `product-owner`, `security-owner`, `ops-ani`. `[owner: ops-ani]`

## 5. Measured (this phase's own numbers)

| what | result |
|---|---|
| `npm run test` (vitest, jsdom) | **145 passed, 20 files, 0 failed** — of which P09's own: `lib/depth` 18, `lib/ladders` 12, `lib/upstream` 9, `lib/anchor` 6, `screens/OrderBook` 5 |
| `npx tsc --noEmit` (strict, `noUncheckedIndexedAccess`) | clean |
| `node scripts/i18n-check.mjs` | 345 keys, 303 used, 0 missing, 0 malformed, 0 dynamic; the 42 unused are advisory and predate this phase |
| `npm run build` (Next 16, **webpack**) | 21 routes; the sandbox has ~700 MB free and Turbopack's builder is OOM-killed there, so the measured build is `next build --webpack` with a 1.2 GB old-space cap. Same input, different bundler — recorded because a build tool is part of a measurement |
| `npm run measure` | `/` 188.4 KB, worst route **`/markets` 198.0 KB of a 200 KB budget**, `/tma` and `/profile` 191.5 KB; route-level splitting proven — the landing document does not fetch the money module and `/markets` does |
| `make test` (backend, unchanged by this phase except for the contract) | 674 OK, 200/200 contract checks, `make lint` 92 files 0 findings |
| `tools/check-css-blocks.mjs` | `globals.css` balanced; the P09 block adds ~380 lines and no literal (c5 is green over it) |
| `python3 tools/p08-gate-check.py` (`make p08`) — re-run with P09 in the tree | **15/15** (c8 now reports 198.0 KB and the split as proven; c14 passes with the retry path rendered through one `MarketsClient` site) |
| a real browser run (Lighthouse ≥ 90, LCP) on the three P09 routes | **not run** — no browser in this environment; launch item 1 `[UNVERIFIED — confirm before launch: lighthouse-routes]` |
| ladder fixture, P01's own shape | `0xM9`: 94 ask levels 0.001 → 0.094, no bids, notional **$21,899,999.999976**; the banner prints the count, the notional and the no-bid sentence |
| event fixture | `0xEV128`: probability sum `1.002`, deviation `0.002`, tick-implied tolerance `0.128`, `buyAllCost` `1.13` — inside tolerance and stated as such |

## 6. Decisions recorded for the phases after this one

- **`priceUnitsOf` is the only bridge from a decimal price string to a `kind="price"` renderer.** Any new
  surface that shows a price calls it (or `Number`, which calls the money layer). The tick-unit/micro-unit
  confusion is silent, plausible-looking and 1000× wrong on exactly the markets this app is for.
- **`SuccessBody<Path, "get">` is the only way to name a response body.** If a field is missing from the type,
  the fix is `contracts/openapi.yaml` plus `npm run gen:api` — not a local interface. P09 needed three contract
  additions to render what the API already served.
- **The flash policy's `source` parameter is the design-system-level answer to "should this move?"** A REST
  surface passes `rest` and stays still; the day a surface knows it is live, it passes `ws` and inherits 120 ms
  rate limiting, rounding-window suppression and the reduced-motion fallback without writing any of it.
- **Layout values that a screen depends on belong in `brand/tokens.json`.** `--pgm-book-max-block`,
  `--pgm-rail-left` and `--pgm-rail-right` were added there (and regenerated) this phase; the last two had been
  used with `auto` fallbacks since P08, which is a layout that looks right for as long as nobody asks which two
  widths it is laying out against.
- **A screen test earns its place by rendering.** The five screen tests in this phase found three unit bugs
  that the pure-function tests could not, because the bug was in the *contract between* a pure function and a
  renderer. P10 and later should test the composition, not only the arithmetic.
