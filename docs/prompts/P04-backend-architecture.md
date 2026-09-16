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
