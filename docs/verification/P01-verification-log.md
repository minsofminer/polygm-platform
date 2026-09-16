# P01 verification log

*Run: 2026-09-16 16:25–16:31 UTC, from this workspace. All probes read-only GETs against public
endpoints. Reproduce with `python3 tools/datasource-probe.py`; the machine-readable result is
[`P01-probe.json`](P01-probe.json) and the console transcript
[`P01-probe-output.txt`](P01-probe-output.txt).*

## Method

1. Clone `minsofminer/polygm`, read `README.md`, `AGENTS.md`, `00-SHARED-CONTEXT.md`, `SKILLS.md`,
   `prompts/P00-README.md`, `prompts/P01-research.md`.
2. Before writing any spec claim, call the endpoint that claim depends on. `tools/datasource-probe.py`
   encodes each claim as an assertion (status + field presence + header value), so the doc and the
   evidence cannot drift apart.
3. Write the spec. Run `tools/p01-gate-check.py`, which recomputes D6's arithmetic from the table and
   verifies cited API facts against the stored probe log.
4. Mutation-test the gate itself (below) — a check that cannot fail is not a check.

No mutating call was made to any Polymarket endpoint; no L2/authed endpoint was used; no credentials
appear in any file in this directory.

## Raw result

`26/26` endpoint checks passed on the final run. Findings that changed the spec:

| ID | Assertion | Result |
|---|---|---|
| E1 | `GET gamma /events?limit=200` and `?limit=500` | **100 rows** both times; default **20**; `offset=` works (3 pages → 300 events, **$59,199,253.96** 24h volume) |
| E2 | `GET data-api /trades?limit=500` twice, 8 s apart | `cf-cache-status: HIT`; newest trade timestamp **identical**; a `?_=<ts>` cache-buster returned a newer row once and not the next time ⇒ cache behaviour is not dependable |
| E2 | fills/sec from one 500-row window | 14.7 / 26.3 / 33.3 across three pulls ⇒ quote a range, never one number |
| E3 | up/down share of top-100 **markets** by 24h volume | 0 of 100 on two pulls, 1 of 100 on one (`btc-updown-5m-1789575300`, $64,842, rank #91) ⇒ **≤0.14% of volume**, corrected from my first draft's "0 of 100" |
| E3 | up/down in volume-ordered `/events` (300 rows) | **0 of 300**; their event-level `volume24hr` is `null`/`0` |
| E4 | `lb-api /volume` `/profit` × `{1d,7d,30d,all}` | 8/8 return rows with `amount`; `/pnl` → **404**; `/rank` → **400 `required query param 'rank' not provided`** even with `rank=volume&address=…` |
| E5 | `data-api /trades` field set | includes `name, pseudonym, bio, profileImage, title, eventSlug, icon, transactionHash` ⇒ no profile join needed |
| E5 | `/positions /activity /value /traded` | **400** naming the missing param without it; **200** with `user=` (and `market=` for `/holders`) |
| E5 | `/profile?address=` | **404** — does not exist |
| — | `clob /markets/{conditionId}` | `minimum_order_size: 5`, `minimum_tick_size: 0.001`, `neg_risk: true`, `accepting_orders: true`, `seconds_delay: 0` (matches the kit's Fed example) |
| — | `clob /data/trades` unauthenticated | **401** ⇒ the L2 boundary is real |
| — | `clob /book` on one 5-min market | 62 bids / 37 asks, `tick_size 0.01`, largest single ask level $10,508, ask-side notional $33,342 |
| — | empty-side book on a 0.001-tick market | best bid `None` vs best ask `0.999` ⇒ **46% of mid** spread; the basis for the ≤3¢ follow guard |
| — | `gamma /markets` `feeType` | `crypto_fees_v2, culture_fees, economics_fees, finance_prices_fees, politics_fees, sports_fees_v2` ⇒ fees readable per market |
| — | REDEEM rows (366 across 12 live wallets) | `price == 0` on **366/366**; `usdcSize > 0` on **280/366**; `usdcSize == size` on **all 280** ⇒ redemption pays $1/share; the 86 zeros are unredeemed/zero-payout rows, not missing data |
| — | `traded` / `value` for one wallet | `{"traded": 3381}`, `{"value": 1093.7245}` ⇒ free portfolio-value ground truth for reconciliation |

## Things I got wrong first, and what caught them

Recording these because the alternative is a document that looks like it was right all along.

| My first-draft claim | Reality | Caught by |
|---|---|---|
| "0 of the top 100 **markets** are up/down" | one pull showed 1 of 100 (rank #91) | the probe, on its first run — `E3` asserted `len(ud) == 0` and **failed** |
| "median fill **$5.00**" as a single number | 5.00–6.27 depending on the pull | added `C8`, which now requires a **range** in the prose and checks it against the log |
| D6 base Mo-6/Mo-12 ARR ($147.6k / $480k) | $207.6k / $492k — grants were summed into the wrong months | `C4`, which re-adds every column |
| "bear case breaches the 60% builder rule at 62%" | bear is 31–32%; the real breaches are bull month-6/12 | re-deriving the share row from the corrected table |
| "`revenue $/mo` row: base Mo-3 = 6,800" | 1,800 (0 + 1,800 + 0) | `C4` after I added a revenue-row assertion |
| "0.0001 ETH…" style endpoint claims from the kit (20.8 fills/s, `?tag=crypto`, 1 ETH lifetime) | tape is 14.7–33.3/s; the param is `tag_slug`; `polymarketanalytics.com` → 429 bot-gate (the 1 ETH figure is the kit's claim, left as cited-not-verified) | probing instead of transcribing |

## Gate self-test (mutation suite)

A check that cannot fail is decoration. Each mutation was applied to a copy of the spec; the gate had
to exit 1 with the *named* failure, and the control had to exit 0. **9/9 caught.**

| Mutation | Gate reaction |
|---|---|
| corrupt one ARR figure | C4 — "ARR not 12× revenue; 9,900 != 7,200" |
| delete both `⚠` breach flags | C5 — "over-60 cols: [7, 8], flagged: []" |
| add an unflagged 61% share | C5 — same, with the new column |
| rename `outcome_tokens` everywhere | C3 — "missing: ['outcome_tokens']" |
| assert `cf-cache-status: MISS` (contradicting the probe) | C9 — cache-claim mismatch |
| cite a `lottery_fees` fee type | C9 — "cited-but-unobserved: ['lottery_fees']" |
| claim a 250-row Gamma cap | C9 — "probe=100, spec claims [250]" |
| strip a P0 feature's endpoint and write "probably fine" | C6 — hedged language; C2 — no data-source host |
| remove one `[UNVERIFIED …]` marker | C6 — "unflagged: ['Polygon RPC']" |

Three earlier "not caught" results were **my test's** bugs (a no-op string replacement, a rename that
left the token name in other rows, and a check that accepted either HIT or MISS). Fixed, then re-run.

## What remains unverified after P01

Blocking, in priority order, each with the cheapest way to close it:

1. **WebSocket subscription semantics** — host resolves (`104.18.34.219` / `172.64.153.51`, Cloudflare),
   `book`/`price_change`/`last_trade_price`/`tick_size_change` message shapes **not yet observed**.
   Close by subscribing for 60 s in P5; it is the load-bearing assumption of the whole wedge.
2. **negRisk redemption exactness** — need the adapter contract, not an API. Test against one real
   resolved negRisk event.
3. **Polygon RPC + pUSD/CTF contract addresses on 137** — needed for D4.4 reconciliation.
4. **Deep history** — whether `/activity` paginates to a wallet's first trade (needed for the
   "Fresh wallet" tag and true lifetime PnL).
5. **Competitor onboarding step counts** — 4/6 sites are JS-rendered to curl, 1 returns a Vercel
   checkpoint. Needs a human with a wallet, ~20 min, screen recording.
6. **Kalshi API** — never probed; blocks any cross-venue claim.
7. **Builder profile queries** — the kit says profiles/rates are "publicly queryable"; I found no
   endpoint. `builder_attribution` is therefore imported, not scraped.
8. **Telegram Stars net take** — determines whether the in-bot price can match the web price.
9. **Infra instance prices** — every recommendation in D3/D4 needs a monthly number (AGENTS rule 9).
10. **Whale threshold buckets** — 24 h × 20 k fills to pick $2k vs $5k vs $10k from a distribution
    instead of a 500-row guess.
