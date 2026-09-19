# P10 — Frontend: the terminal (tape, dossiers, whales, radar, portfolio, copy, automation, alerts)

Status: **D1–D9 built; all nine surfaces reachable** — `/terminal`, `/trader/[anon]`, `/whales`,
`/radar`, `/portfolio`, `/copy`, `/automation`, `/alerts`. D8 and D9 were built last, as this phase's closing
work, because the P10 screen list names them and a screen shipped against an API that does not exist is not a
screen. The earlier reading that put them in P11 was a misread of the kit: `prompts/P11-leaderboard.md` is the
leaderboard, rankings and referrals, and §5.1 below records that reconciliation. This file is written as the
phase is built, so what follows states what exists and what does not, and the "open" list at the end is the
working list rather than a retrospective.

The phase's acceptance sentence, from the kit, is the thing everything below is arranged around:

> a new user can find a whale fill in the tape → open the trader's profile → see the win rate is real (sample ≥
> gate) → see their drawdown → set up a copy config in dry-run → understand the slippage risk, without reading
> documentation.

`tools/p10-gate-check.py` walks exactly that sentence as `c9`, over the real API, in one script, because a chain
verified one endpoint at a time is a chain nobody has ever walked.

## 1. What this phase is made of

| # | Surface | State |
|---|---------|-------|
| D1 | three-column terminal: left rail, centre (chart + Activity/Traders/Holders), right rail, resizable and collapsible, per-user persistence, mobile as tabs | **built and mounted** (`TerminalLayout.tsx` + `TerminalScreen.tsx`, `/terminal`), 7 watchlist tests |
| D2 | live tape: filters (absolute **and** market-relative notional), classification badges with their rule, virtualised, coalesced at 20+ fills/s, "paused — N new", click → market, shift-click → watchlist, sound off by default | built (`web/src/terminal/TapePanel.tsx`, `tape.ts`, `useTerminal.ts`), 21 unit tests |
| D3 | trader dossier: four windows of one metric set, PnL curve with a mandatory drawdown overlay, behaviour labels with methodology + disclaimer, "insufficient sample" instead of a win rate below the gate | **built** (`src/terminal/{dossier.ts,DossierView.tsx}`, `app/trader/[anon]/page.tsx`), 21 unit + 7 render tests |
| D4 | whale tracker: threshold feed, saved views with channel/severity, per-market and global, severity formula stated, inline alert-rule creation | **built** (`src/terminal/{whales.ts,WhaleTracker.tsx}`, `app/whales/page.tsx`), 17 unit + 4 render tests |
| D5 | Wallet Radar: ≤10 markets, four rankings, row = wallet + matched markets + bought/sold + realised PnL + win rate + classification, one-click track/follow/copy/open, cost control | **built** (`src/terminal/{radar.ts,RadarView.tsx}`, `app/(app)/radar/page.tsx`), 14 unit + 4 render tests, on the gated API (25 API tests) |
| D6 | portfolio: positions with mark and unrealised, negRisk groups, order history with `unknown` rows marked, PnL curve + benchmark, CSV export, empty state | **built** (`src/terminal/{portfolio.ts,PortfolioView.tsx}`, `app/(app)/portfolio/page.tsx`), 17 unit + 6 render tests |
| D7 | copy trading: risk-adjusted discovery (and saying so), the config panel, the slip warning **before** confirm, monitor with skip reasons, pause/stop | **built** (`src/terminal/{copy.ts,CopyView.tsx}`, `app/(app)/copy/page.tsx`, `GET /v1/copy/sources` added for the sort D7 demands), 20 unit + 5 render tests |
| D8 | automation: rule list, visual builder, templates, mandatory dry-run, run history, daily-loss banner | **built** (`src/terminal/{automation.ts,AutomationView.tsx}`, `app/(app)/automation/page.tsx`), 14 unit + 4 render tests, on `GET/POST /v1/automations`, `/preview`, `/guards`, `/runs`, `/templates` (18 API tests) |
| D9 | alerts: rule list, inline editor, test-fire, delivery history, quiet hours, digest, per-rule cooldown | **built** (`src/terminal/{alerts.ts,AlertsView.tsx}`, `app/(app)/alerts/page.tsx`), 26 unit + 4 render tests, on `GET/POST /v1/alerts`, `/test`, `/deliveries`, `/settings` (20 API tests) |

## 2. Decisions taken here, and why

### 2.1 The tape's two thresholds are both real, and neither replaces the other

A fixed whale line is wrong in both directions: at a $5 median fill, $1,000 is a wall; in a market whose median is
$400, $1,000 is a rounding error. So a fill is a whale when it clears **both** the market's own relative line
(p99.5 of that market's window) and an absolute floor, and the row carries the sentence that produced its badge —
`whale = max(the p99.5 fill of this market's 900 fills ($150.00), $500.00 absolute floor) = $500.00: this
market's own fills set the bar`. The screen never re-derives that rule; it renders the server's sentence, so the
tooltip and the decision cannot drift apart.

Measured on the seeded tape: median fill **$5**, p95 **$133**, max **$3,000**. A fixed $1,000 line would have
flagged nothing in most markets and everything in one.

### 2.2 Every win rate is gated, and the gate says why

`SAMPLE_GATE = 20` settled markets. Below it, `winRateBps` is **null** and `sampleNote` says "insufficient
sample: 4 settled markets; a win rate needs 20" — not a percentage, and not silence. The radar's profit ranking
enforces the same gate at the list level: an under-sampled wallet is not ranked, and it is returned under
`unranked` with that sentence, because the failure mode of a gate is a shorter list nobody can explain.

### 2.3 The drawdown is not a chart option

`drawdown_overlay()` is the only function that produces a PnL curve, so every point arrives carrying
`peakMicro` and `drawdownMicro`; the terminal's `curvePoints()` draws the same series twice (the PnL line and the
distance below its high-water mark) and there is no code path that renders one without the other. The gate's `c3`
fails the build if a curve point is missing either field.

### 2.4 Cost control is part of the radar's contract, not an operations note

A scan is cached for 60 seconds; the cache key is **order-insensitive** (the same three markets in a different
order is the same scan); a cached scan is **not billed**, because the cache is the whole reason the second scan
is cheap; past six uncached markets the scan is enqueued and the job runs on its first poll, exactly once; and
the budget is counted from `audit_log`, which is append-only at the trigger level, so the code being counted
cannot adjust the count. Refusals are sentences: `RADAR_SCOPE` says the limit **and** the count ("radar scans up
to 10 markets; 11 were sent"), `QUOTA_EXCEEDED` says the plan, the numbers, and that a cached repeat is free.

Two consequences worth stating plainly:

* the schema-level `maxItems` was **removed** from the request body: FastAPI's 422 answers first with "outside
  the range", which is true and useless, and the app's own sentence is the one a user can act on;
* the async job runs **on the first poll** rather than on a worker thread. That is a deliberate trade for a
  single-process API: the request that asked for ten markets returns immediately, the work happens on a later
  request, and the second poll reads the completed job. P12's worker queue is where this plugs in, and the shape
  of the answer does not change when it does.

### 2.5 The Idempotency-Key hole this phase found

Writing the radar's tests exposed something the contract had been claiming for eight phases: the P10 POSTs
accepted a mutation with **no** `Idempotency-Key`, while `contracts/openapi.yaml` rule 1 says every mutating
endpoint requires one and answers 400 without it. `_idem_shape` treated `None` as "nothing to check", so the
header was decorative on exactly the routes where a duplicate costs money.

It now answers `IDEM_KEY_REQUIRED`; the four response tables and the four contract operations document the 400;
and each of the two paths that serves both a read and a write has **two** tables (the read is the write minus
the key-required answer), an invariant `tools/check-openapi.py` checks per verb. One table per path would have
forced one of the two verbs to declare a status it never returns.

The second half landed with the same review: the four routes now **record** the key. They run their work under
`_idem_run(uid, key, body, rid, work)`, which is the module's vocabulary and nothing else — a key reused with a
different body is `409 IDEM_CONFLICT` rather than a replay of an answer to a question nobody asked; a key whose
first request is still running is `409 IDEM_IN_PROGRESS` (tested through `Record.busy`, because `state ==
'in_progress'` is also true of the request that owns the row); and a replay returns the **stored** body verbatim,
stamp included, so a retry cannot disagree with the answer it is retrying.

Two things that shaped it. A refusal is **not** an outcome: when `work()` returns a `JSONResponse` from
`err(...)` — the 409 `REFUSED` on guards, the 429 `QUOTA_EXCEEDED` on a scan, the 404 on an unknown source — the
key is abandoned rather than stored, because a user who fixes the typo must be able to retry with the same key.
And an exception abandons it too, since an `in_progress` row left behind answers every later attempt
`IDEM_IN_PROGRESS` forever.

The split between `_idem_shape` and `_idem_run` is deliberate and documented in both docstrings: a malformed key
is a 4xx *about the request* and never touches the table; a well-formed key is a promise *about the outcome* and
always does.

Splitting the four handlers to make room for the wrapper is what broke them, and the way it broke them is worth
recording: each `_*_work` function was inserted **above** its thin handler, so `@app.post` decorated the work
function and FastAPI read `(rid, uid, body)` as required **query** parameters. Every call 422'd with
`(query.rid, query.uid, query.work)`, and the only visible symptom was five new tests failing on a validation
error that named fields no client had ever heard of. The lesson is cheap to state and was expensive to learn: a
decorator and its handler are one unit, and the way to check is to ask the app which function each route points
at, before running any test.

### 2.6 Money never becomes a float, in either direction

The wire sends money as decimal strings (rule 2) and micro-integers where the arithmetic is defined against
micro. `web/src/money/cents.ts` gained `microToCents`/`centsToMicro` so the terminal converts in one place, and
`tape.ts` parses share strings by string arithmetic (`sharesMicro`) rather than `Number(...)` — a size that
round-trips through a double is a size the number layer renders wrong. The gate's `c7` scanner reads the
terminal's own source for float literals in the money path and the radar test asserts no JSON float appears in
any scan payload.

### 2.7 A classification label's rule is text, not a tooltip

The gate's rule — every classification label carries a visible rule and disclaimer — is enforced by rendering
them: the badge chip is a chip, and the sentence (`rule · disclaimer`) is text on the screen, both on a tape row
(when its chip is expanded) and on every whale row. The `title` attribute survives as a convenience for a pointer;
it is not the disclosure. A label screenshot without its caveat is how "wash roundtrips" becomes an accusation,
and the render tests assert the caveat is present as text.

The same rule is why a label whose fact arrives without a `rule` or without a `disclaimer` is **dropped** rather
than rendered as a bare chip.

### 2.8 The header says what it does not know

D3's kit text asks for "address + copy + explorer". This product is pseudonymous by construction — the API's own
gate check (`c2`) fails the build if a `0x…` address appears in any P10 payload — so the header offers the
pseudonym to copy, states in the same breath that a pseudonym is not resolved to an address, and carries no
explorer link. That is a deviation from the kit's wording, taken deliberately and stated on screen: an explorer
link here would either 404 or invite a user to look up an address the product deliberately does not hold.

### 2.9 `params` fills `{placeholders}`; `query` is the query string

`useCopySources` passed its filters as `params` on a route with no `{segment}`, and `urlFor` throws when a param
has nowhere to go. The throw happened inside a `void load()`, so the rejection vanished: the screen showed an
empty list and no error — the worst pair of symptoms, because an empty list looks like data. Two fixes, and the
second is the one to remember: the call site uses `query`, and the hook now catches and reports, because a
rejected read must reach the screen. Its sibling is the shadowing trap `whales.ts` already documented: `Number`
in a file that imports the number layer is the component, so `Number.parseInt` is a type error, not a parse.

### 2.10 D7's discovery sort needed an endpoint, so it got one

D7 says the discovery list's default sort must be risk-adjusted and must say so. The radar ranks by activity,
profit, earliness and overlap; the risk-adjusted figure existed only inside a config's `sourceStats`, which is
only visible *after* picking a source. The phase's first draft would have shipped a list that could not honour
its own acceptance sentence. `GET /v1/copy/sources` closes it: rows from `copy_source_stats`, default sort
`riskAdjusted`, the denominator and the division stated per row, the sample gate on the win rate, and the
negative window returned as it is. The seed grew two sources so the claim is testable — the gambler has the
largest net PnL and the worst risk-adjusted number, and `TestDiscovery` asserts it is not rank 1.

### 2.11 Freshness on every surface, including the ones that are wrong about it

`asOf` is the **age of the data** and `staleAfter` is derived from it; a no-store read may legitimately have
`staleAfter == asOf`, which is why the gate's `c8` fails on `<` rather than `<=` (and on `<=` only when the
cache TTL is above zero). The tape keeps its last good rows and renders them under a stale indicator rather than
emptying: an empty tape on one dropped request reads as "the market stopped", which is a different and worse
claim than "this is ten seconds old".

### 2.11 The dictionary is split by route family, because the measurement said so

Re-recording the P08 first-load artefact once the phase's screens had landed came back **204.4 KB against a
200 KB budget, on `/markets`** — a route that renders no terminal surface at all. The cause was the dictionary:
`en.ts` is one object that every component calling `t()` imports, so all 664 keys sat in the graph of every
route, and the terminal's copy was **319 of them**. The tape's sentences, the radar's refusals, the copy engine's
warnings: all of it was shipping to a page that never renders a fill.

So the dictionary is two files. `en.ts` holds the shell, auth, wallet, market and event copy; `en.terminal.ts`
holds the 319 `terminal.*` keys, and `src/i18n/terminal.ts` registers it and re-exports `t`. A terminal file
imports `t` from there, and that import is the edge that pulls the bytes into its route's graph — the same
mechanism that keeps the money layer off the landing document. Measured after the split: `/markets` 199.1 KB,
`/`, `/tma` and `/profile` 188.5–191.6 KB, all under budget, route-level splitting still proven.

`scripts/i18n-check.mjs` grew the rule that keeps it from rotting: a `terminal.*` key asked for by a file that
imports `@/i18n/t` **fails the build**, because that file would render the key itself — a missing key's failure
mode reached from the other direction. Its `--self-test` plants exactly that case alongside the two it already
planted, and the check reports the same shape as before (`ok (664 keys, 621 used, 43 unused-advisory)`) because
the two files are one key space.

**And the measurement tool had the bug this phase keeps finding in other people's code.** The first re-record
said 178.9 KB and "money layer absent" for every route, which is not what the build says. A `next start` from an
earlier run was still holding port 3111; `waitReady()` asked the port, got an answer, and measured *that*
process's build — the one from before the change. A tool whose whole job is to describe the tree in front of it
was silently describing a tree that no longer existed, which is the same class of failure as the stale bundle
artefact P09's c7 has a rule about. `measure-first-load.mjs` now probes the port before spawning and refuses to
measure a server it did not start.

### 2.12 The 60fps line, measured as far as this environment allows

`npm run measure:tape` drives the tape's real functions — `coalesceFills`, `arrivalRate`, `applyFilters`,
`virtualWindow`, imported through esbuild rather than re-implemented — at **200 fills/second for 10 seconds**,
and writes `docs/verification/P10-perf.txt`. Measured: **2.134 ms of JavaScript per second of load** against a
16.7 ms frame, worst second 3.554 ms, worst single batch release **0.884 ms**, 33 rows mounted for a 700px
viewport out of 400 buffered.

What that is: the JavaScript half of the frame budget, met with about an order of magnitude of headroom, plus
the structural bound that makes it believable (batching above 20 fills/s, a 400-row buffer, a virtual window that
does not grow with the buffer). What it is not: a rendering measurement. Paint, layout and compositing are absent
from that number, and **no browser exists in this environment** — a Playwright Chromium download fails its
host-requirements check, which is why P08's numbers are byte counts too. The artefact says both things in its own
text, and the gate's new `c11` parses the caveat: an artefact with numbers but no caveat fails, so the file cannot
quietly upgrade itself into a rendering claim. `src/terminal/perf.test.ts` asserts the same budgets inside
`npm run test`, which is what makes a regression fail a build rather than a review.

### 2.13 D8/D9: the four decisions the closing work turned on

**A rule is saved as a dry run and there is no field that makes it live.** `POST /v1/automations` writes
`enabled = 0` and the API's create response says `dryRunOnly: true`; arming is a second endpoint
(`POST /v1/automations/guards`) that refuses without a `dry_run_completed_ms` the engine itself wrote, with
`DRY_RUN_REQUIRED` and the next step named. Pausing is always allowed, because the safe direction must never sit
behind a precondition. The kit's "dry-run mandatory" is therefore not a UI convention that a client could skip:
the only path to `enabled = 1` runs through an evaluation that was actually recorded.

**The status order is the product decision.** A halted rule reads `halted` even when its `enabled` flag is 1 —
the risk service stopped the account, and rendering that as "active" would hide the one state D8 asks to be loud
about. A rule with no completed dry run reads `dry_run` whatever the flag says, because that is the flag the
engine reads before it will fire. `paused` names who parked it. Every one of the four carries its own sentence,
and a rule that has never been evaluated says so rather than showing a zero.

**The cooldown is the window budget, not a second setting.** `3 per 1h` and "one every 20m at most" are the same
fact stated twice from one source (`window_ms / fires_per_window`), the list shows both plus how many fires the
current window has spent, and the gate asserts the sentence is on every rule. A separate "cooldown" field would
be a second authority over the same number, and the first disagreement would be invisible.

**Quiet hours are the user's instruction; a digest is our batching — and only an `urgent` alert outranks them.**
Held (`quiet_hours`), batched (`digest`) and refused by the rule's own cap (`rate_limited`) are three different
rows in the delivery history with three different sentences, and a row that was never sent has no latency. The
test-fire path writes its rows under `test:<ruleId>` and spends nothing, which the artefact says in as many
words: a test that consumed the budget it was testing would poison the feature it validates.

## 3. What the phase's own tools caught

* **`_market_rows()` returns a dict keyed by condition id.** The radar iterated it as a list and every scan was a
  500 (`TypeError: string indices must be integers`). Found by the radar's own tests, fixed by keying on
  `marketId` — and worth recording because the function's shape is the tape's lookup, so the next caller will
  trip over the same thing if it is read as a list.
* **The test that spent the budget it was measuring.** The quota test burned the fixture account's day, so every
  later test in the class saw a 429 — a test-ordering bug wearing a product failure's clothes. The test now owns
  an account.
* **A gate probe that measured its own bug.** After the Idempotency-Key fix, `c9` created a copy config without a
  key, got the new 400, and reported "the config is not a dry run". The probe now sends a key, as a client must,
  and `c10` is the one place that deliberately sends none.
* **Four tabs, one reason.** The first radar draft wrote each ranking's `reason` onto shared row objects, so the
  last ranking to run overwrote every sentence. Each ranking now gets its own copies.
* **A panel that rendered its own keys.** The tape's copy was written as `t(`${ROW_SAID}.label`)`, and the i18n
  check refuses a computed key precisely because it cannot verify one — the keys did not exist in the dictionary,
  so every string in the panel rendered as `terminal.tape.row.label`. Each terminal component now holds a literal
  table of its copy, and the dictionary grew 129 entries written with the components that ask for them.
* **The Makefile promised a check it did not have.** The `p10` comment cited a web-side `c10` while the gate had
  nine checks. `c10` now exists and is about the radar's cost control; the comment says what the gate actually
  does, and the web half is named as `npm run test` / `npm run build`.

* **A rule with no loss ceiling was a 500, not a refuse.** The new gate check (`c12`) saved the smallest legal
  rule it could, left `maxLossMicro` out, and got `INTERNAL` — because `automation_rule_policy` has a CHECK
  (`redemption_needs_no_ceiling`, exempting only `auto_redeem`) and the ceiling was being read after the insert
  path had already begun. The form refused a zero ceiling client-side, so the *server* was the looser of the two,
  which is backwards: the write now refuses with `VALIDATION` naming `maxLossMicro`, and
  `test_a_rule_with_no_loss_ceiling_is_refused_not_saved_with_zero` pins both directions (refused for an exit
  rule, saved for `auto_redeem`).
* **Two D9 paths were in the contract but outside the status-set table.** `c1` compares the phase's paths against
  `tools/check-openapi.py`'s `TABLE_FOR_PATH`, and its membership test only recognised the bare-key spelling —
  the alerts paths are keyed `("POST", "/v1/alerts")` because a GET and a POST on one path answer different
  status sets, so two paths read as unguarded. The test now accepts either spelling; the paths were always
  guarded, the check was wrong about how to tell.
* **The D9 list and the D9 write disagreed about the settings shape.** The list returned the raw row while the
  write returned the row plus `quietHours`/`digestNow`, and the screen reads `settings.quietHours.note` on both.
  One `_settings_view(uid, at)` now serves both reads, with `test_the_list_carries_the_settings_shape_the_screen_reads`
  as the regression — found by a render test, which is the argument for having render tests.
* **A probability was being rendered through the money helpers.** `formatCents(microToCents(620000))` produced
  `0.0062`: cents are hundredths of a dollar, and a price of 0.62 is not money. `priceMicroText`/
  `priceMicroFromText` do the integer arithmetic instead and return `null` on garbage; no probability may go
  through the cents path again.

## 4. Where the numbers come from

* `docs/verification/P10-gate.txt` — the recorded gate run, **15/15**, `--self-test` **12/12 canaries fired**.
* `docs/verification/P08-gate.txt` — **15/15** (the bundle and money-path checks over the same tree);
  `docs/verification/P09-gate.txt` — **7/7**.
* `python3 -m unittest discover -s tests` — **822 tests, OK**, of which the terminal's are 25 (radar), 54
  (terminal API), 18 (automation API), 20 (alerts API) and 88 (copy automation), plus the engine's own automation and
  signal tests under `packages/polygm_core`.
* `cd web && npm test` — **339 tests in 37 files**: `tape.test.ts` 21, `dossier.test.ts` 21, `whales.test.ts` 17,
  `radar.test.ts` 14, `portfolio.test.ts` 17, `copy.test.ts` 20, `automation.test.ts` 14, `alerts.test.ts` 26,
  `watchlist.test.ts` 7, `perf.test.ts` 3, plus the render tests `DossierView` 7, `WhaleTracker` 4, `RadarView` 4,
  `PortfolioView` 6, `CopyView` 5, `AutomationView` 4, `AlertsView` 4. `npm run typecheck` — clean.
  `npm run i18n:check` — `ok (825 keys, 782 used, 43 unused-advisory)`.
* `python3 tools/check-openapi.py` — **365 passed, 0 failed** — 365 comparisons over a 46-path / 50-operation contract, every D8/D9 path
  among them (`TABLE_FOR_PATH` keys the alerts pair by verb, because a GET and a POST on one path answer
  different status sets).
* `cd web && npm run measure` — first-load 188.5 / **199.6** / 191.6 / 191.6 KB, `status: pass`, route-level
  splitting proven (the landing document does not fetch the money module; `/markets` does). Budget 200 KB, so
  `/markets` is 0.4 KB under it — worth knowing before the next surface adds a chart library.
* `cd web && npm run measure:tape` — `status: pass`; the gate's own re-read of it: **1.729 ms per second of load
  at 200 fills/s**, worst single batch release 0.133 ms, against a 16.7 ms frame.
* Seeded tape (post `make migrate && make seed`): 159 markets, 1,090 fills, four wallets, 133 markets with ≥4
  fills.

## 5. Open, in the order it should be closed

1. **The pixel half of the 60fps line, and the rest of the browser pass.** `c11` reads a measured budget for the
   tape's own JavaScript and `vitest` fails when it regresses. Paint, layout and compositing are still
   **`[UNVERIFIED]`**: the sandbox cannot run a browser (Playwright's host requirements fail to validate), so
   resize persistence, mobile tab parity and the perceived frame rate are claims about code, not measurements.
   The first machine with a browser closes this; nothing else in P10 does.
2. **The bundle record and the shipped web build are one artefact again**. `docs/verification/P08-bundle.txt` is
   re-measured against the current tree (it was stale for D8/D9 for exactly the reason the check exists: the
   measurement described a build that no longer existed) and `P08-gate.txt` is re-recorded at 15/15.
3. **A real transport for the alert channels.** No delivery transport runs in this build, and the API says so on
   every surface that could imply one (`the rule's own window was not spent`, `no delivery transport runs in this
   build`). The Telegram bot plumbing is P06's and is verified there; wiring it to `alert_deliveries` rows is the
   step that turns `queued` into `sent`, and it belongs with the executor's live path.
4. **The line-by-line reviewer's pass over D8/D9's copy.** Every rule this phase states in words — the cooldown
   sentence, the fee arithmetic, the halt banner, the "would" language of a test fire — is asserted by a test, but
   the *tone* is not something a test can hold.

### 5.1 The reconciliation owed against the kit's P11

D8 and D9 were built here as P10's closing work, and that needs to be said plainly rather than left as a note in
a status line, because a phase boundary that moves without a reason is how two phases end up half-built.

* `prompts/P10-frontend-terminal.md` lists **eight** deliverables, D8 (automation) and D9 (alerts) among them,
  and its screen list names the two routes. `prompts/P11-leaderboard.md` is titled **"Leaderboard, Rankings &
  Referrals"** and asks for six boards, integrity rules, self-rank, referrals and public shareable pages. There is
  no automation or alert deliverable in it.
* So the earlier reading — "D8/D9 are P11's subject, do not invent the API here" — was wrong twice over: wrong
  about the kit, and wrong about the consequence. Deferring them would have left P10 with two screens missing and
  P11 with two deliverables it never asked for.
* What P11 now is, unchanged by that: risk-adjusted PnL as the default board with the formula stated, win rate
  behind a sample gate, volume, rising 7d, category specialists, copied; integrity work (wash and copy-farm
  filtering, a provisional label under 7 days, blown-up accounts shown rather than dropped, the lucky-gambler
  share, UMA-disputed markets excluded); a rank badge and a 30-day sparkline; a private self-rank; referrals with
  a first-matched-order trigger, device/IP/funding dedupe, clawback and a hard self-referral block; SSR
  `/trader/<handle>`, `/market/<slug>` and `/leaderboard/<board>` with OG tags; and an anti-gaming dashboard.
  Its gate is to explain why rank 47 with fewer resolved markets is correct above rank 12, and to catch a
  second-wallet referral.
* Two things in this phase's own table are load-bearing for P11 and already exist because of it: the gate's
  `win_rate_findings`/`threshold_findings` scanners (a board that prints a win rate under the sample gate fails
  the gate that P11 will be held to as well), and `GET /v1/radar/...`'s row shape — wallet, matched markets,
  bought/sold, realised PnL, win rate with its gate, classification with its rule — which is the same row a
  leaderboard needs and should be extended rather than re-derived.
