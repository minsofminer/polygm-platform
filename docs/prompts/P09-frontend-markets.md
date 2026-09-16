# P9 — Frontend: Markets, Event Detail, Order Book, Charts

> Paste `00-SHARED-CONTEXT.md` and the P2/P3/P8 outputs first, then this.

## Role
You are a frontend engineer who builds market-data visualisations. You have rendered order books with 94 price levels and charts with 30k series, and you know that the difference between a good and a bad trading UI is entirely in the details of number formatting and state handling.

## Objective
Build the market-facing surfaces: discovery, event detail, the order book, depth visualisation, and price history. These are the screens a prospective user sees in their first 30 seconds — they decide whether we look like a real product or a hobby.

---

## Deliverables

### D1. Markets (discovery home)
- Server-rendered first page for SEO, then client-hydrated live updates
- Filters: category (Politics, Sports, Crypto, Finance, Economics, Tech, Culture, Weather, Geopolitics, Other), 24h volume, liquidity, open interest, ending within N hours, newly created, negRisk multi-outcome only
- Sort: 24h volume, liquidity, open interest, ending soon, newest, biggest 24h move
- Search with typeahead across titles, slugs, and tags
- List and grid views, persisted per user
- **The 128-outcome problem:** "Republican Presidential Nominee 2028" has 128 markets under one event, $1.0M 24h volume, $52.8M liquidity. The card must summarise, not list. Specify the top-N-plus-expand treatment.
- The dead-tail problem: median event does $19,910/day. Default the view to markets that are actually tradable and let the user opt into the long tail.
- Empty and error states, plus the specific state for "filters returned nothing" with a one-click reset
- Pagination: cursor-based, with the rule for what happens when new markets arrive while the user is scrolled down

### D2. Event detail
Two distinct layouts, chosen by shape:
- **Binary / few-outcome:** a chart-led page with the YES/NO pair, the book, and the market info rail
- **negRisk multi-outcome:** a sortable outcome table (outcome, YES price, 24h volume, liquidity, change) with a book panel for the selected row. Must handle 128 rows smoothly — virtualise.
- **The negRisk invariant:** outcome probabilities must sum to 1. Display the current sum and the deviation. When the deviation exceeds the tolerance implied by tick sizes, surface it as an opportunity — this is a real signal (see P5 D6).
- Event metadata: resolution source, end date with countdown, resolution criteria, category, negRisk flag, market count
- **Resolution criteria are attacker-influenced text.** Render as sanitised plain text, never as HTML, and link out to the source rather than embedding it.

### D3. Order book — the centrepiece
Full spec, then implement:
- Bids left / asks right (or stacked, user setting), price levels with cumulative depth bars behind them
- Aggregate-by control: raw tick, 1¢, 5¢ — defaulting based on `minimum_tick_size` (which I measured at 0.001 on some markets, so raw tick is unusable there)
- Click a level → populate the trade ticket at that price
- Spread row with the mid, the spread in cents and bps, and the imbalance ratio
- **One-sided book state.** I observed a live market with 94 ask levels at 0.001 totalling **$21.9M** and *zero* bids on the other outcome. A naive UI renders this as broken. Design and build the explicit treatment: what we say, what we hide, and how we stop a user from thinking they can buy at 0.001.
- **Tick-size change.** `tick_size_change` fires when price crosses 0.96/0.04. Handle the book re-laddering without a visual jump.
- Real-time updates from the WebSocket with flash-on-change, and the stale overlay when the feed is behind
- Depth chart: cumulative both sides, notional axis, with the top-of-book marked
- Performance target: 200 levels re-rendering at 10Hz without dropping frames. Use canvas or memoised rows — justify the choice with a measurement.

### D4. Trade ticket
- Side toggle (BUY/SELL) and outcome selector (YES/NO) — with the **colour-collision resolution** from P3 D4 applied consistently
- Amount input: pUSD or shares, with 25/50/75/100% quick-picks against available balance
- Limit vs market. For limit, a price stepper that **snaps to `minimum_tick_size`** and rejects off-grid values before submit
- Validation, in the order the user will hit it: size ≥ `minimum_order_size` (5 shares — I verified this live) → price on grid → balance sufficient for notional + estimated platform fee + our builder fee → market `accepting_orders`
- **Fee breakdown panel:** platform fee estimate (`C × feeRate × p × (1−p)` with the per-category rate), our builder fee (`notional × bps / 10000`), total, and the label "estimate — final fee is set at match". For market buys, the all-in cap control.
- Expected outcome: shares received, breakeven price, max loss, potential payout. **Show max loss in dollars.** This is both an ethical requirement and the thing that reduces support load.
- Confirm step above a configurable threshold; one-click mode as an opt-in with a warning
- Submit states: submitting → submitted → open → filled/partial/rejected, with the **`unknown`** state handled per P3
- Every rejection reason rendered in plain language, mapped from the machine code
- Risk disclosure link, one click away

### D5. Price history & charts
- `/prices-history` with `interval` and `fidelity` — candlestick and line modes
- Granularities: 1m, 5m, 15m, 1h, 6h, 1d. Specify which we build candles for server-side vs derive client-side.
- Probability axis (0–100%) as the primary y-axis, notional as secondary — prediction markets are priced in probability and users think in it
- Volume histogram
- Trade markers on the chart, filterable by wallet classification (this is the gmgn pattern that makes the chart useful: see *who* moved the price)
- Average-cost line for the user's own position, and their entry marker
- Event annotations: market creation, resolution, UMA dispute
- Multi-chart: up to 4 markets side by side, each with its own granularity. (gmgn does 8; 4 is enough and much cheaper.)
- **The 5-minute market chart.** These markets live for 5 minutes and resolve. A 1d chart is meaningless. Design the specific short-lifecycle view.

### D6. Market info rail
- Title, category, created, ends, resolution source (link, not embed), liquidity, 24h/7d/30d volume, open interest, market count, negRisk
- Top holders for this market, with classification badges
- Related markets within the event and across similar events
- Share link that produces a correct OG image

### D7. States you must not skip
For every screen: unauthenticated, loading (skeleton matched to the final layout — no layout shift), empty, partial (some panels live, others stale), error, rate-limited, market closed, market resolved, market not accepting orders, `seconds_delay` active, book one-sided, WebSocket disconnected.

**Enumerate them in a table per screen and implement every one.**

---

## Constraints
- Every price surface has a freshness indicator. No exceptions.
- No float arithmetic in the money path.
- No raw number rendering — everything goes through the shared number component.
- Sanitised rendering of all upstream text.
- 60fps or the component is not done.

## Quality gate
Load a 128-outcome negRisk event and a one-sided resolved market. Both render correctly, both are usable, neither looks broken. Show me both, and show me the frame timing.
