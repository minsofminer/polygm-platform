# P11 — Leaderboard, Rankings & Referrals

Status: **D1 and D2 built.** D1 is the specification, the integrity rules and the ranking engine (17 unit tests);
D2 is the rankings API, the population the boards are demonstrated on, and the read path they are ranked from
(20 + 28 tests, and `check-openapi` at 405/0). D3–D7 are next: the profile integration, self-rank and privacy,
referrals, the public SSR pages, and the anti-gaming dashboard. This file is written as the phase is built.

The phase's acceptance sentence, from the kit, is the thing everything below is arranged around:

> Show me: a trader at rank 47 who has fewer resolved markets than the trader at rank 12, and explain why the
> ranking is still correct. Then show me a referral attempt from a second wallet funded by the first, and show it
> being caught.

The first half is a property of the engine, not a screenshot: the sample size decides *eligibility*, never order,
and `rank.explain()` prints the two component sets side by side. `tests/test_leaderboard_rank.py` asserts both
directions of it (a smaller sample above a larger one, and below), because the interesting case — fewer markets,
better rank — is the one a user reports as a bug.

## 1. What this phase is made of

| # | Deliverable | State |
|---|-------------|-------|
| D1 | leaderboard specification: six boards, formula per board, eligibility gate, windows, tie-breaks, recompute cadence; integrity rules (wash, copy farms, provisional, blown-up, lucky-gambler share, disputed markets) | **built** — `packages/polygm_core/leaderboard/{boards,integrity,rank}.py`, 15 unit tests; `0013_leaderboard.sql` + its SQLite twin |
| D2 | the rankings API: boards, rows with components, unranked-with-reasons, methodology, snapshots, the worker's recompute | **built** — `GET /v1/leaderboard{,/boards,/methodology,/why,/snapshots,/runs}` + `POST /v1/leaderboard/recompute`; `leaderboard/source.py`; `services/api/seed_leaderboard.py`; 20 + 28 tests |
| D3 | profile integration: rank badge, 30-day rank sparkline, "why this rank", follow/copy from the row, compare up to 3 | not started |
| D4 | self-rank: your own position on every board, pinned when off-page, the unranked state, private-by-default with an opt-in | not started |
| D5 | referrals: link + short code, the reward model and its argument, Sybil defence (first matched order over a notional threshold, device/IP/funding dedupe, velocity limits, review queue, clawback, hard self-referral block), the referrer dashboard, payout terms | not started |
| D6 | public SSR pages: `/trader/<handle>`, `/market/<slug>`, `/leaderboard/<board>` with OG images, structured data, rate limiting | not started |
| D7 | anti-gaming dashboard: fast climbers, correlated clusters, synthetic referral chains, unusual builder-code attribution; exclude and flag in one click | not started |

## 2. Decisions taken here, and why

### 2.1 Volume alone is not a ranking, and the default board says what it is

Polymarket already publishes an all-time volume board, and the kit's instruction is blunt: ours must be better or
nobody uses it. Volume rewards churn — a wallet that round-trips its own money looks like a whale — so the default
board is **risk-adjusted PnL**, and the volume board counts only **verified** turnover (round trips subtracted,
with the subtraction printed on the row).

The default score is:

```
score = net realised after fees EXCLUDING the single best market
        ────────────────────────────────────────────────────────
        max(largest peak-to-trough drawdown, 2 × σ of per-market results)
```

Every term is there to answer a question a user will ask:

* **excluding the single best market** — one 100× bet must not carry a placing. The exclusion is *uniform* (it
  applies to every wallet, so no threshold decides who gets trimmed) and it is skipped when the best market was a
  loss, because then there is no lucky win to remove and trimming would punish a losing wallet twice. The row
  publishes that market's share of raw PnL, so a reader can see the 62%-from-one-trade wallet for themselves.
* **`max(drawdown, 2 × σ)`** — the risk that happened, floored by the risk the ledger shows was typical. Stated as
  a `max` rather than a sum so it **reduces to the rule the /copy discovery list already sorts by** (net after
  fees per unit of drawdown) whenever volatility is the smaller term: two surfaces ranking the same wallets must
  not disagree about what risk means.
* **per-market results, not per-fill** — a wallet that traded the same market forty times made one decision.
* **integers only** — the variance is `(n·Σx² − (Σx)²) / n²` and σ is `isqrt` of it. A float here would let a
  rounding difference decide an ordering, and the ordering is the product.

### 2.2 The gate: N = 20 settled markets, M = $500 verified lifetime turnover

The kit asks for N and M justified from the observed data, with the observation that a wallet can rack up "trades"
cheaply. Both halves are needed because they fail differently: a resolved-market count can be manufactured with
$1 positions, and turnover can be manufactured by churning one position.

* **N = 20 settled markets**, which is `terminal.metrics.SAMPLE_GATE` — the same gate every win rate on the
  platform already waits for. A second, leaderboard-only gate would be a second answer to "is this a rate or a
  coin flip", and the two would drift. It is a *shared constant*, not a copied one: `boards.MIN_RESOLVED` is
  `metrics.SAMPLE_GATE` by reference.
* **M = $500 verified lifetime turnover.** On our seeded tape (1,090 fills, median fill **$0.02** [measured]), $500
  is 25,000 fills; on the venue median the kit cites ($5 [CTX]) it is 100 fills. The number is a judgement and is
  stated as one, with its sensitivity, because both readings are in the data. **Re-derive it on production data
  before launch** — it is on the D2 launch list, and the constant is in one place.

The floor is deliberately low enough not to be a status gate and high enough that cheap trades cannot buy
eligibility. A wallet under either half is not hidden: it appears in `unranked` with the number that refused it
("3 settled markets; this board needs 20").

**A 24-hour skill board does not exist.** Skill boards run 7d/30d/90d/all, which is what `trader_metrics` is
computed for; ranking a win rate on a 24-hour sample would print a number with no sample behind it and call it a
leaderboard. The activity boards (`volume`, `rising`) do use 24h/7d, because a day's volume is a fact regardless
of whether a day's PnL is a signal.

Tie-breaks are total and deterministic: score, then settled markets, then smaller drawdown, then wallet id
ascending. The same data always produces the same board, which is what makes "I was 47th this morning" verifiable
rather than remembered.

### 2.3 Integrity: six rules, each with what it does *not* do

`boards.INTEGRITY_RULES` is served as data (D7's dashboard renders the same list beside the wallets it flags), and
each rule carries its own "doesNot" because a filter that hides a wallet is a different product from one that
labels it. The short version:

* **wash / round trips** — opposite side, same market, inside **10 minutes** (the payout gate's own window, imported
  from `security.abuse`, so "round trip" means one thing across the product), prices within **50 bps**. Subtracted
  from volume everywhere, printed on the row. The tolerance is what separates a wash from a scalp that caught a
  move; a rule that flagged both would be unusable.
* **copy farms** — mirrored market and side within 2 minutes on ≥80% of a wallet's fills (≥10 fills), and the
  candidate must *lead* the fill: the flag says "derived from", never "fraud".
* **provisional** — under 7 days, labelled with its age, removed from nothing (it is barred from the rising board's
  placing, because a 3-day-old wallet has no 7-day history to have improved on).
* **blown up** — was above zero, is at or below zero now: keeps its real (negative) score, stays on the board, is
  counted in the board's own summary. A wallet that has only ever lost is not "blown up", it is a losing wallet.
* **the lucky gambler** — the trimmed numerator above, plus `bestTradeShareBps` on every row.
* **disputed markets** — results from markets on the risk blocklist with an active `uma_dispute` entry are
  *withheld and counted*, never zeroed: the outcome of a disputed market is unknown, and unknown is not zero.

### 2.4 The specification is served, not transcribed

`boards.methodology()` returns the boards, their formulas, gates, tie-breaks, cadences and the integrity rules —
the same object `rank_board()` reads. D2 serves it at `/v1/leaderboard/methodology`. A methodology page maintained
by hand beside a ranking engine maintained in code is two authorities over one number, and the first disagreement
is the one users screenshot.

### 2.5 A board is ranked from the ledger on every read, and the snapshot is only the sparkline

`GET /v1/leaderboard` computes the board when it is asked, not from a cached table. At our size that is one pass
over the fills we hold; the alternative is a board that can disagree with the ledger it claims to summarise, and
the disagreement would be found by the first user who reconciles their own trades. `leaderboard_snapshots` — the
table 0013 exists for — is written by the recompute and read by exactly one surface: the 30-day rank sparkline,
which is a question about the past. A rank recomputed from today's rows is *today's* rank, not the rank somebody
held last Tuesday, and "were they falling?" cannot be answered by a recompute at all.

The price of that choice is stated in the response rather than hidden: `freshness.source` is `live`, and
`freshness.ageMs`/`stale` compare the last snapshot against the cadence the board declares in `cadenceMs` — so
"is this stale" is a comparison, not a reading of the word "hourly".

### 2.6 The read plan: four boards, four windows, and one of them is fourteen days

`source.read_plan()` decides which rows a board sees, and the four answers are different on purpose:

* **volume** reads the window's own fills. The window is the fact it ranks, so a round trip that happened
  outside it is not volume inside it.
* **every skill board** reads *lifetime* fills, because its eligibility floor is written in lifetime turnover.
  A windowed read would make the same wallet eligible on the 7-day tab and ineligible on the 90-day one, which is
  a leaderboard that changes its own admission rule when you click.
* **rising** reads fourteen days of settled results. Its metric is "the last 7 days minus the 7 before", and a
  seven-day read makes the second half of that subtraction structurally zero — a wallet that improved by $5,000
  would be shown improving by $5,000 because it had nothing to improve *from*.
* **the refusal is honoured**: asking a skill board for `24h` is a 422 naming the window list, not a silent
  substitution of the board's default. There is no 24-hour skill board (§2.2), and a server that quietly answers
  a different question is worse than one that says no.

The plan travels in the response (`readPlan`), because "which rows was this rank computed from" is the first
question in any ranking dispute and a plan that only exists inside a WHERE clause cannot be shown to the person
asking.

### 2.7 `unranked` is part of the response, not the absence of one

Every board returns the wallets it refused with the number that refused them: "9 settled markets; this board
needs 20", "verified turnover 8400000 micro is below the 500000000 micro floor", "fills are mechanically derived
from w_…, so it cannot rank as copied demand". The population fixture contains both refusal kinds deliberately,
because they fail differently — a market count can be manufactured with $1 positions and turnover can be
manufactured by churning one position — and a refusal row is still a row about a wallet, so it carries the wash
subtraction too.

### 2.8 Exclusions are applied, and the reasons are not published

`leaderboard_exclusions` is replayed newest-first per (wallet, board): `exclude` removes a wallet from that board
(and from every board when the row's board is empty), `include` puts it back, and `flag` does *not* remove
anything — a flag is a question for a human, and a leaderboard that dropped flagged wallets would be hiding them
instead of having them reviewed.

The public response carries `excludedTotal` (the board's own totals have to add up) and nothing else; the list
with its reasons is returned only to a caller the app recognises as an operator. A public wall of shame is a
different product from a leaderboard, and the decision about who is out is a decision with an actor and a
timestamp on it — which is exactly why it is a row in an append-only table rather than a DELETE.

### 2.9 The heading is a claim about the order, so a board is ordered by the field it is named after

Six boards means six different orders, and the failure mode is quiet: a "Win rate" board whose rows were sorted
by the risk-adjusted score looks entirely correct row by row, and its heading is the only thing that is wrong.
`rank._sort_key` therefore has a branch per board — `winRateBps` for `win_rate`, `verifiedVolumeMicro` for
`volume`, `improvementMicro` for `rising`, `copiers` for `copied`, `scoreBps` for the two skill boards — and the
gate re-derives the ordering from the served rows (`c14`) instead of trusting the branch list. It also checks the
cheap disproof the fixture can provide: the win-rate board's order must differ from the default board's, or the
two boards are one board wearing two labels.

The win-rate branch is the one that needed a rule rather than a comment: `winRateBps` is `null` under the sample
gate, and a null is not a win rate of zero, so a row that never cleared the gate sorts last on that board. In
practice the gate refuses those rows before they are ranked (`MIN_RESOLVED` is the same 20), so the branch is a
floor and not a policy.

### 2.10 `settledMarkets` is the sample the row's own win rate came from — on every board

A win rate is meaningless without the sample it was taken over, and the product rule ("every win rate is behind a
sample gate") is only enforceable if the row that carries the rate carries that sample. So the number has one
meaning everywhere: **the markets the row's own win rate and score were computed from.**

That required two corrections, and both were found by reading the served rows rather than the code:

* the **category board** scored a specialist on their markets *in that category* and printed their markets in
  *every* category beside the rate — 40 markets under a Politics win rate taken over 21;
* the **rising board** read fourteen days (its metric is a subtraction of two weeks) and printed the seven-day
  count under a rate taken over the fourteen-day one.

Now the category row is sampled on its category's own markets (`categorySettled` still publishes the slice it came
from), and the rising row publishes `windowSettledMarkets` — the seven-day half — beside the fourteen-day sample
the rate is over. The gate re-derives every rate from the row (`wins / settledMarkets`) and fails when a row
serves a rate over a sample it does not print.

### 2.11 The read plan carries the instant it was derived at

`readPlan.atMs` is not decoration: a window start is `at − window`, so a plan with a window start and no "as of"
cannot be checked against anything — a reader a second later would find the arithmetic off by a second and would
have no way to tell a rounding difference from a bug. The gate compares each board's floors against the plan's own
clock (`c9`), and the volume board's rows state `volumeWindow` so that "ranked by volume" can never silently mean
"ranked by volume as of a different period".

### 2.12 The run record is keyed by the window — plus the category, for the board that is four boards

`leaderboard_runs` is keyed `(board, window, hour)`, and the category board is four boards in one. Without the
category in the key the four runs collapse into one row per hour and the cadence record says we recompute a
quarter as often as we do. The key travels into the API as the row's `window` (`30d:Politics`), which is what
`/v1/leaderboard/runs` serves.

### 2.13 Idempotent per key, bucketed per hour

`POST /v1/leaderboard/recompute` is the worker's job, and it is callable by an account: deterministic, idempotent
per `Idempotency-Key`, and — the reason it is a route rather than an internal function — a gate that has to reach
inside the process to prove the cadence works is a gate that never runs. `leaderboard_runs` is keyed by
(board, window, hour), so a second recompute inside the same hour *replaces* that hour's row rather than
inventing cadence we did not have.

## 3. Where the numbers come from

* `tests/test_leaderboard_rank.py` — **20 tests, OK** (`python3 -m unittest discover -s tests -p "test_leaderboard_rank.py"`).
* `tests/test_leaderboard_source.py` — **20 tests, OK**: the windows, the realised arithmetic (both sides, both
  outcomes, and the unresolved case that is NOT a zero), per-market folding, determinism under a reversed read,
  and the two refusal paths.
* `tests/test_leaderboard_api.py` — **28 tests, OK**, including the gate's own pair. The population it runs on:
  `services/api/seed_leaderboard.py` writes **60 generated wallets + 5 specimens** — a lucky gambler, a blown-up
  account, a washer, a copy farm, a three-day-old wallet — plus two wallets built to be refused for different
  reasons (9 settled markets; 21 markets of forty cents). Measured on that population: **64 ranked**, 7 unranked,
  **5 blown up**, 1 provisional, 1 disputed result withheld, and the gate pair at ranks 12/47 has **48 versus 26
  settled markets** — the smaller sample ranked *below*, and the sentence explaining it is served by `/why`.
* `tools/check-openapi.py` — **405 passed, 0 failed** over 53 paths (the contract gained the seven leaderboard
  paths and 22 components).
* `tools/p11-gate-check.py` — **14/14 checks, 8/8 scanners canaried**, recorded in `docs/verification/P11-gate.txt`.
  c3 is the phase's own acceptance sentence walked over the API (rank 47 with 26 settled markets above rank 12
  with 48, and `/why` saying so in one sentence), and the scanners that make it a floor rather than a screenshot
  are canaried: each one is handed a planted violation and fails the run if it walks past it.
* The whole backend suite: **890 tests, OK** (`python3 -m unittest discover -s tests`).
* `db/migrations/0013_leaderboard.sql` + `db/migrations-sqlite/0013_leaderboard.sql` (generated) — the portable
  subset executes: **106 tables, 54 triggers**, with `leaderboard_exclusions` append-only in both.
* Seeded tape measurement used for the gate's justification: 1,090 fills, median fill $0.02, p90 $137.50
  [measured: `db/seed.sql`].

## 4. Open, in the order it should be closed

1. **D3–D7** as listed in §1.
2. **The eligibility floor re-derived on production data** ($500 and its $0.02-median sensitivity are a judgement,
   §2.2), plus a real **category taxonomy**: `seed_leaderboard` invents four strings (Politics/Sports/Crypto/
   Finance) because the venue's own tags are not in our ingest yet, and the category board is only as meaningful
   as the tags it groups by.
3. ~~A leaderboard seed population.~~ **Closed by D2** — `services/api/seed_leaderboard.py`, opt-in via
   `python3 services/api/seed_leaderboard.py --sqlite` so the other phases' fixtures do not grow by two thousand
   markets. It is deliberately outside `db/seed.sql`: the phases that count things (the tape's page, the whale
   hour's 400 fills, the copy monitor's history) must not start failing for reasons unrelated to them. The one
   adversarial case still missing is the second-wallet referral, which arrives with D5's tables.
4. **The rank-history table is written by the recompute, so a fresh database has an empty sparkline** until
   somebody calls `/recompute` once. D3/D4's screens have to render that state honestly ("no history yet")
   rather than as a flat line at rank 0.
