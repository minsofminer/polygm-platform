# P5 — Data Ingestion, Signals & Alert Engine

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are a data engineer who builds real-time market-data pipelines. You have been paged at 3am by a silent WebSocket and you design so that never happens again. You assume every upstream API will lie, stall, or change without notice.

## Objective
Build the ingestion layer that turns Polymarket's four public APIs into a normalised, queryable, alertable stream — plus the signal engine and alert fanout that become the free product and the acquisition channel.

## What this layer is for
This is **the whole free product**. The alert channel is how we get users before we can trade for them. It also becomes the paid analytics tier and the API product later. Build it to be sellable, not to be demoed.

---

## Deliverables

### D1. Source inventory & fetch strategy
For every upstream endpoint we use, document: payload shape (with a real example), useful fields, ignored fields, limit class, poll interval, and the fallback if it changes.

Cover at minimum:
- Gamma `/events` (list, by slug, by tag), `/markets`, `/tags`
- CLOB `/book`, `/books` (batch), `/price`, `/prices`, `/midpoint`, `/spread`, `/prices-history`, `/markets/{conditionId}`
- Data `/trades`, `/positions`, `/holders`, `/activity`, leaderboard `/volume`
- WebSocket market channel: `book`, `price_change`, `last_trade_price`, `tick_size_change`

⚠️ **Verify every field name against `docs.polymarket.com` before writing code.** CLOB V1 documentation is dead and still ranks highly in search results.

### D2. Market universe sync
There are ~30k markets and Gamma `/markets` allows **300 requests per 10 seconds**. Design the sync:
- Initial backfill strategy that respects the limit and is resumable after a crash
- Incremental strategy: how do you learn about **new markets** without polling the whole universe? (The `new_market` WebSocket event requires a flag — check availability, and design the polling fallback.)
- Detecting **resolution**: a market resolves and prices snap to 0/1. How do you detect it, and how do you avoid recording a 0.999→1.00 move as a trade signal?
- Detecting **metadata changes** (question edited, end date moved) and versioning them
- Dead-market pruning: median event does $19,910/day and the long tail is dead. Define what we stop tracking and how we resume if it wakes up.

### D3. Book maintenance
- Maintain in-memory L2 books for a **tracked set** (define how the set is chosen — top-N by volume, plus user watchlists, plus anything with an open alert)
- Snapshot on subscribe, then apply `price_change` deltas
- **Sequence-gap detection and resync.** If you miss a delta the book is silently wrong forever. Specify detection and recovery.
- Handle the **one-sided book** case: I observed a live Fed market with 94 ask levels at 0.001 totalling $21.9M and zero bids on the other side, plus `tick_size_change` firing when price crosses 0.96/0.04. Design for it.
- Depth aggregation levels and the derived values we expose (best bid/ask, spread, mid, depth at ±1¢/±5¢, imbalance ratio)
- Per-connection subscription cap is 200 — design the sharding across connections
- Memory budget for 2,000 tracked books. Show the arithmetic.

### D4. Tape normalisation
- Consume `/trades` **and** the WebSocket `last_trade_price`; deduplicate across both (they overlap). Specify the dedupe key.
- Observed baseline: **~20.8 fills/sec**, 342 distinct wallets per 500 trades. Design for 10× that during a news event.
- Normalise to one row per fill: `trade_id, market_id, token_id, outcome, wallet, side, price, size, usd_notional, ts, source`
- **ClickHouse schema**: table engine, partitioning, TTL, materialised views for the rollups (1m/5m/1h/1d volume and VWAP per market; per-wallet daily aggregates)
- Late/out-of-order data handling
- The 5-minute crypto Up/Down markets (`btc-updown-5m-*`, `eth-updown-5m-*`) are a special case: new market every 5 minutes, enormous fill rate, market resolves and disappears. Design their lifecycle explicitly.

### D5. Wallet classification engine
This is the differentiated feature. gmgn's taxonomy is `smart money · KOL/VC · whale · new wallet · sniper · large holder · developer · followed · rat warehouse`. **Map it onto prediction markets** — some categories have no equivalent (there is no "developer wallet" in a Fed market) and some are new.

Propose and justify a prediction-market taxonomy. At minimum define, with exact computable rules:
- **Whale** — notional threshold. Justify the number from the observed distribution (I measured a median fill of $5, p95 of $133, max $3,000 in one window — so a fixed $1,000 threshold is far too low to be interesting and far too high to be selective across market sizes). Consider a **relative** threshold: fill size vs the market's recent median fill.
- **Smart money** — realised PnL, win rate, sample size, category specialisation. Define the minimum sample before a wallet can be labelled, or you will label lucky beginners.
- **New wallet** — account age and total trade count
- **Insider-suspect** — the prediction-market equivalent of gmgn's "rat warehouse". A wallet that repeatedly enters shortly before a price move that later proves correct, especially in low-liquidity markets. Define the detection precisely, and **define the false-positive control** — labelling an innocent trader as an insider is defamatory and will get us sued.
- **Coordinated cluster** — wallets that trade the same markets in the same direction within a short window. Specify the clustering method and its cost at our data volume.
- **Copy-farm / wash** — buy-and-sell within seconds, self-matching patterns. Also relevant because Polymarket revokes builder codes for non-bona-fide volume; we must not accidentally attribute our own users' wash trades to ourselves.

Every label gets: the rule, the inputs, the recompute cadence, the storage, and **the user-visible disclaimer**.

### D6. Signal engine
A rule evaluator over the normalised stream. Built-in signals:
- Large fill (absolute and relative thresholds)
- Volume spike (z-score vs trailing window — specify window and the minimum sample)
- Rapid price move with depth confirmation (a move on thin depth is noise; specify how you tell them apart)
- New market created in a tracked category
- Order-book imbalance flip
- Cross-market divergence within a negRisk event (outcomes must sum to 1 — **this is a real arbitrage signal**, specify the tolerance given tick sizes)
- Watched-wallet activity
- Resolution imminent + position still open

For each: inputs, computation, cooldown/dedupe (an alert that fires 400 times is worse than no alert), severity, and default channel.

Design the rules so **users can compose their own** (threshold, filter, cooldown, channel) without us deploying code.

### D7. Alert fanout
- One evaluation → many subscribers. Design so 10,000 subscribers to one popular rule cost one evaluation, not 10,000.
- Telegram delivery: Bot API limits (⚠️ ~30 messages/sec globally, ~1 message/sec per chat, 20 messages/min per group — **verify current limits**), batching, priority queue so a paying user's alert is not stuck behind the free channel
- Digest mode for low-value rules
- **Delivery SLO and the observable that tells you when you are breaching it.** An alert that arrives 90 seconds late in a 5-minute market is worthless and the user will churn.
- Per-user rate limiting so one user's 200 rules cannot starve everyone else

### D8. Reliability
- **Silent-death detection.** A WebSocket that stays open but stops sending is the classic failure. Heartbeat per source, per-source last-message-age alarm, and automatic resync.
- Lag metric per source (wall-clock now − newest event ts), alarm threshold, and what the UI shows when a source is lagging
- Backpressure: when ingest falls behind, what do you drop? Specify the priority order (user watchlists > tracked top-N > long tail).
- Restart behaviour: resume from a durable cursor, not from "now". Otherwise every deploy creates a data hole.
- A **chaos test**: kill each consumer at random for 60s and prove no duplicate alerts and no missed large fills.

### D9. Backfill & historical analytics
- `/prices-history` gives per-token price history (`interval`, `fidelity`). Design the backfill for the top 2,000 markets.
- What we store long-term vs what we recompute
- The rollups that power: trader PnL curves, market volume charts, the leaderboard, and the future API product

---

## Constraints
- No polling loop without a limit-aware token bucket. Name the bucket and its rate for every source.
- Every derived number in the UI must be reproducible from stored data. No in-memory-only analytics.
- Every classification label must have a stated rule and a false-positive control.
- Nothing in this layer touches a private key. Ever.

## Quality gate
Kill the WebSocket for 5 minutes during a live test. On reconnect: no duplicate alerts, no missed fills above threshold, book resyncs correctly, and the UI showed a stale indicator the whole time. Show me the test.
