# P13 — Test Strategy & Implementation

Phase P13 of the PolyGM build. The kit's directive is one sentence long and it is the only one that matters:

> The goal is not coverage percentage — it is that the specific ways this system can lose a user's money are
> all covered by a test that runs in CI.

Everything below is written against that: what runs where, what each level costs, what is *not* covered, and
the exact evidence for every claim. Where a clause of the kit is not satisfied in this environment, the section
says so in the same paragraph as the claim it qualifies.

Evidence lives in `docs/verification/`, the gate is `tools/p13-gate-check.py`, and the money-path matrix is
`tools/p13-money-matrix.py` — 43 rows, every one resolving to a collected test (see D2).

---

## D1. Test architecture

### The pyramid, with the budget each level actually costs

| Level | What lives here | Budget | Measured here |
|---|---|---|---|
| **Unit** | Pure modules: money/`cents`, the ledger, the risk gate, `Reconciler` cases, the signal engine, order construction, the property sweeps. No sockets, no HTTP, no migrated database | < 60 s | **559 tests in 22.9 s** |
| **Integration** | The API under a real HTTP client against a migrated SQLite file, the executor service with the scenario venue, ingest, the Telegram and wallet routes, the contracts. Everything that can fail because two components disagree | < 5 min | **773 tests in 100.9 s** |
| **End-to-end** | `web/e2e/*.spec.ts` on Playwright: buy flow (intercepted at the wire, geometry asserted), withdrawal ceremony, Telegram webview | < 15 min | not runnable in this sandbox — see the note below |
| **Nightly** | The eleven chaos drills, the 100× kill loop, D5's full load clauses, the browser suite, the phase gate over the night's records | ~2 h | `.github/workflows/nightly.yml` |

The whole Python suite is **1,332 tests in 120.4 s** (`PGM_TEST_TMPDIR=/home/user/.cache/pytest-tmp python3 -m
pytest tests/ -q`), which is the integration budget for everything at once. The split above is real rather than
nominal: the unit subset is the files that never import `app`.

**The browser level, honestly:** Playwright's chromium downloads fine into this sandbox and cannot launch —
`libxkbcommon0`, `libasound2t64`, `libnss3` and friends are missing and this box runs as uid 1000 with no way
to install them. `web/playwright.config.ts` and three specs exist, `npm run test:e2e` is wired, `npm ci` pulls
`@playwright/test@1.55.0`, and the nightly `e2e` job runs `playwright install --with-deps chromium` before it.
What this environment can prove is that the suite is written, collected and run by CI; what it cannot prove is
that the specs pass. That distinction is in the gate's report rather than in a footnote: the gate says
`deferred to the web job` when there is no `node_modules`, and only runs them when there is.

### The recorded corpus — and the rule that no test touches a live API

`tests/fixtures/p05/` holds the anonymised Polymarket responses (3.0 MB: markets, events, tags, books, history,
trades, positions, leaderboards, each with a `.shape.json` beside it) and `tests/fixtures/p13/contracts.json`
holds the request/response shapes P13 recorded. Tests replay them; nothing fetches. The one drill that is
allowed to reach the network is D7's drill 2, because a five-minute WebSocket outage cannot be simulated
convincingly, and it is opt-in (`--execute-live`) with its own artifact.

The shapes beside each fixture are the point of the corpus: `tests/test_ingest.py` replays the payloads and the
`shape.json` files are checked for drift, so an upstream schema change fails the build here rather than in a
user's browser (`tools/build-p13-contract-fixtures.py --check`).

### `executor-mock` as the spine

`services/executor-mock/mock_clob.py` is not a fake that says yes. Its scenarios are a menu of plausible
misbehaviour and every one of them has a test that would fail if the product ignored it:

| Scenario | What it models | Where the product is proven against it |
|---|---|---|
| `accept` | the venue working | `test_executor_service.py::TestHappyPath` |
| `reject` | a venue refusal with a reason | `TestPreflightRefusals` |
| `partial_fill` | a partial, then the remainder | `TestFillsAndLedger`, `TestCancellation` |
| `timeout_after_accept` | **the bad one**: accepted, response lost — resubmitting duplicates the order | `TestCrashBetweenSignAndPost`, `tools/p06-chaos-test.py`, `tools/p13-recovery-loop.py` |
| `fill_then_disconnect` | filled, then the connection died — the money already moved | `test_p13_money_matrix.py::TestTheVenueBehavesBadly` |
| `wrong_price_fill` | matched at a price we never agreed to | `TestTheVenueBehavesBadly` (refused above our limit, booked below it) |
| `rate_limit` | 429s | D7 drill 6, `test_executor.py::TestUncertainSafety` |
| `unreachable` | no venue at all | `test_executor.py::TestCrashRecovery` |
| `builder_disabled` | our builder code switched off at the venue | D7 drill 7, `test_executor.py` |
| `ghost_order` / `cancel_races_fill` | a cancel that lies / a cancel that loses to a fill | `test_reconciler.py::TestReconcileTaxonomy` |

### Time control

There is no sleeping in the suite and no `freezegun`. Every time-sensitive entry point takes the clock as an
argument — `Reconciler.run_pass(at=…)`, `Executor.tick(at=…)`, `store.*(at=…)`, and the wallet lifecycle ladder —
and the HTTP layer's clock is injectable (`app._now_ms`, swapped in `tests/test_alerts_api.py` to test cooldowns).
A test that needs "twenty minutes later" passes `self.at + 1_200_000` and is over in microseconds. The chaos
drills cannot use that trick for the failures they model (a SIGKILL is a real event), so they are the one place
where wall-clock time is spent on purpose.

### Factories

The factories are per-suite `Harness` classes rather than a `factories.py`, and deliberate: `Harness.fund()`,
`Harness.queue()` and `OpsBase.seed_market()` write through the same doors as the product (the same store
methods, the same CHECK constraints, the same `wallets`/`balances` rows) so a factory cannot manufacture a state
the product could not reach. The invariants live in the schema — `builder_code_status.source`, the `wallets`
custody CHECKs, `reconcile_open.case_name` against `CASE_ORDER`, `tape_fills`' `UNIQUE(dedupe_key)`,
`signals`' `UNIQUE(rule_id, dedupe_key, fired_bucket)` — and the factories inherit them instead of restating
them. `tests/conftest.py` provides the per-run tmp dirs (`PGM_TEST_TMPDIR`), the schema apply and the app import.

---

## D2. The money-path matrix

`tools/p13-money-matrix.py` enumerates the kit's rows and maps each to the test that proves it: **43 rows, 64
mapped test references (62 distinct tests), all collected and all executed**. The matrix is the only coverage gate in this repo — the kit's D8 asks for exactly
that ("coverage reported but not gated — except the money paths, which are gated at 100% of the enumerated
matrix"), and `tools/p13-gate-check.py` fails if a workflow introduces a global coverage threshold.

The seven groups, and what the rows required beyond what earlier phases already had:

* **Order lifecycle (OL-1…13).** The happy path, each refusal with its user-facing message, partial-then-cancel,
  partial-then-resolve, FOK rejected with nothing left behind, the all-in spending cap, and the two the kit's D1
  list named but nothing tested: a fill the connection never reported, and a venue that fills at a price worse
  than our limit.
* **Ambiguity (AMB-1…8).** The P6 D3 cases, plus the same two above from the other side.
* **Risk gate (RG-1…5).** Every limit trips; the risk service failing closed; the kill switch stopping submission;
  the kill switch mid-copy-trade; the daily-loss halt.
* **Fees (FEE-1…5).** `C × feeRate × p × (1−p)` against exact rational arithmetic at 0/70/100/200/1000 bps and at
  p=0.01/0.99; builder fees at 0/1/50/100 bps; realised-versus-estimated with the delta stored; the no-float
  round-trip property.
* **negRisk (NEG-1…3).** Three legs of a mutually exclusive event; merge/split as the explicit refusal; prices
  that do not sum to a dollar and what the UI does with that.
* **Wallet (WAL-1…5).** Deposits on each chain; allowance exhausted mid-session; a non-allowlisted withdrawal;
  key export audited; the provider down.
* **Auth (AUTH-1…4).** `initData` HMAC valid / tampered / stale / replayed.

### Two false greens in the matrix itself

`--run` was written to prove the mapped tests pass, and for one commit it proved nothing. It passed pytest the
node ids without the `tests/` prefix, so pytest answered `ERROR: file or directory not found … no tests ran in
0.00s`; the tool's only failure signal was a regex over its own output, that set stayed empty, and the matrix
reported **43 of 43 rows green in 0.3 s**. The fix surfaced the second bug in the same code path: the replacement
parser treated *any* node-id-shaped line as a failure, and the warnings summary supplies one for a test that
passed. `summarise()` now requires a zero return code, a parseable summary line, counts that add up to exactly
the number of mapped tests, zero skips (a skipped money-path test is a hole wearing a green badge), and an empty
failure set — and both false greens are replayed as canaries in `tools/p13-gate-check.py`. What caught them was
not a verdict but a runtime: 0.3 s for 62 tests, then a green run that said `1 failed`. A gate that cannot fail
is not a gate, and neither is one that fails on its own warnings.

### The bug the matrix found in the product

`TestTheVenueBehavesBadly::test_a_fill_worse_than_our_limit_is_refused_and_becomes_a_case` failed on the code as
written in P06. `Reconciler.sync_fills` booked every venue trade through `book_fill` and checked the price/size
only for grid-exactness — a BUY filled *above* our limit was booked silently at the venue's price. The user's
cost basis moved, the notification quoted a number they never agreed to, and nothing anywhere said so: precisely
the "silent inconsistency" this phase has zero tolerance for. The fix refuses the fill, leaves the ledger
untouched, and opens an `ambiguous_settlement` case naming both prices; price *improvement* (a limit is a bound,
not an equality) still books at the venue's better price, which is asserted separately so neither half can rot.
Both directions were proven by reverting the fix (`git stash`) and watching the test go red, then restoring it.

---

## D3. Contract tests

* **OpenAPI validated on every request and response** — `tools/check-openapi.py` audits the contract against the
  implementation (**659 checks, 0 failures**) and `make openapi-selftest` proves the checker can fail. A field
  rename upstream fails the build: `tests/test_api.py::TestEnvelope::test_the_contract_and_the_implementation_agree_on_the_order_status_set`
  is that rule for the status vocabulary, and it caught P13's own new `SERVICE_UNAVAILABLE` code, which had to be
  added to `contracts/openapi.yaml`, `web/src/api/envelope.ts` and `web/src/tma/trade.ts` in the same commit
  (`npm run gen:api` regenerates `schema.gen.ts`, and the P08 gate fails on drift).
* **Consumer-driven pairs.** `api ↔ executor` (the intent/order contract, `tests/test_executor_service.py`),
  `signals ↔ notifier` (`tests/test_fanout.py`), `api ↔ ingest` (`tests/test_ingest_main.py`), `api ↔ bot`
  (`tests/test_telegrambot_api.py`). Each pair's contract is exercised through the real modules on both sides.
* **Recorded replay.** `tests/fixtures/p13/contracts.json` plus the fixture registry test; regenerated by
  `tools/build-p13-contract-fixtures.py`, checked with `--check` — including a gate canary that perturbs a
  recorded shape and requires the checker to refuse it.
* **`initData` HMAC.** AUTH-1…4: valid accepted; tampered signature rejected; stale `auth_date` rejected;
  replayed payload rejected. The implementation is P07's `security/telegram.py` and it is the only one.

---

## D4. Frontend tests

* **The number layer gets property tests.** `web/src/num/p13-property.test.tsx` sweeps seeded inputs over
  `microToCents`, `priceToUnits`/`unitsToPrice`, `groupThousands`, the flash policy (`decideFlash`: the rate cap,
  the rest-source refusal, the not-integer-display-units refusal) and `StaleIndicator`. 2,000 money values are
  rendered and each must print as digits — never `1e-7`, never `0.30000000000000004`, never a rounding artifact.
  Writing it found two bugs in the *test* (a regex that did not know the design system's U+2009 separator, and a
  React `Number` binding shadowing the arithmetic coercion) which are documented in the file because both are the
  kind of mistake that makes a green suite meaningless.
* **`StaleIndicator` is asserted, not assumed** — both by the property sweep and inside the ladder test below.
* **Accessibility and keyboard**, as the rules an axe run would apply, written out and checked against the real
  components (`web/src/screens/p13-a11y.test.tsx`): every control has an accessible name, nothing focusable sits
  inside `aria-hidden`, no positive `tabindex`, inputs are labelled, live regions announce. The ladder's depth bar
  must be `aria-hidden` (it is a visual encoding, not a character stream), staleness must be words rather than a
  tint, and the ticket must be reachable and refuse out loud with the feed down. There is no axe dependency in
  this repo and adding one that cannot run here would be a claim without evidence; the checks above are the
  findings that actually block a screen-reader user.
* **The tape's `aria-live` rule**: `web/src/screens/Tape.tsx` renders the list `aria-live="off"` with a separate
  polite status region (`web/ui/Dialog.tsx` owns the single reused region), so a fill tape at 20 updates/s does
  not spam a screen reader. It is written in the component and asserted in the a11y rule above.
* **Layout shift on a flash** is asserted where it can be measured: `web/e2e/buy-flow.spec.ts` takes the ladder's
  bounding box before and after a tick and requires it unchanged. jsdom cannot make this claim — there is no
  layout — so it is not pretended into a unit test.
* **E2E flows** (`web/e2e/`): `buy-flow.spec.ts` (the ticket's exact request body asserted at the wire, keyboard
  only, geometry unchanged), `wallet-ceremony.spec.ts` (the withdrawal ladder cannot be skipped; a typed amount
  that does not match is refused, not corrected), `telegram-webview.spec.ts` (the Mini App loads
  `telegram-web-app.js` and nothing else external; with that script blocked it degrades to a readable refusal; no
  horizontal overflow at 390×844).

---

## D5. Load tests

`tools/p13-load.py` — six tests, `--quick` for CI sizes and full sizes for the nightly. The measured numbers
from the full runs recorded in `docs/verification/`:

| Clause | Result | Artifact |
|---|---|---|
| 200 fills/s for 30 minutes, no lag growth, no duplicate alerts | **360,000 fills at 200/s in 1,800.0 s of wall clock** (paced, `elapsed_s` recorded and asserted); **0 duplicate deliveries**, 0 tape duplicates; 7,536 alerts fired and 31,272 suppressed by cooldown; consumer skew flat at **−204 ms → −198 ms** (no growth); RSS 26.1 → 35.9 MB with **2.1 MB of second-half growth** (0.098 MB/min steady, ceiling 6 MB); 385 s of CPU | `P13-soak-1800s.{txt,json}` |
| 2,000 books under continuous deltas | **11,955,200 deltas at 99,625/s**, cpu 96.9 % of one core, **RSS +7.8 MB** | `P13-load-books.txt` |
| p95/p99 at 500 concurrent users, reads from cache | p50 1,841 / p95 1,997 / p99 2,138 ms at 500 clients, 268 req/s, **0 errors**; ramp 10→500 with the knee at 250 (p95 1,085 ms) | `P13-load-api.txt` |
| 10,000 subscribers, one evaluation, delivery inside SLO | **10,000 drained in 54.7 s (182.8/s)** at 16 workers; SLO 300 s | `P13-load-fanout.txt` |
| 1,000 clients + reconnect storm after a forced restart | **1,000 clients, 4,268 ok / 57,827 refused during 10.4 s of outage, back in 6.09 s, 20 answers after**, 0 slow failures while the server was definitively down (60 were in flight when the process died) | `P13-load-storm.txt` |
| 100 aggressive users never exceed the per-IP budget | **30,000 requested, 184 served, 27,812 dropped by the queue**; peak 10 s windows `{data.trades 60, clob.book 62, gamma.markets 62}` | `P13-load-budget.txt` |

### The soak clause, run for real

The kit's clause is the one number in D5 that cannot be argued with: sustained 200 fills/s for 30 minutes with no
lag growth and no duplicate alerts. `P13-soak-1800s.{txt,json}` is that run — **360,000 fills in 1,800.0 s of wall
clock**, zero duplicate deliveries, zero tape duplicates, 7,536 alerts delivered with 31,272 suppressed by
cooldown, consumer skew flat from −204 ms to −198 ms (ahead of schedule, not behind it), 2.1 MB of second-half RSS
growth against a 6 MB ceiling, 385 s of CPU.

Three attempts at this clause are on record and the first two failed, which is why it is worth stating as a
sequence: the first counted 6,000 duplicate deliveries of the harness's own making, and the second was an
*unpaced* 30-minute soak that finished in 45 s and reported 1,800 s. The pacing assertion in the gate exists
because of the second.

Two harness bugs were found and fixed by running these at full size, and both are recorded because a load
harness that lies is worse than no load harness:

1. **The soak counted 6,000 duplicate deliveries of its own making.** It queued one delivery per alert keyed on
   `(rule_id, dedupe_key)`, while the product's `Ingest.record_alerts` keys a signal on
   `UNIQUE (rule_id, dedupe_key, fired_bucket)` — so a rule that legitimately re-fired in a later cooldown window
   reused the fanout idempotency key and the harness called the product's correct behaviour a duplicate. The
   queue now carries the cooldown bucket, which is what makes "no duplicate deliveries" a claim about the product.
   (Ironic and worth saying: the *harness* was the duplicate-alert bug this clause exists to catch.)
2. **The storm's "the API did not come back" was the harness's own 1-second health probe.** The restart was up
   and answering inside 3.6 s while the probe timed out against a 500-client p95 of 2.0 s and reported an
   outage. The probe now waits 2 s per attempt for 60 s, the restart's own log is captured and quoted when it
   genuinely fails, and requests already in flight at the moment of the kill are excluded from the "fail fast"
   assertion (their socket sat in the dead process's accept queue; that is the kernel, not the client).

The full-size run also found a real product issue that the soak's own `--pace` run exposed: an unpaced 30-minute
"soak" finishes in 45 s, and a harness that reports 1,800 s for it is reporting its own arithmetic — hence
`elapsed_s` in the result and the pacing assertion in the gate.

### Scale note

The kit's baselines are ~20.8 fills/s and it says "design for 10×". The soak drives **200 fills/s**, the books
test 2,000 books, the fanout 10,000 subscribers and the storm 1,000 clients. The API's absolute numbers are a
*floor*: the engine here is SQLite with a single writer, and the harness states that in every transcript rather
than letting the number look like a Postgres benchmark.

---

## D6. Property-based and fuzz tests

`tests/test_p13_properties.py`, five groups against the five clauses:

* **Order construction** — arbitrary valid inputs produce orders on the tick grid, at or above minimum size, with
  the builder code present; values that cannot cross the float boundary are refused rather than rounded.
* **Rule-engine budget** — arbitrary user-composed rules and adversarial events stay inside the evaluation
  budget, and the fanout scheduler holds its budget at 100,000 rows.
* **Adversarial search** — huge strings, regex bombs, homoglyphs, RTL overrides and null bytes never hang, throw
  or inject; a long query is bounded before it reaches the matcher.
* **Attacker-controlled market text** — anyone can create a Polymarket market, so a title is hostile input: every
  nasty title is escaped in the Telegram card, and a market carrying one is served without executable markup.
* **Numeric parsing** — malformed prices and sizes are rejected, never coerced; floats are refused *by type* in
  the money path; round-tripping never loses precision (the float boundary is measured: 8,703,815,948,975,240).

---

## D7. Chaos tests

`tools/p13-chaos-suite.py` — the kit's ten drills plus **11, the 500-copier cascade**, each with a written
artifact, each artifact headed by the **expected outcome written before the drill ran** (the kit's own
constraint, made mechanical: the text lives in the suite's `EXPECT` table, the artifact prints it above the
observations, and the gate refuses an index that lacks one). Current record: **11 of 11 run here, all PASS**,
plus drill 2's live artifact.

Drill 11 was added after P16, and it is a P14 finding rather than a kit row: P14 measured the 500-copier cascade
**arithmetically** — it sized 500 configs through the engine and found every one inside its own caps — while
recording, honestly, that *"no run placed 500 real orders against a filling venue."* Arithmetic is the wrong half
to be confident about, because 500 configs is a load shape and the bound that matters is the one the queue
enforces. So the same 500 copiers now run end to end: 500 funded accounts, 500 `copy_configs` rows on one source,
one `SourceFill` through the product's own `CopyEngine`, 500 intents through `enqueue_intent` (the same door a
human order uses), the executor claiming them `batch_size` at a time, the venue filling all 500, and the fills
booked through the same `book_fill` the venue's trade stream uses.

| # | Drill | Evidence |
|---|---|---|
| 1 | Executor killed mid-submit | `P13-chaos-1-executor-kill.txt` (6 runs here) + the headline loop below |
| 2 | WebSocket killed for 5 minutes | `P13-chaos-2-ws-kill.txt` (live, `--execute-live`) |
| 3 | Ingest killed mid-write | 3,476 of 4,000 durable mid-write; replay → 4,000 distinct, no duplicates |
| 4 | Database killed mid-flight | locked store → **503 in 5,030 ms, retryable**, rows unchanged (1,0,1); after release 200/202 → (2,0,2) |
| 5 | Cache killed | market 200 + 5 book levels with an empty cache, 25 reads in 91 ms |
| 6 | 429 storm from the venue | 8/8 `THROTTLED`, then 0 posts |
| 7 | Builder code disabled | `BUILDER_DISABLED`, `builder_code_status.source = venue_rejection` |
| 8 | Signer / wallet provider down | `SIGNATURE_REFUSED`, 0 posts, reads still 200 |
| 9 | Kill switch during a copy-trade | stops in 2.1–2.2 s, nothing placed |
| 10 | Key compromise | 10,000 keys, revocation in 1.0 s |
| 11 | **500 copiers on one source fill** (P14's open item, closed) | fan-out **86–92 ms** (0.17–0.18 ms per copier), $6,375.00 aggregate over 500 orders, largest **$12.75 of a $25.00 ceiling**, replay of the same fill **0 new intents**, 500/500 filled at the venue and booked in 2 reconciler passes |

**The headline loop** (`tools/p13-recovery-loop.py`, kit quality gate): 100 kills between signing and the
response, random timing, fixed seed 13 → **100/100, zero duplicate orders, zero lost positions, zero limbo**,
354.8 s, recorded in `P13-chaos-recovery.{txt,json}`. That is the test the kit says is worth more than the rest
of the suite, and it is the one to re-run first when anything in the submission path changes.

**Two substitutions, stated in the drills rather than hidden:** there is no Redis in this deployment (the cache
is in-process) and the product database is SQLite in dev/CI rather than Postgres. Drills 4 and 5 test *the
failure* against the equivalent mechanism; the Postgres failover behaviour itself is unverified here and belongs
on the deployment checklist, not in a green suite.

---

## D8. CI/CD gates

Measured by the phase gate: `p13-gate-check: 25 passed, 0 failed` — recorded in `docs/verification/P13-gate.txt`.

| Workflow | Trigger | What it runs |
|---|---|---|
| `.github/workflows/ci.yml` | PR + push to `main` | lint + lint-canary, OpenAPI audit + self-test, dependency scan, P07 gate, unit and integration tests, the envelope demo, every earlier phase gate, generated artefact drift, **the money matrix (`--check` and `--run`)**, **the P13 gate (`--skip-heavy`)**, a `web` job (typecheck, generated-client drift, vitest incl. the P13 suites) and the container/compose job |
| `.github/workflows/nightly.yml` | 03:17 daily + manual | the eleven chaos drills with `--execute-live`, the 100× kill loop, `p13-load.py --test all --pace`, the money matrix `--run`, the Playwright suite, then the phase gate over the night's records |
| `.github/workflows/release.yml` | `v*` tags | `make check`, the phase gate re-running the drills and the load clauses, the money matrix, then images labelled with the release version and a smoke order through the built stack |

* **Flaky-test policy**: `tests/quarantine.txt` — one test per line with its reason, an owner and an expiry
  (`@YYYY-MM-DD`). The gate fails on an entry without an expiry, and on the file being absent altogether (a
  quarantine with nowhere to write is how a suite stops being trusted).
* **Coverage is reported, never gated globally**, and the gate *fails* a workflow that adds `--cov-fail-under`.
  The money matrix is the only percentage in the system and it is 100 % of the enumerated rows.
* **"A green build that has not executed the money-path matrix is not a green build"** is why the matrix is in
  the PR path in both directions: `--check` (every row resolves to a test that exists) and `--run` (those tests
  pass). The gate's own canaries prove both halves can fail.
* **Release gate honesty**: the kit asks for "nightly green for 3 consecutive days" before a release. The build
  repo has no runner history, so this cannot be shown here; `release.yml` gates a release on the same evidence
  in one run — the drills, the load clauses, the matrix and a real container — which is what the clause is for.
  The three-day streak is a property of the CI account, and it becomes checkable the day the workflow runs.

---

## The phase gate

`tools/p13-gate-check.py` — because this phase's subject is the other gates, it is built around how a test suite
lies to itself, and **every section has a canary that feeds it a broken input and requires it to complain**:

1. every matrix row resolves to a collected test, and every artifact a row cites exists (canary: a row naming a
   test that does not exist);
2. the matrix, the contracts and the property sweep run green;
3. the recorded contract fixtures match the product (canary: a perturbed fixture must be refused);
4. every recorded chaos drill is PASS, its artifact exists, and its expected outcome was written before the run
   (canary: a FAIL in the index, and a drill with no expectation);
5. the load record: the quick run passes **and** the kit's 30-minute soak meets its clause — 1,800 s of wall
   clock, 200 fills/s, ≥ 360,000 fills, zero duplicate deliveries, ≤ 6 MB second-half RSS growth, alerts fired
   (canary: a 5-minute soak with 6,000 duplicates);
6. the frontend suites exist, cover the kit's flows, keep the Telegram webview profile and the layout claim, and
   are collected by the project's own `npm test` rather than only by this gate (canary: a missing suite);
7. the CI gates: a workflow per trigger, the matrix in the PR path, the drills and load in the nightly path, the
   phase gate in the release path, no global coverage threshold, quarantine entries with expiries (canary: a
   workflow that runs nothing, plus a quarantine entry with no expiry);
8. the floor: the lint rules fire on a planted violation, the OpenAPI audit passes, the SQLite subset is current,
   and the P12 gate still passes.

The first version of section 1 asserted a canary by comparing literals — which proves nothing. Rewriting it so
each section is a pure function over its inputs, fed a deliberately broken input, is the difference between a
gate and a decoration, and it is the reason this file carries more canaries than assertions.

---

## Constraints, and where each one is discharged

* **No test hits a live external API.** Tests replay the recorded corpus. The only network-touching drill is D7.2
  (a real WebSocket kill), opt-in, with its own artifact.
* **No test uses real keys.** Every token in the suite is a fixture; `tools/lint-rules.py` fails the build on a
  token-shaped literal in the tree, with a canary that proves the rule fires.
* **Every chaos test has a written expected outcome before it is run.** Mechanised: `EXPECT` in the suite, printed
  into the artifact above the observations, checked by the gate.
* **Nothing touches real funds until D7 passes against `executor-mock`.** It does (9/9 here, 100/100 on the kill
  loop). The canary and the user phase remain owner-gated, and P13 does not spend a cent.
