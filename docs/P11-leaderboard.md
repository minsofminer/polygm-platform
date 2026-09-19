# P11 — Leaderboard, Rankings & Referrals

Status: **D1 built** (the specification, the integrity rules and the ranking engine, with 15 unit tests). D2–D7 are
next: the API and its gate, the profile integration, self-rank and privacy, referrals, the public SSR pages, and
the anti-gaming dashboard. This file is written as the phase is built.

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
| D2 | the rankings API: boards, rows with components, unranked-with-reasons, methodology, snapshots, the worker's recompute | not started |
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

## 3. Where the numbers come from

* `tests/test_leaderboard_rank.py` — **15 tests, OK** (`python3 -m unittest discover -s tests -p "test_leaderboard_rank.py"`).
* `db/migrations/0013_leaderboard.sql` + `db/migrations-sqlite/0013_leaderboard.sql` (generated) — the portable
  subset executes: **106 tables, 54 triggers**, with `leaderboard_exclusions` append-only in both.
* Seeded tape measurement used for the gate's justification: 1,090 fills, median fill $0.02, p90 $137.50
  [measured: `db/seed.sql`].

## 4. Open, in the order it should be closed

1. **D2–D7** as listed in §1.
2. **The eligibility floor re-derived on production data** ($500 and its $0.02-median sensitivity are a judgement,
   §2.2), plus the six category labels the category board needs — our seed has no category taxonomy yet.
3. **A leaderboard seed population.** The seeded tape has four wallets, which is enough for the tape and useless
   for six boards: D2 needs a synthetic population with the adversarial cases deliberately present (a lucky
   gambler, a blown-up account, a washer, a copy farm, a second-wallet referral) or the integrity rules are only
   ever tested against well-behaved fixtures.
