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

## 12. Corrections from the P01 build session (added 2026-09-16 16:31 UTC, nothing above deleted)

Verified live from the build workspace with `tools/datasource-probe.py` (26/26 endpoint assertions).
These are fact corrections to §2 and §6, so later sessions do not re-inherit them.

| § | Stated | Correct |
|---|---|---|
| §2 | Gamma `/markets` 300, `/events` 500 per 10 min (implies large pages) | **Pages are capped at 100 rows** regardless of `limit=`; default is 20. `offset=` works. Any top-N volume figure needs pagination (300 rows → $59.2M, which reproduces §5's $59.1M). |
| §11 | "Fill rate on `/trades` feed ~20.8 trades/sec" | `data-api /trades` is **served from Cloudflare cache** (`cf-cache-status: HIT`; byte-identical 8 s apart, one pull 300 s stale). Measured tape rate **14.7–33.3 fills/sec**. It is a snapshot for backfill, **not a feed** — real-time tape requires the WebSocket. |
| §5 | Wedge "5-minute crypto Up/Down — highest frequency, highest fee rate" | Up/down markets are **≤0.14% of top-100 market volume** and **absent entirely** from volume-ordered `/events` (0 of 300; event-level `volume24hr` is null/0 there). Frequency ≠ volume; do not size a product on it. |
| §2 | Leaderboard `lb-api /volume` | Also `GET /profit` (both for `window=1d\|7d\|30d\|all`). **`/pnl` does not exist (404)** and **`/rank` is unusable** (400 naming a missing `rank` param even when it is supplied). PnL/rank must be computed by us and labelled as our estimate. |
| §2 | Data API "trades, positions, holders, activity" | Exact required params: `/positions?user=`, `/activity?user=`, `/value?user=`, `/traded?user=`, `/holders?market=` (400 with the param name in the error otherwise). **There is no `/profile` endpoint (404)** — trade rows already carry `name, pseudonym, bio, profileImage, title, eventSlug, icon, transactionHash`. |
| §3 | Fee category table lives only in this doc | Gamma exposes **`feeType` per market** (`crypto_fees_v2`, `politics_fees`, `sports_fees_v2`, `economics_fees`, `culture_fees`, `finance_prices_fees`), so fee category is readable, not guessable. `feeRate` itself was `null` on sampled markets. |
| §4 | (PnL guidance) | `REDEEM` rows in `/activity` have **`price == 0` on 366/366** and carry payout in **`usdcSize`** (nonzero on 280/366, exactly `1:1` with `size` ⇒ $1/share). Computing realised PnL from `price × size` prices every redemption at zero. |
| §6 | "Polywhaler (30k…)" etc. | Competitor onboarding flows are **not** machine-inspectable from a sandbox: 4/6 sites return empty bodies to curl, `polymarketanalytics.com` returns **429 Vercel Security Checkpoint**. Step counts must be measured by a human with a wallet. |

Open upstream gap: `⚠️` on §7 brand tokens was not re-checked this session (CSS-derived values are a
design input, not an API fact).
