# P04 — Backend architecture and scaffold

Product: **Openout** (repo `polygm-platform`, package `polygm_core`). This is the P04 deliverable: D1–D7 of
`docs/prompts/P04-backend-architecture.md`, plus D8 (what the phase actually broke) and the verification
ledger. Every number below was produced by running the command next to it in this workspace; anything that
could not be run here is marked **[UNVERIFIED]** and named in D9.

Read this sentence first, because it is the one a reviewer will want to skip: **this document is not the
deliverable — `make gate` is.** The doc explains the choices; `tools/p04-gate-check.py` fails (exit 1) if any
of them rot (55 checks, all green as of the last run), and `tools/p04-mutation-test.py` fails if the gate stops
being able to *see* a break — 24 mutants, each a bug this phase actually had or the exact inverse of a rule it
depends on. The doc will drift; the gate will not be quiet about it.

---

## D1. Service topology

Eight services, six of which exist as real containers or real processes today. The "in P04" column is a claim
about this repo; the rest is a claim about the design.

| service | language | owns in the DB | must never touch | scales by | if it dies | in P04 |
|---|---|---|---|---|---|---|
| `api` | Python 3.13, FastAPI | reads everything; writes `order_intents`, `idempotency_keys`, `audit_log`, `flag_audit`, `kill_switch_state` | a private key, a signer, an outbound trading socket | replicas behind the load balancer (stateless; all state is in Postgres/Redis) | users see 5xx on reads and orders; positions and money are unaffected (no key here, so nothing to lose) | **built** |
| `ingest` | Python | writes `markets`, `events`, `event_markets`, `books`/`book_levels`, `tape_trades`, `users`/`user_stats` rollups in P05 | user data, keys | one process per venue feed partition (markets sharded by `event_id`) | reads go stale → `stale_ms_*` trips → the gate denies new orders; existing orders untouched | designed; **P05** |
| `signals` | Python | reads `tape_trades`, writes `alerts` | writes to money tables | horizontally by alert-rule shard | no new notifications; tape still displayed | designed; **P09** |
| `executor` | Python, `py-clob-client-v2` | writes `orders`, `fills`, `builder_attribution`, `position_snapshots` | inbound connections (none bound), HTTP serving, the browser | one process per signer; concurrency bounded by per-signer budget | orders already sent are reconciled by the recovery path (D5 step 7); new orders queue until it returns | designed; **P06** (mock built) |
| `risk` | in-process library (`polygm_core.risk.gate`) | reads `cash_ledger`, `orders`, `flags`; writes nothing | the network | it is not a service: it is a function call with a <50 ms budget | no orders are placed at all — that is the correct failure mode, and the reason the gate is a call and not a hop | **built** (as a library, see D1.1) |
| `billing` | Python | writes `entitlements`, `stripe_events`, `stars_events` | trading keys | replicas; idempotent on webhook `event.id` | free tier keeps working; upgrades queue | designed; **P12** |
| `notifier` | Python | reads `alerts`, writes `deliveries` | money tables | replicas; Telegram is per-bot-rate limited | delayed notifications, no data loss (outbox rows remain) | designed; **P10** |
| `worker` | Python | writes `position_snapshots`, leaderboard recompute, backfills | signing | cron-style, one leader via a Postgres advisory lock | staleness grows; `readyz` reflects it | designed; **P11** |

### D1.1 The one boundary rule, and why `risk` is not a network hop

Rule 1 of the prompt: the trading service holds keys and has no inbound internet exposure; rule 3: every order
path goes through the risk gate. Both are satisfied by *removing a failure mode*, not by adding a check:

* `api` has no wallet module at all. `grep -R "private\|signer\|keystore" services/api/` finds prose, not
  imports — the API cannot sign, so it cannot leak a signature or a key through a log line, an error body or a
  core dump containing an unused dependency.
* The gate is a synchronous function call **inside** `api` (before the queue), not a service behind a socket.
  A hop would add latency budget pressure (the prompt's <50 ms), a second place for the kill switch to be
  stale, and a way to bypass it — any caller that forgets the RPC gets a trading path. A function that sits in
  the same module as the ledger cannot be skipped by forgetting an import. The executor in P06 will re-evaluate
  the same pure function against its own view before signing; the gate is called twice by design, and
  `polygm_core.risk.gate.evaluate` is pure so both calls agree.

**Cache as a boundary (rule 2).** `api` never calls a venue endpoint on a user's behalf. Everything a page
needs comes from tables `ingest` fills; the per-user rate limit classes live in `Flags.rate_class`
(`("trader", 12, 4, 120)`: burst 12, 4/s sustained, 120/min). One user hammering `/v1/markets` therefore
costs us Postgres index reads, not Polymarket budget. The measured budgets this protects: Gamma 300 req/10 s,
balance-allowance 200 req/10 s **across all users**.

---

## D2. Tech stack — chosen and defended

**Chosen: Python everywhere (FastAPI + `polygm_core` + `py-clob-client-v2`), TypeScript confined to the
browser, Postgres 17 as the only store with state, Redis for cache/queue, Redis Streams for the intent queue.**

**Rejected: TypeScript backend (NestJS/Fastify + `@polymarket/clob-client-v2`).** It is the respectable choice
and it loses on one measured fact and one social fact:

* The measured fact. I installed both SDKs and read their order builders (D8.1). Both are at **1.1.0** — the
  prompt's premise that only the TS client had caught up to V2 is stale, so "pick TS because the Python SDK is
  behind" is false today. What *is* true of both: the order args are `float`. Python's Decimal and integer
  micro-units let us keep the money path exact and confine that float to one function with a round-trip
  assertion; in TS the same care is possible but every boundary is `number` by default and the failure is
  silent. `int(price / 0.01) != round(price / 0.01)` for **6/99** on-tick prices (0.001 tick: **126/999**),
  and at size `1.000001` shares the floored notional disagrees for **499,999 in 999,999** cases — that
  is not a rounding nit, it is the difference between an order and a rejection, and it is the exact bug class
  the prompt's "no floats in the money path" rule exists to prevent.
* The social fact (the prompt's constraint: debuggable by a non-expert, 1–2 contractors, <$10k). One language
  means one test runner, one profiler, one lockfile, and no class of "the TS types said it was fine" bugs. A
  split stack doubles the number of ways a 3am incident can be a serialization problem.

**Store: Postgres, no ClickHouse, no Timescale.** The tape arrives at a measured **14.7–33.3 fills/s**
(0.5 s × 200 messages, `tools/datasource-probe.py`); that is ~2.6 M rows/day, which a plain Postgres table
with `(market_id, ts_ms DESC)` handles, and we keep one backup system, one query language and one thing for
contractors to know. ClickHouse buys an order of magnitude we do not need at the cost of a second source of
truth for money-adjacent rows. **Decision to revisit when a query is actually slow**, and the schema keeps
`tape_trades` append-only and time-partition-friendly so the move is a `CREATE TABLE ... PARTITION OF` or a
copy, not a redesign.

**Queue: Redis Streams, not NATS, not SQS.** The queue needs: at-least-once delivery, consumer groups, a
short retention, and nothing else. We already run Redis for cache, so Streams costs one service we are already
operating and one thing to debug; NATS adds a second message broker to learn for a topic count under ten; SQS
adds per-message cost, an IAM surface, and — decisive for rule 1 — a queue that the API can *also* write to
directly, which is a bypass. A queue cannot be the enforcement point here, so it is not the enforcement point:
the gate is.

**Hosting: Hetzner for stateful, Railway for the edges.** `docker compose up` in this repo is the same image set
the production boxes run, so "works on my machine" and "works in prod" are the same claim. Indicative monthly
(small-business tier, prices as of 2026-09, **[UNVERIFIED]** — I cannot price a cloud from this sandbox):
Hetzner CPX31 (8 vCPU/16 GB, DB+Redis) ≈ €27, a second CPX21 for ingest/executor ≈ €14, Railway api/notifier
≈ $20, managed Postgres fallback ≈ $15 → **under $100/month**, two orders of magnitude under the $10k budget,
leaving room for the bandwidth a WS-heavy product actually costs.

**Versions, pinned from measurement** (`make doctor`, `requirements.txt`): python 3.13.14, fastapi 0.141.1,
starlette 1.6.0, pydantic 2.13.4, uvicorn 0.53.0, PyYAML 6.0.3, httpx 0.28.1, stdlib `sqlite3` 3.46.1,
`asyncpg` 0.30.0, `redis` 5.2.1, `py-clob-client-v2==1.1.0`. **`py-clob-client` (V1) is deliberately absent** —
CLOB V1 is dead, and a dependency on it is a way for a dead venue to come back through `pip install`.

---

## D3. Database schema — real SQL, and the three questions

`db/migrations/*.sql` is the source of truth (five files, Postgres dialect); `db/migrations-sqlite/*.sql` is
**generated** from it by `tools/build-sqlite-migrations.py`, never edited — `--check` runs in the gate and
fails on drift, because a hand-diverged dev dialect is how a phase starts passing tests against a schema
production does not have.

Money path (the three-stage model the prompt asks for):

```
order_intents   requested: what the user asked for + the gate's verdict   (state: queued|rejected|...)
    -> orders   submitted: what we actually sent, with the venue order id (state: live|matched|killed|uncertain)
        -> fills   matched: exchange truth, one row per venue fill, keyed by (tx_hash, log_index)
            -> cash_ledger   append-only balance deltas, one row per effect, UNIQUE(ref_table, ref_id, kind, user_id)
```

`builder_attribution` is written next to every `orders` row with our builder code and `fee_bps`, so revenue is
reconcilable against on-chain events without trusting our own accounting — and `position_snapshots` is the
comparison against chain truth that makes a lost `fills` row visible as a diff rather than as an argument.

Types: every money and price column is `BIGINT` in micro-units (`*_micro`), **never** `NUMERIC`, `REAL` or
`FLOAT` — the gate checks the migrations for it (`no float/NUMERIC money column`, and the corollary that the
only three `NUMERIC` columns are the metadata ones `minimum_tick_size`, `minimum_order_size`,
`risk_latency_ms`, where a decimal is the venue's own unit, not our money).

Append-only is a **database** rule, not a code-review rule (rule 4): `0005_triggers.sql` installs
`BEFORE UPDATE OR DELETE` triggers on `cash_ledger`, `fills`, `tape_trades`, `builder_attribution`,
`position_snapshots`, `flag_audit`, `kill_switch_state`, `audit_log` that raise, and revokes `UPDATE`/`DELETE`
from the app role for the same set. Corrections are new rows with a `ref_` back-pointer — the ledger's
`kind` column is exactly this: a refund is `kind='refund'`, not an edited deposit. `kill_switch_state.reason`
also carries a `CHECK (length(reason) >= 20)`, because "kill switch was flipped" with no sentence attached is
the kind of row nobody can act on at 3am; the constraint lives in the DB so no code path can skip it.

Indexes with a reason (the rest are ordinary key lookups):

* `idempotency_keys UNIQUE (user_id, idempotency_key)` — the uniqueness that makes "same key, same answer"
  a database property instead of a race. A `replay` and a `mismatch` are then decided by one INSERT's
  `RETURNING`/`xmax`.
* `cash_ledger UNIQUE (ref_table, ref_id, kind, user_id)` — a retried ingest of the same venue event cannot
  double-credit a balance. This is the only thing standing between a WebSocket replay and a wrong number.
* `orders (user_id, state, updated_ms DESC)` — "my open orders" is the hottest read in the product; a scan
  over all history is how a UI freezes during a busy market.
* `fills (market_id, ts_ms DESC)` and `tape_trades (market_id, ts_ms DESC)` — the tape panel asks for the last
  N of one market, always.
* **deliberately no index on `book_levels.updated_ms`.** The book is a cache with a TTL and a full replace on
  every update; an index led by a column that changes on every write turns each refresh into an index churn,
  and the only query that wants it ("how stale is this book?") is answered by the row we just read. 25 index
  statements exist in the migrations; the gate asserts none of them leads with that column.
* `events (end_ts, id)` with `COALESCE(end_ts, 2**63-1)` — "ends soon" is a product-defining sort, and markets
  without an end date must sort last rather than first (a NULL-optional date is `NULL` more often than it is a
  bug, and Postgres/SQLite disagree about where NULL sorts).

### D3.1 Realised PnL on a negRisk multi-outcome market

The trap: in a negRisk event (`0xM5` in the seed: one event, six candidates, `0xEV1`) a user can hold YES on
three mutually exclusive outcomes and **merge** them. Merging n YES tokens converts them into n NO tokens plus
collateral, so a naive "average entry price" over `fills` reports a position that no longer exists at that
price, and a naive mark-to-market double-counts the collateral that came back out.

The rule we implement (P06, and it is a pure function so it can be tested without a venue):

1. PnL is computed **per token id, per user, over the ledger**, not per market. A merge is two ledger events:
   `REMOVE` n×YES at cost basis, plus `MERGE_COLLATERAL` n×$1.00. Both are rows; neither is a mutation.
2. `realised = Σ proceeds − Σ cost`, and a merge's proceeds are the collateral returned — not zero. Treating
   a merge as a non-event is the bug that makes the number lie.
3. Resolution is a special case of the same formula: at `price == 0` a token redeems for **exactly its size in
   USDC if it won and 0 if it lost** — measured: `REDEEM` events have `price == 0` in **287/287** rows and
   `usdcSize == size` in **116/125**, so the 9 exceptions are where `usdcSize` is the *fee-adjusted* payout;
   we therefore take `size` (shares) as authoritative and derive `usdc = size − fee` rather than trusting
   `usdcSize`.
4. Unrealised PnL uses mid at `asOf`, and the UI must show the freshness marker: `realised` is a ledger fact,
   `unrealised` is an opinion about a price.

### D3.2 Source of truth for a balance

`GET /data/balance-allowance` is authoritative **about the chain** and rate-limited to 200 requests/10 s across
*all our users*, so it cannot be the read path and it must not be the cache either. Our ledger is the read
path; the venue is the **reconciliation** source:

* on every fill (push, no polling), and
* on a 60 s timer per *active* wallet (a wallet with an open order or a fill in the last 5 min), and
* on startup per signer, and
* on demand from the executor's `pre-trade check`, which is the only place a wrong balance loses money
  immediately.

At 200 req/10 s the budget is 1,200 req/min; a 60 s per-active-wallet poll plus fill-driven checks keeps the
worst case under ~40 % of budget with the gate's own backoff absorbing bursts. If the venue disagrees with the
ledger, the ledger is **not** silently corrected: we write a `reconcile` row (append-only, so both numbers
survive) and a `STALE_QUOTE`-class flag can halt trading for that user. "Who is right" is a human question at
that point, and the two rows are the evidence.

### D3.3 Backfilling 30k markets under 300 req/10 s

Gamma returns at most 100 markets per page (measured; and it **ignores** `tag_slug`, which is why P01's filter
logic is client-side). 30,000 markets = 300 pages = 300 requests → at the cap that is a **10 s floor**, so
naive backfill fits — until it shares the budget with live polling. Design:

* backfill runs in the `worker` with its own token bucket (100 req/10 s), leaving 200 for ingest;
* pages are ordered by `ID` ascending (stable keyset, no `OFFSET`) and checkpointed: `markets(id, first_seen_ms)`;
* a 429 or 5xx backs off 500 ms ×2 → 30 s with ±20 % jitter (the numbers in D5, one table for every hop);
* the backfill is *idempotent by upsert*, so a crash mid-run is a restart, not a repair;
* history (price buckets) is not backfilled from Gamma at all — it comes from the CLOB `prices-history`
  endpoint with a per-market cooldown, because Gamma does not have it and pretending otherwise is how a
  backfill becomes an outage.

---

## D4. API contract — OpenAPI 3.1, and the doc that ships is the doc that runs

`contracts/openapi.yaml` is the human-written contract. **The served document is generated from the routes and
then corrected** by `app._rewrite_openapi`, which (a) rewrites every FastAPI-emitted `422` — whose body schema
echoes the client's own `input` — to the error envelope, (b) injects `Error` and `Price` so no `$ref` dangles,
and (c) drops the `HTTPValidationError`/`ValidationError` schemas that generated clients would otherwise turn
into types. A dangling `$ref` from `openapi_extra` was a real defect here: it only bites whoever generates a
client, which is precisely the person we cannot afford to mislead, so `tools/check-openapi.py` resolves every
`$ref` in both documents, compares every schema the two documents share on `type`/`pattern`/`format`, and
fails if either direction drifts (82 checks, 0 failures; `--self-test` proves 13 canaries fire).

Implemented in P04, and each one is both tested and declared:

| path | auth | notes | rate class | cacheability |
|---|---|---|---|---|
| `GET /healthz` | public | process is up | none | not cacheable |
| `GET /readyz` | public | DB reachable **and** flags not from boot defaults; refreshes the store so a pod recovers without a restart | none | not cacheable |
| `GET /v1/markets` | public | `endsSoon` / `spread` / `newMarket` cursors, `asOf`/`staleAfter`, `pageSizeHardCap: 100` | ip | `max-age=1, stale-while-revalidate=30` |
| `GET /v1/markets/{market_id}` | public | one market, metadata freshness | ip | same |
| `GET /v1/markets/{market_id}/book` | public | top-of-book + depth, `spread_micro` | ip | `max-age=0, must-revalidate` (freshness is the product) |
| `GET /v1/tape` | public | last N fills, `le=500`, tape is WS-first and this is the fallback | ip | as above |
| `POST /v1/orders` | user | **`202`**, `Idempotency-Key` required, risk headers on accept *and* deny | user | not cacheable |
| `GET /v1/orders/intents/{intent_id}` | user | the poll the 202 points at | user | no-store |
| `POST /v1/admin/kill-switch` | admin | append-only state change, `reason` ≥ 20 chars, `x-idempotency-exempt` with the exemption written next to it | admin | no-store |

Planned (declared in `docs/P01-product-spec.md` and the endpoint map below, implemented in P05–P13): auth,
events, books-by-token, traders, leaderboard, radar, alerts, watchlists, portfolio, copy-configs,
automation-rules, wallet, billing, referrals, admin. The rule that keeps this honest as the surface grows:
**every `/v1/` route declares a `responses=` table, and `tools/check-openapi.py` compares the *exact* status
set of the served document against the contract, and compares the two documents' shared schemas field by
field** — so "planned" cannot masquerade as "shipped", because an
undeclared route fails the audit rather than quietly not existing.

Contract rules that are enforced, not merely written down:

* **Error envelope**: `{"error": {"code", "message", "retryable", "requestId"}}`, `additionalProperties: false`.
  `err()` accepts a `detail` argument for internal callers and **deliberately does not put it in the body**;
  it goes to the log. `VALIDATION` names `loc` paths only ("missing: price") — never a value, because a value
  is a token id, an address, or a pasted key. `INTERNAL` logs `type(exc).__name__` and nothing else.
  Tests assert `"detail" not in body` on every error class.
* **Cursor pagination everywhere, no offset** on the large tables: the cursor is `"<sort_key>|<id>"` and the
  WHERE clause is a keyset pair (`key > ? OR (key = ? AND id > ?)`), because `id` alone repeats rows when many
  markets share an `end_ts` — a bug this phase actually shipped and then fixed (D8.2).
* **`asOf` + `staleAfter` on every price/quote response**, and the client is told which one applies:
  a `STALE_QUOTE` denial carries `retry-after`, so "out of date" is an actionable answer rather than a shrug.
* **Write endpoints need `Idempotency-Key`** (8–128 chars, `[A-Za-z0-9_-]`). Missing → `400 IDEM_KEY_REQUIRED`.
  One exemption, declared in the spec with its reason (`state-convergent toggle; duplicate rows are audit
  history, not money`).
* Money on the wire is a **decimal string** (`"0.50"`, `"10"`). A JSON number in the money fields is
  `422 BAD_AMOUNT` — because the SDK types them as float, and accepting a number is how a float enters the
  money path through the front door.

---

## D5. The order path, sequenced

```
 1  client            POST /v1/orders {marketId, tokenId, side, price:"0.50", size:"10"}
                      Idempotency-Key: <8-128 [A-Za-z0-9_-]>
                      timeout 5 s, client retry allowed with the SAME key, never a new one
 2  api  middleware   x-request-id in, Server-Timing out; JSON access log with the id on every line
 3  api  _require_schema  no schema -> RuntimeError at import, "run 'make migrate'" (a pod must not boot
                      into an empty DB and answer 200s with nothing behind them)
 4  api  body shape    _check_body() BEFORE the key is claimed: a 422 must not burn a user's key
 5  api  idempotency   idem.begin(user,key) -> mine | replay | busy | mismatch
                      replay  -> the stored response, byte for byte, zero side effects
                      busy    -> 409 IDEM_IN_PROGRESS + Retry-After: 1 (a peer is mid-flight)
                      mismatch-> 409 IDEM_CONFLICT (same key, different intent)
 6  api  money         price_ticks()/parse_usdc(): strings only, scale <= 6, |x| < 2**53
 7  api  entitlements  market exists (else 404), user limits from FLAGS at request time
 8  api  RISK GATE     polygm_core.risk.gate.evaluate() — in-process, budget 50 ms, ordered:
                      kill_switch -> market_state -> freshness -> side -> tick_alignment -> min_size ->
                      notional -> price_band -> user_limits [-> delayed_market, advisory, never a denial]
                      every denial: notional_micro stays 0 (a refusal must not spend the 24h cap), and
                      x-risk-latency-ms + x-risk-checks are on the response for accepts AND denials, so a
                      support ticket says which check refused
 9  api  intent write  _upsert_intent(...) — UPDATE-then-INSERT (one key, one row; a bare INSERT 500s the
                      retry the idempotency layer promised); 202 {state:"queued", poll:"/v1/orders/intents/…"}
10  queue              Redis Streams `intents.v1`, XADD maxlen ~100k, consumer group `executors`
                      at-least-once; api does not wait on the executor (that is what makes 202 honest)
11 executor            loads wallet from the signer vault (never from env of any other service), re-runs the
                      SAME gate against its own view, checks tick alignment against minimum_tick_size and
                      minimum_order_size, builds the V2 struct (timestamp, metadata, builder code, fee bps),
                      converts money -> float ONLY via to_float_for_sdk (asserts the round trip, refuses beyond
                      2**53-2), signs, POST /order
                      timeout 3 s connect / 5 s read; 0 retries by default at this hop (see 12)
12 api/executor  codes OFF_TICK 422, UNKNOWN_TICK 503, STALE_QUOTE 503+Retry-After, BELOW_MIN_SIZE 422,
                      BAD_AMOUNT 422, OVER_ORDER_CAP/DAILY_CAP 403, MARKET_NOT_ACCEPTING 409,
                      RISK_HALT 503, SIGNER_UNAVAILABLE 503, IDEM_* 409, IDEM_KEY_REQUIRED 400, INTERNAL 500
13 reconciliation      WS fills are the fast path; the poll is the truth. On any uncertainty the order goes to
                      state='uncertain' and NEVER 'killed'
14 attribution         builder_attribution row (our code, fee_bps, venue order id) — written even on reject
15 notify              outbox row -> notifier (positions page also just reads the ledger, so a dead notifier
                      costs a notification, not a truth)
```

**Timeouts, retries, breakers — named (rule: every external call has all three).** One table, and `ingest`
(P05) and `executor` (P06) read it from `Flags`, not from their own constants:

| hop | connect | read | retries | backoff | circuit breaker |
|---|---|---|---|---|---|
| Gamma REST | 1 s | 3 s | 2 | 500 ms ×2 → 30 s, ±20 % jitter | open after 5 consecutive failures, half-open after 30 s |
| CLOB book/price | 1 s | 2 s | 1 | 500 ms | open after 3, half-open 10 s; while open, `readyz` reports it and the gate's `STALE_QUOTE` denies orders |
| CLOB order POST | 1 s | 5 s | **0** | n/a | n/a — a *submit* is not retryable, see below |
| order-by-hash lookup | 1 s | 2 s | 3 | 250 ms ×2 → 4 s | not tripped by this call |
| balance-allowance | 1 s | 3 s | 1 | 1 s | budget-aware: 40 % of the 200 req/10 s cap |
| CLOB WS tape | 5 s | 10 s idle | reconnect forever | 500 ms ×2 → 30 s ±20 % | while disconnected: **trading disabled**, `cancel-all` still allowed |
| risk gate (in-proc) | — | 50 ms budget | 0 | n/a | the gate's own failure mode is denial: an exception inside `evaluate` returns a deny, never a pass |

**Why the order POST has zero retries** is the same reason as the next paragraph: a duplicate submission is a
second order, which is money.

### D5.1 The case that loses money: the executor dies after signing, before the response

Nothing here is a guess about what "probably" happens; each branch has a named state and a recovery query.

1. The intent is durable **before** any network write (`order_intents` row, state `queued`). Signing happens
   in the executor, so a dead executor loses no state.
2. The executor writes `orders (state='submitting', client_order_hash)` **before** the POST. The hash is
   derived from `(user_id, idempotency_key)`, so it is stable across processes and restarts.
3. A restart scans `orders WHERE state='submitting' AND updated_ms < now - 15s` and asks the venue
   `GET /order-by-hash/{hash}` — this is exactly why V2's lookup-by-client-order-id exists: the recovery path
   does not have to guess a server-assigned id.
   * found, live/matched → adopt it: write `orders` state + `fills`, continue as if the POST returned. **No
     re-submit.**
   * found, cancelled → terminal `killed`.
   * **not found** → only then is re-submission legal, and it re-uses the same client order hash, so a
     re-submit that races a late venue response is still deduplicated by the venue.
4. If the lookup itself fails (venue 5xx, breaker open), the order becomes `state='uncertain'`, never `killed`,
   and the user sees: "we could not confirm whether this order reached the venue; it may fill", with a cancel
   button wired to `cancel-all` semantics. `uncertain` is a *terminal-ish* state: only reconciliation may move
   it, and a human sees it in the admin surface. Killing an uncertain order is how a system invents a fill it
   cannot see — the honest asymmetry is: never tell a user an order is dead when we only failed to look.
5. `fills` are keyed by `(tx_hash, log_index)` with `UNIQUE`, so a WS replay and a reconciliation poll
   describing the same venue event produce one row, not two.

---

## D6. The scaffold, as it exists on disk

```
packages/polygm_core/    money/cents.py risk/gate.py risk/idempotency.py ledger/ledger.py
                         config/flags.py executor/executor.py     <- zero third-party imports
services/api/app.py      FastAPI app: routes, envelope, flags, gate wiring, served-doc rewrite
services/executor-mock/  mock_clob.py (fake venue), transport.py (the client the real executor will reuse)
services/{api,executor-mock}/Dockerfile      non-root (10001/10002), no `:latest`
db/migrations/*.sql      Postgres, the source of truth
db/migrations-sqlite/    generated subset; `--check` is in the gate
db/seed.sql              generated by services/api/seed.py, re-runnable, time-relative
contracts/openapi.yaml   hand-written 3.1 contract (9 paths, 40+ response declarations)
tests/                   7 files, 151 tests, stdlib unittest, no pytest
tools/                   20 scripts; the four that matter for P04 are below
docker-compose.yml       postgres, pgbouncer, redis, migrate, api, executor-mock (+ `seed` under a dev profile)
Makefile                 30 targets, all of which `make -n` parse
.env.example             27 variables, documented, no usable values
```

Run it, in the order that works on a clean machine:

```bash
make doctor            # what this environment can and cannot prove (it is honest about missing tools)
make dev-core          # postgres+redis via compose if docker exists; otherwise sqlite + uvicorn
make migrate && make seed
make test              # 151 tests
make gate              # the P04 gate: 48 checks
make gate-mutate       # 23 mutants; proves the gate can fail
```

The prompt's quality gate is "I can place an order against `executor-mock` end to end with a curl command you
provide". Here it is, after `make dev-core && make migrate && make seed` (port 8090 api, 8091 mock):

```bash
curl -sS -X POST localhost:8090/v1/orders \
  -H 'content-type: application/json' -H 'x-user-id: u-demo' \
  -H 'Idempotency-Key: demo-0001' \
  -d '{"marketId":"0xM1","tokenId":"0xT1","side":"BUY","price":"0.50","size":"10"}'
# 202 {"intentId":"…","state":"queued","notionalMicro":5000000,
#      "poll":"/v1/orders/intents/…","note":"queued for the executor; a 202 is the answer, not
#      'accepted at the venue'","asOf":…,"risk":{"code":"OK","checks":["kill_switch", …]}}
curl -sS localhost:8090/v1/orders/intents/<intentId>     # follow it; the mock fills it
```

That exact exchange is not left to the reader's diligence: `tools/envelope-demo.py` performs it over **real TCP
sockets** (no test client), twelve steps, and asserts the envelope shape, the `x-risk-latency-ms` /
`x-risk-checks` headers on both accept and deny, `Retry-After` presence on retryables and absence on
permanent 4xx, key replay, off-tick refusal, and the kill switch. `make gate` runs it; exit 0 means "ALL
TWELVE STEPS MATCHED THE CONTRACT".

`docker compose up` **[UNVERIFIED]**: `docker` and `docker-compose` are not installed in this workspace, so the
container wiring — image builds, healthchecks, volume mounts, the `migrate`-before-`api` dependency — has never
been executed. One thing that *could* be checked without a daemon was not being checked: the file was parsed
with `yaml.safe_load`, which forgives a duplicate mapping key, and the file had one (`restart:` written twice
under `seed`, left by an earlier edit). Compose would have aborted on it at `up` time. `tools/p04-gate-check.py`
now parses compose with a loader that rejects duplicates (`yaml_strict`), and the mutation harness plants one to
prove the check fires. What *is* verified is the thing that usually breaks under it: every service's config is
boot-defaultable (`.env` marked `required: false`), the migrations apply to a real Postgres-dialect file set
against a real engine (SQLite, via the generator), and the app refuses to serve without a schema instead of
serving an empty database. The compose file says so at the top rather than pretending.

CI: `.github/workflows/ci.yml` has two jobs. `checks` runs the same set `make check` runs locally — doctor,
lint + lint canary, the OpenAPI audit + its self-test, `make test`, the socket demo, `make gate`, the
P01/P02/P03 gates and the colour gate, the mutation harness, and `--check` on both generated artifact sets —
because a phase that lets an earlier gate rot ships a broken brand next to a working backend. `containers` is
the job that exists specifically to close the gap above: `docker compose config -q`, real `docker build` of
both images, then `compose up --wait` + `migrate` + `seed` + a **curl that asserts the 202 and `"state":
"queued"`** on a clean runner.

**[UNVERIFIED], stated precisely:** this repo has no git remote (`git remote -v` is empty), so no runner has
ever executed this file. What was verified here is that it parses and that its structure is what it claims
(`jobs: ['checks', 'containers']`, 5 steps in the container job) — and while checking that, PyYAML read the
bare `on:` key as the boolean `True`, which is the YAML 1.1 trap GitHub Actions files are usually quoted
against; it is `"on":` now so this repo's own tooling and GitHub agree on what the file says.

---

## D7. Configuration and flags

One place: `packages/polygm_core/config/flags.py`. Boot defaults come from `PGM_*` env (documented in
`.env.example`), and at runtime every value may be overridden by the `feature_flags` table — the override, not
the env, is what `flags()` returns per request.

| key | default | why it is a flag |
|---|---|---|
| `stale_ms_book` / `stale_ms_tape` / `stale_ms_metadata` / `stale_ms_positions` | 3000 / 3000 / 120000 / 30000 | the freshness thresholds the gate denies on; measured from P01, not invented |
| `cache_ttl_market_ms` / `cache_ttl_books_ms` / `cache_ttl_leaderboard_ms` | 1000 / 250 / 60000 | the rate-budget dial (rule 2) |
| `cache_serve_stale_ms` | 30000 | one user must not be able to spend our budget twice |
| `max_order_notional_micro` | 2 500 000 000 ($2 500) | per-order cap |
| `max_24h_notional_micro` | 25 000 000 000 | per-user day |
| `min_order_size_shares_micro` | 5 000 000 | the venue's own minimum is 5 shares (measured) |
| `max_snap_age_ms` | 5000 | the gate's freshness clock |
| `builder_bps` | 100 | our take, and it must equal the registered code's rate or attribution is a lie |
| `whale_flag_micro` | 2 000 000 000 | P01's distribution: median fill $5–6 |
| `tape_ws_max_rows` | 64 | the design system's hard DOM cap (D5.2 there) |
| `rate_class` | `trader 12/4/120`, `anon 30/2/300`, `admin 60/10/600` | the limiter |
| `tape_ws` / `copy_trading` / `auto_redeem` (bool) | on / off / off | kill-class features: money-adjacent defaults are off by default, always |
| `max_tick_sizes` | `("0.001", "0.01")` | the venue's two tick sizes (measured); a market outside them is `UNKNOWN_TICK`, not "fine" |

**Changing a flag without a deploy.** `POST /v1/admin/flag {name, value, reason}` (or
`psql -c "UPDATE feature_flags SET …"` when the API itself is the thing on fire) writes the row plus an
`flag_audit` row; every pod sees it within the store's TTL (1 s) with no restart, because `flags()` reads the
store per request — a module-level cache would mean an emergency flag change needs a redeploy, which is the
opposite of an emergency flag.

**Auditing who changed it.** `flag_audit (name, old_value, append-only by trigger)` with `changed_by`, `reason`,
`at_ms`, and `kill_switch_state` is likewise append-only. Two rules the gate enforces because they were bugs
here, not hypotheticals:

* a numeric flag never carries `on: true` (`{"value": 1234}` only). Storing `on = bool(value)` on a number is
  how `PGM_MAX_ORDER_NOTIONAL=0` becomes "unlimited"; a bool flag is the one that says `on`.
* `readyz` calls `STORE.current()` and reports `flags_from_defaults`, so a pod that booted during a DB blip
  either recovers on the next probe or tells us it is running on defaults. Both halves were written after
  seeing the failure modes; a readiness that never refreshes is a pager that only fires by accident.

---

## D8. What this phase actually broke (the honest half)

Twenty-odd defects were found and fixed during P04. Six of them are worth stating because they are the class
of bug that survives review:

1. **The book's staleness was measured against generation time, not load time.** `db/seed.sql` embedded
   absolute millisecond stamps, so a freshly seeded dev database was 18 minutes stale on the first order and
   the gate correctly answered `STALE_QUOTE` — a *correct* program producing an unusable demo. Now every
   ms column is emitted as `{{NOW_MS}}` and expanded per engine at apply time (`tools/run-sql.py`). The gate
   now greps the seed for any absolute 13-digit timestamp. The general rule: a generated artifact must never
   freeze wall-clock.
2. **`nextCursor` was built from the wrong column.** `/v1/markets` sorted by `end_ts` and paged by `id`, so a
   page boundary could repeat a market. Fixed with a keyset on the sort value itself.
3. **`sortBy=spread` returned 500** because the alias was referenced in the same `SELECT` that defined it, and
   **unknown `marketId`s returned 500** because the shape check ran after the key was claimed. Both are
   now declared enums with live tests over every documented value.
4. **The gate compared a `NUMERIC(6,4)` tick from Postgres (`"0.0100"`) with a `REAL` from SQLite (`0.01`)**
   and failed on both engines while passing the hand-written fixtures. `norm_tick()` at the boundary is the
   fix, applied in the gate *and* the endpoint.
5. **`cursor.rowcount == 0` is not "I did not create that row"** under `ON CONFLICT DO NOTHING`: a waiter and
   the owner looked identical, so one user's retry could be told it was someone else's conflict. `Record`
   answers with `replay`/`busy`/`mismatch`, decided by `RETURNING`/`xmax`.
6. **A rejection that inserted a second `order_intents` row 500'd the retry** the idempotency layer had just
   promised, and a rejection that forgot `idem.abandon()` burned the key permanently. `_upsert_intent` revises;
   the gate audits every denial after `begin()` for a paired abandon (5 audited, 0 unpaired).

### D8.1 Measured facts that corrected the phase's premises

* `@polymarket/clob-client-v2` and `py-clob-client-v2` are **both at 1.1.0** (measured via `npm view` /
  `pip index versions`), so "the Python SDK lags on V2" was false; the real asymmetry is typing.
* V2 order args are **`float`** in *both* SDKs. Ledger math therefore stays integer micro-USDC and crosses the
  boundary only through `to_float_for_sdk`, which asserts the round trip and refuses beyond `2**53-2`
  (`2**53-1` is where it actually breaks — measured, not derived).
* V1 ships `MarketOrderArgsV1`; V2 does not accept it. There is no compatibility shim, on purpose.
* The tape is WebSocket-only in practice (`/trades` is Cloudflare-cached); Gamma caps at 100/page and ignores
  `tag_slug`; 8 of 22 sampled books had ≤5 near-side levels, which is why the UI never promises depth it may
  not have.

### D8.2 Corrections to my own tooling (a checker's bug looks like a product gap)

`tools/check-openapi.py` reported six nonexistent endpoints because the contract said `{marketId}` and the
route said `{market_id}` — path parameters cannot be aliased, so the contract was wrong. `tools/lint-rules.py`
found 399 "violations" that were docstrings until it parsed literals instead of raw text. The gate's first
secret scan used `git grep`, which only sees *tracked* files — i.e. it was blind to an entire uncommitted
phase; it now scans the working tree with `git ls-files --cached --others --exclude-standard`, and the one
fixture that must look like a key (the lint rule's own canary) is exempted only by exact line, with a check
that fails if an exemption stops matching anything, so the allowlist cannot become a hiding place. Each of
those was a bug in a checker that made a true claim look false — which is why every checker in this phase has
a canary (`--self-test`, 11/11) and why the gate prints pass counts rather than only failures.

And the reverse failure, which is the one worth writing down: a checker that is *more* forgiving than the thing
it checks. `yaml.safe_load` accepting a duplicate mapping key meant the compose file passed a check while
carrying a document `docker compose up` would refuse to start; a secret scan built on `git grep` reported
"clean" about 250 files it had never looked at, because they were not committed yet. Both are now written to
check what the real tool checks — a strict YAML parse with duplicates rejected, and `git ls-files
--cached --others --exclude-standard` — and both have a mutant in `tools/p04-mutation-test.py`
(`compose-duplicate-key`, `planted-secret-untracked`) so the fix cannot silently degrade back into the
forgiving version. A canary proves a check *can* fire; a mutant proves it fires at the right place.

---

## D9. Verification ledger, and what is not verified

Run in this workspace, all of it after the last edit (2026-09-17):

| command | result |
|---|---|
| `python3 -m unittest discover -s tests` | **156 tests, OK** (3.8 s) — api 51, core_governance 21, ledger 19, migrations 18, executor 17, gate 16, money 14 |
| `python3 tools/check-openapi.py` | **82 passed, 0 failed** |
| `python3 tools/check-openapi.py --self-test` | **13/13** canaries fire |
| `python3 tools/lint-rules.py` / `--canary` | 0 findings / 8 of 8 rules fire |
| `python3 tools/envelope-demo.py` | **exit 0**, 12 steps over real sockets |
| `python3 tools/p04-gate-check.py` | **55/55 checks passed**, exit 0 (`docs/verification/P04-gate-output.txt`) |
| `python3 tools/p04-mutation-test.py` | **24 caught, 0 not caught, 0 broken mutants** — and the harness refuses to run at all if the *clean* tree's gate is not green (`docs/verification/P04-mutation-output.txt`) |
| `make -n` on all 30 targets | parses |
| `python3 tools/build-sqlite-migrations.py --check` | generated subset current |
| `tools/run-sql.py --check` | ledger records applied files, refuses drift |
| `python3 tools/doctor.py` | reports the missing tools below rather than working around them |

**Re-verified after P05 landed** (same day, because a phase that breaks the last phase's gate has not finished):
221 tests OK · P04 gate 55/55 · `make probe-fresh` re-run against the live venue (it found `cache_buster_works`
reading false, which turned out to be a measurement that compared two *busted* fetches to each other; the tool
now compares busted against plain and records the 0/3–3/3 count, and the re-run confirms the buster still
works — plain newest ts 1789645254 vs busted 1789645554, 5 minutes newer). Two checker defects surfaced during
that re-verification and are fixed here, not worked around: the migration-chain test asserted a file count
(`== 4`) instead of the property the migrator needs, and `CORE_STDLIB` was missing `statistics`.

**Not verified, and why:** `docker` and `docker-compose` are absent, so no image was built and no container
network was exercised (`[UNVERIFIED]` for compose, Dockerfiles, and their healthchecks); `psql` and the
`sqlite3` CLI are absent, so the Postgres dialect has been read, linted and translated but never executed
against a live server — the migrations run on SQLite through the generated subset, which proves the *logic*
that both dialects share and nothing about `RETURNING … xmax = 0`, which is exercised only by the pg-specific
SQL string; `mypy` and `pytest` are absent, so types are checked by lint rules and tests run on stdlib
`unittest`; `vercel`/`supabase` CLIs are absent and the web app does not exist until P07. `make gate`
therefore tests what a sandbox without docker can honestly test, and says so.
