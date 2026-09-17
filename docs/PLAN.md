# Openout — "gmgn.ai for Polymarket"

> Working name during specification: `PolyGM`. Renamed per P02 D2 — see `brand/BRAND-KIT.md`.
### Build plan, revenue model, budget and risks
*Prepared 16 Sep 2026. Every market figure below was pulled live from Polymarket's public APIs today or verified against a named source.*

---

## 0. What you are actually building (and what you are not)

You described Binance / OKX / Crypto.com. **Drop that frame.** Those are custodial exchanges: they hold customer money, list assets, and carry money-transmission / exchange licences in every jurisdiction they touch. That is a $10M+ and multi-year business, and it is a *worse* business than the one you actually described.

What you described in your follow-up — "same to same like gmgn.ai, they have their own platform plus a Telegram bot where the user can trade and check everything he needs" — is a completely different and far better-shaped product:

> **A non-custodial analytics + trading front end that sits on top of someone else's order book.**

You do not need a licence for this, because you never custody user funds. gmgn.ai does exactly this for Solana memecoins. You want the same thing for prediction markets.

**Product definition:** a Telegram Mini App (plus web) that gives a trader, in one screen: live Polymarket prices, order-book depth, whale flow, trader leaderboards, alerts, and one-tap execution into Polymarket's own order book — with their funds in their own wallet.

### For comparison, the scale you are modelling yourself on
gmgn.ai, per DefiLlama (on-chain measured, tracked via Dune): **$46.74M in fees over the last 30 days**, of which **$38.35M was protocol revenue**; trailing-year annualised **$209.26M fees / $167.52M revenue**. Its whole model is a flat ~1% handling fee per trade through a custodial hot wallet, no subscription.

That is the ceiling of the *category*, not of prediction markets. Read §2 before you anchor on it.

---

## 1. The opportunity, verified today

I ran these against Polymarket's live APIs at **15:07 UTC, 16 Sep 2026**:

| Metric | Value | Source |
|---|---|---|
| 24h volume, top 500 active events | **$59.1M** | Gamma `/events?order=volume24hr` |
| Liquidity across those events | **$477.3M** | same |
| Open interest | **$287.7M** | same |
| Top-10 events' share of that volume | **58.4%** | same |
| Median event 24h volume | **$19,910** | same |
| Live fill rate on the trade feed | **~20.8 trades/sec**, 342 distinct wallets per 500 trades | Data API `/trades` |
| Largest single fill in a 24-second window | $3,000 | same |

Run-rate that implies: roughly **$600–750M/month** and **$8–10B/year** of volume on the international CLOB. It is a real market. It is also *concentrated* — 58% of volume sits in 10 events, and the median event does $20k/day, so long-tail markets are basically dead water.

---

## 2. The honest ceiling — read this before you spend anything

Your revenue comes almost entirely from **Polymarket's Builder Program**. Verified mechanics, from `docs.polymarket.com/programs/builders/fees`:

- You register a builder profile at `polymarket.com/settings?tab=builder` and get a `bytes32` **builder code**.
- You attach it to every order. It is part of the signed CLOB V2 order struct (`salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp, metadata, builder`) and appears in every on-chain `OrderFilled` event. Attribution is on-chain, not an off-chain label.
- **You set your own rates, hard-capped: taker 100 bps (1%) max, maker 50 bps (0.5%) max.** Default is 0.
- Rate changes are gated: one change per 7 days, 3 days advance notice, one pending change at a time.
- Builder fees are **additive** to platform fees — the user pays both.
- Fees only accrue on **matched** orders. An order that never fills earns nothing.
- **Polymarket can revoke your ability to charge a builder fee in its sole discretion**, and can disable your code entirely — orders carrying a disabled code are rejected by the CLOB.

### What that actually pays

The whole builder ecosystem did **$125M of attributed volume in one week** in Feb 2026 (third consecutive week over $100M), across roughly 114 tracked builders. Recent 30-day top-10 by volume:

| # | Builder | 30d volume |
|---|---|---|
| 1 | Betmoar | **$101M** |
| 2 | PolyCop | $38.8M |
| 3 | WagerUpPilot | $24.2M |
| 4 | PolyTraderPro | $18.9M |
| 5 | Polymtrade | $13.5M |
| 6 | Kreo | $10.8M |
| 7 | Stand.trade | $9.6M |
| 8 | Polygun | $9.5M |
| 9 | Chance | $9.4M |
| 10 | Gate | $9.0M |

Distribution is brutal: Gini coefficient **0.83** across the top 50. Six builders hold **81%** of all lifetime volume. The median builder in the top 50 has done **$4.7M lifetime**. In Q1 2026, **80% of builders did under $1M for the whole quarter**, and a third never crossed $10k.

Only one builder has publicly disclosed economics: **Based, ~$1M ARR from builder rewards at roughly $10M/month of attributed volume** → an implied **~0.83% take rate**. PolyTrack independently estimates 0.5–1%.

### So the "$1M" question

At the 100 bps taker cap:

```
$1,000,000 / year  ÷  1.0%  =  $100,000,000 / year of routed volume
                            =  $8.3M / month
```

**$8.3M/month of attributed volume makes you roughly the #3 builder on the platform, behind Betmoar.** That is not a first-year outcome. It is a 24–36 month outcome if everything goes right.

Stacking the other lines:

| Revenue line | Realistic 12-month contribution |
|---|---|
| Builder fees (0–1% of routed volume) | $20k–$150k at $200k–$1.5M/mo volume |
| Pro subscription ($10–20/mo) | $30k–$120k at 300–800 paying users |
| Polymarket builder **grants** ($2.5M pool, $100–$75k each) | $25k–$75k, one-off |
| Data / API licensing (you index everything anyway) | $0–$50k |
| Referral / affiliate | $5k–$30k |

**Realistic year-one total: $80k–$400k.** Honest answer: **$1M ARR is a year-2/3 target, not a year-1 target.** Anyone who tells you otherwise is selling you something.

**The single biggest strategic risk is that one company controls 100% of your revenue and can switch it off at will.** Never let builder fees be your only line.

---

## 3. Competitive reality — the space is NOT empty

You should know this before hiring. Both halves of your idea already exist, separately:

**Analytics (crowded, cheap):** Hashdive (72k monthly visits, free), Polywhaler (30k, the original), Polymarket Analytics (free + $20/mo, 1 ETH lifetime), Polysights (24k, pre-1.0), PolyTrack ($9.99/wk or $19/mo), PredictFolio (free, CC BY-NC), OrcaLayer ($9.99/$19.99), PolyMonit ($5.99/$9.99), PolySharks ($19.99), Unusual Predictions ($30–80/mo), plus Alphascope, Polyburg, Merlin Trade.

**Telegram trading bots (crowded):** Betmoar (the volume leader — anonymous, no press, $101M/30d), PolyCop, TradePolyBot, Polyfox, Polygun, PolyBot, Polylerts, PolyTracker Bot, PolyxBot, PolyCopy, Polycopybot, Kreo, Stand.trade, Chance, Rainbow.

### So where is the actual gap?

I looked at what these actually *are*. Every Telegram bot in this list is an **inline-button menu**: paste a link, get price buttons, tap BUY. There is no order book, no chart, no depth, no position view, no portfolio. And the analytics tools are all **web dashboards with a separate Telegram alert pipe**.

**Nobody has shipped a real terminal inside Telegram.** That is precisely the gmgn move — gmgn won memecoins not by having better data than DEX Screener, but by collapsing *look at it* and *trade it* into one tap on a phone. That is the wedge.

Secondary gaps worth taking:
- **Cross-venue.** Nobody credibly does Polymarket ↔ Kalshi in one terminal. gmgn's moat is multi-chain; yours would be multi-venue.
- **The 5-minute crypto Up/Down markets.** These are the highest-frequency markets on the platform (I saw several in a single 24-second trade window: `btc-updown-5m-1789571100`, `eth-updown-5m-1789571400`). They are pure terminal-and-speed products — exactly what a Mini App is good at and what a button-menu bot is bad at. Note crypto carries the **highest taker fee rate (0.07)**, so size your builder fee accordingly or you will price yourself out.
- **Alert → trade in one tap.** Everyone does alerts; nobody closes the loop into an execution.

---

## 4. Product spec

### Wedge (be narrow, or die)
Pick **one** of these and win it, in order of my preference:

1. **"The 5-minute crypto terminal in Telegram"** — highest frequency, highest fee rate, worst-served by existing button bots, natural fit for a Mini App. Fastest path to routed volume, which is what pays you.
2. **Whale-flow → one-tap follow.** Alerts on ≥$5k fills with a BUY button pre-sized to the alert. Converts free users to trading users faster than anything else.
3. **Copy-trading with a real UI.** PolyCop and Polyfox prove demand; both have terrible interfaces.

### Surfaces
- **Telegram Mini App** (primary) — full web UI inside Telegram, inherits identity, no login. This is the differentiator.
- **Telegram bot** — alerts, `/pnl`, `/pos`, `/stop`. Distribution + retention.
- **Web app** — the same UI, for SEO and for taking card/crypto subscriptions outside Telegram.
- **Free public whale channel** — your acquisition engine. gmgn and every successful bot here grew through a free alert channel, not paid ads.

### Feature ladder
| Tier | Feature | Why |
|---|---|---|
| Free | Live tape, top events, order book, whale alerts (capped), 3 watchlists | acquisition |
| Free | Trade, manual orders, portfolio | **routed volume = your revenue** |
| Pro | Unlimited watchlists, 2s refresh, insider/cluster scoring, backtests, copy-trading, API | subscription |
| Pro | Automation rules (price trigger, TP/SL, time-window entries) | the "automate Polymarket" ask |
| Later | Cross-venue arb detection (Polymarket ↔ Kalshi) | the moat |

### Pricing
- Launch at **0 bps builder fee**. Betmoar and Stand.trade both charge zero and both are top-5. You are buying volume share, not margin. The leaderboard is the moat.
- Monetise with **Pro subscription** first ($12/mo web, ~$9.99-equivalent Stars in Telegram).
- Raise builder fees to 10–25 bps only after you have retention. Remember the 7-day cooldown + 3-day notice — you cannot react fast.
- **Never charge >25 bps** in politics/finance (platform taker fee is 0.04 there, so users are already paying ~1¢/share at 50¢; your fee stacks on top and users are fee-sensitive).

---

## 5. Architecture

```
┌──────────────────────── DATA PLANE (read-only) ───────────────────────┐
│  Gamma API ─── market/event metadata                                 │
│  CLOB REST ─── books, prices, /prices-history                        │
│  CLOB WS  ──── wss://ws-subscriptions-clob.polymarket.com/ws/market   │
│                (book, price_change, last_trade_price, tick_size_change)│
│  Data API ──── /trades, /positions, holders, leaderboard              │
│        │                                                             │
│        ▼                                                             │
│  ingest workers → Postgres (state, users, orders) + ClickHouse (tape)│
│        │                                                             │
│        ▼                                                             │
│  signal engine: whale threshold, volume-spike z-score, new-wallet    │
│  clustering, cross-market divergence → alert fanout → Telegram       │
└──────────────────────────────────────────────────────────────────────┘

┌────────────────────── TRADING PLANE (per user) ───────────────────────┐
│  wallet service: 1 Polygon wallet per Telegram user                   │
│    key encrypted (AES-GCM) under KMS/HSM envelope, exportable         │
│    → deposit USDC/USDT any chain → bridge → swap to pUSD              │
│  order service: derive L2 creds (api key / secret / passphrase)       │
│    → py-clob-client-v2 / @polymarket/clob-client-v2                   │
│    → signed CLOB V2 order (+ your builderCode) → POST /order          │
│    gas paid by Polymarket's relayer (users never touch POL)           │
│  risk service: per-user position cap, daily loss halt, kill switch    │
└──────────────────────────────────────────────────────────────────────┘

┌────────────────────── PRODUCT PLANE ──────────────────────────────────┐
│  Telegram Mini App (React)  ·  Bot  ·  Web  ·  Billing (Stripe+Stars) │
└──────────────────────────────────────────────────────────────────────┘
```

### The technical facts your devs must not get wrong

These are the things that will silently break a build, and I verified each one:

1. **CLOB V1 is dead.** V2 has been mandatory since **28 April 2026**. New SDKs are `py-clob-client-v2` / `@polymarket/clob-client-v2` / `polymarket_client_sdk_v2`. Constructor takes an options object; `chainId` → `chain`.
2. **Collateral is pUSD**, not USDC.e. It is an ERC-20 backed by USDC. Every onboarding flow must convert.
3. **The order struct changed.** `nonce, feeRateBps, taker` → `timestamp (ms), metadata, builder`. EIP-712 Exchange domain version is `"2"` (API auth unchanged).
4. **Fees are no longer in the signed order** — the protocol sets them at match time. So you cannot compute a user's exact fee client-side with certainty. Show an estimate and cap market buys with an all-in spending limit.
5. **Minimum order size is 5 shares** and `minimum_tick_size` varies per market — I read `minimum_order_size: 5`, `minimum_tick_size: 0.001` live off a Fed market today. Orders below min or off-tick are rejected. Check `accepting_orders`, `seconds_delay`, and `neg_risk` per market before submitting.
6. **Rate limits are per-IP and per-signer.** Documented: CLOB general 9,000/10s, `POST /order` 5,000 burst / 120,000 per 10 min, `POST /orders` (batch, ≤15) 2,000/21,000, `DELETE /cancel-all` only 250/6,000, `GET /balance-allowance` just 200. **Server-side caching is mandatory** — this is why my prototype has a shared cache loop instead of calling per request.
7. **Use WebSocket, not polling**, for anything latency-sensitive. Polling will lose you the 5-minute markets.
8. **Cache positions locally**, sync every few minutes. Syncing every loop iteration burns your rate budget.
9. **Circuit breakers are not optional.** Daily loss halt + kill switch, enforced *before* the order reaches the execution layer.
10. **Builder profile rates are publicly queryable** — users can see what you charge. Pricing is a public product decision.

### Key management — the decision that determines whether you survive

You are not a custodian legally, but operationally **you hold keys that can move user money**. One breach ends the company. Options:

| Option | Cost | Trade-off |
|---|---|---|
| **Managed embedded wallets** (Turnkey, Privy, Dynamic) | ~$0.05–0.20 per wallet/mo | Fastest, audited, policy engine (restrict to CLOB contract only). **Start here.** Turnkey already publishes a Polymarket builders cookbook. |
| Self-hosted, AES-GCM + cloud KMS/HSM | ~$50/mo + audit | Cheaper at scale, but *you* own the incident. Needs a real audit ($15–30k) before you hold meaningful TVL. |
| User imports own key | $0 | Safest for you, worst UX, kills conversion. Offer as a "pro" escape hatch only. |

Non-negotiables regardless of choice: key export must work (competitors advertise it, users check), withdrawal password, per-key spending policy scoped to the CLOB contract address only, and an on-chain audit log users can read themselves.

### Regulatory — the good news

- **You are not an exchange.** Non-custodial, no listings, no order book of your own, no fiat on-ramp of your own. No MSB registration, no money-transmitter licences, no VASP registration for the tool itself. This is the entire reason this business is buildable by a small team and Binance is not.
- **Telegram platform rule:** digital goods and services sold *inside* Telegram must be paid for **exclusively in Telegram Stars** (XTR), for App Store/Play compliance. No crypto, no third-party processor, inside the Mini App. Stars ≈ $0.013–0.015, app stores take up to 30%, withdrawal via Fragment has a 21-day hold and a 1,000-Star minimum. → **Sell Pro on your website with Stripe + crypto; give Telegram users a Stars-priced equivalent.** Do not try to sell a subscription in crypto inside Telegram.
- **If you ever launch your own token and distribute it via a Mini App, Telegram requires TON** and removes apps distributing Ethereum/BNB assets. Don't plan a token for year one.
- **Geofencing:** the international CLOB is not the US-regulated product. Polymarket bought CFTC-licensed QCEX for $112M and got CFTC approval as a Designated Contract Market (Oct 2025), and relaunched in the US on a separate regulated footing with its own fee schedule (uniform 0.05 taker, −0.0125 maker rebate, effective 3 Apr 2026). Your tool routes to the international CLOB, so **geofence US users out of trading** until you deliberately build the US path. Getting this wrong is how prediction-market companies get sued.
- **India (you're in Surat):** a non-custodial analytics/execution tool for global users is not a reporting entity under FIU-IND's VDA regime the way an exchange is. But this is the one place you should actually spend money on a lawyer, not on a guess. Also model the 30% VDA tax + 1% TDS for any Indian users you do serve.
- **Terms of service risk is the real regulatory risk.** Polymarket's builder terms let them revoke your fee privileges for "self-referred or non-genuine trading activity." Wash-trading your own volume to climb the leaderboard gets your code disabled and your orders rejected. Don't.

---

## 6. Roadmap — 90 days to first routed dollar

### Phase 0 — Days 1–30: distribution before product
You cannot monetise without volume, and volume comes from distribution. Ship the **read-only** half first — it is ~40% of the work and 100% of the acquisition.

- [ ] Free public Telegram channel: whale alerts (≥$5k fills), volume-spike alerts, new-market alerts. Post programmatically from the tape. **Target: 2,000 members.**
- [ ] Mini App MVP (read-only): live tape, top events, order book + depth, trader leaderboard, watchlists with alerts.
- [ ] Web version of the same, for SEO. Target the queries your competitors rank for ("polymarket whale tracker", "polymarket analytics").
- [ ] **Apply for the builder code now** (`polymarket.com/settings?tab=builder`) even though you aren't routing yet — approval takes 1–2 weeks.
- **Gate to proceed:** ≥2,000 channel members or ≥500 Mini App MAU. If you can't get this with a free product and zero ad spend, the rest of the plan is moot — stop and change the wedge.

### Phase 1 — Days 31–60: first routed dollar
- [ ] Wallet service (Turnkey or Privy), deposit → pUSD, key export.
- [ ] CLOB V2 order execution with your builder code attached. **Test on tiny sizes; the V1→V2 migration broke every bot on the platform in April.**
- [ ] Manual BUY/SELL in the Mini App. Nothing automated yet.
- [ ] Builder fee at **0 bps**.
- [ ] Risk service: per-user daily cap + global kill switch, before anything else goes in.
- **Gate to proceed:** ≥$50k of attributed volume in a week, ≥30 users who trade twice.

### Phase 2 — Days 61–90: the automation you asked for
- [ ] Copy-trading: pick a wallet from the leaderboard, set multiplier + daily cap + category filter, mirror fills.
- [ ] Rule engine: price-trigger entries, TP/SL, time-window entries (built for the 5-minute markets).
- [ ] Pro tier live: web via Stripe, Telegram via Stars.
- [ ] Grant application to the Polymarket builders program with your traction numbers.
- **Gate to proceed:** $150k/month attributed volume, ≥150 paying users.

### Phase 3 — Months 4–12
Cross-venue (Kalshi), API/data licensing, raise builder fee to 10–25 bps only if retention holds, and the second venue that stops Polymarket from being able to kill you.

---

## 7. Budget — the uncomfortable part

You said under **$10k** and that you'll **hire devs**. I have to be straight with you: **a production build of this scope is $25k–$60k** from a competent contract team (India/Eastern Europe rates, 2–3 devs, 3 months). $10k buys you roughly one senior contractor for 6–8 weeks, or one very good contractor for 3 months if the scope is cut hard.

**What $10k actually buys:**

| Item | Cost |
|---|---|
| Senior full-stack contractor, 3 months, part-time on a cut scope | **$6,000–7,500** |
| Infra (Hetzner VPS ×2, managed Postgres, ClickHouse Cloud free tier) | $150/mo → **$450** |
| Domain, Telegram BotFather (free), Turnkey/Privy free tier | **$100** |
| Lawyer — one jurisdictional opinion (do not skip this) | **$1,000–1,500** |
| Key-management audit | deferred to Phase 1 gate |
| Contingency | **$500** |

**Scope cuts that make $10k work:**
1. Read-only Mini App + alert channel first. No trading in month one. (Saves ~$8k.)
2. Managed wallets only. No self-hosted key infra, no audit until you hold real TVL. (Saves ~$5k + $20k audit.)
3. One venue (Polymarket). No Kalshi.
4. No native mobile app. Mini App only.
5. No token, no points, no airdrop.
6. No copy-trading engine until Phase 2.

**Do not** hand a $10k budget to an agency that promises the full feature list. You will get a demo that cannot hold a private key safely.

---

## 8. Hand-off spec for your developer

Give them exactly this. It is the working reference implementation I built you in `/home/user/polygm`:

| File | What it is |
|---|---|
| `server.py` | Zero-dependency Python backend. Live ingestion from Gamma + CLOB + Data + `lb-api`, a shared cache refreshed every 20s, 5 JSON endpoints, static file serving. |
| `public/index.html` | The Mini App front end — mobile-first, 4 tabs, live tape with whale flags, order-book depth sheet, paper-trading flow, portfolio stub. |

**Verified working today** (I ran it): `/` → 200, `/api/state` → 200 with 60 events, 300 tape entries, 25 leaderboard rows, 24 order books, `errors: []`. It runs on `0.0.0.0:8080` and accepts arbitrary `Host` headers, so it works behind the preview proxy.

What your dev does with it:
1. Replace `server.py`'s polling loop with the **CLOB WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) for books and tape. Keep the cache; drop the polling interval to 60s+ as a fallback.
2. Move the cache to Redis + ClickHouse so the tape is queryable historically (this is what turns into the paid analytics tier and the API product).
3. Add the **trading plane as a separate service** with its own credentials and no inbound internet exposure. Never put a private key in the same process that serves HTTP.
4. Replace `localStorage` paper trades with real orders via `py-clob-client-v2`, with the risk service in front.
5. Attach the builder code to every order from day one, even at 0 bps — attribution history is what gets you a grant.

---

## 9. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Polymarket disables your builder code | **Fatal** | Never let builder fees exceed ~60% of revenue. Ship subscriptions and API licensing early. No self-referral, ever. |
| Key-management breach | **Fatal** | Managed wallets, policy-scoped keys, audit before TVL, insurance once you're real. |
| Another CLOB version migration (V1→V2 broke everyone in April) | High | Pin SDK versions, subscribe to their Discord `#devs`, keep a v1→v2 adapter layer. |
| Betmoar's distribution advantage ($101M/30d, anonymous, no press) | High | Don't fight on general trading. Win one narrow wedge (5-minute markets) they don't care about. |
| Users lose money and blame you | High | Prominent risk disclosure, no "guaranteed" language anywhere, copy-trading shows the *loser* wallets too. Do not run a signal channel that looks like advice. |
| Telegram bans the bot | Medium | Mirror to Discord + web. Web is the hedge. |
| Rate-limit bans during load spikes | Medium | Server-side cache (already in the prototype), per-signer token buckets, backoff. |
| Analytics space is saturated | Medium | Don't sell analytics. Sell execution. Analytics is the acquisition layer. |
| You are solo and non-technical | Medium | One senior contractor, tight scope, weekly demo gate. Do not hire an agency. |

---

## 10. Bottom line

- **The product is right.** A gmgn-style non-custodial terminal for Polymarket is a real, buildable, unlicensed business.
- **The framing was wrong.** Binance/OKX/Crypto.com is a licensing and capital problem you cannot solve at $10k. This isn't.
- **The number is wrong.** $1M ARR requires ~$8.3M/month of routed volume at the fee cap — top-3-builder territory. Year one is realistically $80k–$400k. Plan for that and you'll still be building something real; plan for $1M in year one and you'll overspend on the wrong things.
- **The gap is real but narrow.** Analytics is saturated; execution UX inside Telegram is not. Build the terminal, not another dashboard.
- **Your constraint is distribution, not code.** Phase 0 is a free alert channel. If that fails, nothing downstream matters.
- **$10k is tight but workable** if you cut to read-only Mini App + alerts in month one, use managed wallets, and hire one senior contractor rather than an agency.

*One term I couldn't identify: you wrote "onada" in your list of platforms. I couldn't match it to a product and left it out. If it's a specific exchange or tool you admire, tell me and I'll fold it into the comparison.*
