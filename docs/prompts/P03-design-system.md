# P3 — Design System & Screen Specifications

> Paste `00-SHARED-CONTEXT.md` and the output of `P2-branding.md` first, then this.

## Role
You are a senior product designer who builds dense trading interfaces. You have shipped order books, depth charts, and real-time tape UIs. You know that a trading UI is judged by whether a user can act in under two seconds, and you design for that.

## Objective
Produce a complete design system and pixel-level specifications for every screen, implementable in React + Tailwind, that a frontend engineer can build without asking a single clarifying question.

## Product shape to match (rebuild, don't copy)
A three-column desktop terminal + a mobile/Telegram Mini App variant. Same information architecture as §8 of shared context: left rail (holdings / watchlist / following / trending), centre (chart + activity tabs + position/limit/auto), right rail (market info + trading module).

---

## Deliverables

### D1. Design system foundations
- **Spacing scale** (4px base), **border widths**, **elevation levels** (state when to use each — this UI should use elevation almost never)
- **Density modes:** comfortable / compact / dense, with the row-height and padding values for each. Traders want dense. Make it a user setting with a sane default.
- **Breakpoints:** mobile (<640), tablet, desktop terminal (≥1280), wide (≥1680). Specify what the terminal does at each.
- **Motion:** durations and easings. Rule: nothing animates a *number*. Price changes flash background, never translate.

### D2. Component library — spec every one
For each component: anatomy, all variants, all states (default/hover/active/focus/disabled/loading/error/empty), sizes, do/don't, and the accessibility requirement.

**Primitives:** Button, IconButton, Icon, Input, NumberInput, Select, Checkbox, Toggle, Slider, Tooltip, Badge, Tag, Avatar, Skeleton, Spinner, Progress, Divider, Modal, Sheet (bottom drawer), Popover, Menu, Tabs, SegmentedControl, Accordion, Toast, EmptyState, ErrorState, DataTable, VirtualList, Pagination/InfiniteScroll, CommandPalette

**Domain components** — these matter most:
- `PriceCell` — cents display, flash-on-change (green/red background decay over 600ms), direction caret, stale indicator
- `YesNoPair` — the two-sided price display unique to prediction markets. Show YES and NO with the relationship `NO = 1 − YES` visible
- `OrderBook` — bids/asks ladder, depth bars behind price, spread row, tick-size-aware stepping, click-to-populate-order-form, aggregate-by-level control, "book is one-sided" state (this happens constantly near resolution — I observed a Fed market with 94 ask levels and zero bids)
- `DepthChart` — cumulative depth, both sides, with the notional axis
- `TapeRow` — trade row: wallet avatar+name, classification badge, side, outcome, price, size, USD notional, age, market link, whale threshold flag
- `TradeTicket` — side toggle, outcome selector, amount input with quick-picks (25/50/100% of balance), limit vs market, price stepper respecting `minimum_tick_size`, min-size validation against `minimum_order_size`, estimated fee breakdown (platform + builder), all-in cap for market buys, submit with confirm step above a threshold
- `PositionRow` — market, outcome, size, avg entry, mark, unrealised PnL, % of portfolio, quick-exit
- `TraderCard` — avatar, name, 7D/30D PnL, win rate, volume, category specialisation, drawdown, classification badges, follow + copy actions
- `AlertRuleBuilder` — condition rows, threshold inputs, channel selection, test-alert button
- `CopyConfigPanel` — target wallet, multiplier, per-trade cap, daily cap, category filter, TP/SL, confirm-with-consequences
- `WalletPanel` — balance (pUSD), deposit address + QR, network selector, key export (with typed confirmation), withdrawal with password
- `MarketCard` — title, category, YES price, 24h volume, liquidity, ends-in countdown, sparkline
- `StaleIndicator` — appears whenever any displayed price is older than the freshness threshold. **This is a safety component, not a nicety.**

### D3. Screen specifications
For each screen produce: purpose, URL, layout wireframe described in words (regions, sizes, what goes where), every data field with its exact API source, all states (loading/empty/error/partial/stale/unauthenticated), keyboard shortcuts, and mobile adaptation.

Screens:
1. **Landing / marketing** (public, SEO)
2. **Sign up** — email+password, Telegram OAuth, Google; the wallet is created *after* signup, not before
3. **Sign in** + 2FA challenge + recovery
4. **Markets** — the discovery home. Filters (category, volume, liquidity, ending soon, new), sort, search, list/grid toggle
5. **Event detail** — for negRisk multi-outcome events this is a table of outcomes with an order book per selected outcome, not a single chart. Handle 128-outcome events (Republican Nominee 2028 has 128 markets).
6. **Market detail / terminal** — the three-column layout
7. **Live tape** — full-page feed with filters (min size, side, category, classification, wallet)
8. **Traders / leaderboard** — sortable, filterable, time-window switcher
9. **Trader profile** — full gmgn-equivalent dossier including the suspicious-behaviour metrics
10. **Wallet Radar** — multi-market intersection scanner
11. **Whale tracker** — threshold feed + saved views
12. **Portfolio** — positions, order history, PnL curve, export
13. **Copy trading** — discovery + config + monitor
14. **Automation** — rule list + builder + run history
15. **Alerts** — rules, history, channels
16. **Profile & settings** — account, security, API keys, notifications, density/theme, referral
17. **Wallet / deposit / withdraw / key export**
18. **Billing** — plan comparison, Stripe checkout, Telegram Stars path, invoices
19. **Referrals**
20. **Risk disclosure & terms** (must be reachable in one click from the trade ticket)
21. **404 / 500 / maintenance / rate-limited**
22. **Onboarding** — first-run, 4 steps max, ending in a first paper or real trade

### D4. The two hardest UI problems — solve them explicitly

**a) One-sided books.** Near resolution, markets become effectively settled: I observed a live market with best bid 0.999 on NO, zero bids on YES, and $21.9M resting on one side at 0.001. A naive order book renders this as "no liquidity" and a user will think the app is broken. Design the treatment.

**b) YES/NO vs BUY/SELL colour collision.** A user holding a profitable NO position sees green PnL and a red outcome chip. Decide the encoding, then design a `PositionRow` that cannot be misread at a glance by a colour-blind user. Test your answer against deuteranopia.

### D5. Real-time behaviour spec
- What updates via WebSocket vs what is polled, and at what interval
- Freshness thresholds per data type, and what renders when exceeded
- Backpressure: the tape does ~21 fills/sec. Specify coalescing, the max DOM row count, virtualisation, and a "paused — N new" affordance
- Reconnect UX: exponential backoff, a visible connection state, and **trading disabled while disconnected**
- Tab visibility: throttle when hidden, resync on focus

### D6. Accessibility & internationalisation
- WCAG 2.2 AA. Keyboard-complete: every action reachable, visible focus, logical order, no focus traps
- Screen-reader treatment for a live-updating tape (aria-live regions — specify politeness, or you will make the product unusable with a screen reader)
- Number localisation: **do not** localise the decimal separator in prices. Decide and document.
- RTL readiness, and the i18n key structure
- Reduced-motion support

### D7. Implementation package
- Tailwind config with the full token set from P2
- Component file structure and naming convention
- Storybook story list (every component × every state)
- A `DESIGN.md` for the repo with the rules a developer must not break

---

## Quality gate
A competent frontend engineer builds the whole product from this document and never has to invent a spacing value, guess a state, or ask what a stale price should look like.
