# PolyGM — Complete Build Prompt Pack (combined)

_18 files combined. Paste `00-SHARED-CONTEXT.md` into every session, then the relevant prompt._




---


# SHARED CONTEXT
### Paste this into EVERY session before the task prompt.
*Last verified: 16 Sep 2026. Re-verify anything marked ⚠️ before shipping.*

---

## 1. What we are building

A **non-custodial trading terminal for Polymarket**, modelled on gmgn.ai's product shape (analytics + one-tap execution + Telegram bot), branded in Polymarket's visual language.

- We are **not** an exchange. No custody, no listings, no fiat on-ramp, no order book of our own.
- Users trade into **Polymarket's** order book. Funds live in a wallet the user controls and can export.
- Revenue: **Polymarket Builder Program fees** (primary) + Pro subscription + data/API licensing.

---

## 2. Polymarket APIs — verified live on 16 Sep 2026

| API | Base | Auth | Use |
|---|---|---|---|
| **Gamma** | `https://gamma-api.polymarket.com` | none | events, markets, tags, search, metadata |
| **CLOB** | `https://clob.polymarket.com` | none for data; L2 for orders | books, prices, history, order placement |
| **Data** | `https://data-api.polymarket.com` | none | trades, positions, holders, activity |
| **Leaderboard** | `https://lb-api.polymarket.com` | none | `/volume?window=all&limit=N` |
| **WebSocket** | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | none for market channel | `book`, `price_change`, `last_trade_price`, `tick_size_change` |

### Gamma linking flow (the one everyone gets wrong)
```
GET /events?slug=<slug>  →  markets[].clobTokenIds  (JSON *string*, parse it)
                         →  markets[].conditionId
                         →  use tokenId with CLOB /price, /book
```
A market is tradable only when `enableOrderBook == true`.

### CLOB endpoints
`GET /price`, `GET /prices`, `GET /book`, `POST /books`, `GET /midpoint`, `GET /spread`, `GET /prices-history`, `GET /markets`, `GET /markets/{conditionId}`, `GET /last-trade-price`
Authed (L2): `POST /order`, `POST /orders` (batch ≤15), `DELETE /order/{id}`, `DELETE /orders`, `DELETE /cancel-all`, `GET /data/balance-allowance`, `GET /data/trades`, `GET /orders`

### Per-market fields you MUST check before submitting an order
`accepting_orders` · `seconds_delay` · `minimum_order_size` · `minimum_tick_size` · `neg_risk` · `enable_order_book`

Verified live example (Fed market, 16 Sep 2026): `minimum_order_size: 5`, `minimum_tick_size: 0.001`, `neg_risk: true`, `accepting_orders: true`, `seconds_delay: 0`.

### Rate limits (documented) — **these shape the architecture**
| Endpoint | Burst (10s) | Sustained (10 min) |
|---|---|---|
| CLOB general | 9,000 | — |
| `POST /order` | 5,000 | 120,000 |
| `POST /orders` (batch ≤15) | 2,000 | 21,000 |
| `DELETE /order` | 5,000 | 120,000 |
| `DELETE /orders` | 2,000 | 15,000 |
| `DELETE /cancel-all` | **250** | 6,000 |
| `DELETE /cancel-market-orders` | 1,500 | 21,000 |
| `GET /balance-allowance` | **200** | — |
| Gamma general | 4,000 | — |
| Gamma `/markets` | **300** | — |
| Gamma `/events` | **500** | — |
| Data general | 1,000 | — |
| Data `/trades` | 200 | — |
| Data `/positions` | 150 | — |

Limits are **per-IP** (Cloudflare) *and* **per-signer** (token buckets on orders/cancels). ⇒ Server-side caching is mandatory, not an optimisation.

---

## 3. CLOB V2 — non-negotiable facts

⚠️ **V1 is dead. V2 has been mandatory since 28 April 2026.** Every tutorial pre-dating that is wrong.

| | V1 (dead) | V2 (current) |
|---|---|---|
| SDK | `py-clob-client` / `@polymarket/clob-client` | `py-clob-client-v2` / `@polymarket/clob-client-v2` |
| Constructor | positional args | options object; `chainId` → `chain` |
| Order fields | `nonce`, `feeRateBps`, `taker` | `timestamp` (ms), `metadata`, `builder` |
| Fees | embedded in signed order | **set by protocol at match time** |
| Collateral | USDC.e | **pUSD** (ERC-20, backed by USDC) |
| Builder attribution | `POLY_BUILDER_*` HMAC headers | one `builderCode` field on the order |
| EIP-712 Exchange domain | version `"1"` | version `"2"` (API auth unchanged) |
| Base URL | `https://clob.polymarket.com` | unchanged |

V2 signed order struct: `salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp, metadata, builder`

Signature types: `Eoa`, `PolyProxy`, `PolyGnosisSafe`, `POLY_1271` (type 3, ERC-7739-wrapped, for new deposit-wallet accounts — both maker and signer must be the deposit wallet address).

Two credential layers: **L1** = wallet private key (signs); **L2** = api key + secret + passphrase (CLOB auth). Never hardcode either.

**Because fees are set at match time, you cannot compute a user's exact fee client-side.** Show an estimate; for market buys use an all-in spending limit so the order amount is adjusted for fees before signing.

---

## 4. Fees

### Platform fees (taker-only) — Fee Structure V2, effective 30 Mar 2026
```
platform_fee = C × feeRate × p × (1 − p)
```
| Category | feeRate | Max per 100 shares @ 50¢ | Maker rebate |
|---|---|---|---|
| Politics / Finance / Tech / Mentions | 0.04 | $1.00 | 25% |
| Sports | 0.05 | $1.25 | 15% |
| Economics / Culture / Weather / Other | 0.05 | $1.25 | 25% |
| **Crypto** | **0.07** | **$1.75** | 20% |
| Geopolitics | **0** | $0 | — |

**Makers pay zero** in every category and earn daily rebates in pUSD. No fee at settlement (formula → 0 at p=0 or p=1).

### Builder fees (ours)
- Register at `polymarket.com/settings?tab=builder` → `bytes32` builder code. Approval 1–2 weeks.
- **Caps: taker 100 bps (1%), maker 50 bps (0.5%).** Default 0. Granularity 1 bp.
- `builder_fee = notional × rate_bps / 10000`. **Additive** to platform fees — user pays both.
- Rate changes: one per 7 days, 3 days advance notice, one pending change at a time.
- Only accrues on **matched** orders.
- Maker and taker sides can carry different builder codes and rates.
- Polymarket **can revoke your fee privilege at its sole discretion** and disable your code — orders with a disabled code are rejected by the CLOB. Revocation grounds include "self-referred or non-genuine trading activity."
- Builder profiles and rates are **publicly queryable**.

**⇒ Launch at 0 bps. Betmoar and Stand.trade both charge zero and are top-5.**

### US arm (separate)
Polymarket acquired CFTC-licensed QCEX for $112M; got CFTC approval as a Designated Contract Market (Oct 2025). The US product has its own schedule: uniform 0.05 taker, −0.0125 maker rebate, effective 3 Apr 2026. **Our tool routes to the international CLOB ⇒ geofence US users out of trading.**

---

## 5. Market size — measured live 16 Sep 2026, 15:07 UTC

| Metric | Value |
|---|---|
| 24h volume, top 500 active events | **$59.1M** |
| Liquidity (same set) | **$477.3M** |
| Open interest | **$287.7M** |
| Top-10 events' share of volume | **58.4%** |
| Median event 24h volume | **$19,910** |
| Fill rate on `/trades` feed | **~20.8 trades/sec** |
| Unique wallets per 500 trades | 342 |

Run rate ≈ $600–750M/month, $8–10B/year. Highly concentrated — long tail is dead water.

### Builder ecosystem (your revenue pool)
- Weekly attributed volume hit **$125M** in Feb 2026 (3rd consecutive week >$100M), ~114 builders.
- 30d top 10: **Betmoar $101M**, PolyCop $38.8M, WagerUpPilot $24.2M, PolyTraderPro $18.9M, Polymtrade $13.5M, Kreo $10.8M, Stand $9.6M, Polygun $9.5M, Chance $9.4M, Gate $9.0M.
- Gini **0.83** across top 50. Top 6 = **81%** of lifetime volume. Median top-50 builder: **$4.7M lifetime**.
- Q1 2026: **80% of builders did <$1M for the quarter**; a third never crossed $10k.
- Only disclosed economics: **Based — ~$1M ARR at ~$10M/month** ⇒ implied ~0.83%. PolyTrack estimates 0.5–1%.

### ⇒ Revenue reality
```
$1M ARR ÷ 1.0% = $100M/yr routed = $8.3M/month  →  top-3-builder territory
```
**Year 1 realistic: $80k–$400k.** $1M ARR is a year-2/3 target. Never let builder fees exceed ~60% of revenue — Polymarket can switch it off.

---

## 6. Competitors

**Analytics (saturated, $6–30/mo):** Hashdive (72k visits/mo, free), Polywhaler (30k, the original), Polymarket Analytics (free + $20/mo, 1 ETH lifetime), Polysights (24k, pre-1.0), PolyTrack ($9.99/wk, $19/mo), PredictFolio (free, CC BY-NC), OrcaLayer ($9.99/$19.99), PolyMonit ($5.99/$9.99), PolySharks ($19.99), Unusual Predictions ($30–80/mo), Alphascope, Polyburg, Merlin Trade.

**Telegram bots:** Betmoar (leader, anonymous, no press), PolyCop, TradePolyBot, Polyfox, Polygun, PolyBot, Polylerts, PolyTracker Bot, PolyxBot, PolyCopy, Polycopybot, Kreo, Stand.trade, Chance, Rainbow.

**The gap:** every Telegram bot here is an inline-button menu. Nobody ships a real *terminal* inside Telegram. That's the wedge.

**Benchmark:** gmgn.ai does **$46.74M fees / $38.35M revenue per 30 days** ($167.52M annualised revenue), on a flat ~1% per-trade fee with a custodial hot wallet. That's the category ceiling, not prediction markets'.

---

## 7. Brand tokens — extracted from polymarket.com's compiled CSS

⚠️ These are Polymarket's production tokens. **Do not ship Polymarket's exact logo, wordmark, or marketing copy.** Use the palette and type as inspiration and build an original identity (see P2).

### Brand (blue) scale
```
light:  50 #f4fcff   100 #cadfff  200 #a0c1ff  300 #76a2ff  400 #4e7fff
        500 #2e5cff  600 #1c3fe2  700 #0f1ac6  800 #0c00a4  900 #06006f
dark:   50 #020041  100 #060071  200 #0d00a8  300 #1020c9  400 #1e44e7
        500 #3262ff  600 #5485ff  700 #7ba5ff  800 #a1c0ff  900 #c7daff
```
**Primary action: `#2e5cff`** (this is `--color-pk-brand-500`, used as the progress-bar colour).

### Neutral scale (note: 50 = darkest in dark theme, lightest in light)
```
dark:   50 #0c0e13 (base bg)  100 #1c1f27 (elevated)  200 #2d3037 (border)
        300 #45474e  400 #62646a  500 #7d8189  600 #8b929b  700 #6b727b
        800 #454a52  900 #23272d  950 #000
light:  50 #fff      100 #f4f5f7  200 #e6e8ec  300 #d4d7dc  400 #bfc3ca
        500 #abb2bb  600 #999ea7  700 #b6bbc4  800 #d2d4d9  900 #edeff1
```
### Semantics
`--color-pk-surface: gray-50` · `--color-pk-elevated: gray-100` · `--color-pk-border: gray-200` · `--color-pk-brand-strong: brand-700` · `--color-pk-brand-subtle: brand-200` (dark) / `brand-50` (light)

### Data-viz palette (Polymarket's own chart colours)
`#87BFFF` · `#4378FF` · `#2797FF` · `#FDC503` · `#FF7F0E` · `#144E8C`

### Semantic (derived from their CSS — ⚠️ not confirmed as named tokens)
success/yes `#16a34a` · danger/no `#ef4444` · warning `#f1ce57`

### Type
```
Body:      Inter Variable (100–900)
Headlines: Instrument Sans Condensed (600, 700)   ← the distinctive one
Numbers:   Geist Mono
Display:   Open Sauce One
```
### Radius & weight
`--radius: .7rem` (11.2px) · `xs` −6px · `sm` −4px · `md` −2px · `lg` = base · `xl` +4px · `2xl` 1rem · `3xl` 1.5rem
Weights: light 300 · normal 400 · medium 500 · semibold 600 · bold 700 · extrabold 800

### Feel
Dense, data-first, financial-terminal. Light is the default brand surface; **build dark-first for the terminal** and ship light as parity. High information density, tight type, mono for every number, thin borders, minimal shadow, no gradients on chrome.

---

## 8. gmgn's information architecture (patterns to rebuild, not copy)

Extracted from gmgn's own documentation. These are the product patterns that make the product work.

**Terminal = 3 columns**
- **Left rail:** Holding / Watchlist / Following lists, then Trending / Pump (new) lists
- **Centre:** chart (multi-chart up to 8, selectable granularity, trade markers on candles, avg-price lines, limit-order lines) → below it the activity tabs: **Activity · Traders · Holders** → **Position / Limit / Auto**
- **Right rail:** project metadata/socials, the **trading module** (wallet switcher), pool/contract info

**Activity classification taxonomy** — every trade is tagged by wallet type:
`smart money · KOL/VC · whale · new wallet · sniper · large holder · developer · followed · rat warehouse`

**Per-trader metrics:** SOL bal / account age · funding source + transfer time · bought / sold · total PnL (realised + unrealised) · avg cost / avg sold · TXs (buys green, sells red)

**Wallet profile (7D/30D):** PnL % + amount · win rate · balance · TX counts · distribution of buys/sells · **phishing check**: blacklist count, "didn't buy" (transfer-in) count, sold>bought count, **buy/sell within 10s count** (bot/copy-farm detector)

**Wallet Radar** — scan up to 10 tokens at once, rank wallets four ways: **Most Bought · Highest Profit · Earliest Bought · Shared Holdings.** Then one-click Track or Copy Trade.

**Copy trading:** follow up to 10 wallets, set multiplier, per-trade cap, daily cap, category filter, TP/SL, optional dev-sell automation.

**AFK automation:** rule-based auto-buy/auto-sell, limit orders, TP/SL, condition triggers, executes unattended.

**Alerts:** FOMO alerts on new listings, sub-second exchange-listing alerts, wallet alerts with sound toggle, push via Telegram bot.

**Two surfaces, one wallet:** web terminal for scanning/analysis, Telegram bot for fast execution. Same account, same balance.

---

## 9. Telegram constraints

- Digital goods/services sold **inside** Telegram must be paid for **exclusively in Telegram Stars (XTR)**. No crypto, no third-party processor inside the Mini App. This is for App Store/Play compliance.
- Stars ≈ $0.013–0.015. App stores take up to 30%. Withdrawal via Fragment: 21-day hold, 1,000-Star minimum.
- ⇒ **Sell Pro on the website (Stripe + crypto). Offer a Stars-priced equivalent inside Telegram.**
- If you distribute your own token via a Mini App, Telegram requires **TON** and removes apps distributing Ethereum/BNB assets. **No token in year one.**
- Mini App = full web app inside Telegram, inherits user identity, no separate login.
- BotFather is free. Bot usernames are permanent-ish — pick carefully.

---

## 10. Regulatory posture

- **Non-custodial ⇒ not an exchange.** No MSB registration, no money-transmitter licences, no VASP registration for the tool itself.
- **But** you operationally hold keys that can move user money. Treat it like a custodian even though you aren't one legally. One breach ends the company.
- **Geofence US users out of trading** (international CLOB ≠ US-regulated product).
- **No "guaranteed returns" language anywhere.** Show losing wallets next to winning ones. Copy-trading must display drawdown, not just PnL.
- **India (founder is in Surat):** a non-custodial analytics/execution tool for global users is not a reporting entity under FIU-IND's VDA regime the way an exchange is. Get one paid legal opinion rather than guessing. Model 30% VDA tax + 1% TDS for any Indian users.
- **Never wash-trade to climb the builder leaderboard.** That's an explicit revocation ground.

---

## 11. Reference implementation

`/home/user/polygm/server.py` — zero-dependency Python: live Gamma + CLOB + Data + leaderboard ingestion, shared cache refreshed every 20s, 5 JSON endpoints, static serving. Verified working: 60 events, 300 tape rows, 24 books, 25 leaderboard rows, `errors: []`.

`/home/user/polygm/public/index.html` — mobile-first Mini App UI, 4 tabs, live tape with whale flags, order-book depth sheet, paper-trade flow.

Use it as the starting point for P4–P6, not as a throwaway.



---


# Build Prompt Pack — PolyGM
### 16 sequential prompts to build a Polymarket trading terminal + Telegram bot

A gmgn-style product: analytics terminal + one-tap execution + Telegram bot, on Polymarket's order book. Branded in Polymarket's visual language.

---

## First, one legal correction

You asked for "the same ui/ux as gmgn.ai" and "the same telegram bot." I won't write a prompt that says *clone this site* — copying another product's markup, icons, copy text, or visual assets is copyright/trade-dress infringement, and it's the kind of thing that gets an app-store listing pulled or a C&D sent two weeks after you get traction.

What these prompts **do** give you is the thing that actually matters: **gmgn's information architecture and interaction model, rebuilt from scratch.** I pulled their own documentation and extracted the real patterns — the three-column terminal layout, the left-rail watchlist/trending split, wallet classification taxonomy (smart money / KOL / whale / sniper / new wallet / rat warehouse), Wallet Radar's four ranking modes, the 7D/30D PnL + win-rate profile, phishing-check metrics, copy-trade config, AFK automation. Those are **product patterns, not protected expression.** You get the same product; you own the code.

Everything here is original work in a documented pattern, wearing Polymarket's colours.

---

## How to use this pack

**Order matters.** Each prompt assumes the output of the ones before it. Don't skip P0.

| # | File | What you get out of it |
|---|---|---|
| — | `00-SHARED-CONTEXT.md` | **Paste this into every single session first.** Verified API facts, brand tokens, constraints. |
| P0 | `P00-README.md` | This file |
| P1 | `P01-research.md` | Validated wedge, competitor teardown, feature spec, data model |
| P2 | `P02-branding.md` | Name, logo, voice, full brand kit |
| P3 | `P03-design-system.md` | Design tokens, component library, every screen spec'd |
| P4 | `P04-backend-architecture.md` | Repo scaffold, schema, API contracts, infra |
| P5 | `P05-data-ingestion.md` | Ingest + WebSocket + signals + alert fanout |
| P6 | `P06-trading-engine.md` | Wallets, CLOB V2 orders, risk service, copy-trading |
| P7 | `P07-security.md` | Key management, authN/Z, threat model, hardening |
| P8 | `P08-frontend-shell.md` | Auth, signup/signin, profile, nav, billing |
| P9 | `P09-frontend-markets.md` | Markets, event detail, order book, charts |
| P10 | `P10-frontend-terminal.md` | The terminal: tape, traders, whale tracker, radar, portfolio |
| P11 | `P11-leaderboard.md` | Leaderboard + trader profiles + referrals |
| P12 | `P12-telegram-bot.md` | Mini App + bot commands + alerts channel |
| P13 | `P13-testing.md` | Unit → integration → E2E → load → chaos |
| P14 | `P14-security-testing.md` | Pentest, key-compromise drills, dependency audit |
| P15 | `P15-deployment.md` | CI/CD, staging→prod, observability, runbooks |
| P16 | `P16-launch-growth.md` | Go-to-market, distribution, retention, metrics |

---

## Operating rules for every session

Paste these at the top of each prompt:

1. **Do not invent API behaviour.** Every Polymarket endpoint, field name, and limit must be checked against `docs.polymarket.com` before you write code against it. CLOB V1 is dead.
2. **Fail closed.** If a price, balance, or book is stale or missing, the UI must say so and disable trading. Never render a stale number as if it were live.
3. **No money moves without the risk service.** Every order path goes through the risk gate. No exceptions, including admin tooling.
4. **No secrets in code, logs, errors, or telemetry.** Ever.
5. **Read-only by default.** New endpoints are read-only until explicitly made write.
6. **Write tests before you claim done.** "It works" without a test that ran is not done.
7. **Surface unknowns.** If you cannot verify something, say so in the deliverable rather than filling the gap with a plausible guess.

---

## Sequencing against budget

You have <$10k and you're hiring. Run it like this:

- **Weeks 1–2:** P1, P2, P3 (cheap — mostly you + AI, no dev needed)
- **Weeks 3–6:** P4, P5, P8, P9 → **read-only product live.** This is your Phase 0 distribution play.
- **Weeks 7–10:** P7, P6, P12 → trading + bot. First routed dollar.
- **Weeks 11–12:** P13, P14, P15 → harden and ship.
- **Ongoing:** P10, P11, P16.

If money runs out after week 6, you still have a live product with an audience. That's the point of the ordering.

---

## Reference implementation

`/home/user/polygm/` contains a working, verified prototype: zero-dependency Python backend pulling live Gamma + CLOB + Data + leaderboard APIs into a shared cache, plus a mobile-first Mini App front end. It is the thing P4–P6 replace, and the proof that the data layer works. Point your developer at it before they start.



---


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



---


# P2 — Brand & Identity

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are a brand designer who has built identities for trading products (not lifestyle brands). You design for density, legibility at 11px, and instant recognition in a Telegram chat list. You dislike decoration.

## Objective
Produce a complete, implementable brand identity for the product, **inspired by Polymarket's visual language but legally original**.

## Hard constraints — read before designing

1. **Do not reproduce Polymarket's logo, wordmark, icon, or marketing copy.** We are taking inspiration from their *palette and typographic register*, not their identity. If a design could be mistaken for an official Polymarket product, it is wrong — and it will get us a C&D the week we get traction.
2. **Do not reproduce gmgn's logo, mascot, icons, or copy.** We are rebuilding their information architecture, not their art.
3. The name must not contain "Polymarket", "Poly" as a standalone prefix suggesting affiliation, or "gmgn". ⚠️ Note that most competitors already use "Poly-" prefixes (PolyCop, Polyfox, PolyTrack, PolyMonit, Polywhaler, PolySharks, Polygun, PolyCopy, Polycopybot, Polylerts). That namespace is exhausted and it makes every product look like a Polymarket subsidiary. **Prefer a name outside it.**
4. Must survive: 16px favicon, Telegram bot avatar at 512px and at 40px in a chat list, a dark terminal, a light marketing page, and a monochrome fax.

---

## Deliverables

### D1. Naming — 12 candidates, then one
For each: the name, what it means/evokes, a one-line rationale, and the risk (trademark collision likelihood, pronunciation ambiguity, domain availability assumption).

Constraints: ≤9 characters preferred, pronounceable by a non-native English speaker (our audience is global — heavy India, SEA, LatAm, CIS), works as a Telegram `@handle`, no unfortunate meaning in Hindi/Gujarati/Spanish/Portuguese/Russian/Arabic/Chinese.

Then pick one and justify. Include a fallback if the domain is taken.

### D2. Positioning statement
One sentence, ≤15 words, of the form: *For [who], [product] is the [category] that [unique benefit], unlike [alternative] which [weakness].*

Then: the three messages for the landing page, in the voice of a trader talking to a trader. No "revolutionising", no "seamless", no "empowering".

### D3. Logo system
Describe precisely enough to brief an illustrator or generate:
- **Primary mark** — concept, geometry, construction grid, minimum size
- **Wordmark** — typeface treatment, letter-spacing, capitalisation
- **Stacked and horizontal lockups**
- **Monochrome and single-colour variants**
- **Avatar crop** (the square Telegram/Discord version) — must read at 40px
- What the mark must NOT do (no candlestick clichés, no rocket, no crystal ball, no dice, no chart-arrow-through-a-letter)

Provide 3 distinct concept directions with rationale, then recommend one.

### D4. Colour system
Build on the extracted Polymarket tokens (§7 of shared context) but make it ours:

- **Semantic tokens** with exact hex for **dark and light**: `bg.base`, `bg.elevated`, `bg.inset`, `border.default`, `border.strong`, `text.primary`, `text.secondary`, `text.muted`, `text.inverse`
- **Brand:** primary, primary-hover, primary-active, primary-subtle, on-primary
- **Trading semantics** — this is the important one. Prediction markets are YES/NO, not long/short:
  - `yes` / `no` (outcome colours)
  - `buy` / `sell` (action colours)
  - `profit` / `loss` (PnL colours)
  - Decide explicitly: **do YES/NO and BUY/SELL use the same green/red?** In gmgn they do. In prediction markets that creates real ambiguity — a red "NO" position that is profitable. Resolve this and explain the resolution.
- **Chart palette** — 8 distinct categorical colours that survive colour-blind deuteranopia and protanopia simulation, readable on both themes, and distinct from the yes/no semantics
- **Alert severity:** info / watch / high / critical
- Contrast ratios for every text-on-background pair, to WCAG AA minimum (4.5:1 body, 3:1 large). Show the numbers.

Output as CSS custom properties in both `:root` (light) and `[data-theme="dark"]`, plus a JSON token file for the design system.

### D5. Typography
- Type scale: 10 named steps from 11px to 32px with line-height and letter-spacing for each
- **Numeric display rules** — prices, PnL, volume, percentages. Tabular figures mandatory. Define the exact treatment for: prices in cents (`63.5¢`), USD (`$1.24M`), signed PnL (`+$412.09` / `−$88.40`), percentages, and timestamps
- Body, UI, headline, and mono assignments
- Webfont strategy: subsetting, `font-display`, fallback stack with metric overrides, total payload budget in KB
- The condensed-headline trick Polymarket uses (Instrument Sans Condensed) — decide whether we adopt an equivalent and name a licensed/self-hostable substitute

### D6. Iconography & icon set spec
Specify the icon set we need, one line each, with the visual metaphor: markets, tape, whales, traders, radar, portfolio, alerts, copy, automation, settings, deposit, withdraw, export-key, verified, suspicious, insider, sniper-equivalent, new-wallet.

Style rules: grid, stroke weight, corner treatment, optical corrections. Must be drawable in a single colour.

### D7. Voice & microcopy
- The tone in three adjectives, and three adjectives we are avoiding
- **Write the actual copy** for: empty states (no positions, no alerts, no results), loading, error, rate-limited, stale data, order rejected, insufficient balance, key export confirmation, withdrawal confirmation, first-trade onboarding, risk disclosure
- Rules for numbers in copy (always tabular, always signed for PnL, never round a loss in the user's favour)
- What we never say: "guaranteed", "safe", "sure thing", "insider tip", "free money"

### D8. Brand assets checklist
Favicon set, OG image (1200×630) concept, Telegram bot avatar, Discord emoji set, 404 page concept, email templates (welcome, key-export warning, large-withdrawal confirmation).

---

## Quality gate
Hand this to a developer with no design taste and they should produce something coherent. Every colour has a hex. Every text pair has a contrast ratio. Every screen state has copy. Nothing says "TBD".



---


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



---


# P4 — Backend Architecture & Scaffold

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are a staff backend engineer who has built low-latency market-data and order-execution systems. You are allergic to premature abstraction and to services that cannot be debugged at 3am. You write boring, observable, testable code.

## Objective
Design and scaffold the backend: monorepo layout, service boundaries, database schema, API contracts, infrastructure, and the development environment. Produce runnable code, not a diagram.

## Non-negotiable architectural rules

1. **The trading service holds keys and has no inbound internet exposure.** It talks to the outside world only via an internal queue. The public API server must never have a private key in its process, its memory, or its environment.
2. **Server-side caching is mandatory, not an optimisation.** Polymarket's limits are per-IP and per-signer (see §2 of shared context). One user must not be able to cause us to exhaust our own rate budget.
3. **Every order path goes through the risk service.** Including admin tooling. Including tests that hit production.
4. **Append-only ledgers for anything money-shaped.** Orders, fills, deposits, withdrawals, fee accruals. Corrections are new rows, never updates.
5. **Idempotency keys on every mutating endpoint.** Retries are normal.

---

## Deliverables

### D1. Service topology
Justify each boundary. At minimum:
- `api` — public HTTP/WS, stateless, horizontally scalable, **no secrets beyond a DB credential**
- `ingest` — Gamma/CLOB/Data pollers + CLOB WebSocket consumers → normalises → writes
- `signals` — reads the normalised stream, evaluates alert rules, publishes to a fanout topic
- `executor` — the only service with wallet keys. Consumes order intents, signs, submits, reconciles
- `risk` — synchronous gate in front of `executor`. Owns limits and the kill switch
- `billing` — Stripe + Telegram Stars reconciliation, entitlements
- `notifier` — Telegram/email/push delivery
- `worker` — scheduled jobs, backfills, leaderboard recompute

For each: language, responsibilities, what it owns in the DB, what it must never touch, scaling unit, failure mode, and what happens to users when it dies.

### D2. Tech stack — choose and defend
Recommend one stack and justify it against the alternative you rejected. Constraints: <$10k budget, 1–2 contractors, must be debuggable by a non-expert, must have excellent Polymarket SDK support.

Consider: Python (FastAPI + `py-clob-client-v2`) vs TypeScript (NestJS/Fastify + `@polymarket/clob-client-v2`) vs split (TS frontend + Python executor). State which you pick and why the SDK situation decides it.

Then: Postgres (managed), ClickHouse vs TimescaleDB for the tape, Redis, queue (NATS vs Redis Streams vs SQS — pick one and say why), object storage, and the hosting choice (Hetzner vs Fly vs Railway vs AWS) with a monthly cost estimate.

### D3. Database schema — real migrations
Write the SQL. Every table, column, type, constraint, index, and the reasoning for non-obvious indexes. Cover the entities in `P1 D4`, plus:
- `order_intents` (requested) → `orders` (submitted) → `fills` (matched) — the three-stage money path
- `builder_attribution` — every order we submit, with our builder code, so we can reconcile our revenue against Polymarket's on-chain events independently
- `position_snapshots` — for reconciliation against on-chain truth
- `idempotency_keys`, `audit_log`, `kill_switch_state`

Answer explicitly:
- How do you compute **realised PnL on a negRisk multi-outcome market**? A user can hold YES on three mutually exclusive outcomes and merge them. Show the logic.
- What is the source of truth for a balance — our DB or `GET /data/balance-allowance`? How often do you reconcile, given that endpoint is limited to 200 requests per 10 seconds across all users?
- How do you backfill 30k markets of history without blowing the Gamma `/markets` limit of 300 per 10 seconds?

### D4. API contracts — OpenAPI 3.1
Write the spec. REST for CRUD, WebSocket for streams. Cover: auth, markets, events, books, tape, traders, leaderboard, radar, alerts, watchlists, portfolio, orders, copy-configs, automation-rules, wallet, billing, referrals, admin.

For every endpoint: method, path, auth level (public / user / admin), request schema, response schema, error envelope, rate limit class, cacheability.

Rules:
- Consistent error envelope with a machine-readable `code` and a human `message` that never leaks internals
- Cursor pagination everywhere, never offset on large tables
- Every price/quote response carries `asOf` and `staleAfter` timestamps
- Versioned under `/v1/`
- Read-only by default; write endpoints require an explicit `Idempotency-Key`

### D5. The order path — sequence it precisely
From "user taps BUY" to "fill recorded", including:
1. Client builds intent with `Idempotency-Key`
2. API validates auth, entitlement, and market state (`accepting_orders`, `seconds_delay`, `enable_order_book`)
3. **Risk gate** — synchronous, <50ms, with its own circuit breaker
4. Intent enqueued
5. Executor: loads wallet, checks tick alignment against `minimum_tick_size`, checks `minimum_order_size`, builds the V2 order struct (`timestamp`, `metadata`, `builder`), signs, `POST /order`
6. Response handling for each documented failure: rejected, rate-limited, insufficient balance, off-tick, market closed, disabled builder code
7. Fill ingestion via WebSocket + reconciliation poll
8. Attribution row written
9. User notified

Include the timeout and retry policy at each hop, and what the user sees in each failure case. **Specify what happens when the executor dies between signing and receiving the response** — this is the case that loses money.

### D6. Repository scaffold — runnable
Produce the actual tree with real files: `docker-compose.yml` (Postgres, ClickHouse, Redis, NATS, api, ingest, executor-mock), `Makefile` with `dev/test/lint/migrate/seed`, CI config, `.env.example` with every variable documented and **no real values**, logging setup with structured JSON and request IDs, health and readiness endpoints, graceful shutdown.

Include an **`executor-mock`**: a fake CLOB that accepts signed orders and returns configurable fills. This lets the whole product be built and tested with zero money and zero risk. This is not optional.

### D7. Configuration & feature flags
Every tunable in one place: rate limits, cache TTLs, freshness thresholds, risk limits, builder bps, feature flags. Explain how a flag is changed without a deploy, and how you audit who changed it.

---

## Constraints
- No secrets in code, logs, errors, or telemetry.
- Every external call has a timeout, a retry policy, and a circuit breaker. Name the values.
- No endpoint without a test.
- `docker compose up` must work on a clean machine in under 5 minutes.

## Quality gate
`make dev` starts the stack, `make test` passes, and I can place an order against `executor-mock` end to end with a curl command you provide.



---


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



---


# P6 — Wallets, Trading Engine, Copy & Automation

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are a backend engineer who builds order-execution systems and has been responsible for other people's money. You are paranoid about the case where you signed an order and never learned whether it filled. You would rather reject a valid trade than execute an ambiguous one.

## Objective
Build the trading plane: wallet lifecycle, CLOB V2 order execution, the risk gate, reconciliation, copy-trading, and rule-based automation. **This is the part that earns revenue** — every matched order carrying our builder code is money.

## Absolute rules
1. **Only the executor service touches private keys.** It has no inbound network exposure. It consumes from a queue and writes to a DB.
2. **Every order goes through the risk gate synchronously.** No bypass path, including admin and tests.
3. **If we cannot determine whether an order was submitted, we do not resubmit blindly.** We reconcile first.
4. **The kill switch stops everything within one second**, including in-flight automation and copy-trading.
5. **Never log a key, a signature, or a full order payload.** Log the order ID and the token ID.

---

## Deliverables

### D1. Wallet lifecycle
- **Creation:** one Polygon wallet per user, created on first login, not at signup. Specify the provider (Turnkey vs Privy vs Dynamic vs self-hosted) and justify against cost, key policy granularity, audit posture, and exit cost. Turnkey publishes a Polymarket builders cookbook — evaluate it seriously.
- **Key policy:** the key must be scoped so it can *only* interact with the CLOB exchange contract and the pUSD token. It must not be able to approve arbitrary contracts. Specify the policy in the provider's terms, and specify what happens if the provider cannot express it.
- **Funding:** deposit USDC/USDT from Polygon, Base, Ethereum, Arbitrum, BSC → detect → bridge → swap to **pUSD**. Specify the bridge/SWAP provider, the failure handling for a stuck bridge, and the minimum deposit that is economically sensible after fees.
- **Approvals:** pUSD allowance to the CLOB exchange. Who pays the gas, when is it refreshed, and what happens when the allowance is exhausted mid-session.
- **Withdrawal:** to a user-specified address, with typed confirmation, withdrawal password, address allowlist with a cooldown on new addresses, and an email/Telegram confirmation that cannot be suppressed.
- **Key export:** must work, must be obvious, must be logged, and must be presented with a real warning. Every serious competitor offers it and users check before depositing.
- **Signature type:** decide between EOA, PolyProxy, PolyGnosisSafe, and POLY_1271. New accounts can use deposit wallets with `signatureType = 3` (POLY_1271, ERC-7739-wrapped, where maker and signer must both be the deposit wallet address). Justify the choice — gasless relayer support is the deciding factor for UX.

### D2. CLOB V2 order execution
⚠️ **V1 is dead since 28 April 2026.** Use `py-clob-client-v2` / `@polymarket/clob-client-v2`. Pin exact versions and write an adapter layer so the next migration is a config change, not a rewrite.

Implement:
- L2 credential derivation and secure storage (`create_or_derive_api_creds`)
- Order construction: `salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp (ms), metadata, builder`
- **Our `builderCode` on every order from day one**, even at 0 bps — attribution history is what earns a grant
- Order types: GTC, GTD, FOK, FAK, market. Specify which we expose to users and which only to automation.
- **Pre-flight validation**, in order: market `accepting_orders` → `enable_order_book` → `seconds_delay` elapsed → price on `minimum_tick_size` grid → size ≥ `minimum_order_size` → balance + allowance sufficient for notional **plus estimated platform fee plus our builder fee** → risk gate
- Batch submission (`POST /orders`, ≤15) for automation, with partial-failure handling
- Cancellation: single, batch, by-market, and `cancel-all` — noting `cancel-all` is limited to **250 per 10 seconds** across all users, so it must be a scarce, rate-limited, audited operation

**Fee handling:** fees are set by the protocol at match time and are *not* in the signed order. So:
- Estimate the platform fee with `C × feeRate × p × (1−p)` using the per-category rate, and our builder fee as `notional × bps / 10000`
- Display the estimate as an estimate, with the range
- For market buys use an **all-in spending limit** so the order amount is adjusted for fees before signing
- After the fill, reconcile the actual fee and store the delta. Track our estimate accuracy as a metric — if it drifts, users get surprised.

### D3. The reconciliation problem — the case that loses money
Answer all of these explicitly, with code:

1. **Executor crashes after signing, before receiving the HTTP response.** The order may or may not be on the book. What do you do on restart? (Blind resubmit = double position. Blind assume-failed = user thinks they have no position while they do.)
2. **HTTP timeout on `POST /order`.** Same ambiguity, different cause.
3. **Order accepted but fill event never arrives over WebSocket.** How long do you wait, and what do you poll?
4. **Partial fill.** Then the remainder is cancelled by the user from another surface (Telegram while the web app is open).
5. **Our DB says position X, `GET /data/positions` says Y.** Who wins, how do you detect it, and what does the user see? Note `/positions` is limited to 150 requests per 10 seconds across all users — you cannot poll it per user.
6. **`GET /data/balance-allowance` is limited to 200 per 10 seconds.** Design the balance strategy that does not depend on polling it per user.
7. **A market resolves while the user holds a winning position.** Redemption of CTF positions — who triggers it, who pays gas, and what happens if it fails.
8. **negRisk merge/split.** A user holding YES on multiple mutually exclusive outcomes can merge them. Support it or explicitly refuse it, and say which.

Design a **single reconciler** with one durable cursor that owns all of the above, and a metric that reports unreconciled orders. **Alarm when it is non-zero for more than 60 seconds.**

### D4. Risk service
Synchronous gate, target <50ms, with its own circuit breaker that **fails closed**.

Limits (all per-user, all configurable, all auditable):
- Max notional per order
- Max open notional per market and in total
- Max orders per minute (protects us from a runaway automation loop draining a wallet *and* from us hitting the CLOB signer bucket)
- Max daily loss → halt trading for the user until they acknowledge
- Max slippage vs the quoted price at intent time
- Price sanity: reject orders at prices that moved more than N cents since the quote
- New-market cooldown: respect `seconds_delay`
- **Global kill switch** — one flag stops all order submission platform-wide within one second. Include a drill that proves it.
- Per-market blocklist (e.g. markets under UMA dispute)

Every rejection returns a machine-readable code the UI can explain in plain language. **A user whose order was rejected for a reason they cannot understand will not retry — they will leave.**

### D5. Copy trading
- Target: any wallet address. Show its real track record **including drawdown and losing streaks**, not just PnL. This is both an ethical requirement and the thing that reduces chargebacks.
- Config: multiplier or fixed size, per-trade cap, daily cap, category filter, minimum/maximum price bounds (do not copy a 0.99 entry), TP/SL, and a "do not copy into markets resolving within N hours" rule
- **Latency reality:** by the time we see a fill, the price has moved. I measured ~21 fills/sec; a whale entering at 0.60 may be at 0.65 before we react. Build the config around this: max price deviation from the source fill, and skip-instead-of-chase as the default.
- Source wallet stops trading / gets flagged as insider-suspect → what happens to copiers?
- Copying a wallet that is itself copying → detect and warn
- Every copied trade is attributed to us for builder fees. Track per-copy-source economics so we can see which sources are worth promoting.

### D6. Automation (the "AFK" equivalent)
A rule engine that runs unattended:
- Triggers: price crosses threshold (up/down), book imbalance, volume spike, time-of-day, market created in category, watched wallet acts, X minutes before resolution
- Actions: market buy, limit buy at offset, sell N% of position, close all in market, take profit, stop loss
- Conditions compose with AND/OR. Keep the builder visual — no user writes expressions.
- **Every rule goes through the same risk gate as a manual order.** No exceptions.
- Run history: every evaluation, every fired action, every skip and why. Users must be able to audit why the bot did what it did.
- Per-user concurrent-rule cap, and a global cap so one user cannot consume the signer bucket
- Dry-run mode mandatory before a rule can go live

**Special case — the 5-minute crypto markets.** These are the highest-frequency, highest-fee-rate (0.07) markets on the platform and the best fit for automation. Design the specific rule template: enter within the first N seconds, exit on reversal, hard exit before resolution. Include the fee arithmetic showing at what win rate the 0.07 taker fee plus our builder fee makes this profitable — **if the arithmetic does not work, say so and do not ship the template.**

### D7. Order lifecycle & user feedback
State machine: `draft → intent → risk_passed → signing → submitted → open → partially_filled → filled | cancelled | rejected | unknown → reconciled`

`unknown` is a real state. Design the UI for it (see P3) — the user must know their order is in limbo and what we are doing about it.

Notifications for: filled, partially filled, cancelled, rejected (with the reason in plain language), TP/SL triggered, automation fired, daily loss halt, kill switch engaged.

### D8. Builder revenue accounting
- Every order we submit writes an attribution row: our builder code, side, bps, notional, expected fee
- A daily job reconciles our expected fees against on-chain `OrderFilled` events (the `builder` field is in the event) and reports the delta
- Dashboard: attributed volume, expected fees, actual fees, per-market and per-source breakdown
- **This is our revenue. If we cannot measure it independently of Polymarket's dashboard, we are flying blind.**

---

## Constraints
- No private key outside the executor process. Not in the API, not in logs, not in error reports, not in Sentry breadcrumbs.
- Every order path is idempotent.
- Every automated action is attributable to a rule ID and a user.
- Ship against `executor-mock` first. Real money only after the chaos tests in P13 pass.

## Quality gate
A test that: places an order, kills the executor mid-flight, restarts it, and proves the position is reconciled to the correct state with no duplicate order and no user-visible inconsistency. Show it running.



---


# P7 — Security Architecture

> Paste `00-SHARED-CONTEXT.md` first, then this.

## Role
You are an application security engineer who has done incident response for a crypto product that held user keys. You have written the post-mortem. You design so that the post-mortem never gets written.

## Objective
Produce the security architecture: threat model, key management, authentication and authorisation, transport and data protection, dependency and supply-chain controls, and the incident response capability. Produce implementable controls, not a compliance checklist.

## The one thing to internalise
We are **legally non-custodial and operationally custodial**. We hold keys that can move user money. There is no regulator forcing us to secure them and no insurer bailing us out. **One breach ends the company.** Everything below follows from that.

---

## Deliverables

### D1. Threat model — STRIDE per component, ranked by expected loss
Cover at minimum these components: Telegram bot + Mini App, web app, public API, ingest, signals, executor, risk, billing, wallet provider integration, the alert channel, admin tooling, CI/CD.

For each threat: actor, capability required, attack path, impact in dollars and in trust, likelihood, and the specific control that reduces it. Rank the top 15 by expected loss.

Make sure you cover the ones people forget:
- **Malicious market metadata.** Market titles and descriptions come from Polymarket and are attacker-controllable — anyone can create a market. A title containing markup, a homoglyph, or a fake resolution source is a phishing vector rendered in *our* UI. Specify sanitisation and the display rules.
- **Poisoned alert channel.** If an attacker can get a market created and traded, they can get our free channel to broadcast it to thousands. Specify the quality gate before anything is broadcast.
- **Clipboard/address substitution** on deposit and withdrawal.
- **Support impersonation** — the single most common attack on Telegram trading products. Competitors' users are actively warned about it. Design the defence into the product, not into a FAQ.
- **Rate-limit exhaustion as a denial of service against our own upstream budget.** One user with 200 alert rules can consume our per-IP Cloudflare budget and break the product for everyone.
- **Wash trading through our own builder code.** Polymarket explicitly revokes builder codes for "self-referred or non-genuine trading activity." A user gaming our volume can cost us our entire revenue line. Design the detection.
- **Referral abuse.**
- **Insider risk** — a contractor with production access. We are hiring; assume someone will be disgruntled.

### D2. Key management — the crown jewels
Decide and fully specify:

| Option | Cost | Verdict |
|---|---|---|
| Managed embedded wallets (Turnkey / Privy / Dynamic) | per-wallet/mo | Evaluate seriously; Turnkey publishes a Polymarket builders cookbook |
| Self-hosted: AES-GCM envelope + cloud KMS/HSM | infra + audit | Requires a real audit ($15–30k) before meaningful TVL |
| User imports own key | $0 | Terrible UX; offer as a pro escape hatch only |

Whichever you choose, specify:
- **Key policy:** the key may only call the CLOB exchange contract and the pUSD token. No arbitrary approvals. If the provider cannot express this, that is a disqualifying finding — say so.
- **Envelope encryption** if self-hosted: DEK per wallet, KEK in HSM, rotation procedure, and the ceremony for it
- **The signing path:** what leaves the secure boundary (a signature) vs what never does (the key)
- **Break-glass procedure** — who can sign an emergency withdrawal, how many people, what audit trail
- **Key export** flow with typed confirmation and an immutable audit entry
- **Compromise response:** detect, revoke, notify. How fast can we revoke 10,000 keys? What do users lose? Write the drill.
- **What we do NOT hold:** never a seed phrase for a user's external wallet; never a withdrawal destination we chose

Also cover the **L2 CLOB credentials** (api key / secret / passphrase) — these are separate from the wallet key and are equally sensitive. Same treatment.

### D3. Authentication
- Email + password with Argon2id (state parameters), or a delegated provider — pick and justify
- **Telegram OAuth** (`initData` validation): the signature check against the bot token HMAC, the `auth_date` freshness window, and the replay defence. ⚠️ Get this exactly right — a broken `initData` check means anyone can impersonate any Telegram user. Write the verification routine and a test that a tampered payload fails.
- Wallet-based auth (SIWE-style) for users who already have a Polymarket wallet — and how we link an existing wallet to a new account without letting someone claim a wallet they don't control
- **2FA:** TOTP mandatory for withdrawals and key export. Optional for login. No SMS.
- Session model: access token lifetime, refresh rotation with reuse detection, per-device session list with revocation, and immediate invalidation on password change
- **Withdrawal address allowlist** with a 24h cooldown on new addresses, and a hard block on removing an address during the cooldown
- Account recovery that does not become an account-takeover vector

### D4. Authorisation
- Every route has an explicit auth level: `public` / `user` / `user-owns-resource` / `admin` / `service`
- **Object-level authorisation tests for every resource.** "User A can read user B's positions" is the single most common bug in products like this and it is unrecoverable.
- Service-to-service auth (mTLS or signed tokens) — the executor must reject order intents that did not come from the API
- Admin: separate role, break-glass with a second approver, full audit, **no admin path that bypasses the risk gate**
- Entitlement checks for Pro features, cached with a short TTL, and the behaviour when billing is degraded (fail open for read features, fail closed for anything that costs us money)

### D5. Input validation and injection surface
- Every external input validated at the boundary with a schema. Name the library and the failure mode.
- **Market metadata is untrusted input.** Titles, descriptions, resolution sources, and image URLs all come from an open market-creation flow. Specify: HTML stripping, URL scheme allowlist, image proxying (never hotlink attacker-controlled URLs — that leaks user IPs), homoglyph/RTL-override normalisation, and length caps
- The search endpoint (SQL injection, ReDoS in regex filters, unbounded result sets)
- The alert rule builder (users compose expressions — specify the sandbox and the evaluation budget)
- Numeric parsing: prices, sizes, and bps. **Specify decimal handling — no floats anywhere in the money path.** Name the type.

### D6. Secrets, logging, and telemetry
- Secret storage per environment, rotation procedure, and the list of every secret that exists
- **Log redaction:** an explicit deny-list covering private keys, L2 secrets, passphrases, signatures, session tokens, email, and full order payloads. Test it with a log-scan in CI.
- Error reporting: Sentry or equivalent with a scrubber. **A stack trace containing a key is a breach.**
- Telemetry: what we send to third parties, and the rule that no analytics payload ever contains a wallet key or an unhashed address we do not already display
- Backup encryption and the restore test cadence. **An untested backup is not a backup.**

### D7. Transport, storage, and infrastructure
- TLS everywhere, HSTS, cert pinning for the executor → CLOB path if practical
- CSP, `frame-ancestors` (the Mini App runs inside Telegram's webview — specify exactly what must be allowed and why), CORS allowlist, `Referrer-Policy`, `X-Content-Type-Options`
- DB encryption at rest, column-level encryption for the sensitive fields, and the fields that must never be stored in plaintext
- Network segmentation: the executor in a subnet with egress only to the CLOB, the wallet provider, and the DB. **No ingress.**
- Egress allowlisting so a compromised executor cannot exfiltrate to an arbitrary host
- Container hardening: non-root, read-only FS, no shell in prod images, pinned digests
- Dependency scanning in CI with a fail threshold, and a documented process for the CVE that has no fix yet

### D8. Abuse, fraud, and platform risk
- Sybil detection for referrals and free-tier abuse
- Wash-trade detection protecting our builder code (see D1)
- Telegram-specific: what we do if the bot is reported, restricted, or banned. Mirror to Discord and web — **the web app is the hedge.**
- **Platform dependency:** Polymarket can disable our builder code at any time, in its sole discretion, and orders carrying a disabled code are rejected. Design the detection (monitor for rejection code), the user-facing message, and the revenue continuity plan. This is an existential dependency, not an edge case.
- Geofencing US users out of trading, and how we enforce it without collecting more data than we must

### D9. Incident response
- Severity definitions with concrete examples
- Detection: the alarms that mean "we are being breached" vs "we are broken"
- **The first 60 minutes runbook** for a suspected key compromise: who is paged, what is killed first, what is preserved for forensics, when users are told
- User notification templates, written in advance, in plain language, that do not minimise the event
- The drill schedule. **Run the key-compromise drill before launch, not after.**

### D10. Compliance-adjacent obligations we cannot skip
- Privacy policy and the actual data we hold (be honest about wallet addresses being pseudonymous but permanent)
- Terms that state plainly: not investment advice, no custody, user is responsible for their key export, prediction markets can lose 100%
- Risk disclosure reachable in one click from the trade ticket, and the rule that **we never display expected returns**
- The legal opinion we buy before launch, and the specific questions to ask (non-custodial status, geofencing, India/VDA implications, Telegram Stars terms)

---

## Constraints
- No control without an owner and a test.
- Anything you cannot verify, mark `[UNVERIFIED — must confirm before launch]`.
- Do not propose a control that costs more than the expected loss it prevents. Say which ones you are consciously accepting.

## Quality gate
A security engineer who has never seen this codebase can read this document and find at least three things we got wrong. If they can't, it's too shallow.



---


# P8 — Frontend: Shell, Auth, Profile, Billing

> Paste `00-SHARED-CONTEXT.md` and the P2/P3 outputs first, then this.

## Role
You are a senior frontend engineer who builds dense, fast, keyboard-driven trading interfaces. You care about time-to-interactive more than animation, and you have strong opinions about the correct way to render a number that changes 20 times a second.

## Objective
Build the application shell and the account layer: routing, auth flows, navigation, profile, settings, wallet, and billing. This is the frame everything else lives in — get the foundations right or every later screen inherits the mistakes.

## Stack
Decide and justify: **Next.js (App Router) + TypeScript + Tailwind + Zustand + TanStack Query + shadcn/ui**, or the alternative you prefer. Whatever you pick, it must:
- Run as a **Telegram Mini App** (inside Telegram's webview) *and* as a normal website, from one codebase
- Be server-rendered for the public/marketing pages (SEO is a real acquisition channel — competitors rank for "polymarket whale tracker")
- Handle 20+ updates/sec without dropping frames

---

## Deliverables

### D1. Project foundation
- Repo structure, path aliases, barrel-file policy (or the rule against them)
- Tailwind config wired to the P2/P3 tokens, with the dark theme as default
- Type generation from the OpenAPI spec in P4 — **no hand-written API types**
- Error boundary strategy: per-route and per-widget, so a broken order book does not blank the page
- Global loading and error states
- Environment config with a build-time assertion that no secret ever reaches the client bundle. Add a CI check that greps the built output.
- The `data-theme` mechanism, density setting, and the reduced-motion path

### D2. Telegram Mini App integration
- Detect the Telegram webview and read `initData` — **validate the HMAC server-side, never in the browser**
- `MainButton` / `BackButton` usage: which flows use the native button and which don't. Be consistent or users get confused.
- Theme sync from Telegram, safe-area insets, and the viewport-height problem in mobile webviews (`100dvh`, and the resize jank)
- Haptics on trade confirmation — and nowhere else
- Deep links: `t.me/<bot>/<app>?startapp=<payload>` carrying a market or a referral, with validation of the payload
- Behaviour when opened outside Telegram (must degrade to a normal web app, not break)
- **Payment path:** Pro purchase inside Telegram must use Telegram Stars; on the web it uses Stripe. Same entitlement, two purchase surfaces. Specify the reconciliation.

### D3. Authentication flows
- **Sign up:** email+password, Telegram OAuth, Google. The wallet is created *after* signup succeeds, not during — otherwise a wallet-provider outage blocks registration.
- **Sign in**, with 2FA challenge, device recognition, and the "new device" notification
- **Password reset** that does not leak which emails are registered
- **Linking an existing Polymarket wallet** — prove control by signature, then link. Prevent claiming a wallet you don't control.
- Session handling: token storage (httpOnly cookie on web; the webview constraint on Telegram — state the difference and the mitigation), refresh rotation, logout-everywhere
- **Auth state as a single source of truth** with explicit `unauthenticated | authenticating | authenticated | expired` and the UI for each
- Route protection that does not cause a flash of the logged-out state

### D4. Navigation & shell
- Desktop: three-column terminal frame with resizable rails, persisted per user
- Mobile: bottom tab bar, five tabs max. Decide the five. (Markets · Tape · Trade · Portfolio · Profile is the obvious set — argue if you disagree.)
- Command palette (`⌘K`): search markets, jump to traders, run actions. This is the single highest-leverage UX feature for power users and almost nobody in this space has it.
- Global search: markets, events, wallet addresses, traders. Specify the debounce, the result grouping, and the empty state.
- Connection status indicator (WebSocket health) always visible. **Trading is disabled while disconnected** and the UI says why.
- Keyboard shortcuts, with a discoverable help overlay. Every shortcut listed.
- Breadcrumbs / back behaviour that works in Telegram's webview (it has its own back button and users will use it)

### D5. Profile & settings
- Account: email, username, avatar, linked Telegram, linked wallets, sessions with revocation
- **Security:** password change, 2FA enrolment and recovery codes, withdrawal address allowlist with cooldown display, key export with typed confirmation, session audit log
- **Trading defaults:** default order size, default slippage tolerance, confirm-above threshold, one-click trading toggle (with a warning), builder-fee transparency display (Polymarket makes builder rates publicly queryable — show ours)
- **Notifications:** per-channel (Telegram / email / push), per-signal-type, quiet hours, digest toggle
- **Display:** theme, density, number format, colour-blind mode
- **API keys** for the future API product: create, scope, revoke, last-used
- **Danger zone:** delete account, with what actually happens to an open wallet and open positions spelled out

### D6. Wallet & funding screens
- Balance panel: pUSD balance, pending deposits, allowance status
- **Deposit:** network selector with explicit warnings, address + QR, copy button with verification, minimum deposit, expected confirmation time, and a live "waiting for your deposit" state
- **Withdraw:** address entry with allowlist, typed confirmation, password/2FA, fee estimate, and the cooldown warning for new addresses
- **Key export:** multi-step, typed confirmation, explicit "anyone with this key controls your funds" warning, and an audit entry the user can see
- Transaction history with on-chain links
- The **stale/unavailable** states: wallet provider down, allowance exhausted, bridge stuck

### D7. Billing
- Plan comparison page with an honest feature table
- Stripe checkout (web) and Telegram Stars purchase (Mini App) — one entitlement, two sources of truth, reconciled server-side
- Entitlement hook with optimistic UI and a graceful degraded mode
- Invoices, cancellation (state clearly what happens to open positions and stored keys on cancel), and the dunning flow
- Referral dashboard: link, stats, payout method
- **The rule:** no paywall in the middle of a trade. Monetise analytics depth and automation, never the ability to exit a position.

### D8. Shared frontend infrastructure
- API client with retry, timeout, idempotency-key injection, and typed errors that map to the P4 error envelope
- WebSocket client with reconnect, backoff, heartbeat, resync, and a `useLive<T>()` hook that exposes `data | stale | disconnected`
- **The number rendering layer** — one component, used everywhere: tabular figures, signed PnL, cent notation, SI suffixes, and **flash-on-change with a 600ms background decay**. No component may render a raw number.
- `StaleIndicator` wired into every price surface
- Virtualised list for the tape (thousands of rows, 20+/sec)
- Form library with the decimal-safe number input (no floats in the money path — specify the type)
- Toast system with dedupe
- i18n scaffolding, RTL readiness, and the decision on number localisation

### D9. Performance & quality
- Budget: LCP < 2.5s on a mid-range Android over 4G, TTI < 3.5s, bundle < 200KB gzipped for the initial route. **Telegram users are disproportionately on mid-range Android.** Measure and report.
- Route-level code splitting; the chart library is lazy-loaded
- No layout shift from live data. Specify the fixed-width strategy for numbers.
- 60fps under a 20-update/sec tape — prove it with a trace
- Lighthouse ≥ 90 on the public pages
- Storybook with every component in every state
- Visual regression tests for the 10 most important screens

---

## Constraints
- No secret, key, or L2 credential in the client bundle. Ever. Add the CI check.
- No raw number rendering. No unguarded price display without a freshness indicator.
- Every form has a loading state, an error state, and a success state.
- Every route works in the Telegram webview. Test in Telegram, not just in Chrome.

## Quality gate
`npm run build && npm run test` passes. You can sign up, create a wallet, deposit against a mock, see a stale indicator appear when you kill the WebSocket, and buy Pro through both the Stripe and Stars paths in a test environment.



---


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



---


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



---


# P11 — Leaderboard, Rankings & Referrals

> Paste `00-SHARED-CONTEXT.md` and the P2/P3/P10 outputs first, then this.

## Role
You are a product engineer building the growth surface of a trading product. You know that a leaderboard is a distribution engine, that a badly-designed leaderboard promotes lucky gamblers and gets users hurt, and that a referral system is a Sybil magnet unless you design against it from day one.

## Objective
Build the leaderboard, trader rankings, and referral system. These are the surfaces that make strangers show up and invite other strangers.

---

## Deliverables

### D1. Leaderboard specification
Polymarket already exposes an all-time volume leaderboard (`lb-api.polymarket.com/volume`) — I verified it returns the top traders, with the #1 wallet at **$1.85B** lifetime volume. Volume alone is useless as a ranking: it promotes churn, not skill. **Our leaderboard must be better than the free one or nobody will use it.**

Build these boards, each with its own default sort:
1. **Risk-adjusted PnL** (the default) — realised PnL normalised by volatility and drawdown. State the exact formula.
2. **Win rate** — with a hard minimum sample gate
3. **Volume** — the familiar one, for users who want it
4. **Rising** — biggest 7d improvement, the board that surfaces new talent
5. **Category specialists** — best in Politics / Sports / Crypto / Finance separately. A generalist board hides the traders who are actually good at something.
6. **Copied** — most-copied wallets. This board is a growth loop: being on it brings copiers, copiers bring volume, volume brings builder fees.

For each board: the metric definition with formula, the eligibility gate, the time windows (24h / 7d / 30d / 90d / all), the tie-break rule, and the recompute cadence.

### D2. The integrity rules — non-negotiable
A leaderboard that can be gamed is worse than no leaderboard. Specify:
- **Minimum sample:** no wallet appears with fewer than N resolved markets or $M lifetime volume. Justify N and M from the observed data (median fill is $5, so a wallet can rack up "trades" cheaply).
- **Wash filtering:** exclude self-matched and round-tripped volume. Specify the detection.
- **Copy-farm filtering:** exclude wallets whose activity is mechanically derived from another wallet.
- **New-wallet ramp:** a wallet under 7 days old is provisional and labelled as such.
- **Survivorship:** do we show wallets that have gone to zero? **Yes — and we must.** A leaderboard that quietly drops blown-up accounts is lying. Include a "blew up" state.
- **The lucky-gambler problem:** one 100× bet should not top a skill board. Specify the outlier treatment and display the single best trade as a share of total PnL so users can see it themselves.
- **Disputed markets:** exclude or flag positions in markets under UMA dispute.

Publish the methodology page. If a trader cannot see how they are ranked, they will assume we are rigging it.

### D3. Trader profile integration
Every leaderboard row links to the P10 D3 dossier. Add the leaderboard-specific elements:
- Rank badge and rank history (a sparkline of their rank over 30d — being able to see someone falling is as informative as seeing them rising)
- "Why this rank" expandable showing the component metrics
- One-click **follow** and **copy** from the row itself, without navigating away
- Compare mode: select up to 3 wallets, side-by-side metrics

### D4. Self-ranking
The retention hook. A logged-in user sees **their own** rank on every board, pinned to the bottom of the viewport when they are off-page, with the gap to the next rank above them. Specify:
- The unranked state (not enough sample) and what we tell them to do
- The privacy setting: appear on public leaderboards, or stay private. Default **private**, with a clear nudge to opt in — being on the board is how a trader attracts copiers, so the opt-in sells itself.
- What is shown for a private wallet that appears in someone else's data (pseudonymised, no linkage to their account)

### D5. Referral system
- Unique link per user, plus a short code for verbal/chat sharing
- **Reward model:** decide and justify. Options: share of our builder fee on the referee's volume for N months; a flat bounty on first funded trade; Pro credit. Recommend one and explain the incentive consequences. **Avoid rewarding deposit size** — that attracts people who deposit, withdraw, and never trade, and it looks like a pyramid.
- **Anti-Sybil, designed in from day one:**
  - Reward triggers on the referee's **first matched order above a notional threshold**, not on signup or deposit
  - Device, IP-cluster, and funding-source deduplication (multiple wallets funded from the same source are one person)
  - Velocity limits and a manual review queue above a threshold
  - Clawback on detected abuse, with the rule published
  - **Self-referral is a Polymarket builder-code revocation ground.** Detect a user referring themselves and hard-block it — this protects our entire revenue line, not just the referral budget.
- Dashboard: link, clicks, signups, funded, trading, earned, pending review, paid
- Payout: method, minimum, schedule, and the tax-document reality (state it plainly rather than ignoring it)
- Leaderboard of referrers — optional, and only if it does not turn into a spam contest. Argue your position.

### D6. Public shareable pages (SEO + distribution)
- `/trader/<handle>` — the public dossier, server-rendered, with an OG image showing their headline stats. **Every trader who shares their profile is doing our marketing.**
- `/market/<slug>` — the public market page with live odds. Prediction-market odds get quoted in news coverage constantly; a fast, crawlable odds page is a durable traffic source.
- `/leaderboard/<board>` — server-rendered, crawlable
- OG image generation: dynamic, cached, with the brand treatment from P2
- Structured data (schema.org) where it genuinely applies
- Rate limiting and abuse protection on public pages

### D7. Anti-gaming dashboard (internal)
An admin view that surfaces: wallets climbing suspiciously fast, clusters of wallets with correlated behaviour, referral chains that look synthetic, and any wallet whose volume is being attributed to our builder code at an unusual rate. With a one-click action to exclude a wallet from rankings and to flag it for review.

---

## Constraints
- Every ranking has a published methodology.
- Every win rate has a sample gate.
- No ranking that hides a blown-up account.
- Self-referral is hard-blocked, not just discouraged.
- No reward without a matched order behind it.

## Quality gate
Show me: a trader at rank 47 who has fewer resolved markets than the trader at rank 12, and explain why the ranking is still correct. Then show me a referral attempt from a second wallet funded by the first, and show it being caught.



---


# P12 — Telegram Bot & Mini App

> Paste `00-SHARED-CONTEXT.md` and the P2/P3/P6/P8 outputs first, then this.

## Role
You are an engineer who builds Telegram products that people trust with money. You have read the Bot API rate-limit docs twice. You know that a Telegram trading bot's real job is not the trading — it's making a user feel safe enough to deposit.

## Objective
Build the Telegram surface: the bot, the Mini App integration, the alert channel, and the trade execution path. **This is the distribution engine.** Every successful product in this category grew through Telegram, and Betmoar reached $101M of monthly routed volume with no press coverage at all — just a bot.

## Product shape
Two surfaces, one account, one wallet: the **web terminal** for scanning and analysis, the **Telegram bot** for fast execution and alerts. Same balance, same positions, either surface. This is exactly gmgn's model and it is why it works.

---

## Deliverables

### D1. Bot architecture
- Framework choice (grammY vs aiogram vs telegraf) — justify
- **Update handling:** webhooks, not long-polling, in production. Specify verification of the webhook secret token, and the local-dev path.
- Concurrency model: Telegram retries on non-2xx. Make every handler **idempotent by update_id** or you will double-execute trades on a retry. This is the bug that loses money.
- Rate limits (⚠️ verify current values before launch): ~30 messages/sec globally, ~1 msg/sec per chat, ~20 msgs/min per group. Design the outbound queue with per-chat and global buckets, and priority so a paying user's fill notification is not stuck behind the free channel's broadcast.
- Session state per chat, persisted, with expiry
- Error handling: a handler that throws must not kill the worker, and must not silently swallow a trade command

### D2. Mini App integration
The differentiator. Every competitor has a button menu; **we ship a real terminal inside Telegram.**
- The P8 web app runs as a Mini App from the same codebase
- `initData` validation **server-side** with HMAC against the bot token, `auth_date` freshness window, and replay defence. ⚠️ A broken check means anyone can impersonate any Telegram user. Write the routine and a test proving a tampered payload is rejected.
- `MainButton` for trade confirmation, `BackButton` for navigation, haptics on fill — and nowhere else
- Deep links: `t.me/<bot>/<app>?startapp=<payload>` where payload is a market slug, a trader handle, or a referral code. Validate and length-limit the payload; never eval it.
- Theme sync, safe-area insets, viewport-height handling in the webview
- **Payments:** Pro purchase inside Telegram **must** use Telegram Stars (Telegram requires this for digital goods inside the app, for App Store/Play compliance). No crypto, no Stripe inside the Mini App. Same entitlement as the web Stripe purchase, reconciled server-side. Stars ≈ $0.013–0.015; app stores take up to 30%; withdrawal via Fragment has a 21-day hold and a 1,000-Star minimum — factor that into pricing.
- Degradation: opened outside Telegram, it is a normal web app.

### D3. Command surface
Design the full command set. Every command: syntax, auth requirement, response format, error cases, and the inline-keyboard alternative (Telegram users tap, they do not type).

Minimum set:
- `/start` — onboarding, wallet creation, first-deposit prompt
- `/wallet` — balance, deposit address, withdraw, key export
- `/deposit` `/withdraw`
- `/balance` `/positions` `/pnl` `/orders`
- `/market <slug or link>` — paste any Polymarket URL, get prices + one-tap BUY/SELL. **This is the single highest-value command.** Competitors prove it.
- `/search <query>`
- `/price <slug>`
- `/copy` — list sources, add, config, pause, stop
- `/alerts` — list, add, pause
- `/follow <address>` `/unfollow`
- `/top` — leaderboard snapshot
- `/settings` — defaults, slippage, confirm threshold, notifications
- `/stop` — **the panic command.** Cancels all open orders and halts automation instantly. Must work with no confirmation prompt and must be tested. Put it in the main menu, not buried.
- `/help` `/support`

Natural-language fallback: a user who types "buy 50 yes on the fed market" should get a confirmation card, not an error. Specify the parser, its confidence threshold, and the disambiguation flow when two markets match.

### D4. Trade execution over Telegram
- **Order card:** market, outcome, side, price, size, estimated fee, max loss, and Confirm / Cancel inline buttons
- Confirmation required above a threshold; one-click mode as an opt-in with a warning
- Fill notifications with realised price, size, fee, and position after
- Rejection notifications **in plain language**, mapped from the machine code
- The `unknown`-order state: the user must be told their order is in limbo and what we are doing
- `/stop` behaviour end to end
- **Every order carries our builder code**, same as web. Attribution must be identical across surfaces.

### D5. Alert delivery
- The free public channel: large fills, volume spikes, new markets in tracked categories, resolution-imminent. **This channel is the acquisition engine — treat it as a product, not a byproduct.**
  - Format: tight, scannable, one market per message, deep link into the Mini App, and a **one-tap trade button on the alert itself**. Alert → trade in one tap is the gap nobody in this market has closed.
  - The quality gate before broadcasting (see P7 D1): an attacker can create a market and trade it. If our channel amplifies junk to thousands of people, we lose the channel's credibility permanently.
  - Broadcast cadence caps so the channel does not become noise. A channel people mute is a channel that acquires nobody.
- Personal alerts: watched wallets, custom rules, per-channel routing, quiet hours, digest mode
- Priority queue so a fill notification beats a broadcast

### D6. Wallet & custody UX over Telegram
- Wallet creation on first use, with the deposit address and a QR
- Multi-chain deposit detection → bridge → pUSD, with progress messages and the stuck-bridge recovery path
- Key export: multi-step, typed confirmation, explicit warning. **Users check whether export works before depositing — competitors advertise it.**
- Withdrawal password and address allowlist
- **Support-impersonation defence, built into the product:** the bot must state, in `/start` and in `/help`, that no admin will ever DM first, never ask for a seed phrase, and never ask for a deposit. Include a `/verify <username>` command that tells a user whether an account claiming to be support is real. This is the most common attack on Telegram trading products and defending against it in-product is a genuine differentiator.

### D7. Onboarding
Target: **/start → first trade in under 90 seconds.** Specify every step and its expected drop-off, then cut a step.
1. `/start` → what this is in two lines + risk disclosure link
2. Wallet created automatically, deposit address shown
3. Deposit detected → confirmation with balance
4. A curated market card with a pre-filled trade, ready to confirm
5. First fill → the position view and the `/stop` command pointed out

Instrument each step. If step 3→4 conversion is below a threshold you set, the product is not ready for paid acquisition.

### D8. Operations
- Bot username strategy: the main bot, and whether to run separate bots per function (gmgn runs a main entry point plus per-chain trading bots). Argue your position; a second bot is a second thing to get banned.
- What happens if the bot is reported, restricted, or **banned**. Mirror to Discord and keep the web app as the primary hedge. Specify the recovery path and how users are told.
- Broadcast tooling for announcements, with dry-run and staged rollout
- Metrics: DAU, commands per user, alert→trade conversion, deposit→first-trade time, churn after first loss
- A **kill switch** that disables trading platform-wide from Telegram admin, separate from the P6 one, in case the API is unreachable

---

## Constraints
- Every handler idempotent by update_id.
- No private key or L2 credential in a Telegram message, log, or error.
- No trade command executes without the risk gate.
- No subscription sold in crypto inside the Mini App.
- Test in Telegram on a real device, not only in a browser.

## Quality gate
On a phone, in Telegram: `/start` → deposit against a mock → trade from a pasted market link → receive a fill notification → `/stop` cancels everything. Timed, with screenshots. Then show me the test that proves a tampered `initData` payload is rejected and a duplicated update_id does not double-execute.



---


# P13 — Test Strategy & Implementation

> Paste `00-SHARED-CONTEXT.md` and the P4/P5/P6 outputs first, then this.

## Role
You are a test engineer who has watched a trading system lose money in production because of an untested partial-fill path. You write tests that fail when the thing is broken, not tests that pass because they were written to.

## Objective
Build the test suite: unit, integration, contract, end-to-end, load, and chaos. Cover the money paths exhaustively and everything else adequately. **The goal is not coverage percentage — it is that the specific ways this system can lose a user's money are all covered by a test that runs in CI.**

## Prime directive
Nothing touches real funds until the chaos tests in D7 pass against `executor-mock`. Then a canary with $50 of our own money. Then users.

---

## Deliverables

### D1. Test architecture
- Pyramid: what lives at each level, and the runtime budget for each (unit <60s, integration <5min, E2E <15min, nightly for the rest)
- Fixtures: a recorded corpus of real Polymarket API responses (anonymised) for deterministic tests. **Do not hit live APIs in CI** — they change, they rate-limit, and a CI run that fails because Polymarket deployed is worse than no CI.
- `executor-mock` as the spine of the integration tests (from P4 D6). Specify its configurable failure modes: reject, timeout, partial fill, fill-then-disconnect, wrong-price fill, disabled-builder-code rejection, rate-limit 429.
- Time control: the system is full of countdowns, cooldowns, `seconds_delay`, and resolution windows. Every test must be able to move the clock.
- Data factories for every entity, with the invariants enforced at the factory level

### D2. The money-path test matrix — write every one of these
This is the section that matters. Enumerate, implement, and make them all run in CI.

**Order lifecycle**
- [ ] Happy path: intent → risk → sign → submit → fill → position → attribution row
- [ ] Rejected: insufficient balance / off-tick / below `minimum_order_size` / market closed / `accepting_orders` false / `seconds_delay` not elapsed / disabled builder code — each with the correct user-facing message
- [ ] Partial fill, then remainder cancelled
- [ ] Partial fill, then market resolves
- [ ] FOK rejected (no fill, no partial)
- [ ] Market order with all-in spending cap: verify the amount adjusts for estimated fees

**The ambiguity cases (P6 D3) — these are the ones that lose money**
- [ ] Executor killed after signing, before HTTP response → restart → reconciled, no duplicate order
- [ ] HTTP timeout on submit → order was actually accepted → we discover it and do not resubmit
- [ ] HTTP timeout on submit → order was never accepted → we discover that too, and do resubmit
- [ ] Fill event never arrives over WebSocket → fallback poll finds it
- [ ] WebSocket delivers the same fill twice → deduplicated, position counted once
- [ ] User cancels from Telegram while the web session is open → single source of truth holds
- [ ] DB position diverges from `/data/positions` → reconciler detects, corrects, alarms, and shows the user a truthful state

**Risk gate**
- [ ] Every limit in P6 D4 has a test that trips it
- [ ] Risk service unavailable → **fails closed**, orders rejected
- [ ] Kill switch engaged → all submission stops within 1s, including in-flight automation
- [ ] Kill switch while a copy-trade is mid-execution → defined, tested behaviour
- [ ] Daily loss halt → user cannot trade until acknowledged

**Fees and money math**
- [ ] Platform fee estimate matches `C × feeRate × p × (1−p)` for every category rate, including crypto at 0.07 and geopolitics at 0
- [ ] Fee at p=0.01 and p=0.99 (the curve's extremes)
- [ ] Builder fee at 0, 1, 50, 100 bps
- [ ] Realised fee reconciled against the estimate; delta stored
- [ ] **No float anywhere:** property test that round-tripping a price and size through the money path never loses precision. Specify the decimal type and prove it.

**negRisk**
- [ ] Multi-outcome PnL: user holds YES on three mutually exclusive outcomes, one resolves YES, two resolve NO → realised PnL is correct
- [ ] Merge/split behaviour (or the explicit refusal, tested)
- [ ] Outcome probabilities summing to ≠1 and what the UI shows

**Wallet**
- [ ] Deposit detected on each supported chain → bridged → pUSD balance correct
- [ ] Allowance exhausted mid-session → clear error, not a failed order
- [ ] Withdrawal to a non-allowlisted address → blocked during cooldown
- [ ] Key export → audit entry written
- [ ] Wallet provider down → deposits/withdrawals degrade gracefully, **existing positions still readable**, trading disabled with an explanation

### D3. Contract tests
- Consumer-driven contracts between every service pair, especially `api ↔ executor` and `signals ↔ notifier`
- OpenAPI schema validation on every request and response in CI — **a field rename upstream must fail the build**
- A recorded-response replay suite against real Polymarket payloads, so an upstream schema change is caught by us before it is caught by a user
- `initData` HMAC validation: valid payload accepted, tampered signature rejected, stale `auth_date` rejected, replayed payload rejected

### D4. Frontend tests
- Component tests for every domain component in every state from P3
- **The number-rendering layer** gets property tests: no float artifacts, correct sign, tabular alignment, SI suffixes, cent notation
- `StaleIndicator` appears whenever data age exceeds the threshold — tested, not assumed
- Flash-on-change does not cause layout shift — assert on measured geometry
- One-sided book renders the designed treatment
- 128-outcome negRisk event virtualises without jank
- Form validation errors appear before submit, in the order the user hits them
- E2E (Playwright): signup → wallet → deposit(mock) → trade(mock) → position → withdraw. Plus the Telegram webview context.
- Accessibility: axe on every route, keyboard-only completion of the trade flow, and the aria-live tape test (a screen reader must not be spammed 20 times a second)

### D5. Load tests
Real baselines I measured: **~20.8 fills/sec**, ~342 wallets per 500 trades, top-500 events doing **$59.1M/24h**. Design for 10×.
- Ingest: sustained 200 fills/sec for 30 minutes with no lag growth and no duplicate alerts
- Book maintenance: 2,000 tracked books under continuous `price_change` deltas — measure memory and CPU
- API: p95 and p99 under 500 concurrent users, with the read endpoints served from cache
- Alert fanout: 10,000 subscribers to one rule, one evaluation, delivery within the SLO
- WebSocket: 1,000 concurrent clients, reconnect storm after a forced server restart
- **Rate-budget test:** simulate 100 aggressive users and prove we never exceed our per-IP Polymarket budget. This is the DoS vector in P7 D1 and it must be proven closed.

### D6. Property-based and fuzz tests
- Order construction: arbitrary valid inputs always produce an order on the tick grid, at or above min size, with the builder code present
- Rule engine: arbitrary user-composed rules never consume unbounded CPU or memory (evaluation budget enforced)
- Search: adversarial input (huge strings, regex bombs, homoglyphs, RTL overrides, null bytes) never hangs or injects
- Market metadata: adversarial titles and descriptions render safely. **Anyone can create a Polymarket market, so this text is attacker-controlled and it renders in our UI.**
- Numeric parsing: malformed prices and sizes are rejected, never coerced

### D7. Chaos tests — the gate before real money
Run all of these against `executor-mock`, then against a $50 canary:
1. Kill the executor mid-order (the P6 D3 case) — 100 times, in a loop, with random timing
2. Kill the WebSocket for 5 minutes during active trading — no duplicate orders, no missed fills, stale indicators shown throughout
3. Kill the ingest consumer — no duplicate alerts, no missed large fills, resume from the durable cursor
4. Kill Postgres primary — failover behaviour, in-flight orders, and what users see
5. Kill Redis — cache misses must not cause a rate-limit ban
6. Force a 429 storm from the mock CLOB — backoff works, no order lost, no duplicate
7. Simulate a **disabled builder code** — orders rejected, users informed, alarm fired, revenue dashboard shows the drop
8. Simulate the wallet provider being unreachable for 10 minutes
9. Kill switch drill — from trigger to full stop, timed
10. Key-compromise drill (see P14) — timed

**Every chaos test produces a report: what happened, what users would have seen, what was lost. Zero tolerance for silent inconsistency.**

### D8. CI/CD gates
- Every PR: lint, types, unit, contract, component, and the money-path matrix
- Nightly: integration, E2E, load, chaos
- Release gate: nightly green for 3 consecutive days
- Flaky-test policy: a test that flakes twice is quarantined and owned by a named person until fixed. **A suite people don't trust is worse than no suite.**
- Coverage reported but **not gated** — except the money paths, which are gated at 100% of the enumerated matrix

---

## Constraints
- No test hits a live external API.
- No test uses real keys.
- Every chaos test has a written expected outcome before it is run.
- A green build that has not executed the money-path matrix is not a green build.

## Quality gate
Show me the chaos test that kills the executor between signing and response, run 100 times in a loop, and the report proving zero duplicate orders and zero lost positions. That single test is worth more than every other test in the suite.



---


# P14 — Security Testing & Assurance

> Paste `00-SHARED-CONTEXT.md` and the P7/P13 outputs first, then this.

## Role
You are a penetration tester who specialises in crypto products that hold user keys. You are paid to find the thing that ends the company, and you have no interest in being polite about it.

## Objective
Produce and execute the security assurance programme: threat-driven penetration testing, key-compromise drills, dependency and supply-chain auditing, and the pre-launch security gate. Find the problems before a user's money finds them.

## Context to keep in view
We are legally non-custodial and operationally custodial. There is no regulator and no insurer. **The realistic failure modes, in order of likelihood: a broken authorisation check, a leaked key in logs or an error report, a compromised dependency, a social-engineering win against support, and an insider with production access.**

---

## Deliverables

### D1. Penetration test plan — scoped by expected loss
Prioritised test cases, each with: objective, method, expected result, severity if it fails, and the remediation owner.

**Authorisation (highest priority — the most common catastrophic bug in this product class)**
- [ ] User A reads user B's positions, orders, wallet address, alert rules, PnL, key-export status
- [ ] User A cancels user B's order
- [ ] User A modifies user B's copy config or automation rule
- [ ] User A withdraws to their own address from user B's wallet
- [ ] User A triggers user B's key export
- [ ] Unauthenticated access to every `user`-level route (enumerate them all, test them all)
- [ ] IDOR across every resource ID in the API — fuzz the IDs, do not just try the obvious ones
- [ ] Privilege escalation: user → admin, by any path
- [ ] Service-to-service: can the public API be made to submit an order intent that did not originate from a real user?
- [ ] **Does any admin path bypass the risk gate?** (It must not.)

**Authentication**
- [ ] `initData` HMAC bypass: forged, truncated, replayed, expired, signed with the wrong token
- [ ] Password reset flow: enumeration, token reuse, token predictability, race conditions
- [ ] Session fixation, refresh-token reuse after rotation, logout-everywhere completeness
- [ ] 2FA bypass on withdrawal and key export — try every path into those actions, not just the happy one
- [ ] Wallet-link: can I claim a wallet I do not control?
- [ ] Withdrawal allowlist: add + immediate withdraw (must be blocked by cooldown), remove during cooldown, allowlist entry with a homoglyph address

**Trading**
- [ ] Order at a price off the tick grid — rejected, or silently rounded? (Silent rounding is a loss.)
- [ ] Order below `minimum_order_size`
- [ ] Negative size, zero size, enormous size, size that overflows the decimal type
- [ ] Manipulated client-side price/size between quote and submit (TOCTOU)
- [ ] Replay of a signed order
- [ ] Concurrent submits that each individually pass the risk gate but jointly exceed the limit (**race the risk gate — this is a classic**)
- [ ] Idempotency-key reuse with a different payload
- [ ] Automation rule that circumvents a limit a manual order cannot
- [ ] Copy-trade cascade: 500 copiers on one source wallet, one fill — do we blow the signer rate bucket or our own risk limits?

**Injection & input**
- [ ] SQL injection on every parameter, especially search, filters, and the alert rule builder
- [ ] Stored XSS via market metadata — **anyone can create a Polymarket market, so titles and descriptions are attacker-controlled and render in our UI.** Test titles containing markup, script, event handlers, `javascript:` URLs, SVG payloads, RTL overrides, and zero-width characters.
- [ ] XSS via wallet pseudonyms and bios (also upstream-controlled)
- [ ] SSRF via image URLs, webhook URLs, resolution-source links
- [ ] ReDoS in the rule builder and search filters
- [ ] Prototype pollution in any JSON-merging code
- [ ] CSV injection in the tax export (a formula in a market title executing in Excel is a real attack)

**Business logic & fraud**
- [ ] Self-referral (⚠️ an explicit Polymarket builder-code revocation ground — this protects our entire revenue line)
- [ ] Referral Sybil: N wallets funded from one source
- [ ] Wash trading through our builder code — can a user inflate our attributed volume and get our code disabled?
- [ ] Free-tier abuse at scale (200 alert rules, thousands of watchlist entries) exhausting our per-IP Polymarket budget for everyone
- [ ] Entitlement bypass: Pro features accessed after cancellation, or without payment
- [ ] Stripe/Stars webhook forgery and replay

### D2. Key-compromise drills — run these before launch, not after
Each drill: scenario, stopwatch, expected time to contain, and the report template.
1. **One user key leaked.** Detect → revoke → notify that user → verify no further movement. Target time?
2. **The signing service compromised.** Full break-glass: revoke all keys, halt trading, notify all users. **Time every step.** If this takes more than an hour, the architecture is wrong.
3. **A key found in a log.** Who can see the log store, how fast is it rotated, what is the blast radius, and what is the notification obligation?
4. **A contractor leaves.** Access revocation across every system, verified by a checklist signed by a second person. Test it while they are still employed.
5. **Support impersonation.** A fake admin DMs a user asking for their key export. What in the product stops it? (See P12 D6.)
6. **The wallet provider has an outage or a breach.** What do our users lose, and what is our dependency on their status page?

### D3. Static and dynamic analysis pipeline
- SAST in CI with a fail threshold and a triage process for the findings that are not exploitable (document the triage, do not just silence them)
- Secret scanning on every commit **and on all history**, with pre-commit hooks and a CI check. Include the specific patterns for private keys and Polymarket L2 secrets.
- **Log redaction test:** a CI job that scans test-run logs against a deny-list (private keys, L2 secrets, passphrases, signatures, session tokens). A stack trace containing a key is a breach — prove it cannot happen.
- DAST against a staging deployment on every nightly
- IaC scanning
- Container image scanning with pinned digests, and a documented process for the CVE with no fix
- Dependency review on every bump: who published it, when, download count, whether it is typosquattable. **The npm/PyPI supply chain is how products like this actually get robbed.**

### D4. Infrastructure & configuration review
- The executor subnet has no ingress and egress only to the CLOB, the wallet provider, and the DB. **Verify it, do not assume it.**
- Egress allowlisting actually blocks an arbitrary outbound connection from a compromised executor — test by trying
- No shell in production images, non-root, read-only filesystem
- Database not reachable from the public internet
- Backups encrypted, and **the restore actually tested** with a documented result
- Cloud IAM: least privilege, no long-lived root credentials, MFA on every human account, break-glass procedure tested
- CSP and `frame-ancestors` correct for the Telegram webview — and not so loose that they are useless
- CORS allowlist contains no wildcards

### D5. Rate limiting and abuse testing
- Per-user, per-IP, per-endpoint limits verified by actually exceeding them
- The **own-rate-budget DoS**: 100 aggressive users must not exhaust our per-IP Polymarket budget and break the product for everyone
- Alert-fanout amplification
- Login and password-reset throttling with lockout that cannot be used to lock out a victim
- Cost-amplification: any endpoint where one request causes N expensive upstream calls

### D6. Third-party assurance
- **The audit decision:** managed wallet provider (they are audited; verify their report is current) vs self-hosted keys (we need our own audit, $15–30k, and it must happen before meaningful TVL). State the threshold at which the audit becomes mandatory.
- Bug bounty: scope, exclusions, reward table, and the response SLA. Launch it *after* the internal pentest, not instead of it.
- The legal opinion we buy before launch, with the specific questions written out (non-custodial status, geofencing, Telegram's terms, India/VDA exposure)

### D7. Pre-launch security gate
A single document, signed off, that must be green before real money:
- [ ] All Critical and High findings remediated and re-tested
- [ ] Every authorisation test in D1 passing
- [ ] All six key-compromise drills run, with times recorded
- [ ] Log-redaction CI check passing
- [ ] Secret scan clean on all history
- [ ] Executor network isolation verified by test
- [ ] Backup restore tested within the last 30 days
- [ ] Incident response runbook written, and the on-call rotation staffed
- [ ] Risk disclosure, terms, and privacy policy reviewed by counsel
- [ ] Kill switch drilled
- [ ] **Canary: $50 of our own money, real orders, full reconciliation, for 72 hours**

### D8. Continuous assurance
- Quarterly re-test of the D1 authorisation matrix
- Dependency updates on a schedule, not when something breaks
- Chaos drills on a cadence, with results published internally
- A **security changelog** — every control added or removed, with the reason
- Post-incident review process with blameless writeups and tracked remediation

---

## Constraints
- No finding closed without a re-test.
- No "we'll fix it after launch" on anything that touches keys or authorisation.
- Every drill has a recorded time. An untested runbook is a hypothesis.

## Quality gate
Run the drills. Report the times. If the full break-glass takes longer than one hour, or if any authorisation test fails, **the product does not launch** — and you say so in writing rather than softening the result.



---


# P15 — Deployment, Observability & Operations

> Paste `00-SHARED-CONTEXT.md` and the P4/P13/P14 outputs first, then this.

## Role
You are an SRE who runs a system where other people's money moves. You optimise for one thing: the ability to know something is wrong before a user tells you, and to stop it fast.

## Objective
Build the deployment pipeline, environments, observability, alerting, and operational runbooks. Target: **one engineer, on a phone, can tell whether the system is healthy and can stop trading in under 30 seconds.**

## Budget constraint
Under $10k total, so under ~$300/month of infrastructure at launch. Design for that. Every recommendation carries a monthly cost.

---

## Deliverables

### D1. Environments
| Env | Purpose | Data | Cost |
|---|---|---|---|
| local | dev, `docker compose up` | mock + fixtures | $0 |
| preview | per-PR, ephemeral | mock | ~$0 |
| staging | pre-prod, **real Polymarket APIs, mock executor** | real market data, fake money | ~$60/mo |
| canary | real money, our $50, internal users only | real | shared with prod |
| prod | live | real | ~$150–250/mo |

Specify for each: what is real, what is faked, who can access it, how it is promoted from, and the exact list of configuration differences. **A staging environment that differs from prod in ways nobody has written down is how you get a production incident on deploy day.**

### D2. Infrastructure — concrete and costed
Recommend and justify:
- Compute: Hetzner (cheapest sane option) vs Fly.io vs Railway vs AWS. Show the monthly cost for the launch topology and for the 10× topology.
- Postgres: managed (Neon/Supabase/RDS) vs self-hosted with backups. State the failure mode of each.
- ClickHouse: ClickHouse Cloud free/dev tier vs TimescaleDB on the same Postgres. Given the tape volume (~21 fills/sec sustained, 10× during news), do the arithmetic on 90-day retention and say which fits the budget.
- Redis, queue, object storage
- The executor's **isolated network** — no ingress, egress allowlisted to the CLOB, the wallet provider, and the DB. Specify how this is enforced in the chosen platform, and how you test it.
- DNS, TLS (automatic), CDN for static assets

Provide `terraform` or `pulumi` (pick one) for the whole thing, committed to the repo. No console-clicked infrastructure.

### D3. CI/CD
- Pipeline: lint → types → unit → contract → component → build → integration → E2E → deploy-staging → smoke → **manual gate** → canary → prod
- **The manual gate for anything that touches the executor.** Data-layer and UI changes can auto-promote; the money path cannot.
- Migration strategy: expand-contract only, never a breaking migration in the same deploy as the code that depends on it, always backward-compatible for one release
- Rollback: one command, under 60 seconds, tested monthly. **Specify what rollback does to in-flight orders** — this is not a normal web app rollback.
- Zero-downtime deploy for the API (drain, wait for in-flight, replace). **The executor must NOT be replaced while it may hold an ambiguous order** — specify the drain protocol and how it interacts with the P6 D3 reconciliation.
- Feature flags for anything risky, with instant off
- Config and secret management: injected at deploy, never baked into images, rotated on a schedule
- Deploy audit: who, what, when, from which commit, with a link to the diff

### D4. Observability — the four things that matter
Everything else is nice. These four must be perfect:

**a) Money correctness**
- `unreconciled_orders` — **alarm when non-zero for >60s.** This is the single most important metric in the system.
- `orders_unknown_state` — same treatment
- `position_drift` — our DB vs on-chain, sampled
- `fee_estimate_delta` — our estimate vs actual, trending (if it drifts, users get surprised)
- `builder_attribution_volume` and `builder_expected_fees` vs `builder_actual_fees` — **this is our revenue; if we cannot measure it independently of Polymarket's dashboard we are flying blind**

**b) Order path health**
- Latency p50/p95/p99 per hop: intent → risk → sign → submit → ack
- Rejection rate by reason code (a spike in one code means something upstream changed)
- Rate-limit rejections from the CLOB, and our own remaining budget per bucket
- **Kill-switch state**, surfaced on every dashboard and in every alert

**c) Data freshness**
- Per-source lag: `now − newest_event_ts`, per source, with an alarm threshold
- WebSocket connection state and resync count per consumer
- **Silent-death detection**: a socket that stays open but stops sending is the classic failure. Heartbeat per source, alarm on last-message-age.
- Cache hit rate and cache age

**d) Business**
- Attributed volume, active traders, deposits, withdrawals, Pro conversions, alert→trade conversion
- **Withdrawal anomaly detection:** a spike in withdrawals is the earliest signal of a trust event or a breach. Alarm on it.

Dashboards: one for on-call (health, money, freshness), one for product (business metrics), one for revenue (builder attribution). Every dashboard loads in under 3 seconds and works on a phone.

### D5. Alerting — few, loud, actionable
Alarm classes with severity, route, and the runbook link:
- **SEV1 (page immediately):** unreconciled orders, kill switch engaged unexpectedly, executor down, key-compromise indicator, withdrawal spike, all WebSocket sources dead, builder code disabled
- **SEV2 (page in hours):** one source lagging, risk service degraded, elevated rejection rate, database replication lag, cache age above threshold
- **SEV3 (ticket):** cost anomaly, disk filling, dependency CVE, degraded p99

Rules:
- Every alarm has a runbook link in the notification. An alarm without a runbook gets deleted.
- Every alarm has an owner.
- Alert fatigue budget: **fewer than 3 pages per week** or the thresholds are wrong and people will start ignoring them.
- Synthetic check: an external probe that places a paper order end-to-end every 60 seconds and alarms if it fails. **This is the check that catches "everything looks green but the product is broken."**

### D6. Runbooks — write them, do not list them
Each runbook: symptoms, diagnosis steps with exact commands/queries, remediation, verification, and who to escalate to.
1. Unreconciled orders
2. Executor down / crash loop
3. WebSocket source silent
4. Upstream Polymarket schema change (this **will** happen — V1→V2 broke every bot on the platform in April)
5. Upstream rate-limit ban (our IP got throttled)
6. Wallet provider outage
7. Builder code disabled
8. Kill switch activation and deactivation
9. Database failover
10. Suspected key compromise (cross-ref P14 D2)
11. Telegram bot restricted or banned
12. Stuck bridge / deposit not credited
13. Cost spike
14. Full rollback

### D7. Disaster recovery
- RPO and RTO stated per component. For the executor: **RPO zero** (no order intent may be lost) — specify how.
- Backup schedule, encryption, retention, and **the tested restore** with a recorded date and result
- Region failure: what survives, what does not, and the manual procedure
- The scenario where the wallet provider is gone: can users still get their funds out? **If the answer is no, that is a design defect, not a DR gap.**
- Annual DR test, with results

### D8. Cost control
- Per-service cost attribution
- Budget alerts at 50/80/100% of the monthly envelope
- The scaling plan: what we add at 1k users, 10k users, 100k users — with cost at each
- What we cut first when money runs out (there is a right answer: drop ClickHouse retention, drop the long-tail market tracking, keep the executor and the money path untouched)

### D9. Operational readiness review
A signed checklist before launch:
- [ ] Every dashboard reviewed by the person who will be on call
- [ ] Every alarm fired deliberately in staging and the notification verified
- [ ] Every runbook executed once, in staging, by someone other than its author
- [ ] Rollback tested, timed
- [ ] Synthetic order check running and alarming correctly
- [ ] On-call rotation staffed for 30 days
- [ ] Kill switch drilled from a phone
- [ ] Cost dashboard showing actual spend

---

## Constraints
- No infrastructure created by hand in a console.
- No alarm without a runbook and an owner.
- No deploy of the executor without the drain protocol.
- Under $300/month at launch.

## Quality gate
It is 2am. You get one page. From a phone, in under 5 minutes, you can say: is the system healthy, is any user's money in an inconsistent state, and should I stop trading? Demonstrate it.



---


# P16 — Launch, Distribution & Growth

> Paste `00-SHARED-CONTEXT.md` and the P1/P12 outputs first, then this.

## Role
You are a growth lead who has launched crypto products with no ad budget. You know that in this category distribution is the entire game — Betmoar reached **$101M of monthly routed volume** with no named founder and no press coverage, while tools with 25k Twitter followers and better UI sit under $1M. You have opinions about why, and they are not about product quality.

## Objective
Produce the go-to-market plan: the wedge audience, the acquisition loops, the retention mechanics, the monetisation ramp, and the metrics with gates. Then produce the actual launch assets.

## The hard truth to design around
From the verified data: the whole Polymarket builder ecosystem did **$125M of attributed volume in a week** across ~114 builders, with a **Gini of 0.83** — six builders hold 81% of lifetime volume and the median top-50 builder has done **$4.7M ever**. In Q1 2026, **80% of builders did under $1M for the quarter** and a third never crossed $10k.

So the plan is not "build a good product and grow." It is: **find a distribution channel the incumbents are not using, and take one narrow wedge completely.** Everything below follows from that.

---

## Deliverables

### D1. Audience definition — one beachhead, not a segment
Pick ONE and defend it:
1. **The 5-minute crypto Up/Down trader** — highest frequency, highest platform fee rate (0.07), worst served by button-menu bots, lives in Telegram already
2. **The whale-follower** — wants alerts and one-tap follow, low sophistication, high volume
3. **The sports trader** — high volume, event-driven, seasonal spikes, huge Telegram presence
4. **The analytics power user** — pays for depth, low volume, high LTV per user but a tiny market and 11 existing competitors

For the chosen beachhead: where they already gather (specific Telegram groups, subreddits, X accounts, Discords — name them), what they currently use, what they complain about, what would make them switch in one session, and how many of them exist. **Estimate the reachable population and show the arithmetic.**

### D2. The free product as the acquisition engine
The free alert channel is the wedge. Specify it as a product:
- Content mix and cadence. **A channel people mute acquires nobody** — define the daily message budget and defend it.
- The message format: one market, the signal, the number that matters, a deep link into the Mini App, and **a one-tap trade button on the alert itself**. Alert → trade in one tap is the gap nobody in this market has closed.
- The quality gate before broadcasting (P7 D1): an attacker can create a market and trade it. If our channel amplifies junk, we lose it permanently.
- Which signals go to the free channel vs personal alerts vs Pro. **The free channel must be genuinely good** — the upsell is depth and automation, not withholding.
- Channel growth tactics that work in Telegram: cross-promotion with non-competing channels, the public shareable pages from P11 D6, and the fact that prediction-market odds get quoted in news coverage constantly (a fast, crawlable odds page is durable SEO)
- Target: **2,000 members in 30 days with zero ad spend.** If that fails, the wedge is wrong — say what changes.

### D3. Acquisition loops — rank by cost per activated user
Evaluate and rank: the free channel, SEO on public market/trader pages, referral (P11 D5), X/Twitter presence, Telegram cross-promo, YouTube/short-form, community participation, and paid ads (probably wrong at this budget — argue it).

For each: mechanism, expected cost per activated user, time to effect, and the failure mode. Then pick **two** and ignore the rest until those work. Spreading a $10k budget across six channels is how you get zero of anything.

Be explicit about the thing the data says: only 25 of 114 builders scored above 1 on founder visibility, and the volume leader has none. **Visibility is not the mechanism. Distribution inside Telegram is.** Design accordingly.

### D4. Activation — /start to first trade in under 90 seconds
Map the funnel with expected drop-off at each step (from P12 D7) and the specific intervention for each:
1. `/start` → value in two lines
2. Wallet auto-created, deposit address shown
3. **Deposit** — the biggest drop-off. Interventions: minimum deposit framing, multi-chain detection, a "waiting for your deposit" live state, and a nudge sequence
4. First market card, pre-filled
5. **First trade** — the activation event
6. First fill → position view + `/stop` pointed out

Then: the second-session hook. A user who trades once and never returns is the most expensive user we will ever acquire. Specify the day-1, day-3, and day-7 re-engagement (an alert that is genuinely relevant, not a "we miss you" message).

**Define activation precisely and measure it.** Recommend: a funded wallet plus one matched order within 7 days of signup.

### D5. Retention mechanics
- **Alerts are the retention engine.** A user with one active alert comes back. Specify the nudge that gets a new user to their first alert, and the target alerts-per-active-user.
- Watchlists and follows — same logic
- The self-ranking hook (P11 D4)
- Automation rules: a user with a live rule checks the app daily. This is the strongest retention mechanic we have and it is also the Pro upsell.
- **The losing-streak problem.** Prediction markets resolve to zero. A user who loses their first deposit churns and tells people. Specify the honest onboarding about risk, the max-loss display in the trade ticket, and what we send after a total loss. **Do not send a "win it back" message.** That is how a product becomes a scam.
- Churn definition, measurement, and the win-back sequence (or the deliberate decision not to have one)

### D6. Monetisation ramp — sequenced, not simultaneous
1. **Launch at 0 bps builder fee.** Betmoar and Stand.trade both charge zero and are top-5. We are buying volume share, not margin. The leaderboard position is the moat.
2. **Pro subscription** ($12/mo web, Stars-equivalent in Telegram) — depth, automation, API, unlimited watchlists. **Never paywall the ability to exit a position.**
3. **Raise builder fees to 10–25 bps only after retention holds.** Remember the mechanics: one change per 7 days, 3 days advance notice, one pending change at a time — we cannot react fast, so we cannot experiment aggressively. And rates are publicly queryable, so pricing is a public product decision.
4. **Grants:** apply to the Polymarket builders program ($2.5M pool, $100–$75k each) with real traction numbers. This is non-dilutive money and it is available to us specifically.
5. **API/data licensing** — we index everything anyway. Competitors charge $19.99/mo for API access.
6. **Never:** a token. Telegram requires TON for Mini App token distribution, it is a regulatory target, and it will consume the entire year.

State the revenue mix target at month 6 and month 12, with the constraint from P1: **builder fees never exceed ~60% of revenue**, because Polymarket can revoke the privilege at its sole discretion.

### D7. Metrics, gates, and the decision rules
Define exactly, with the formula and the source:
- Activation rate (signup → funded + first matched order, 7d)
- Routed volume per active user per month
- Alert → trade conversion
- D1 / D7 / D30 retention
- Free → Pro conversion, and revenue per active user
- Attributed volume as a share of platform builder volume
- **Revenue per 1,000 routed dollars** — the number that tells us whether the builder economics actually work
- CAC per channel vs LTV per channel

Then the **gates**, with the decision attached to each:

| When | Gate | If missed |
|---|---|---|
| Week 4 | 2,000 channel members OR 500 Mini App MAU | Change the wedge. Do not build trading. |
| Week 8 | 30 users who traded twice; $50k attributed volume in a week | The product isn't sticky. Fix before scaling. |
| Week 12 | $150k/mo attributed volume, 150 paying users | Reassess whether this is a business or a hobby. |
| Month 6 | $500k/mo attributed volume, $15k MRR | Raise fees, or raise money, or stop. |
| Month 12 | $1.5M/mo volume, $40k MRR | On the path to $1M ARR in year 2–3. |

**Write the honest version of the month-12 miss case: what we do, what we tell users, and how we shut down cleanly if it fails.** A plan with no exit is not a plan.

### D8. Launch assets — write them
- Landing page copy: headline, subhead, the three messages, the risk disclosure, the CTA. No "revolutionising", no "seamless", no "empowering".
- The `/start` message
- The pinned channel message
- The launch post for X and for Telegram
- The Product Hunt / Hacker News post (and the honest assessment of whether either audience is our beachhead — probably not, argue it)
- The DM template for channel cross-promotion
- The support macros for the 10 questions we will get most
- The "we are not affiliated with Polymarket" disclaimer, worded correctly, in the footer of every page

### D9. Community & trust
- Where we are present, and the rule that we are useful before we are promotional
- How we handle a public complaint about a loss (spoiler: transparently, and without arguing)
- The public status page and the public changelog
- **The support-impersonation defence** (P12 D6) as a marketing asset — competitors' users are actively warned about fake support; making our defence visible is a differentiator
- What we say about returns: never expected returns, never "guaranteed", losing wallets shown next to winning ones

---

## Constraints
- Two channels maximum until the first one works.
- No paid acquisition before activation is above the gate.
- No feature built for growth that is not instrumented.
- No message that implies a profit is likely.

## Quality gate
Read this plan and answer: who exactly is the first 1,000 users, where do we find them, what do we say to them, what do we measure on day 7, and what do we do if the number is half of what we hoped? If any answer is vague, the plan is not done.
