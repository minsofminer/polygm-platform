# P10 — Frontend: the terminal (tape, dossiers, whales, radar, portfolio, copy, automation, alerts)

Status: **in progress.** D1, D2 and D5's API are built and pushed; D3, D4, D6–D9 are the remaining screens. This
file is written as the phase is built, so what follows states what exists and what does not, and the "open" list
at the end is the working list rather than a retrospective.

The phase's acceptance sentence, from the kit, is the thing everything below is arranged around:

> a new user can find a whale fill in the tape → open the trader's profile → see the win rate is real (sample ≥
> gate) → see their drawdown → set up a copy config in dry-run → understand the slippage risk, without reading
> documentation.

`tools/p10-gate-check.py` walks exactly that sentence as `c9`, over the real API, in one script, because a chain
verified one endpoint at a time is a chain nobody has ever walked.

## 1. What this phase is made of

| # | Surface | State |
|---|---------|-------|
| D1 | three-column terminal: left rail, centre (chart + Activity/Traders/Holders), right rail, resizable and collapsible, per-user persistence, mobile as tabs | frame built (`web/src/terminal/TerminalLayout.tsx`), panels not all wired |
| D2 | live tape: filters (absolute **and** market-relative notional), classification badges with their rule, virtualised, coalesced at 20+ fills/s, "paused — N new", click → market, shift-click → watchlist, sound off by default | built (`web/src/terminal/TapePanel.tsx`, `tape.ts`, `useTerminal.ts`), 21 unit tests |
| D3 | trader dossier: four windows of one metric set, PnL curve with a mandatory drawdown overlay, behaviour labels with methodology + disclaimer, "insufficient sample" instead of a win rate below the gate | **built** (`src/terminal/{dossier.ts,DossierView.tsx}`, `app/trader/[anon]/page.tsx`), 21 unit + 7 render tests |
| D4 | whale tracker: threshold feed, saved views with channel/severity, per-market and global, severity formula stated, inline alert-rule creation | **built** (`src/terminal/{whales.ts,WhaleTracker.tsx}`, `app/whales/page.tsx`), 17 unit + 4 render tests |
| D5 | Wallet Radar: ≤10 markets, four rankings, row = wallet + matched markets + bought/sold + realised PnL + win rate + classification, one-click track/follow/copy/open, cost control | **API built and gated** (`packages/polygm_core/radar/rankings.py`, `POST /v1/radar/runs`, `GET /v1/radar/runs/{job_id}`), 25 tests; screen pending |
| D6 | portfolio: positions with mark and unrealised, negRisk groups, order history with `unknown` rows marked, PnL curve + benchmark, CSV export, empty state | API built and gated (`/v1/me/portfolio`); screen pending |
| D7 | copy trading: risk-adjusted discovery (and saying so), the config panel, the slip warning **before** confirm, monitor with skip reasons, pause/stop | API built and gated (`/v1/copy/configs*`); screen pending |
| D8 | automation: rule list, visual builder, templates, mandatory dry-run, run history, daily-loss banner | pending (P11's subject in the kit's own order; the API surface is not in the P10 contract) |
| D9 | alerts: rule list, inline editor, test-fire, delivery history, quiet hours, digest, per-rule cooldown | pending (same as D8) |

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

Still open, and stated rather than assumed: these routes **validate** the key but do not yet **record** it, so a
replayed key will create a second config rather than returning the first answer. `idem.begin/finish` still
covers `POST /v1/orders` only. Tracked in §5.

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

### 2.9 Freshness on every surface, including the ones that are wrong about it

`asOf` is the **age of the data** and `staleAfter` is derived from it; a no-store read may legitimately have
`staleAfter == asOf`, which is why the gate's `c8` fails on `<` rather than `<=` (and on `<=` only when the
cache TTL is above zero). The tape keeps its last good rows and renders them under a stale indicator rather than
emptying: an empty tape on one dropped request reads as "the market stopped", which is a different and worse
claim than "this is ten seconds old".

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

## 4. Where the numbers come from

* `docs/verification/P10-gate.txt` — the recorded gate run (10/10).
* `python3 -m unittest discover -s tests` — 771 tests, 25 of them the radar's, 42 the terminal API's.
* `cd web && npx vitest run` — 215 tests; the terminal owns 70 of them, file by file: `tape.test.ts` 21,
  `dossier.test.ts` 21, `whales.test.ts` 17, `DossierView.test.tsx` 7, `WhaleTracker.test.tsx` 4.
  `npm run i18n:check` — 474 keys, 0 missing, 0 dynamic.
* `python3 tools/check-openapi.py` — 287 passed, 0 failed, over a 36-path contract.
* Seeded tape (post `make migrate && make seed`): 159 markets, 1,090 fills, four wallets, 133 markets with ≥4
  fills.

## 5. Open, in the order it should be closed

1. **The record half of idempotency** (§2.5): `idem.begin/finish` on the four P10 mutations, with a test that
   replays a key and asserts one config exists.
2. **D6, D7 screens** — the APIs are gated; the screens are the phase's remaining work (`/portfolio` and `/copy`
   routes exist as shells from P08).
3. **D8, D9** (automation and alerts) — the kit puts the rule engine in P11's scope; the P10 screens depend on
   that API, so they land with it rather than against a surface that does not exist.
4. **A web-side gate check.** `c10` covers the radar's cost control; the D1–D9 components are covered by
   `vitest` and `npm run build` under `p08`/`p09`. A gate check that reads the terminal's own components (the
   60 fps claim, the resize persistence, the mobile tab parity) is the next one to add, and until then the
   60 fps requirement is **`[UNVERIFIED]`** — no browser has been in the loop.
