# P11 — Leaderboard, Rankings & Referrals

Status: **D1, D2, D3, D4 and D5 built**, verified by `tools/p11-gate-check.py` at **22/22** (16 scanners
canaried, `docs/verification/P11-gate.txt`) with the whole backend suite at **990 tests OK**, the web suite at
**397 tests in 44 files OK** and `check-openapi` at 498/0. D1 is the specification, the integrity rules and the
ranking engine; D2 is the rankings API, the population the boards are demonstrated on, and the read path they are
ranked from; D3 is the standing a wallet can see — its rank, its gap to the place above, its sparkline and the
board it can put two other wallets beside; D4 is the reader's own row on all nine boards, pinned when it is off
the page, with the listing control that decides whether that row is tied to an account; D5 is referrals — the
reward model and its argument, the Sybil rules in their order of precedence, the clawback, the builder-code
revocation ground, the funnel a referrer reads, and the payout and tax terms that go with being paid. D6–D7 are
next: the public SSR pages and the anti-gaming dashboard. This file is written as the phase is built.

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
| D1 | leaderboard specification: six boards, formula per board, eligibility gate, windows, tie-breaks, recompute cadence; integrity rules (wash, copy farms, provisional, blown-up, lucky-gambler share, disputed markets) | **built** — `packages/polygm_core/leaderboard/{boards,integrity,rank}.py`, 20 unit tests; `0013_leaderboard.sql` + its SQLite twin |
| D2 | the rankings API: boards, rows with components, unranked-with-reasons, methodology, snapshots, the worker's recompute | **built** — `GET /v1/leaderboard{,/boards,/methodology,/why,/snapshots,/runs}` + `POST /v1/leaderboard/recompute`; `leaderboard/source.py`; `services/api/seed_leaderboard.py`; 20 + 28 tests |
| D3 | profile integration: rank badge, 30-day rank sparkline, "why this rank", follow/copy from the row, compare up to 3 | **built** — `GET /v1/leaderboard/{rank,compare,follows}` + `POST /v1/leaderboard/follows`; `0014_follows.sql` + its SQLite twin; `web/src/terminal/{board.ts,BoardPanel.tsx}` on `/leaderboard`; 49 API tests + 18 + 7 + 2 web tests |
| D4 | self-rank: your own position on every board, pinned when off-page, the unranked state, private-by-default with an opt-in | **built** — `GET /v1/leaderboard/me` + `GET|POST /v1/leaderboard/identity`; `0015_leaderboard_identity.sql` + its SQLite twin; `web/src/terminal/{selfRank.ts,SelfRank.tsx}` mounted in `BoardPanel` and rendered on `/leaderboard`; 16 API tests + 10 + 6 + 1 web tests |
| D5 | referrals: link + short code, the reward model and its argument, Sybil defence (first matched order over a notional threshold, device/IP/funding dedupe, velocity limits, review queue, clawback, hard self-referral block), the referrer dashboard, payout terms | **built** — `packages/polygm_core/referrals/{terms,sybil,code}.py`, 37 unit tests; `0016_referrals.sql` + its SQLite twin; `GET /v1/referrals/terms`, `GET /me`, `POST /code`, `POST /apply`, `POST /accrue`, `GET\|POST /review`; `web/src/terminal/{referrals.ts,ReferralsView.tsx}` on `/referrals`; 26 API tests + 9 + 4 web tests |
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

### 2.14 The gap to the place above is stated in the board's own field

Rank 47 is a position; "625 bps behind" is a distance, and only one of the two tells a trader whether the next
recompute can move them. `/rank` therefore carries `orderField`/`orderUnits` per board — `scoreBps` in bps on
`risk_adjusted` and `category`, `winRateBps` in bps on `win_rate`, `verifiedVolumeMicro` and `improvementMicro`
in micro-pounds on `volume` and `rising`, `copiers` in count on `copied` — and the gap is expressed in that unit
(`625 bps behind …, 5626 bps would pass them`). A gap in "points" would be a fifth vocabulary for four boards
that already say what they rank by (§2.9), and the screen's `gapSentence` is built from the served field, not
from a table of its own.

### 2.15 One read is one board, and a comparison is one read

`/compare` takes two or three pseudonyms and answers for **one** board and window, in one request. Three
separate `/rank` reads assembled by the client is three reads that can disagree — different recompute instants,
different windows, and a screen that shows a "leader" its own rows contradict. The pairwise sentences come from
the engine's `rank.explain()` (§2.10's vocabulary), and `verdict` is computed from the board's `order` array
rather than from `rows[0]`, because the two differ exactly when `order` is the honest answer.

The route refuses a non-pseudonym **before** it echoes anything: `0x…` in `anons` is a 422 naming the field, and
the response never carries an address back. That ordering is what the gate's c16 canary plants — an
address-echoing comparison is a privacy leak with a nice table around it, and it is the same rule as §2.8's
"exclusions are applied, and the reasons are not published".

### 2.16 A follow is a watch, not a copy config

`POST /v1/leaderboard/follows` writes to `trader_follows` and to nothing else. It is keyed by **pseudonym** —
`0x…` is a 422, because the follow list is a list of rows a leaderboard served, not a list of addresses a user
typed — it is idempotent per `Idempotency-Key` like every other write in this API, and unfollow reports whether
a row existed rather than pretending the second press was the first. `GET` returns the followed wallets with
their **current** standing attached: a follow list that does not say what happened since is a list of names.

The distinction is the phase's own subject matter. A follow is a read; a copy config is money and belongs to D7
of P10, which is why the response says "a follow is a watch, not a copy config" and why the tests assert that
`/v1/copy/configs` is untouched by a follow.

### 2.17 The prose that explains a missing route is not on the wire

The measured first load of `/markets` was **200.7 KB against a 200 KB budget** — over, after D3's four routes
were added to `web/src/api/routes.ts`. The ledger is imported by `src/api/client.ts`, so every key, path, flag
and **note** in it is fetched by every signed-in document; the notes, which are prose for a human and which no
screen renders, were **2.9 KB** of that. They moved to `src/api/route-notes.ts`, which no screen imports, and the
measured cost fell to **199.2 KB**.

What makes that a fix rather than a trick is the pair that holds it: the P08 gate's c1 (extended in this phase,
with a new canary) and `web/src/api/route-notes.test.ts` both fail if a note outlives its route or if a `note`
field reappears on a `RouteDecl`. Coverage is deliberately not checked — the launch list in
`docs/P08-frontend-shell.md` §4 already explains every unbuilt route, and a second list that must agree with the
first is a second list that can disagree.

### 2.18 The setting decides identity, not inclusion — and the screen says so

The kit asks for "appear on public leaderboards, or stay private", and the two halves of that sentence cannot
both be taken literally: §2.3 already refuses to drop a blown-up account from a board, and a board that omits
whoever asked not to be listed is a board that reports a flattering field. D4 takes the half that survives
contact with the integrity rules and states the other half on screen:

* **Inclusion is not optional.** Every wallet over the gate is ranked on every board it qualifies for, and
  `GET /v1/leaderboard/me` answers for a private account exactly as it does for a listed one. A ranking nobody
  can leave is the only kind whose worst rows mean anything.
* **Identity is optional, and defaults to off.** `identity.state` is `private` until the account records a
  decision, and "private" means precisely what the kit's other sentence requires: the row is a **pseudonym with
  no linkage to the account** — nothing in any public payload connects `w_…` to `u-…`, no dossier resolves, and
  the account cannot be found by name.
* **The screen says the true half out loud.** `identityDetail()` renders "your rows are on the board under your
  pseudonym: private removes the link to this account, not the row", and `identityText()` labels the default
  "private (by default)" rather than "hidden". A control that leaves the user believing the opposite of what it
  does is the failure mode this subsection exists to prevent.

The consent record is written as an append-only row (`audit_log`, action `leaderboard.identity`) carrying the
previous state, so "I never agreed to that" has an answer that is not a shrug.

### 2.19 A private wallet is still pseudonymised in everybody else's data

The privacy scanner (`privacy_findings`) walks **every public payload** the gate can produce — boards, rows,
`/why`, `/rank`, comparisons, follows, dossiers — and fails the run if a handle that has been withdrawn appears
in any of them. It is canaried in both directions: a planted handle in a board row fails the run, and the same
board with the handle absent passes it.

Two consequences were forced by writing it, and both are the interesting part:

* **The scanner must not read the account's own payloads.** On opt-out the handle is removed from every published
  row and *kept* on the account's own `/me` and `/identity` ("kept, not published"): a user who turns listing off
  and then sees the handle gone from their own settings would reasonably conclude the rename never happened. The
  first run of c18 failed on exactly this, which is what a scanner is for; the check now scans
  `Probe.public_payloads()` and additionally asserts the retention, so a later "delete it on opt-out" change has
  to argue with the gate rather than pass it.
* **Opt-out keeps the row.** c18 asserts the rank is still served after the handle goes, because that is the
  promise §2.18 makes in copy.

### 2.20 "Pinned when off the page" is arithmetic the server does, not a scroll listener

`pin.offPage` is `rank is None or rank > pageSize`, `rankedOnPage` is `(rank - 1) // pageSize + 1`, and the strip
renders only when `offPage` is true — the reader's own row is the one row that is always either on the current
page or in the strip, never both and never neither. Doing that arithmetic on the client would mean the client
deciding what page size the server used, and the first time the two disagreed the strip would pin a row that is
visible three lines below it.

The pin is matched to the board **exactly**, with no fallback: `pinFor` returning the default board's entry for
an unknown board is how a strip shows a risk-adjusted rank under a win-rate selector. The category board is the
ambiguous case (four boards wearing one name), and with no category chosen the panel pins nothing and the
account's own table — which lists all four — is the answer.

### 2.21 Unranked is a to-do list, and the numbers in it are the API's

`nextSteps` arrives from the API naming the numbers that refused the wallet ("settle 11 more markets to reach the
20 this board needs", "$190 of $500 verified turnover"), and the panel renders them in order. A client that
composed those sentences would be a second implementation of the gate, and the first time the gate moved the
to-do list would send users to a number that no longer exists. A wallet with no linked wallet at all gets the one
instruction that makes a standing possible, and it comes from the same list.

### 2.22 The idempotency key the client generated was one the API refused

`newIdempotencyKey()` joined its scope and its random half with a **colon**, and `_IDEM_RE` in the API accepts
`[A-Za-z0-9_-]{8,128}`. Every mutating request that did not pass a key of its own was therefore refused with a
422 about a header the client had just made up. No screen showed it — the screens that were exercised pass their
own key, and a *missing* header is caught by the API — and it surfaced only because D4 added a mutation whose key
nobody passes.

Three things hold it now: the shape is asserted in `web/src/api/client.test.ts` against the server's own regex,
`tools/p08-gate-check.py` gained **c16** (the contract's `pattern`, the API's `_IDEM_RE` and the client's producer
must be the same rule, and the producer must clamp a long scope), and c16 has a canary that plants the colon-joined
key. The scope is normalised and clamped to 111 characters, because an order ticket's scope is its parameters and
`order:0xabc:BUY:25` is a legitimate thing to pass.

### 2.23 The board panel had classes and no rules

D3 shipped `BoardPanel` with `pgm-board__*` class names and no stylesheet behind them: it rendered on browser
defaults, which is the one way a panel can be finished and still look broken. D4 added the rules for both the
board and the strip — the pinned strip is `position: sticky` at the bottom of the panel, because "pinned when the
row is off the page" means pinned *while the reader scrolls*, not merely present in the markup — and the strip's
`z-index` uses the design system's existing sticky rung rather than a literal, which is what P08's c5 exists to
prevent.

### 2.24 The reward is a share of a fee we were actually paid

The kit asks for one of three models, chosen and justified. D5 chooses **a share of the builder fee we are
actually paid** — `SHARE_BPS` (25%) of the fee `revenue/attribution.py` *observed* on the referee's own
attributable fills, for `TERM_DAYS` (365) from the referee's qualifying order. Three properties decided it, and
each one deletes a class of abuse instead of detecting it later:

* **There is no Sybil equilibrium, because the reward has no fixed cost.** A flat bounty on a first funded trade
  pays the moment a stranger crosses a threshold, and the cost of manufacturing a stranger — one small matched
  order — can be less than the bounty. A share of an observed fee pays only where the venue collected fees, so
  earning $X requires causing about `10_000 / SHARE_BPS` × X of real builder fees ($4 per $1 at 25%) to be paid to
  us, out of the attacker's own money. **The attacker is the customer.** The threshold below is still there (an
  unmatchable dust order must not open a twelve-month revenue claim), but it is a rate limit on claims, not the
  thing standing between us and a farm.
* **Deposits are invisible to it**, which is the kit's own trap: a deposit-and-withdraw account earns a referrer
  exactly zero until it trades. The dashboard's numbers are about trading, not about money moved in.
* **Nothing is ever paid for recruiting.** No second level, no recruitment bonus, so "recruit recruiters" has no
  payout behind it and cannot be dressed up as one.

The honest costs, because a document that lists only upsides is a brochure: the liability has a **tail** (a year
of accrual on a whale's volume, hence the settle hold, the payout minimum and the $2,000 review threshold below),
and it **pays slowly at the bottom** (one small trader earns cents in month one — which is the point, and the
dashboard names the amount still to go rather than hiding it behind a "pending" that never clears).

The two rejected models are answered in the artefact rather than in this document: `terms.rejected_models` is part
of `GET /v1/referrals/terms`, so the flat bounty ("a fixed payment for crossing a threshold, where the cost of
manufacturing the crossing can be less than the payment") and Pro credit ("costs margin rather than cash, but it
pays referrers in a currency they may not want and turns the program into an upsell funnel") are answered in the
same response the client renders. An argument that only lives in a phase document stops being made the moment
someone changes the code.

### 2.25 No trade, no referral: the qualifying event is a matched order, not a signup

`terms.qualifies()` is the only door into the program, and it takes the order's facts — matched, notional,
whether an upstream rule already excluded the market, whether the order crossed itself:

* **An unfilled order pays no fee**, so it cannot qualify a referral or feed one. This is not a policy choice; it
  is the model. A share of zero is zero.
* **Self-crossing is refused structurally, not by threshold.** An order whose maker and taker are the same wallet
  is a round trip, and `revenue.attribution` already refuses to write an attribution row for it. A rule like
  "ignore round trips under $1" is a rate card for wash trading, and the eligibility floor (20 settled markets,
  $500 verified turnover, §2.2) is inherited unchanged: a referral cannot launder a wallet past the integrity
  rules, because the markets the referral's fees come from are the same markets the wash filter already looked at.
* **The term runs from the qualifying order, not from signup.** Signup is free and unbounded; an account created
  today and funded in a year would otherwise carry a fresh twelve-month claim from a date it never traded on. The
  schema enforces the ordering (`CHECK (qualify_ms = 0 OR qualify_ms >= signed_up_ms)`), which is what caught the
  gate's own fixture stamping the qualifying order a millisecond *before* the signup it was meant to follow.

### 2.26 "Earned" is what the venue paid us, never what we expected

`_ref_accrue_work` accrues on `fee_micro_observed` — the fee the venue actually settled — and cannot see the
expected fee at all. That distinction is the whole difference between a dashboard and a liability: an accrual on
expected fees would be a number in the referrer's favour that the ledger does not agree with, and the first month
the venue charged less than our estimate it would be our money going out the door. The gate's c19 asserts it
directly: with the fee expected at $40 and observed at $8, the accrual is 25% of **8**, and the check prints
"1 accrual row(s), 8.00 of fee observed".

Two structural guards sit under it, and both are asserted rather than assumed:

* `referral_accruals` is **append-only** (trigger + the `polygm_app` grant block, §2.28) with `UNIQUE (referrer,
  referee, day)`, so a re-run of the accrual job for a day cannot pay twice, and the sum of the table *is* what a
  referrer was owed.
* A row is written only for a referral that is `qualified`. c19 walks the negative: an unqualified referee on
  whose orders no fee was paid produces **zero** rows, and the response says why.

### 2.27 The Sybil rules are an order, not a bag of filters

`sybil.PRECEDENCE = ("self_referral", "duplicate_funding", "shared_device_or_ip", "velocity")`, and the order is
the design rather than an implementation detail: the first rule that fires decides, so a self-referral is never
quietly downgraded to a device collision, and a second wallet on one funding source is refused rather than
reviewed. The two outcomes are deliberately different things:

| Rule | Outcome | What it means to the referrer |
|------|---------|-------------------------------|
| `self_referral` (identity rows or a shared `stonks_address`) | **refused**, and no attribution row is written | the hard block: nothing accrued, nothing to claw back |
| `duplicate_funding` | **refused** (`409 REFUSED`) | multiple wallets funded from one source are one person |
| `shared_device_or_ip` | **review** | held for a person; nothing accrues until it clears, nothing is lost |
| `velocity` (5/hour, 25/day per referrer) | **review** | the limits are set so that sharing a link in public is not a violation of them |

Refusal and review are not severity levels of the same thing: a refusal says *no fee will ever accrue from this*,
and a review says *not yet, and nothing is forfeited while we look*. Collapsing them would either refuse honest
referees who share a laptop or let a farm keep accruing during the weeks a queue takes to drain. The identity
check is the same rule D4 already had (`self_referral` reads `user_identities` and the wallet address), which is
why a second wallet on a shared funding source, a shared device and a shared address are three findings from one
arbitration rather than three subsystems.

### 2.28 Signals are salted digests, and the salt is the thing that must exist

`referral_signals` and `referral_clicks` hold `d_…`/`i_…`/`f_…` digests and nothing else — no IP, no user agent,
no funding address. `sybil.hash_` refuses a salt shorter than 16 characters, and the API refuses to run the
referral plane without `PGM_REFERRAL_SALT`. A salt that is absent or weak is the failure to prevent: unsalted
digests of IPs are enumerable by anyone who knows the space, and two deployments sharing a salt are one dataset
wearing two names.

A click row is by definition from somebody who does not have an account yet, so it stores no user — the row that
ties a click to a person is the attribution, and that is the only place the link is made. What this does **not**
do: it does not stop an attacker who manufactures distinct devices, distinct funding sources and real fees. There
is nothing to detect in that — they are a customer §2.24 was built for.

### 2.29 A self-referral is a revenue-integrity matter, and the builder code is the ground

The kit asks that a self-referral be a ground for **revoking the builder code it was made under**, on the argument
that the builder-fee share is a revenue line and an account paying itself is taking it. D5 does exactly that and
nothing broader: `POST /v1/referrals/apply` refuses the attribution *and* writes `builder_code_status` to
`disabled` with "self-referral" in the note. c20 walks it end to end — 409 `SELF_REFERRAL`, no attribution row, the
code disabled, two open review items, zero findings.

The scoping is deliberate and was forced by a real bug in the first implementation: the app revoked the code for
*any* refusal, but one builder code is shared by all of a referrer's referrals, so a blanket revocation on a
duplicate-funding clawback would have killed attribution for every legitimate referee that referrer ever brought.
The rule is now `kind == "self_referral"` and nothing else. A duplicate-funding finding costs the referrer the
accruals; it does not cost them the code.

The revocation is recorded, not silent: the code's status carries its `source` and a note, and a self-referral is
the one case where the note is the sentence a user can read ("a self-referral is a ground for revoking the builder
code it was made under").

### 2.30 A clawback reverses money and keeps the history

`sybil.clawback` cancels **unpaid accruals first**, then reports what was already paid with
`requiresRepayment` when the paid side is non-zero, and writes off anything under `CLAWBACK_MIN_MICRO` ($5) —
which is a published rule, so a clawback is never a surprise. The accrual rows are not deleted: they are
append-only, the reversal is a state (`clawed_back`) plus a clawback record, and the dashboard zeroes the earned
cell for a clawed-back referee rather than showing money that has been reversed. c22 asserts the arithmetic from
both ends: the cancelled unpaid `share_micro` sum, the paid column, and the fact that after a clawback the
referrer's `earned` is 0 while the rows it was computed from are still there.

This is the same rule the rest of the platform runs on — nothing hides a loss, including a loss the referrer
caused — applied to a number that is somebody's expected income, which is exactly when a system is tempted to
soften the message. The row's state text is served with the money ("reversed under the published clawback rule,
with the reason on the row"), so the screen has the sentence and not just a figure that changed.

### 2.31 The funnel is the money chain, and clicks are not a ceiling on it

The kit's dashboard is clicks → signups → funded → trading → earned → pending → paid. D5 splits it rather than
drawing it as one line, because the first term is not part of the same claim:

* `FUNNEL = ("signups", "funded", "trading", "earned")` is asserted **monotone** by `funnel_findings`, and the
  reason to assert it is that each number comes from a different table — a join that counts a row twice, or a
  filter applied to one step and not the next, shows up as an impossible funnel rather than as a plausible figure
  nobody re-derives. `trading` means the referee has accruals or is `clawed_back` (they traded, and the referral
  was reversed); `earned` excludes `clawed_back`.
* `LEADING = ("clicks",)` is checked only for what a counter can be wrong about on its own: it cannot be
  negative. "Clicks ≥ signups" is a requirement with a wrong answer — a click is a person without an account, one
  person can click five times, and a landing token can be forwarded — so the web panel renders the leading row
  **apart** from the chain, labelled as the leading indicator it is, instead of quietly making the honest
  non-monotonicity look like a bug or hiding the row.

c21 re-derives the whole thing over two referees: signups/funded/trading/earned = 2, accrued 2,000,000 micro,
totals equal to the row sums, and the review count equal to the queue length. Pending and paid come from
`referral_payouts` — the only two terms in that list that are not derived from accruals.

### 2.32 The payout terms are the product's, not the finance team's

Payout is monthly by the 10th for the month before, `PAYOUT_MIN_MICRO` ($20) minimum with the balance **carried
forward, never forfeited**, `SETTLE_HOLD_DAYS` (30) between the day an accrual is earned and the day it can be
paid, and a referrer-month above `REVIEW_THRESHOLD_MICRO` ($2,000) reviewed by a person *before* it is paid rather
than after. Tax is stated as what we do rather than as advice: at $600 in a calendar year we file for a US
referrer on a W-9 and report 1099-NEC; a non-US referrer is asked for a W-8BEN and reported on 1042-S — served
as `reportForm` and `reportThresholdMicro` so the screen cannot print a form the API does not know, and
`[UNVERIFIED]` in this document because the forms and thresholds are a jurisdiction review that belongs with
counsel before launch.

One arithmetic detail is worth recording because the first test got it wrong and the engine was right:
`toMinimumMicro` is the gap from the **payable** balance to $20 — $10 of accrued fees still inside the 30-day hold
means $20 to go, not $10, because money that cannot be paid this cycle does not count toward a payout minimum. The
dashboard says which side of the hold each figure is on, and the panel's `minimumMicro`/`holdDays` come from the
API rather than from a client-side reading of the published rules.

### 2.33 There is no referrer leaderboard, and the reason is served rather than argued

The kit asks for the position to be argued. The position is **no public referrer leaderboard**, and it is served
in `GET /v1/referrals/terms` (public, no session): *"a public contest over recruitment is a spam contest with a
scoreboard, and the ranking it would print is a ranking of recruiting, not of trading."* c22 asserts the negative
directly — `/v1/referrals/leaderboard` does not exist — because the way this argument loses is not somebody
disagreeing with it, it is somebody adding the route in a later phase and leaving the sentence behind.

It is also the same claim the rest of P11 makes about ranking: every board in this phase ranks **trading** — the
trades a wallet made, with its sample size, its drawdown and its share of one lucky win — and the one ranking D5
could add is a ranking of recruiting, which is a different product with a different incentive written on it.

### 2.34 The screen reads the rules; it does not carry a copy of them

`ReferralsView` renders `terms.rules` (seven sentences), the per-state sentences and the funnel from the API. The
panel's only local strings are its labels, and they are literal `t()` calls in a `Record<EarningsKey, string>`
and a `Record<PayoutKey, string>` over unions declared in the pure module, because `scripts/i18n-check.mjs`
**refuses interpolated keys by design**: `t(\`terminal.referrals.money.${key}\`)` is a hard failure, and the fix is
not to silence the checker but to make a new bucket without a label a compile error instead of a raw key on
screen.

The rest of the screen follows the same rule as the API's own responses: a refused short code renders the
**engine's** sentence (including a self-referral's builder-code sentence), the claim re-reads `/me` rather than
trusting the POST's echo, and exactly one POST carries one well-formed idempotency key — asserted, because "the
retry button sends the same request twice" is the failure that turns a claim into a duplicate. A signed-out
visitor gets the public terms and the rules, not a login wall: the terms are the thing somebody shares a link to
read.

## 3. Where the numbers come from

* `tests/test_leaderboard_rank.py` — **20 tests, OK** (`python3 -m unittest discover -s tests -p "test_leaderboard_rank.py"`).
* `tests/test_leaderboard_source.py` — **20 tests, OK**: the windows, the realised arithmetic (both sides, both
  outcomes, and the unresolved case that is NOT a zero), per-market folding, determinism under a reversed read,
  and the two refusal paths.
* `tests/test_leaderboard_api.py` — **65 tests, OK**, including the gate's own pair, D3's standing/compare/follow routes and D4's sixteen (`TestSelfRankAndIdentity`): nine board answers per account, the pin and its page arithmetic, the unranked reasons, the gap, private-by-default, listing and renaming, the taken-handle 409, the audit trail, and the idempotency/session enforcement. The population it runs on:
  `services/api/seed_leaderboard.py` writes **60 generated wallets + 5 specimens** — a lucky gambler, a blown-up
  account, a washer, a copy farm, a three-day-old wallet — plus two wallets built to be refused for different
  reasons (9 settled markets; 21 markets of forty cents). Measured on that population: **64 ranked**, 7 unranked,
  **5 blown up**, 1 provisional, 1 disputed result withheld, and the gate pair at ranks 12/47 has **48 versus 26
  settled markets** — the smaller sample ranked *below*, and the sentence explaining it is served by `/why`.
* `tools/check-openapi.py` — **498 passed, 0 failed** (the contract gained the seven leaderboard paths and 22
  components in D2, D3's four routes, D4's `me`/`identity` pair with six components and the `HANDLE_TAKEN` code,
  and D5's seven referral operations with nine schemas — `ReferralTerms`, `ReferralTermSheet`, `ReferralLink`,
  `ReferralMe`, `ReferralCode`, `ReferralApply`, `ReferralAccrue`, `ReferralReviewList`, `ReferralReviewSet` —
  plus the `CODE_TAKEN`, `CODE_INVALID`, `ALREADY_REFERRED` and `SELF_REFERRAL` codes. The contract is now 64
  paths and 97 schemas; `npm run gen:api` regenerates `web/src/api/schema.gen.ts` — 6458 lines — and
  `npm run check:api` fails if it drifts).

  One contract lesson from D5 is worth keeping: `compare_live` compares the **served** FastAPI spec too, so an
  admin header has to be a declared `Header(...)` parameter *and* a yaml parameter, and a `$ref` into a nested
  property path does not resolve — the sub-object needs a top-level schema (`ReferralTermSheet` exists for exactly
  that reason). The status sets must equal the app's own response table plus `_INTERNAL`'s 500 and the implicit
  200, which is what caught the admin routes' missing `503 SIGNER_UNAVAILABLE`.
* `tools/p11-gate-check.py` — **22/22 checks, 16/16 scanners canaried**, recorded in `docs/verification/P11-gate.txt`, 17.3 s. D5 adds c19 `reward_needs_a_trade` (a clean apply is `pending` and §2.25's "no trade, no referral" — an unqualified referee on whose orders no fee was paid writes **zero** accrual rows — and then, once qualified, the accrual is 25% of the **observed** $8 fee, not the expected $40: "1 accrual row(s), 8.00 of fee observed; 0 findings"), c20 `second_wallet_is_caught` (a second wallet on one funding source refused 409 `REFUSED`, a shared device opened for review, a self-referral refused 409 `SELF_REFERRAL` with no attribution row written and the builder code disabled with "self-referral" in its note, two review items open), c21 `dashboard_arithmetic` (two referees: signups/funded/trading/earned = 2, accrued 2,000,000 micro, the totals equal to the row sums, the review count equal to the queue) and c22 `payout_reality_and_clawback` (the $20 minimum as the gap from the **payable** balance, the 30-day hold, seven published rules, the future-day 422, a clawback cancelling the unpaid share without deleting a row, and the absent referrer leaderboard). Four new canaries (`referral_model`, `referral_collisions`, `referral_dashboard`, `referral_terms`) plant violations in each scanner's own input and fail the run if it walks past them. c17 walks the self-rank (`/me` = nine board answers, the pin's board and rank agreeing with the board's own row, `private` for a fresh account, the unranked wallet's steps naming a number, 401 without a session); c18 walks the identity (no handle in any public payload before opt-in, the handle on exactly that wallet's row after it, a second account claiming it refused with 409 `HANDLE_TAKEN`, opt-out removing the link but keeping the row and the rank, the owner's own payloads still carrying it, and at least two `leaderboard.identity` audit rows).
  c3 is the phase's own acceptance sentence walked over the API (rank 47 with 26 settled markets above rank 12
  with 48, and `/why` saying so in one sentence), c15 is the standing a wallet can read back (the badge, the
  re-derivable gap, the empty sparkline that says "no history yet"), and c16 is the comparison that must not
  echo an address. The scanners that make it a floor rather than a screenshot are canaried: each one is handed a
  planted violation and fails the run if it walks past it.
* `tests/test_referrals.py` — **37 tests, OK**: the qualifying order's four refusals, the term's clock, `share_of`/`accrual`/`payable`, the carry-forward arithmetic below the minimum, the funnel's monotonicity and the leading row, the tax requirement's two branches, the code shapes and their refusals, the Sybil precedence with the states it maps onto, and the clawback's unpaid/paid/written-off split. Plus `tests/test_referrals_api.py` — **26 tests, OK** on a database per test: the public terms, `/me`'s camelCase earnings and its refusal to show refused referrals, the idempotency of `apply`, `accrue`'s admin gate and its 503 without a signer, the review queue's two routes, and `PGM_REFERRAL_SALT` being required rather than optional.
* The whole backend suite: **990 tests, OK** (`python3 -m unittest discover -s tests`, 83.0 s, no skips). The count matters here: the suite *skips* the API tests when `fastapi` is absent and still prints OK, so a green line without the "skipped" count is not evidence — see §4.
* The web suite: **397 tests in 44 files, OK** (`web/node_modules/.bin/vitest run`), `tsc --noEmit` clean,
  `i18n-check` ok (919 keys, 876 used, 43 unused-advisory), and `npm run build` renders 20 routes including
  `/leaderboard` and `/referrals`. D4 adds `selfRank.test.ts` (the rules), `SelfRank.test.tsx` (the panel: the
  private label, the strip when off-page and its absence when on-page, the consent write with its key, the
  refusal, the signed-out answer) and one case in `BoardPanel.test.tsx` for the strip inside the panel it belongs
  to. D5 adds `referrals.test.ts` (the pure layer: the earnings and payout key unions, the funnel's split, the
  state text, the carry-forward arithmetic) and `ReferralsView.test.tsx` (4: a signed-out visitor gets the public
  terms rather than a login wall, a refused short code renders the engine's sentence with exactly one POST
  carrying one well-formed key, and the claim re-reads `/me`).
* `npm run measure` (P08 c8) — **pass**: `/` 190.0 KB, `/markets` **199.3 KB**, `/tma` and `/profile` 191.9 KB
  against a 200 KB budget, with route-level splitting still proven; `tools/p08-gate-check.py` is **16/16**, c16
  being the new key-shape agreement of §2.22.
* `tools/p08-gate-check.py` — **16/16**, including the c4 money-layer scan over the new screens (the percentile,
  the best-trade share and the win rate all render through `bpsText`, never `.toFixed`, and D5's referral money is
  no exception); `tools/p09-gate-check.py` — **7/7**; `tools/p10-gate-check.py` — **15/15**. P08's c2 reads
  `check-openapi`, c7 greps the built output, c8 reads `P08-bundle.txt` and c15 runs the web suite, so all three
  phases' gates were re-run and re-recorded on the D5 tree rather than argued forward from D4.
* `db/migrations/0013_leaderboard.sql` + `db/migrations-sqlite/0013_leaderboard.sql` (generated) — the portable
  subset executes: **106 tables, 54 triggers**, with `leaderboard_exclusions` append-only in both.
* `db/migrations/0015_leaderboard_identity.sql` + its SQLite twin — **107 tables, 54 triggers**, with
  `leaderboard_identity` (one row per account: the decision, its instant, and the handle it publishes) and
  `tools/build-sqlite-migrations.py --check` clean.
* `db/migrations/0016_referrals.sql` + its SQLite twin — **113 tables, 54 triggers, 16 files**, `--check` clean,
  and `tests/test_migrations.py` **18 tests, OK**. It **drops** P04's never-written `referrals`/`referral_events`
  rather than migrating them: `referrals.share_bps` was a per-code negotiated rate, which is a rate an account
  manager can raise and the one thing the model's arithmetic (§2.24) cannot survive, and `referral_events` had no
  referrer column, no state and no term, so "who is owed what, and is this referral still inside its year" was not
  expressible against it. The seven tables it replaces them with are `referral_links`, `referral_clicks`,
  `referral_signals`, `referral_attributions` (PK = the referee), `referral_accruals` (append-only), `referral_reviews`
  and `referral_payouts` (a state machine whose CHECK refuses `approved` without a tax form). The portable-subset
  builder recorded **48 PG-only drops**, and `DROPPED.json` is where that list is kept so a later phase can tell a
  deliberate drop from a table somebody forgot to create.
* Seeded tape measurement used for the gate's justification: 1,090 fills, median fill $0.02, p90 $137.50
  [measured: `db/seed.sql`].

## 4. Open, in the order it should be closed

1. **D6–D7** as listed in §1.
2. **The eligibility floor re-derived on production data** ($500 and its $0.02-median sensitivity are a judgement,
   §2.2), plus a real **category taxonomy**: `seed_leaderboard` invents four strings (Politics/Sports/Crypto/
   Finance) because the venue's own tags are not in our ingest yet, and the category board is only as meaningful
   as the tags it groups by.
3. ~~A leaderboard seed population.~~ **Closed by D2** — `services/api/seed_leaderboard.py`, opt-in via
   `python3 services/api/seed_leaderboard.py --sqlite` so the other phases' fixtures do not grow by two thousand
   markets. It is deliberately outside `db/seed.sql`: the phases that count things (the tape's page, the whale
   hour's 400 fills, the copy monitor's history) must not start failing for reasons unrelated to them. ~~The one
   adversarial case still missing is the second-wallet referral, which arrives with D5's tables.~~ **Closed by
   D5**: the referral population is built inside each test and the gate rather than in the shared seed, and the
   second wallet on one funding source is c20's first act — the referral fixture has to *fail* attribution to be
   worth anything, which is not a thing `db/seed.sql` can contain.
4. **The rank-history table is written by the recompute, so a fresh database has an empty sparkline** until
   somebody calls `/recompute` once. D3/D4's screens have to render that state honestly ("no history yet")
   rather than as a flat line at rank 0.

5. ~~`/v1/leaderboard/snapshots` and the `/rank` history were two folds over the same table.~~ **Closed in D4**:
   `/snapshots` now calls `_lb_history`, the same helper the sparkline reads, so the row shape D3 draws and the
   row shape the endpoint serves cannot drift. It was one fold written twice, and the second copy is the one that
   would have been wrong on the day a window was added.
6. **The pinned strip is not yet measured under the P10 live-load budget** (60 fps with a live tape). The strip
   is sticky, not animated, and it renders at most one row per board; the D4 claim is that it adds no layout work
   per tick, and the frame trace that would prove it belongs with P10's harness, which exists (`npm run measure:tape`).
   The D5 panel is not on that path at all — it renders on entry, holds no subscriptions and polls nothing — so it
   adds no frame work to measure.
7. **The `REVOKE`/grant block does not yet name the 0016 tables.** `0005_triggers.sql` enumerates the append-only
   set for both the trigger list and the `polygm_app` grant block, and `referral_accruals` is append-only by
   trigger and by `UNIQUE (referrer, referee, day)` but is not in the grant half of that list. On Postgres the
   trigger is the guard and the grant is the belt; the belt is the thing a migration that "just needs to fix a
   number" cannot be careful past. It is a one-line addition to two arrays in a file that is already generated
   from, so it lands with D6's migration rather than being smuggled into D5's after its record was written.
8. **A green test line is not evidence when the HTTP layer is missing.** `python3 -m unittest discover -s tests`
   prints `OK` with the API tests *skipped* on a machine where `fastapi` is not installed, and this environment
   reinstalls from `requirements.txt` per session, so the D5 sweep read a green `Ran 990 tests ... OK` that had
   skips in it. The suite now gets read for its skip count as well as its result (`OK` with no parenthetical), and
   the same is true of the gates: P08 c2 and c11 exist precisely because a missing dependency there produces a
   *failure*, which is the better behaviour and the reason those checks are worth their runtime.
