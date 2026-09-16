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
