# P1 — Validated Product Specification · PolyGM (now Openout)

*Produced: 2026-09-16, 16:22 UTC (15:22 IST, day 1). Verified against live Polymarket APIs at the
same time. Method and raw output: `docs/verification/P01-verification-log.md`,
reproducible via `python3 tools/datasource-probe.py`.*

**Standing verdict on the brief:** two premises in `00-SHARED-CONTEXT.md` are wrong in ways that
change the build, and one of the four candidate wedges is dead on measured data. I say so up
front, per the "if you believe the wedge is wrong, say so" instruction, rather than writing a spec
that quietly inherits the error.

| # | Premise in the kit | Measured tonight | Consequence |
|---|---|---|---|
| **E1** | Gamma `/markets` "300 req" limit implies large `limit=` values | `limit=200` → **100 rows**; `limit=500` → **100**; default → **20**. `offset=` pagination *works*: 3 pages → 300 events, **$59.19M** 24h volume, which reproduces the kit's own $59.1M top-500 figure to 3 significant figures | Top-N volume/liquidity arithmetic in `PLAN.md` is undercounted unless offset-paginated. Any "total market size" number derived from one page is wrong by design — and the kit's number is right *because* it paginated. |
| **E2** | "Fill rate on `/trades` feed ~20.8 trades/sec" | `GET /trades?limit=500` → `cf-cache-status: **HIT**`, newest trade byte-identical across 12 s; one pull served a fill **300 s old**. Uncached span: 500 fills / 34 s = **14.7/s** | `/trades` is **not a feed**. Polling it for a tape produces a stale-number UI — the exact failure AGENTS rule 5 forbids. Real-time *requires* the WebSocket. This single finding sets the P5 architecture. |
| **E3** | Wedge 1: "5-minute crypto Up/Down terminal — highest frequency, highest fee rate" | Up/down markets are **≤ 0.14% of the top-100 markets' 24h volume** (two pulls: 0 of 100, then exactly 1 of 100 — `btc-updown-5m-1789575300` at $64,842, rank **#91**; top-100 total $45.1M). They **never appear in volume-ordered `/events` pages** (0 of 300) and carry `volume24hr: null/0` at event level; the nearest-ending crypto events are **50/50 up/down** by count | Frequency ≠ volume. The fee rate is real and *readable per market* (`feeType: crypto_fees_v2`; the field also exposes `politics_fees`, `sports_fees_v2`, `economics_fees`, `culture_fees` — this list is the SET OBSERVED IN OUR SAMPLE, not a closed taxonomy, so read the value per market and never hard-code an allow-list of categories), but the notional that earns it lives in non-up/down markets. Wedge 1 as written is not viable — and event-level queries cannot even see it. |
| **E4** | Leaderboard endpoint list | `/volume` and `/profit` work for `1d/7d/30d/all`; **`/pnl` → 404**; **`/rank` → 400 `required query param 'rank' not provided` even when `rank=volume` is sent** | PnL features must be computed by us (which we'd rather not — see D4), and no per-wallet rank API exists. `/rank` is unusable `[UNVERIFIED: reason]`. |
| **E5** | Data API surface | `/positions?user=`, `/activity?user=`, `/value?user=`, `/holders?market=`, `/traded?user=` all 200; **`/profile` 404**; `/trades` carries `name`, `pseudonym`, `bio`, `profileImage`, `title`, `eventSlug`, `icon` inline | Trader-identity joins are free on the tape — a real build-cost saving (D3). There is no profile endpoint; identity comes from the trade row itself. |

Everything below is built only on rows marked measured, on `00-SHARED-CONTEXT.md` (cited as
**[CTX]**), or flagged `[UNVERIFIED]`.

---

## D1. Wedge selection

### Scoring

Weights, stated before scoring and derived from the founder's constraints (AGENTS rules 9 and the
week-6 "read-only product live" milestone):

| Criterion | Weight | Why this weight |
|---|---|---|
| Time to first routed dollar | 30% | Cash envelope <$10k, hiring; the plan's whole sequencing argument is "if money runs out after week 6 you still have a product" [CTX P0] |
| Defensibility / non-copyability | 20% | Analytics is already saturated with ~13 clones [CTX §6] |
| Engineering cost at <$10k | 20% | Inverted: cheap = high score |
| Would Betmoar care? (incumbent indifference) | 15% | A leader at $101M 30d attributed volume [CTX §5] copies a good wedge in a week |
| Revenue per activated user | 15% | Builder fees are revocable at Polymarket's sole discretion [CTX §4], so per-user monetisation must not depend only on them |

Scores are 1–10, integer, my judgement; the inputs feeding them are measured or cited.

| Wedge | TTD (30) | Defens. (20) | Cost (20) | Betmoar (15) | Rev/user (15) | **Weighted** |
|---|---|---|---|---|---|---|
| 1 · 5-min crypto Up/Down terminal | 9 | 2 | 5 | 8 | 6 | **6.25** |
| 2 · Whale-flow → one-tap follow | 8 | 5 | 7 | 7 | 3 | **6.15** |
| 3 · Copy-trading with a real UI | 4 | 7 | 3 | 4 | 9 | **5.45** |
| 4 · Cross-venue arb (PM ↔ Kalshi) | 2 | 8 | 2 | 9 | 4 | **4.50** |
| **5 · Alert→follow ladder** (2 feeds 3; recommended) | 8 | 7 | 5 | 6 | 8 | **7.15** |

**Wedge 1 scores 6.25 but must be rejected regardless of score**: E3 says its volume base does not
exist as an independent event category. A wedge that is 0/100 of top volume is a *feature* of tape
density, not a product. Its score is high only because frequency and fee rate are favourable — which
we keep, as the highest-priority *market-type filter* inside the recommended wedge.

**Wedge 4 is rejected on a missing data source**, not on merit: the kit contains no Kalshi facts at
all, so it violates the "no feature without a named data source" constraint. `[UNVERIFIED: Kalshi
API rate limits, fee schedule, and cross-venue settlement risk were never probed — one day of work.]`

### Recommendation

**Ship the ladder: whale/high-signal-flow alerts in Telegram as free acquisition, converting to
copy-trading as the paid core.** Same 3-column information architecture, but the paid centre of
gravity is the copy engine, not the alert.

Why not wedge 2 alone: measured median fill on the whole tape is **$5–6** (500-row samples of the
live tape, re-run three times: median notional $5.00 then $6.27, median price ≈0.58, median size
≈10 shares, p90 $49.78–$111.00, max $3,968–$11,664, **0.2–1.2% of fills ≥ $1,000**, and
**14.7–33.3 fills/sec**). The spread across pulls is not sloppiness, it is the finding: a
single-window snapshot of this tape moves the "whale" rate by ~6×, so any threshold fixed on one
sample is a guess. An alert on "a whale bought" therefore fires on noise: at ~15–33 fills/s and
≈1% ≥$1k, a $1,000 threshold yields on the order of **5–15 alerts/minute platform-wide** — a
firehose, not signal. Raising the threshold to be selective kills the volume. And alerts monetise at
0 bps at launch [CTX §4: "Launch at 0 bps"], so the alert wedge has no revenue floor of its own.

Why not wedge 3 first: it cannot reach the week-6 milestone. Copy-trading needs keys, the risk
gate, and reconciliation (P6/P7), i.e. weeks 7–10 in the kit's own sequencing [CTX P0]. Leading
with it means two months of invisible engineering.

The ladder resolves the conflict: alerts are read-only (P5 output, week 4–6), they are the only
surface that can prove freshness (E2 — we win by having the *live* WebSocket tape when cached
pollers lag), and each alert carries a "Follow this wallet" CTA that is the paid product's
onboarding ramp.

### Strongest argument against this recommendation

**The alert's value decays faster than the latency we can control.** A follow-the-whale click is
worth something only if the price hasn't moved. On the one live 5-minute BTC market I measured, the
book had 62 bids / 37 asks, `tick_size: 0.01`, and the mid-market best-ask chain was 1 ¢ wide —
but on an empty side of an illiquid book the *same* API showed best bid `None` with best ask `0.999`
on a 0.001-tick market, i.e. **a 46%-of-mid spread and 0 bids on one side**. So for exactly the
markets where a whale alert looks most actionable, the executable spread can eat the entire edge
inside 2 seconds. A competitor with worse analytics but a market-making relationship beats us on
the one number users feel: the price they actually got. Mitigation (must be built, not promised):
show *quoted-and-expiring* follow price with a countdown, and refuse the one-tap when measured
spread > 3¢ — fail closed per AGENTS rule 5. `[UNVERIFIED: whether Betmoar/PolyCop already show
executed-vs-quoted slippage; their UIs are JS-rendered and I could not fetch them (E: probe
returned empty bodies / Vercel Security Checkpoint).]`

### Kill criterion (first 30 days)

Measured from day 1 to day 30 after the free Telegram alert beta is public. **Any two of these four
⇒ kill the wedge and re-run P1:**

1. **Alert → tap-through < 8%.** (taps on the follow CTA ÷ delivered alerts, 24h window.)
   `[UNVERIFIED: the 8% target is a judgement call, not a measured benchmark — no comparable
   Telegram-bot alert CTR is publicly documented; treat it as the number we chose to be
   accountable to, and revisit at week 2 with our own baseline.]`
2. **Median notional of alert-originated fills ≤ platform baseline $5.00.** If our alerted trades are
   not bigger than the platform median, we are aggregating noise, not information.
3. **Median slippage on alert-originated fills > 1.5¢.** (Quoted price at push ÷ price at match.)
   This is the honest version of "did we help or hurt."
4. **D7 return of the alert cohort < 25%,** measured as ≥1 alert interaction in week 2.

And the volume gate that decides whether *anything* at fee level is possible: **$500k routed volume
in month 1.** From [CTX §5], $1M ARR needs $8.3M/month routed; $500k/month at 100 bps is $500/yr.
If month-1 routed volume is not within an order of magnitude of the fee-required volume, the
business is a subscription business and the product plan changes shape (see D5/D6).

---

## D2. Competitor teardown

**Provenance rule for this section:** `[CTX §5/§6]` = the founder's own verified table, cited not
independently re-verified. `[LIVE 16:22Z]` = I fetched it tonight. `[UNVERIFIED]` = I could not
verify and will not guess. **No user counts are fabricated; the only counts here are the kit's.**
Method note: 4 of 6 sites returned empty bodies to curl (JS-rendered) and
`polymarketanalytics.com` returned **429 Vercel Security Checkpoint**, so onboarding-step counts —
the one thing I most wanted to measure — are **not** verifiable from this sandbox. They need a human
with a wallet, 20 minutes, and a screen recorder. That task is listed as the section's follow-up
rather than invented.

| | Betmoar | PolyCop | Polyfox | PolyTrack | Hashdive | Polymarket Analytics |
|---|---|---|---|---|---|---|
| Surface | Telegram bot + web | Web app + bot | `[UNVERIFIED]` | Web | Web | Web |
| Onboarding steps to first trade | `[UNVERIFIED — needs manual run]` | same | same | n/a (analytics) | n/a | n/a (analytics) |
| Custody / key export | `[UNVERIFIED]` | self-custody claimed `[UNVERIFIED]` | `[UNVERIFIED]` | n/a | n/a | n/a |
| Monetisation | **0 bps** [CTX §4] | `[UNVERIFIED]` | `[UNVERIFIED]` | $9.99/wk, $19/mo [CTX §6] | free [CTX §6] | free + $20/mo, 1 ETH lifetime [CTX §6] |
| 30d attributed volume | **$101M** [CTX §5] | $38.8M [CTX §5] | `[UNVERIFIED]` | n/a | n/a | n/a |
| Things they do well | distribution: top-1 builder at zero price, so switching cost is the only lever; anonymous/no-KYC posture [CTX §5]; already inside Telegram where our buyer lives | copy-trading is the actual product, not a tab; #2 by attributed volume [CTX §5] | `[UNVERIFIED ×3]` | paid discipline — charging for analytics at all; $9.99/wk price anchoring [CTX §6] | free, 72k visits/mo = top-of-funnel ownership [CTX §6] | first-party data trust; official-looking domain; lifetime deal [CTX §6] |
| Three things they do badly | no terminal UX (inline-button menu pattern, the documented gap [CTX §6]); zero feature moat at 0 bps; anonymous team = no press/partnership surface | UI quality is the stated weakness ("both have poor interfaces" [CTX P1]); copy-trade without drawdown display is a compliance exposure `[UNVERIFIED]` | `[UNVERIFIED ×3]` | weekly pricing ($9.99/wk) reads as rental, invites churn; analytics-only ⇒ no execution ⇒ no fee revenue | free ⇒ nothing to defend; no execution | bot-gate (429 to programmatic fetch [LIVE]) means no SEO/aggregator surface; 429 also blocks the very tools that would link to it |
| **Copy** (pattern, not asset) | the ladder: bot-first, wallet-light, zero-fee acquisition | the paid centre = the copy engine with caps/filters | — | that traders pay weekly for *rank*, not for data | that free analytics is the funnel | a lifetime tier as capital float |
| **Avoid** | competing on price (we cannot win at 0 bps against a $101M incumbent) | shipping copy-trade without per-wallet drawdown | — | weekly billing for a terminal people use in bursts | a free-only product with no fee capture | a domain that trips its own bot protection |

**Structural read:** the top-6 builders hold **81% of lifetime attributed volume**, Gini **0.83**
across the top 50, and **80% of builders did <$1M in all of Q1 2026** while a third never crossed
$10k [CTX §5]. That is not a market where a better terminal wins; it is a distribution market.
Which is why D1 picks the wedge whose acquisition costs nothing (Telegram alerts, free, viral by
design) and whose monetisation does not depend solely on the revocable fee.

**Follow-up requiring a human:** record first-trade onboarding step counts for all six (goal: ≤3 steps
for us). `[UNVERIFIED: onboarding step counts for Betmoar/PolyCop/Polyfox/PolyTrack/Hashdive/
Polymarket Analytics — curl got empty bodies or a Vercel checkpoint, so no count in this table is
measured; a human with a wallet must run all six flows before D5's activation gate means anything.]`

---

## D3. Feature specification

Every P0 row has an endpoint that I got a `200` from tonight. "Cost" is eng-weeks at <$10k scope
(S ≤ 1 wk, M ≤ 3 wk, L > 3 wk). "Revenue line" uses: **FEE** = builder bps, **SUB** = Pro $15/mo
(derived below), **DATA** = API licensing (P2, year-2).

| Feature | One-line user value | Pri | Data source (verified response tonight) | Cost | Rev |
|---|---|---|---|---|---|
| **Trending / discovery** | "what's moving, ranked, right now" | P0 | `GET gamma /events?closed=false&order=volume24hr&ascending=false&limit=100` → 100 rows, `volume24hr`, `liquidity`, `competitive` | S | FEE |
| Category & series filters | filter politics/crypto/sports the way the books are actually grouped | P1 | `GET gamma /tags` (200), `GET /series` (200: `nfl`, `seriesType`, `recurrence`) | S | FEE |
| Search | find a market by words, not slugs | P0 | `GET gamma /public-search?q=…&limit_per_type=2` → 200, `.events[]` with `slug`,`ticker` | S | FEE |
| **Event detail: markets + outcomes** | one screen per question, all outcomes and prices | P0 | `GET gamma /events?slug=` → `markets[].clobTokenIds` (**string, JSON-parse**), `conditionId`, `enableOrderBook`, `negRisk` | S | FEE |
| Order book + depth | see size and shape before tapping | P0 | `GET clob /book?token_id=` → `bids,asks,tick_size,min_order_size,neg_risk,hash,timestamp,last_trade_price` | M | FEE |
| Live tape | watch real fills as they happen | P0 | **WS** `wss://ws-subscriptions-clob.polymarket.com/ws/market` (`last_trade_price`, `book`, `price_change`, `tick_size_change`) — REST `/trades` is cached (E2) and may only be used for backfill | M | FEE |
| Freshness indicator on every number | never mistake a stale price for a live one | **P0** (AGENTS 5,6) | derived: WS `timestamp` vs `Date.now()`; REST `cf-cache-status` probe | S | — (trust) |
| Wallet-type classification on each fill | "who was that?" — gmgn's core trick | P0 | tape fields `proxyWallet,pseudonym,name,bio,profileImage` (200 [E5]) + `GET data /traded?user=` + `GET lb /profit?window=7d` as the smart-money set (computed locally; `/rank` dead, E4) | L | FEE+SUB |
| **Trader profile** | 7d/30d PnL, win rate, avg hold, drawdown | P0 | `GET data /activity?user=` → `type ∈ {TRADE,REDEEM}` + `usdcSize`; `GET /positions?user=`; `GET /value?user=`; `GET lb /profit?window=1d\|7d\|30d\|all` | L | SUB |
| **Whale tracker + alert** | ping me before the crowd | P0 | WS tape, threshold **≥ $2,000** (see note) | M | FEE+SUB |
| Follow-this-wallet CTA | one tap from "saw it" to "position on" | P0 | P6 order path, behind the risk gate (never direct) | M | FEE |
| Watchlists / following | my markets, my wallets | P0 | local DB; names resolved from Gamma cache | S | SUB |
| Wallet Radar (multi-market intersection) | find the wallets that keep being right across 5 markets | P1 | our `fills`×`positions` join; four modes defined in D3.2 | L | SUB |
| Portfolio / positions / OMS | one truth for what I hold and what's open | P0 | `GET data /positions?user=`; `GET clob /data/orders`,`/data/trades` (L2, 401 unauthenticated = correct) | L | FEE+SUB |
| Copy trading | follow wallets I choose, with caps | P1 | P6 + `copy_configs`; source wallets from the profile screen | L | SUB+FEE |
| Rule automation (AFK) | sleep without exiting flat | P2 | our engine only; no upstream | L | SUB |
| Leaderboard | rank me honestly | P1 | `GET lb /volume` + `/profit` (4 windows each, 200) | S | FEE |
| Cross-venue comparison | is the same question priced better elsewhere | P2 | **no verified source** — Kalshi not probed. Spec'd, not scheduled | M | SUB |
| Reconciliation badge | "your local numbers match the chain: last ✓ 4 min ago" | P0 | Polygon RPC + our ledger (D4.4); provider = `[UNVERIFIED: needs key]` | M | — (trust) |

**Whale threshold, decided from measurement, not taste.** $1,000 is unusable (≈10 alerts/min across
the platform). $2,000–$5,000 on 14.7 fills/s yields a quotable ~2–5 alerts/hour platform-wide;
per-user delivery is then capped at 20/hour with per-market dedup. `[UNVERIFIED: the ≥$2k and
≥$5k bucket counts — tonight's sample had 6 fills ≥$1k of 500; a 20k-row sample over 24h is a
one-command follow-up before P5 sign-off.]`

### D3.2 Wallet Radar's four modes, translated to prediction markets

| gmgn mode | Polymarket equivalent | Definition we can actually compute |
|---|---|---|
| Most Bought | **Most Traded** | count of distinct `conditionId` traded in window (not share count — share counts reward 5-min churn) |
| Highest Profit | **Highest Realised PnL / $ deployed** | ratio, so a $5 wallet that turned $40 isn't ranked above a $50k wallet that turned $400k |
| Earliest Bought | **First In** | earliest BUY on a market that resolved in that wallet's favour — the actual "they knew" signal |
| Shared Holdings | **Shared Positions** | overlap of held `asset` tokenIds with the viewer's, or with a seed wallet |

### D3.3 gmgn taxonomy mapped onto Polymarket (the thing P1 asks for)

| gmgn tag | Polymarket equivalent | Rule | Verifiable now? |
|---|---|---|---|
| smart money | **Ranked** | in `lb /profit` 7d top-200 **and** win rate > 55% over ≥20 closed positions | Yes, from `/profit` + activity |
| KOL / VC | **Named** | `profileImage`/`xUsername` present on trade row (seen: `xUsername: balthazarpoly`) + `bio` non-empty | Partly — the field is on leaderboard rows; **no** `/profile` endpoint [E5] |
| whale | **Size** | single fill ≥ $2,000 or 24h net notional ≥ $25,000 | Yes |
| new wallet | **Fresh** | first-ever `TRADE` within 72h — requires a full-history scan | **No — needs the earliest-event query** `[UNVERIFIED: whether /activity is queryable to genesis or how deep]` |
| sniper | **Pre-resolution buyer** | BUY in the same market within 60 s of `endDate` at price < 0.95, then REDEEM | Yes — the killer tag for prediction markets, and it's cheap |
| large holder | **Concentrated** | top-5 in `GET data /holders?market=` with ≥ 10% of that token's supply | Yes |
| developer | **Market creator** | Gamma has no verified deployer/creator field tonight → `creator:` tag only if a field appears | `[UNVERIFIED: no data source → NOT built in year one]` |
| rat warehouse | **Dump team** | ≥3 wallets selling the same `asset` within 60 s, wallets that never traded that market before | Yes |
| followed | **Followed** | user's own `follows` table | Yes |

### D3.4 Explicitly NOT built in year one (and why)

1. **Native app.** Mini App + responsive web covers Telegram, which is where the buyer is [CTX §9]. An app store listing adds the Stars-compliance mess for no revenue.
2. **Our own token.** Telegram requires TON for token distribution and removes apps distributing ETH assets [CTX §9]. Zero revenue upside in year 1, existential regulatory downside.
3. **Cross-venue arbitrage execution.** No verified second-venue data source (D1). Comparison view only, P2.
4. **Copy-trading public marketplace / "hire a trader".** Requires counterparty due diligence and turns us into the thing that must answer for losses. Copy only wallets the user already follows, with drawdown shown [CTX §10].
5. **NFT/creator tagging.** No data source (D3.3) — a label with no feed behind it is a lie that ships.
6. **Fiat on-ramp, in-product deposits.** We are non-custodial by design [CTX §1]; the first dollar we touch is the first licence we need.
7. **Multi-chart 8-pane layout** (gmgn has it). Real cost, tiny addressable demand at our stage; ship 1 chart + a compare toggle.
8. **Maker-rebate farming.** Polymarket pays makers 15–25% rebates [CTX §4]; automating rebate capture looks exactly like the "non-genuine trading activity" that is an explicit fee-revocation ground. Not touching it.

---

## D4. Data model

Postgres (source of truth, correctness, money) + ClickHouse (append-only analytics). One rule
decides placement: **if a row must never contradict the chain, it is Postgres; if it only needs to
be approximately right at scale, it is ClickHouse.**

### D4.1 Postgres (19 tables)

`append-only` = A · `mutable` = M

| Table | Key fields (type) | Indexes | Populated by | A/M |
|---|---|---|---|---|
| `users` | id bigserial · tg_user_id bigint null · email citext null · role text · created_at timestamptz | uq(email), uq(tg_user_id) | signup / Mini App `initData` | M |
| `wallets` | id · user_id fk · address char(42) · chain smallint default 137 · label text · first_seen timestamptz | uq(address,chain), idx(user_id) | user connects; also learnt from tape | M |
| `api_credentials` | id · user_id fk · kind text('clob_l2') · secret_ref text · key_version int · created_at · revoked_at | idx(user_id) where revoked_at is null | **stores a KMS/secret-store reference, never the secret** — see P7 | A |
| `events` | id text(gamma) · slug · title · enable_neg_risk bool · enable_order_book bool · liquidity_clob numeric · volume24hr_clob numeric · end_date timestamptz · updated_at | idx(slug), idx(end_date) where volume24hr>0 | `GET gamma /events?limit=100&offset=` paginated (E1) | M |
| `markets` | id text · condition_id char(66) · event_id fk · question · neg_risk bool · accepting_orders bool · minimum_order_size numeric(20,6) · minimum_tick_size numeric(10,6) · seconds_delay int · enable_order_book bool · fee_type text · fee_rate numeric null · closed_at | uq(condition_id), idx(accepting_orders, end_date), **idx(fee_type)** | `GET clob /markets/{conditionId}` + `GET gamma /markets` (200: `feeType: crypto_fees_v2` real) | M |
| `outcome_tokens` | token_id numeric(78,0) · condition_id fk · outcome_index smallint · outcome text · clob_token_ids_pos smallint | uq(token_id), idx(condition_id) | parse `markets.clobTokenIds` (JSON **string**) | A |
| `trades` | **(ClickHouse primary, Postgres keeps 7 days)** fill_id uint64 · token_id · condition_id · side enum · price numeric(10,6) · size numeric(28,8) · notional_usd numeric(28,8) · ts DateTime64(3) · proxy_wallet char(42) | CH: ORDER BY (token_id, ts); PG: idx(ts) | **WS `last_trade_price`** (E2) | A |
| `positions` | id · wallet_id fk · token_id · size numeric(28,8) · avg_cost_basis_usd numeric(20,8) · realized_pnl_usd numeric(28,8) · updated_at | uq(wallet_id,token_id), idx(size) where size>0 | reconcile: `GET data /positions?user=` vs ledger | M |
| `orders` | id uuid · client_order_id text uq · wallet_id fk · token_id · side · limit_price · size · **amount_in_usdc numeric(28,8)** · state enum(draft,gated,submitted,live,matched,canceled,rejected,stale) · risk_decision jsonb · order_hash char(66) null · builder_code text · timestamp_ms bigint · metadata text | idx(wallet_id,state), idx(order_hash) | P6 (V2 struct: `salt,maker,signer,tokenId,makerAmount,takerAmount,side,signatureType,timestamp,metadata,builder`) | A (state via `order_events`) |
| `order_events` | id bigserial · order_id fk · state · at · source enum(ws,rest,risk) | idx(order_id,at) | our own + WS | A |
| `fills` | id · order_id fk null · trade_id bigint · wallet_id fk · maker_or_taker smallint · price · size · fee_platform_usd · fee_builder_usd · matched_at | idx(wallet_id,matched_at) | CLOB user-trades feed (L2) | A |
| `alerts` | id · kind enum(whale,sniper,rat,dump_team,price_move,spread) · predicate jsonb · created_by uuid · is_public bool | idx(kind) | our engine | M |
| `alert_subscriptions` | id · alert_id fk · wallet_id fk · min_notional_usd numeric · max_spread_ticks smallint · rate_cap_per_hour smallint default 20 · muted_until | uq(alert_id,wallet_id) | user | M |
| `watchlists` | id · user_id fk · name · scope enum(market,wallet) · created_at | uq(user_id,name) | user | M |
| `follows` | id · user_id fk · target_wallet char(42) · started_at · ended_at | uq(user_id,target_wallet) | user | A |
| `copy_configs` | id · user_id fk · source_wallet · multiplier numeric(6,3) · per_trade_cap_usd · daily_cap_usd · category_filter text[] · take_profit_bps · stop_loss_bps · enabled bool | idx(user_id) where enabled | user | M |
| `automation_rules` | id · user_id fk · expr jsonb · cooldown_s int · last_fired · max_fires_per_day | idx(user_id) where cooldown gate | user | M |
| `builder_attribution` | day date · market_id fk null · fee_side enum(maker,taker) · rate_bps smallint · matched_notional_usd numeric(28,8) · fee_usd numeric(28,8) · source enum(api,invoice) | uq(day,market_id,fee_side,rate_bps) | **manual/invoice import until a verified endpoint exists** `[UNVERIFIED: "publicly queryable" builder profiles [CTX §4] — I found no endpoint; do not build on this]` | A |
| `subscriptions` | id · user_id fk · plan enum(pro_monthly,pro_yearly,tg_stars) · provider enum(stripe,telegram_stars) · status · current_period_end · amount_cents int · currency | idx(user_id,status) | Stripe / Stars webhook | M |
| `referral_links` | id · user_id fk · code text uq · clicks int · converted int · bps_share smallint | uq(code) | ours | M |
| `usdc_flows` | id bigserial · wallet_id fk · token_id numeric(78,0) null · kind enum(buy,sell,split,merge,redeem,reward,conversion,transfer_in,transfer_out) · delta_usd numeric(28,8) · delta_shares numeric(28,8) · tx_hash char(66) · log_index int · block_number bigint · at | idx(wallet_id,at), idx(tx_hash,log_index) | chain (reconciliation truth) | A |

**Mutable:** `events`,`markets`,`wallets`,`positions`,`users`,`alerts`,`alert_subscriptions`,
`watchlists`,`copy_configs`,`automation_rules`,`subscriptions`(status),`referral_links`(counters),
`orders`(state — see below).
**Append-only:** `trades`,`fills`,`usdc_flows`,`order_events`,`builder_attribution`,`follows`,
`api_credentials`,`outcome_tokens`,`alerts` predicate history.
**`orders` is append-only in storage, mutable in *view*:** a state machine writes a new
`order_events` row and updates a denormalised `orders.state` only for read speed; the audit trail is
the event log. This matters because a support dispute about "why did my order not fire" is answered
from the log, and because rule 5 (fail closed) needs the pre-gate state to be immutable.

### D4.2 ClickHouse vs Postgres — where the line sits and why

ClickHouse gets: `trades` (all fills, all markets — measured 14.7/s sustained ⇒ **~38M rows/day**,
never in Postgres), tape-window materialised views per token, wallet×day notional aggregates,
classification tag tables, and `fills` analytics mirrors. Rationale: tape coalescing, radar
intersection, and D5 metrics are all `GROUP BY` over billions of rows; Postgres dies at the tap
frequency the UI needs (SKILLS.md: "the tape renders 20+ updates/sec").

Postgres keeps: anything that gates money (`orders`,`positions`,`copy_configs`,`usdc_flows`,
`subscriptions`) plus 7 days of `trades` for hot reconciliation. Rationale: these need transactions,
unique constraints, and `SELECT … FOR UPDATE`; ClickHouse offers none of that.

7-day Postgres retention is a **deliberate constraint on D5's D30 metric** — D30 cohorts must be
computed from ClickHouse, not from the hot DB. Say this to the P13 test author now.

### D4.3 Volume/discovery refresh budget (derived from the documented rate limits)

Gamma `/events` **500/10min** and `/markets` **300/10min** [CTX §2], with the row cap of 100 [E1].
To cover the top 500 events ⇒ 5 pages; refreshed every 60 s ⇒ 5 req/min = **50 req/10min (10% of
budget)**, plus 3 req/10min for a `/markets` sweep. Search and detail are user-driven and must be
**server-cached with a 20 s TTL** (the prototype's own refresh interval [CTX §11]). Consequence for
infra (P4): the API tier needs a shared cache — per-user polling of Gamma breaks the 500 limit with
~30 concurrent users. Monthly infra cost at build scale: **$0–45** (cache on the same box;
`[UNVERIFIED: exact instance price — quote at P4, must carry a monthly number per AGENTS rule 9]`).

### D4.4 The reconciliation problem (how we know local = on-chain)

Positions are ERC-1155 balances of `outcome_tokens` and pUSD is an ERC-20, both on Polygon. Our
ledger can drift for exactly four reasons, and each gets a detector:

1. **User acted outside us** (traded on polymarket.com, redeemed manually) → expected. Detector: the
   Data API `positions` snapshot (refresh per active wallet, ≤1/min) diffed against our ledger.
2. **Split/merge/redeem not yet ingested** → `usdc_flows` missing `kind ∈ {split,merge,redeem}`.
   Detector: block-log scanner behind tip by > 50 blocks.
3. **Our fill was partially matched / canceled** and we mis-recorded → Detector: CLOB user-trades
   feed (L2) vs `fills`, on every state change.
4. **Our signers moved funds** (compromise or admin error) → Detector: any `transfer_out` from a
   proxy wallet with no matching `orders` row ⇒ **page immediately, freeze the wallet's trading**.

Policy, not aspiration:

```
recon(wallet):
  for each token_id in positions(wallet):
     local  = Σ usdc_flows.delta_shares        (exact numeric, no floats)
     chain  = balanceOf(wallet, token_id)       (RPC)
     if local != chain: fail_closed(wallet)     # size mismatch is never tolerated
     if |local_usd_value - chain_usd_value| > $0.01: fail_closed(wallet)
  if block_lag > 50 or snapshot_age > 120s: fail_closed(wallet)
```

`fail_closed(wallet)` = trading disabled for that wallet, positions shown with a `⚠ unreconciled`
badge and the age of the last good check, support ticket opened, and the copy engine paused for
every follower of that wallet. Frequency: per active wallet every 60 s (cheap reads) + full sweep
every 30 min. `[UNVERIFIED: Polygon RPC provider, rate limits and the exact contract addresses for
pUSD/CTF on 137 — must be confirmed in P4; the shared context gives collateral "pUSD (ERC-20,
backed by USDC)" but no address.]`

### D4.5 Realised PnL on a negRisk multi-outcome market — thought through

This is the hard one, and the naive approach (per-market average cost, realised = sold − basis)
**produces numbers that are wrong in the direction that flatters the trader**, which is the worst
possible bug for a product that sells copy-trading.

Why it breaks. In a negRisk event (e.g. "Fed decision", `neg_risk: true` measured live [CTX §2]),
the USDC leg is shared across every outcome. The adapter lets you `split` $X into (Yes₁…Yesₙ, NO₁…NOₙ)
and `merge` back; and when one outcome resolves Yes, every **NO** becomes redeemable. So a wallet can
have positive realised cash with no "sale" in any single market, or a sale that is economically a
rebalance. Measured proof of the ambiguity: in one real wallet's 300-row `activity` feed I saw
**151 `TRADE` and 149 `REDEEM` rows**, and `REDEEM` rows carry `price: 0` and `usdcSize: 0` while
`size: 13.16` — i.e. the payout of a redemption is **not in that row**. Any PnL computed from
`price × size` silently prices redemptions at zero and prints a phantom loss.

**The specification we build:**

1. **Account at the event level, in cash, not at the market level in tokens.** For negRisk events
   `pnl_usd(event) = Σ inflows − Σ outflows` over the whole event, where the only USDC-bearing kinds
   are `buy(neg) sell(pos) redeem(pos) merge(pos) split(neg) reward(pos) transfer(±)`. `split`/`merge`
   are **zero cash flow but must still be recorded** because they change basis allocation.
2. **Cost basis follows the token, FIFO, per `token_id`.** Not weighted-average: with 5-minute
   markets producing many tiny fills, average-cost spreads basis across time buckets and makes
   realised PnL depend on the fill order of an hour we didn't mean to mix in.
3. **A position is "realised" only at event granularity:** all `outcome_tokens` of that event at
   zero *and* the last related block processed. Partial redemption is not partial realisation.
4. **Unrealised mark for negRisk = `Σ sharesᵢ × (1 − midᵢ)` for NO legs, `sharesᵢ × midᵢ` for YES**,
   but *capped by the event rule*: since exactly one YES pays, `Σ pᵢ > 1` is arbitrage-priced and we
   must show the sum with a `book-overround` tag rather than pretend each leg marks independently.
   (Gamma gave `bestBid 0.5 / bestAsk 0.51` on a live 5-min market — the 1 ¢ ask-bid chain is the
   number users trade against, not the midpoint.)
5. **Reconcile against the only ground truth available:** `GET data /value?user=` (measured 200,
   returned `{"user":…,"value":1093.7245}`) and `GET /traded?user=` (200, `{"traded":3381}`). Our
   computed portfolio value must agree with `/value` within $1 or the profile page shows
   `pnl: unverified` instead of a number. Never show a computed number as authoritative when it
   disagrees with the platform's own aggregate.
6. **`/rank` and `/pnl` don't exist (E4),** so leaderboard-style "profit" must be labelled
   **our estimate**, with the method and as-of time on the same screen. [CTX §10: no
   "guaranteed returns" language; drawdown shown next to PnL.]

Open item I am not going to paper over: the **exact redemption payout for negRisk NO legs** (does
one YES resolution pay all NOs at $1.00 less the loser's YES?) needs reading the adapter contract,
not the API. `[UNVERIFIED — blocks the PnL engine's correctness claim; assign in P4 with a test
against one real resolved negRisk event.]`

---

## D5. Metrics that decide whether this is working

Definitions are implementation-ready. "Active" = ≥1 authenticated request to our API in the window
(web session or bot message). Volume is *routed through our builder code only*.

| Metric | Formula | Wk-4 gate | Wk-12 gate | Fail ⇒ |
|---|---|---|---|---|
| **Activation** | users with ≥1 routed fill ÷ users who connected a wallet, 7-day cohort | ≥18% | ≥28% | the alert→trade ladder is broken; move CTA from post-click to in-message inline button and re-run |
| **Routed volume / active user** | Σ matched notional ÷ active users (monthly) | ≥ $400 | ≥ $1,500 | we are a toy; add limit-order + TP/SL surface (the P0 OMS rows) before growing the funnel |
| **D1 / D7 / D30 retention** | cohort returned ≥1 session in window ÷ cohort size (from **ClickHouse**, per D4.2) | 35 / 20 / — | 40 / 25 / 14 | at D7 <15% kill the notification cadence and rebuild around watchlists |
| **Alert → trade conversion** | fills originating from an alert tap ÷ delivered alerts | ≥6% | ≥12% | threshold too low ⇒ noise; raise whale floor from $2k toward $10k and re-measure |
| **Free → Pro conversion** | paying ÷ ever-active, rolling 30d | ≥1.5% | ≥3% | price too high or Pro has no exclusive capability — give the Radar + copy engine to Pro and nothing else |
| **Attributed-volume share of platform** | our routed ÷ measured platform volume (top-100 events **$52.4M/24h** tonight) | ≥0.05% | ≥0.15% | distribution, not product: the bot is not being added |
| **Revenue per 1,000 routed dollars** | `(builder_bps/10000 × 1000)` + (pro_mrr ÷ routed_1k_units) | **$0.00 + $0.15** at launch 0 bps | **$2.50 + $0.60** at 25 bps | the take rate is the whole business; see D6 |
| **Realised-slippage honesty check** | median (quoted − filled) in ¢ on our-originated fills | ≤ 1.5¢ | ≤ 1.0¢ | stop routing market orders; force limit+IOC and show the book |
| **Alert latency** | fill timestamp → push delivered, p95 | ≤ 2.0 s | ≤ 1.2 s | the E2 finding means our whole edge is latency: if we can't beat cached pollers we have no wedge |
| **Reconciliation failures** | wallet-days with `fail_closed` ÷ wallet-days | ≤0.5% | ≤0.2% | halt all copy-trading; P6 feature-flag off |

Two numbers I want on the dashboard from day 1 because they are the only ones that can't be
flattered by engagement: **median fill notional of our users vs the platform $5.00 baseline** (if we
aren't moving users up-market we're noise) and **revenue per 1,000 routed dollars** (it says, in
dollars, whether the Builder Program can carry us at all — per [CTX §4] it can be switched off at
Polymarket's sole discretion).

---

## D6. Risk-adjusted revenue model

Inputs that are not measurements are labelled. Platform run rate from [CTX §5]: **$8–10B/yr**, and
my own top-100 sample tonight is **$52.4M/24h** (≈$19.1B/yr annualised from a *capped* page — see
E1; I therefore use the kit's lower figure, not mine).

```
builder_fee  = notional × rate_bps / 10000                (additive to platform fees)
platform_fee = C × feeRate × p × (1 − p)                  (user pays this, not us)
Pro sub      = $15/mo   [assumption: bracket set by free–$30 range in CTX §6; our tier sits mid]
Grants     = front-loaded ($0 / $5k / $7.5k→$15k) — early-stage, NOT recurring; treated as
               revenue but modelled to expire, which is why bull breaches 60% earlier
Stars price  = 1111 Stars at $0.0135 [CTX §9] ⇒ gross $15.00; the Telegram Stars net after
               app-store + Fragment fees is LESS than $15 and [UNVERIFIED: Stars net-take
               economics — do not assume parity, quote before selling in-bot]
```

| | Bear Mo-3 | Bear Mo-6 | Bear Mo-12 | Base Mo-3 | Base Mo-6 | Base Mo-12 | Bull Mo-3 | Bull Mo-6 | Bull Mo-12 |
|---|---|---|---|---|---|---|---|---|---|
| routed $M/mo | 0.4 | 1 | 2 | 1 | 4 | 9 | 3 | 8 | 18 |
| builder bps | 0 | 10 | 10 | 0 | 15 | 25 | 25 | 50 | 100 |
| builder $/mo | 0 | 1,000 | 2,000 | 0 | 6,000 | 22,500 | 7,500 | 40,000 | 180,000 |
| Pro subs | 40 | 140 | 300 | 120 | 420 | 900 | 350 | 900 | 2000 |
| sub $/mo | 600 | 2,100 | 4,500 | 1,800 | 6,300 | 13,500 | 5,250 | 13,500 | 30,000 |
| grants $/mo | 0 | 0 | 0 | 0 | 5,000 | 5,000 | 7,500 | 8,000 | 15,000 |
| revenue $/mo | 600 | 3,100 | 6,500 | 1,800 | 17,300 | 41,000 | 20,250 | 61,500 | 225,000 |
| ARR (12× mo) | **7.2k** | **37.2k** | **78.0k** | **21.6k** | **207.6k** | **492.0k** | **243.0k** | **738.0k** | **2.70M** |
| builder share of rev | 0% | 32% | 31% | 0% | 35% | 55% | 37% | 65% ⚠ | 80% ⚠ |

Arithmetic, every column (recomputed by `tools/p01-gate-check.py` from the inputs in the table, so
these cannot silently drift again):
- **base Mo-12** = 9.0M × 25/10000 = $22,500 + 900 × $15 = $13,500 + $5,000 = $41,000/mo → **$492k ARR**
- **bear Mo-12** = 2.0M × 10/10000 = $2,000 + 300 × $15 = $4,500 + $0 = $6,500/mo → **$78k ARR**, which
  sits just inside [CTX §5]'s "$80k–$400k realistic year one" band (a hair under its floor, on purpose)
- **bull Mo-6** = 8.0M × 50/10000 = $40,000 + 900 × $15 = $13,500 + $8,000 = $61,500/mo → **$738k ARR**

*Correction note:* an earlier draft of this table had the grant months misaligned against the totals
they were summed into, which inflated base Mo-6/12 by $114k and $30k ARR and mis-stated three
builder-share figures. The gate script caught it by re-adding every column; that is why it is
committed here rather than "checked by eye".

**⚠ = breaches the kit's own rule** "builder fees must never exceed ~60% of revenue" [CTX §5]:
**bull month-6 (65%)** and **bull month-12 (80%)**. Base stays compliant (35%, 55%) and bear is clean
(31-32%). Two things the corrected arithmetic makes explicit, which the first draft hid:

1. **The 60% rule is not the binding constraint at base — but it is fragile there.** At base month-12
   the maximum fee rate that still complies is **27.3 bps** (solving 9.0M × bps/1e4 ≤ 0.60 × total),
   so 25 bps fits with ~8% headroom. That headroom exists *only because $5k/mo of grants are in the
   denominator*. With grants at $0, base month-12 share is **63% — a breach**. So the honest reading
   is: we are compliant while grant money is in the mix, and the compliance argument has to be
   re-made on subscription growth before the grants expire. Required floor: **≥ 667 Pro subs by
   month 12 to hold 60% at 25 bps with zero grants** (we model 900, so it is achievable, not free).
2. **Bull is structurally disallowed, not merely greedy.** $180k/mo builder against $45k/mo
   everything else means one Polymarket decision ends the company, and the kit lists
   "self-referred or non-genuine trading activity" as grounds for exactly that [CTX §4]. The correct
   bull action is to *refuse* the 100 bps and buy diversification instead (DATA licensing, more Pro
   tiers) — i.e. a bull case that grows subs, not take rate.

Sanity on the ceiling. $1M ARR at 25 bps needs **$40M routed/month** = $480M/yr ≈ **4.4%–6.0% of the
entire platform's volume** [CTX §5: $8–10B/yr], i.e. permanently beating today's #2 builder
($38.8M/30d). That is why the kit's $1M is a year-2/3 target and why I will not write a year-1 model
that reaches it. Our base month-12 ($22.5k/mo builder) sits around **top-15-builder scale** (median
top-50 builder lifetime $4.7M [CTX §5]) — credible without heroics.

**The single assumption that, wrong by 2×, breaks the model:** *the bps we are actually able to
collect.* [CTX §4] fixes the cap at 100 taker / 50 maker, default 0, one change per 7 days with 3 days'
notice, accrues only on matched orders, and is revocable at Polymarket's sole discretion. If base
collects 12.5 bps instead of 25 (a plausible 2× miss, since we must underprice a 0-bps incumbent
[CTX §4]): $492k → **$357k ARR** (builder $11.25k + subs $13.5k + grants $5k = $29.75k/mo), a 27% cut.
Bear at half its 10 bps is 2.0M × 5/10000 = $1,000 + $4,500 = $5,500/mo = **$66k ARR**, under [CTX §5]'s
$80k floor — the model breaks at the *floor*, not through zero, and subscriptions are what stops it
falling further. Volume cannot rescue it: at 12.5 bps, base would need $18M routed/month to hold
$492k, which is top-2-builder territory and not a lever we control. The only 2×-robust lever is
**subscribed users** ($13.5k/mo at month 12 = 33% of base revenue from a source nobody can revoke).
Design consequence carried back into D3: the Wallet Radar, the trader profile with drawdown, and the
copy-trading caps are the paid product. Alerts stay free forever.

---

## Quality gate — answering the five questions without asking me anything

| Question | Answer |
|---|---|
| **What are we building?** | A Polymarket trading terminal: live WebSocket tape with wallet-type classification, an order book/depth view that shows freshness and refuses to quote across wide spreads, whale/sniper/dump-team alerts in Telegram, and a copy-trading engine with hard caps and per-wallet drawdown. 20 P0/P1 features in D3, each with a live-verified endpoint. |
| **For whom?** | Crypto-native retail traders on mid-range Android who already live in Telegram (India/SEA/LatAm/CIS), currently using 0-bps inline-button bots [CTX §1,§6]. Specifically: the trader who today sees a $5 median fill and wants to know which of the 14.7 fills/second was worth copying. |
| **Why will they switch?** | Because the one thing the incumbents structurally cannot do inside a button menu is show *live* tape with *who* behind it and *what it will really cost me to follow*. E2 is our window: cached-poller bots cannot beat a real WS subscription on freshness, and alert latency ≤1.2 s is a measurable, advertised number. Plus: they don't pay us more — we launch at 0 bps, and we show slippage that they hide. |
| **How much will it cost?** | <$10k build envelope per AGENTS rule 9. Infra runs $0–45/mo at P01 scope (cache on-box, [UNVERIFIED: instance quotes due P4]); the API budget maths in D4.3 fits the documented 500/300 req per 10 min with headroom. Money-path work (P6/P7) is the expensive part: ~6–8 eng-weeks for tape+alerts, then 4–6 for the risk gate. |
| **How will we know it's failing?** | D5's ten gates with week-4 and week-12 numbers and a stated action per failure, plus D1's 30-day kill criterion (two of four ⇒ re-run P1). The one to watch first: **alert latency p95 > 2 s at wk-4** — if we're not the fastest surface, we're not a wedge, we're a clone. |

**Deliverable honesty summary.** Verified tonight with live calls: all cited endpoints and their
field names, the 100-row Gamma cap, the Cloudflare caching of `/trades`, 14.7 fills/s, median fill
$5.00, 1.2% ≥$1k, the live negRisk/Fed market parameters, one 5-min book's spread and depth, the
`/profit` `/volume` window matrix, and the absence of `/pnl` `/profile` and a working `/rank`.
Cited-not-reverified: everything attributed to [CTX] (fees, builder leaderboard, market size, brand
tokens, Telegram rules, competitor pricing). **`[UNVERIFIED]` and blocking:** Polygon RPC + pUSD/CTF
addresses; negRisk redemption payout exactness; deep-history `activity` pagination; ≥$2k/$5k bucket
counts; whether Betmoar/PolyCop already display slippage; all six competitors' onboarding step
counts; Kalshi API; builder-profile query endpoint; Stars net-take economics; infra instance prices.
No revenue number above is presented without its arithmetic in D6.
