# P10 — Frontend: The Terminal (Tape, Traders, Whale Tracker, Radar, Portfolio)

> Paste `00-SHARED-CONTEXT.md` and the P2/P3/P8/P9 outputs first, then this.

## Role
You are a frontend engineer building the screens that make this product different from the eleven analytics tools already in the market. You know that a whale tracker is trivial and a *useful* whale tracker is hard, and that the difference is entirely in the classification and the noise control.

## Objective
Build the differentiated surfaces: the live tape, trader profiles, whale tracker, Wallet Radar equivalent, portfolio, copy trading, and automation. These are what people pay for.

---

## Deliverables

### D1. The three-column terminal
Assemble the P9 components into the full terminal layout from P3:
- **Left rail:** holdings, watchlist, following, trending. Each with live prices and a one-tap exit.
- **Centre:** chart + activity tabs (Activity · Traders · Holders) + position/limit/auto panel
- **Right rail:** market info, the trade ticket, top holders
- Resizable, collapsible, persisted per user, sane defaults at 1280 and 1680
- Mobile: the same information reorganised into tabs, not truncated

### D2. Live tape
The screen that proves we are real-time.
- Rows: time, wallet (avatar + name + classification badges), side, outcome, price, size, USD notional, market link
- Filters: min notional (absolute **and** relative-to-market-median), side, outcome, category, classification, specific wallet, market
- **Whale threshold:** I measured a median fill of **$5**, p95 of **$133**, max $3,000 in one window. A fixed $1,000 threshold is simultaneously too low to be interesting in a $25M-volume market and too high to catch anything in a small one. Implement the **relative** threshold from P5 D5 and make the absolute one a fallback.
- Classification badges per P5 D5, with a tooltip explaining the exact rule. **A label the user cannot interrogate is a label they will not trust.**
- Virtualised, capped row count, coalescing at 20+ fills/sec, and the "paused — N new" affordance
- Click a row → market. Click a wallet → profile. Shift-click → add to watchlist.
- Sound toggle for followed wallets, off by default

### D3. Trader profile — the dossier
The gmgn-equivalent, rebuilt for prediction markets:
- **Header:** name/pseudonym, avatar, address with copy + block-explorer link, account age, classification badges, follow + copy buttons
- **Headline metrics:** total volume, realised PnL, unrealised PnL, win rate, avg hold time, best/worst trade, max drawdown
- **Time-window switcher:** 7D / 30D / 90D / all — with the metric set recomputed per window, not just the chart
- **PnL curve** with drawdown overlay. **Drawdown is mandatory** — showing only PnL is how copy-trading products get their users hurt.
- **Category breakdown:** where they actually make money. A trader who is +$200k on politics and −$180k on sports is not a trader to copy wholesale.
- **Trade history** with filters, each row linking to the market and the on-chain fill
- **Current positions** with mark price and unrealised PnL
- **Behaviour metrics** — the prediction-market equivalent of gmgn's phishing check:
  - entries shortly before large correct price moves (insider-suspect score, with the false-positive caveat displayed)
  - coordinated-cluster membership
  - buy-and-sell-within-seconds count (copy-farm / wash indicator)
  - markets entered within `seconds_delay` of creation
  - **Every one of these needs a visible methodology link and a disclaimer.** Labelling someone an insider without substantiation is defamatory.
- **The honest-stats rule:** any wallet shown with fewer than N resolved markets displays "insufficient sample" instead of a win rate. No exceptions. A 3-for-3 trader is not a 100% win rate.

### D4. Whale tracker
- Threshold feed with saved views (name, filters, channel, severity)
- Per-market and global modes
- Severity scoring with a stated formula
- **The noise problem:** in a market doing $25M/day, $5k fills are routine. Design the default thresholds per market-size bucket so the feed is interesting by default and tunable by power users.
- Alert-rule creation inline from any row
- Export to watchlist / follow / copy

### D5. Wallet Radar — the multi-market intersection scanner
gmgn's version scans up to 10 tokens and ranks wallets four ways. Rebuild it for prediction markets:
- **Input:** up to 10 markets or events (search or paste slugs)
- **Four ranking modes:**
  1. **Most active** — wallets trading the most of the selected markets
  2. **Highest profit** — realised PnL across the selection, with the sample-size gate
  3. **Earliest** — who entered first, per market and on average
  4. **Shared exposure** — wallets holding positions across multiple selected markets simultaneously (the coordinated-cluster signal)
- Each row: wallet, matched markets (as chips), bought/sold, realised PnL, win rate, classification
- One-click: track · follow · copy · open profile
- **Cost control:** this is an expensive query across ClickHouse. Specify the cache, the per-user quota by tier, and the async-job pattern if it exceeds a latency budget.
- List and compact-card views

### D6. Portfolio
- Positions table: market, outcome, size, avg entry, mark, unrealised PnL (absolute and %), % of portfolio, ends-in, quick-exit
- **negRisk handling:** positions across mutually exclusive outcomes in one event should be groupable, with the event-level exposure shown. A user long YES on three candidates in one event has a different risk profile than three independent bets — show it.
- Order history with filters and the `unknown`-state rows clearly marked
- PnL curve (realised, unrealised, cumulative) with a benchmark against simply holding pUSD
- Export CSV for tax purposes. **Prediction-market tax treatment is genuinely messy and no tool handles negRisk resolution correctly — a clean export is a real feature.**
- Empty state that points at the markets page, not at a blank rectangle

### D7. Copy trading
- **Discovery:** sortable list of copyable wallets — PnL, win rate, volume, drawdown, category, 30d activity, copier count. **Default sort must not be raw PnL** (that surfaces lucky gamblers). Sort by risk-adjusted return and say so in the UI.
- **Config panel:** target, multiplier or fixed size, per-trade cap, daily cap, category filter, min/max price bounds, TP/SL, skip-if-price-moved-more-than-N-cents, do-not-enter-markets-resolving-within-N-hours
- **The latency warning, in the UI, before they confirm:** by the time we see a source fill the price has moved. State the typical slippage we observe and make "skip instead of chase" the default.
- **Monitor:** live list of copied trades with source price, our fill price, slippage, and the skip reasons
- Per-source performance: are we making money copying this wallet after slippage? Show it honestly, including when the answer is no.
- Pause/stop per source and globally

### D8. Automation
- Rule list with status (active / paused / dry-run / halted), last fired, next evaluation
- **Visual rule builder** — trigger rows, condition rows, action rows, AND/OR grouping. No expression syntax exposed to users.
- Templates for the common cases, including the 5-minute crypto market template (with the fee arithmetic from P6 D6 displayed — if it doesn't work at current fees, do not offer the template)
- **Dry-run mandatory:** a new rule runs in simulation and shows what it *would* have done before it can go live
- Run history: every evaluation, every action, every skip with the reason. Users must be able to answer "why did the bot do that".
- The daily-loss-halt banner when the risk service has stopped a user

### D9. Alerts
- Rule list, inline editor, test-fire button, delivery history with per-channel status
- Channel management: Telegram (default), email, webhook (Pro)
- Quiet hours, digest mode, per-rule cooldown display

---

## Constraints
- Every classification label has a visible rule and a disclaimer.
- Every win rate has a sample-size gate.
- Drawdown appears wherever PnL appears.
- No feature that hides a loss.
- 60fps under live load, or the component is not done.

## Quality gate
A new user can: find a whale fill in the tape, open the trader's profile, see that their win rate is real (sample ≥ gate), see their drawdown, set up a copy config in dry-run, and understand the slippage risk — without reading documentation. Walk me through it.
