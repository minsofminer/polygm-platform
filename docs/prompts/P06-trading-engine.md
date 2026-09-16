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
