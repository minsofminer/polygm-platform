# P06 · The trading plane

Wallet custody, the venue path, reconciliation, limits and the kill switch, copy trading, automation, and
builder-revenue attribution. This is the phase where the product can move money, so every number below is one
this repository can re-measure on a laptop, and every number it cannot measure is marked.

Measured on 2026-09-18 in this tree: `make p06` → **31/31 gate checks**, `python3 -m unittest discover -s
tests` → **500 tests OK**, `make chaos-p06` → **7/7 assertions against a real `SIGKILL`**
(`docs/verification/P06-chaos-output.txt`), `make drill-p06` → **5/5 components, worst refusal 460 ms against
a 1000 ms budget** (`docs/verification/P06-drill.txt`).

---

## D1 · Wallets: custody, states, and the two guards nobody expects to fire

A wallet is a row plus a state machine (`packages/polygm_core/wallets/lifecycle.py`), and the machine is
enforced **in the store**, not merely offered to callers: `Store.set_wallet_state` calls
`wl.assert_transition` and refuses an illegal hop. That distinction was found by the gate — the machine
existed as a helper, and a probe showed `suspended → provisioned` was accepted by the store while the helper
said no. Six states, 36 ordered pairs, **13 legal**, no self-loops: a wallet re-entering the state it is
already in is how a stuck loop hides.

Two guards on the withdrawal path are worth their space because both were broken here first:

- **Typed amount.** `_norm_decimal` compares what the human typed with what will be sent. It once referenced
  a name it never imported; a broad `except` turned every input into the same sentinel, so the check passed by
  agreeing with itself and a digit-slip would have left the building. It is now narrow, and
  `tests/test_wallets_lifecycle.py` pins both directions: `12.50` matches 12 500 000 µ and `12` does not,
  because `12` is a different number, not a different spelling.
- **Policy drift.** The executor re-derives the live custody policy hash before it signs and refuses when the
  provisioned hash differs — `POLICY_DRIFT`, wallet suspended, operator notified, nothing signed. `make
  dev-core` in this phase also proved the check is load-bearing in the wrong direction once: an executor
  started with the default policy refused to sign a wallet provisioned against a custom one. That refusal is
  correct; a harness that fought it would be measuring its own fixture.

Deposits: the minimum economic deposit is **10× the measured on-chain bill** (`minimum_economic_deposit`), the
percentage bridge fee is disclosed rather than folded into the floor, and a deposit past
`DEPOSIT_STUCK_AFTER_MS` (15 min) is called *delayed, not lost* in the user's note — the word "stuck" never
appears in text a user reads. Key export exists and is obvious, but is refused while the wallet has open
orders, unfinished intents, an in-flight withdrawal, or a delayed deposit: two owners of the same money means
one of them is surprised.

Provider pricing is **not verified** and is carried as `[UNVERIFIED]` everywhere it appears: turnkey `[UNVERIFIED]`
(flat monthly tier + per-MAU overage, 1.5e9 µ at 10k wallets), privy `[UNVERIFIED]` (per-MAU plus a
TVL-linked card fee), dynamic `[UNVERIFIED]` (per-MAU), self_hosted `[UNVERIFIED]` (our engineering time; the
$0 row is labour, not a vendor invoice). `PROVIDERS[i].verified` is `False` for all four, and the gate's doc
check fails if any of those names loses its marker. Pricing becomes a number we can say out loud in P13, when
a contract is in front of us.

## D2 · The venue path

`py-clob-client-v2==1.1.0` is the pinned client; `services/executor-mock` is the venue we ship against, and
the executor speaks HTTP to it as a *separate process* so a crash can be observed. Batch cap **15 orders per
POST**, refused rather than truncated. A batch that answers for 13 of 15 items leaves 2 **uncertain** — "no
answer" is not "no order", and treating it as absent is how a retry double-spends. `CancelBudget` is 250
cancels per 10 s, shared across the process; `CANCEL ALL ORDERS` requires a ≥20-character reason when the
scope is the whole book.

Fee maths is integer-only and rounds **up** to the venue's grid; `estimate_fees`, `amounts_for` and
`all_in_spend_limit` are the same functions the executor uses, so a fee the UI quotes is the fee the order
carries. Order types by audience: humans get `GTC/GTD/FAK`; `FOK/MARKET` belong to automation, which must
carry a price bound. Copies get their own exposure — `allow_market=False, requires_price_bound=True` — because
a copier acts on someone else's information seconds late: it may rest a priced limit order and may never
cross the book blindly. Preflight is **10 named checks in a fixed order**, and a shorter list stops signing
(`PREFLIGHT_INCOMPLETE`, a code that had to be added to the deny table rather than worked around).

## D3 · Reconciliation: eight questions, asked in order

`packages/polygm_core/reconcile/reconciler.py`: `no_ack`, `submitted_unacked`, `fill_missing`,
`closing_market`, `ghost_order`, `orphan`, `ambiguous_settlement`, `cancelled_race`, plus
`signed_orphan`-style lookups; each case has a `case_<name>` handler *and* an executed test, and the gate
runs all eight against a live database rather than reading the list.

The rules that keep money safe: a **failed lookup is never a sighting** (12 unreachable passes leave the
intent `uncertain`, `attempts = 0`, and a note that says why; nine *answered* absences close it); recovery
never re-POSTs — `order_attempts` is written *before* the POST and the deterministic `client_order_hash` is
stamped on the intent before the wire, so any later process can ask "did this exist?" and, on "yes", adopt
instead of sending; `book_fill` is the **only** way money enters and it is idempotent on the venue trade id;
unreconciled items alarm after `ALARM_AFTER_MS = 60_000` strictly-greater age; a durable cursor means a
restarted process does not re-open what is already open; `requeue_expired_claims` requeues `submitting` and
never `uncertain`.

`make chaos-p06` kills the process at three moments — after signing, inside the POST, after a fill is booked
— and the assertions are read from the **venue's** counters: `orders created: 1` in each scenario, POST count
unchanged when the order was adopted, and one `cash_ledger` row per venue trade.

## D4 · Limits, the breaker, and the kill switch

The risk gate is one function, run in a stated order, and the executor re-runs it rather than trusting that
the API did: `kill_switch → market_state → freshness → side → tick_alignment → min_size → notional →
price_band → user_limits`. 78 deny codes carry an HTTP status, a retry answer and a severity
(`packages/polygm_core/risk/limits.py`), and the gate harvests the refusal strings out of the source so a code
invented in a hot path is caught by the check rather than by a user.

The breaker fails **closed** (5 consecutive failures or a 50 % error rate over ≥20 samples, 15 s cooldown,
half-open probe), the cooldown running from the last failure; a loss halt blocks until a human acknowledges it
with a named actor; rate windows expire instead of latching. The kill switch's budget is
`lm.KILL_BUDGET_MS = 1000` — **the plane must stop in under a second** — and `make drill-p06` measures it on
real processes: engage through `POST /v1/admin/kill-switch`, then time each component's refusal. Measured
today: api 3 ms (HTTP), executor and worker 460 ms, copy and automation ~1 ms each, worst 460 ms against the
1000 ms budget. The executor's number is not noise: a child re-reads the switch every `PGM_KILL_POLL_MS`
(250 ms) and acts once per **tick** (100 ms here), so an intent queued in the same instant legitimately
finishes. The drill therefore measures two things: the refusal latency, and that **zero** orders reached the
venue after the propagation window.

The drill is also how two gaps were found and fixed in the product, not in the harness: the copy engine used
to queue an intent whose Activity line said "copied" for two seconds before the executor rejected it, and the
automation engine had no view of the switch at all. Both now refuse at the moment of decision (`disabled:…`
skip reason; `RISK_HALT` run row), and a dry run still evaluates so an operator can read what the rules would
have done during a halt.

## D5 · Copy trading

A source fill becomes an ordinary queued intent with `audience='copy'` on the directive — the same gate, the
same preflight, the same reconciliation, no side door. It **skips rather than chases**: more than
`max_entry_deviation_bps` away, a stale or missing quote, an unknown tick clock, an unrecognised chain or
cycle, a market inside `min_seconds_to_resolution`, per-trade or daily caps, `min_interval`, sub-share sizes.
Replays are idempotent on the source fill: the intent is created once, and the replay still writes an event
row saying it was declined, so "why is my copy missing" has an answer in the database. Chain-of-copiers is
refused at depth, cycles are named. The record a copier reads carries `net_after_fees_micro`,
`max_drawdown_micro` and `longest_losing_streak` — a track record without the losses is marketing.

## D6 · Automation

`validate_rule` runs at save, so a rule fails in the editor rather than at 03:00; unknown kinds, nesting
depth and action breadth are refusals with reasons. A rule becomes live only through `mark_dry_run_done` then
`enable`, and `enable` re-validates the saved tree, so a template that went invalid after arming cannot keep
firing. Every evaluation writes a row — placed, would_place, skipped and failed, with the reason — and a
human's recent order parks the rule for `human_priority_ms` (120 s) *and pauses it*: the stand-down is sticky
until a human re-arms it, because the point of the pause is that somebody started trading by hand. A live pass
on a stale quote is refused (`STALE_QUOTE`), no rule may cancel the user's whole book (`cancel_open` with
`scope: 'all'` is a validation error), and the daily-rule and notional caps are checked before the venue is
touched.

**The 5-minute crypto template: shipped, because the arithmetic works — at a bound, and the gate evaluates the
bound rather than trusting it.** At a 50 000 µ (5 ¢) price with a 70 bp taker fee and a 100 bp builder fee,
`break_even_win_rate_bp` says 13 500 µ of edge per share and a 51.35 % win rate; `edge_needed_bp` is 135 bp.
The template ships only for fee schedules that are *known* (`fee_type='None'` is not a schedule) and only when
the measured `edge_available_bp` is strictly greater than `edge_needed_bp`; otherwise the entry rule is not
offered and the three protective rules (hard exit before resolution, take profit, stop loss) ship regardless.
`tools/p06-gate-check.py` runs that decision as a test, so a change in the fee table moves the verdict.

## D7 · Lifecycle, as the user sees it

The prompt's `draft → intent → risk_passed → signing → submitted → open → partially_filled → filled |
cancelled | rejected | unknown → reconciled` maps onto the rows we actually write: `draft`, `preflight`
(= risk_passed, and it lists the checks that ran), `signing`, `submitted`, `live` (= open), `partial`,
`filled`, `cancelled`, `rejected`, `unknown`, `reconciled`. `unknown` is a state with UI: the order card says
"we do not know whether the venue has it; we are asking by the id we chose before sending", and it may not be
silently re-sent. A `cancelled` intent means "this never reached the book", which is a claim we only make
after the venue answered `no such order` `max_sightings` times.

## D8 · Builder revenue

Every attributed order writes a terms row (`builder_attribution_terms`) and an attribution row carrying
`expected_micro`, `observed_micro`, `chain_measured_micro`, `delta_micro` and the code fingerprint. The daily
rollup compares our expectation with on-chain `OrderFilled` events; a payout is computed from the **observed**
fee only, an unmeasured day is `unreconciled`, a disputed one `investigating`, a matched one `matched`, and
per-code caps are enforced. `expected` never pays. A bad claim does not block the order: the registry wins and
the order runs unattributed, because the user's fill is not the place to settle a revenue dispute.

## What is not true yet

- No real funds. The signer in this tree is an HMAC (`TestSigner`) and the venue is `executor-mock`; real keys
  and the real venue arrive in P13, and per `docs/AGENTS-BUILD.md` nothing moves real money until P13 and P14
  are green.
- Every provider number in D1 is `[UNVERIFIED]`.
- The venue's own fee/`builder` semantics are pinned against the mock and the pinned client version; a venue
  change is a P13 finding, not a P06 assumption.
- On-chain `OrderFilled` measurement has a table, a job and an idempotent write, but no live RPC in CI: the
  daily rollup accepts `chain_log | builder_trades_api | manual` sources so the first real measurement can be
  a manual row rather than a guess.

## Running it

```
make p06               # the 31 checks, offline (suite + recorded evidence + invariants)
make gate-p06          # SIGKILLs first, records docs/verification/P06-chaos-output.txt, then the gate
make chaos-p06         # just the kills, streamed
make drill-p06         # kill switch against running processes, docs/verification/P06-drill.txt
make gate-p06-mutate   # breaks each money rule on a copy and requires the gate to notice
```
