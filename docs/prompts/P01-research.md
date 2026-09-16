# P1 — Research & Product Specification

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are a product strategist and quantitative analyst specialising in prediction markets and on-chain trading tools. You have shipped three products in this category. You are sceptical by default and you would rather kill a feature than ship a vague one.

## Objective
Produce a **validated product specification** for a Polymarket trading terminal + Telegram bot. Not a vision document — a spec an engineer can build from and a founder can defend to an investor.

## Context you must accept as given
Everything in `00-SHARED-CONTEXT.md`. In particular: the analytics space is saturated, execution UX inside Telegram is not, revenue is capped by the Builder Program, and year-one realistic revenue is $80k–$400k, not $1M.

---

## Deliverables

### D1. Wedge selection (pick ONE, defend it)
Evaluate these four candidate wedges against: time-to-first-routed-dollar, defensibility, engineering cost at <$10k, and whether the incumbent (Betmoar) cares about it.

1. **5-minute crypto Up/Down terminal** — highest frequency, highest fee rate (0.07), worst served by button-menu bots
2. **Whale-flow → one-tap follow** — alerts on large fills with a pre-sized BUY button
3. **Copy-trading with a real UI** — PolyCop/Polyfox prove demand, both have poor interfaces
4. **Cross-venue arbitrage (Polymarket ↔ Kalshi)** — nobody does it credibly

Output a scored matrix (weights stated), a recommendation, and **the strongest argument against your own recommendation**. Then state the kill criterion: what observable signal in the first 30 days means we picked wrong.

### D2. Competitor teardown — 6 competitors, structured
For **Betmoar, PolyCop, Polyfox, PolyTrack, Hashdive, Polymarket Analytics**, produce for each:
- Surface (web / Telegram bot / Mini App / native app)
- Onboarding steps to first trade (count them)
- Custody model and key-export availability
- Monetisation (fee bps, subscription, both)
- The three things they do well
- The three things they do badly
- What we copy (pattern) and what we avoid

Where you cannot verify something, mark it `[UNVERIFIED]` rather than guessing. Do not fabricate user counts.

### D3. Feature specification
Rebuild gmgn's product shape for prediction markets. For every feature: **name · one-line user value · priority (P0/P1/P2) · data source (exact endpoint) · rough build cost (S/M/L) · revenue line it serves**.

Cover at minimum:
- Market discovery & trending
- Event/market detail with order book + depth
- Live tape with wallet-type classification (map gmgn's taxonomy onto Polymarket: what is the prediction-market equivalent of "sniper", "KOL", "rat warehouse"?)
- Trader profiles: realised/unrealised PnL, win rate, avg hold, category edge, drawdown
- Whale tracker + alerting
- Wallet Radar equivalent (multi-market wallet intersection — define the four ranking modes for prediction markets)
- Portfolio / positions / order management
- Copy trading
- Rule-based automation (AFK equivalent)
- Leaderboard
- Watchlists & following
- Cross-venue comparison (P2)

**Explicitly list features you are NOT building in year one and why.**

### D4. Data model
Entity-relationship specification, concrete enough to become migrations:
`users · wallets · api_credentials · events · markets · outcome_tokens · trades · positions · orders · fills · alerts · alert_subscriptions · watchlists · follows · copy_configs · automation_rules · builder_attribution · subscriptions · referral_links`

For each: fields with types, indexes, and which upstream API populates it. Call out:
- Which tables are append-only and which are mutable
- Where you need ClickHouse vs Postgres and why
- The reconciliation problem: how you know your local position matches on-chain truth
- How you compute realised PnL for a negRisk multi-outcome market (this is genuinely hard — think it through)

### D5. Metrics that decide whether this is working
Define exactly, with formulas and targets:
- Activation (signup → first trade)
- Routed volume per active user
- D1 / D7 / D30 retention
- Alert → trade conversion
- Free → Pro conversion
- Attributed volume share of platform
- **Revenue per 1,000 routed dollars** (the number that tells you if the builder economics work)

State the week-4 and week-12 gate values. If a gate fails, what changes?

### D6. Risk-adjusted revenue model
Three scenarios (bear / base / bull) at months 3, 6, 12. For each: routed monthly volume, builder bps, subscription users, grants, total ARR. Show the arithmetic. Then state the single assumption that, if wrong by 2×, breaks the model.

---

## Constraints
- No feature without a named data source.
- No revenue projection without arithmetic shown.
- Mark every unverifiable claim `[UNVERIFIED]`.
- If you believe the wedge is wrong, say so and propose a better one. I would rather hear it now.

## Quality gate
Your output must let me answer, without asking you anything: *what are we building, for whom, why will they switch, how much will it cost, and how will we know it's failing?*
