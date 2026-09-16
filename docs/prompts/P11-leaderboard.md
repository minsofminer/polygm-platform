# P11 — Leaderboard, Rankings & Referrals

> Paste `00-SHARED-CONTEXT.md` and the P2/P3/P10 outputs first, then this.

## Role
You are a product engineer building the growth surface of a trading product. You know that a leaderboard is a distribution engine, that a badly-designed leaderboard promotes lucky gamblers and gets users hurt, and that a referral system is a Sybil magnet unless you design against it from day one.

## Objective
Build the leaderboard, trader rankings, and referral system. These are the surfaces that make strangers show up and invite other strangers.

---

## Deliverables

### D1. Leaderboard specification
Polymarket already exposes an all-time volume leaderboard (`lb-api.polymarket.com/volume`) — I verified it returns the top traders, with the #1 wallet at **$1.85B** lifetime volume. Volume alone is useless as a ranking: it promotes churn, not skill. **Our leaderboard must be better than the free one or nobody will use it.**

Build these boards, each with its own default sort:
1. **Risk-adjusted PnL** (the default) — realised PnL normalised by volatility and drawdown. State the exact formula.
2. **Win rate** — with a hard minimum sample gate
3. **Volume** — the familiar one, for users who want it
4. **Rising** — biggest 7d improvement, the board that surfaces new talent
5. **Category specialists** — best in Politics / Sports / Crypto / Finance separately. A generalist board hides the traders who are actually good at something.
6. **Copied** — most-copied wallets. This board is a growth loop: being on it brings copiers, copiers bring volume, volume brings builder fees.

For each board: the metric definition with formula, the eligibility gate, the time windows (24h / 7d / 30d / 90d / all), the tie-break rule, and the recompute cadence.

### D2. The integrity rules — non-negotiable
A leaderboard that can be gamed is worse than no leaderboard. Specify:
- **Minimum sample:** no wallet appears with fewer than N resolved markets or $M lifetime volume. Justify N and M from the observed data (median fill is $5, so a wallet can rack up "trades" cheaply).
- **Wash filtering:** exclude self-matched and round-tripped volume. Specify the detection.
- **Copy-farm filtering:** exclude wallets whose activity is mechanically derived from another wallet.
- **New-wallet ramp:** a wallet under 7 days old is provisional and labelled as such.
- **Survivorship:** do we show wallets that have gone to zero? **Yes — and we must.** A leaderboard that quietly drops blown-up accounts is lying. Include a "blew up" state.
- **The lucky-gambler problem:** one 100× bet should not top a skill board. Specify the outlier treatment and display the single best trade as a share of total PnL so users can see it themselves.
- **Disputed markets:** exclude or flag positions in markets under UMA dispute.

Publish the methodology page. If a trader cannot see how they are ranked, they will assume we are rigging it.

### D3. Trader profile integration
Every leaderboard row links to the P10 D3 dossier. Add the leaderboard-specific elements:
- Rank badge and rank history (a sparkline of their rank over 30d — being able to see someone falling is as informative as seeing them rising)
- "Why this rank" expandable showing the component metrics
- One-click **follow** and **copy** from the row itself, without navigating away
- Compare mode: select up to 3 wallets, side-by-side metrics

### D4. Self-ranking
The retention hook. A logged-in user sees **their own** rank on every board, pinned to the bottom of the viewport when they are off-page, with the gap to the next rank above them. Specify:
- The unranked state (not enough sample) and what we tell them to do
- The privacy setting: appear on public leaderboards, or stay private. Default **private**, with a clear nudge to opt in — being on the board is how a trader attracts copiers, so the opt-in sells itself.
- What is shown for a private wallet that appears in someone else's data (pseudonymised, no linkage to their account)

### D5. Referral system
- Unique link per user, plus a short code for verbal/chat sharing
- **Reward model:** decide and justify. Options: share of our builder fee on the referee's volume for N months; a flat bounty on first funded trade; Pro credit. Recommend one and explain the incentive consequences. **Avoid rewarding deposit size** — that attracts people who deposit, withdraw, and never trade, and it looks like a pyramid.
- **Anti-Sybil, designed in from day one:**
  - Reward triggers on the referee's **first matched order above a notional threshold**, not on signup or deposit
  - Device, IP-cluster, and funding-source deduplication (multiple wallets funded from the same source are one person)
  - Velocity limits and a manual review queue above a threshold
  - Clawback on detected abuse, with the rule published
  - **Self-referral is a Polymarket builder-code revocation ground.** Detect a user referring themselves and hard-block it — this protects our entire revenue line, not just the referral budget.
- Dashboard: link, clicks, signups, funded, trading, earned, pending review, paid
- Payout: method, minimum, schedule, and the tax-document reality (state it plainly rather than ignoring it)
- Leaderboard of referrers — optional, and only if it does not turn into a spam contest. Argue your position.

### D6. Public shareable pages (SEO + distribution)
- `/trader/<handle>` — the public dossier, server-rendered, with an OG image showing their headline stats. **Every trader who shares their profile is doing our marketing.**
- `/market/<slug>` — the public market page with live odds. Prediction-market odds get quoted in news coverage constantly; a fast, crawlable odds page is a durable traffic source.
- `/leaderboard/<board>` — server-rendered, crawlable
- OG image generation: dynamic, cached, with the brand treatment from P2
- Structured data (schema.org) where it genuinely applies
- Rate limiting and abuse protection on public pages

### D7. Anti-gaming dashboard (internal)
An admin view that surfaces: wallets climbing suspiciously fast, clusters of wallets with correlated behaviour, referral chains that look synthetic, and any wallet whose volume is being attributed to our builder code at an unusual rate. With a one-click action to exclude a wallet from rankings and to flag it for review.

---

## Constraints
- Every ranking has a published methodology.
- Every win rate has a sample gate.
- No ranking that hides a blown-up account.
- Self-referral is hard-blocked, not just discouraged.
- No reward without a matched order behind it.

## Quality gate
Show me: a trader at rank 47 who has fewer resolved markets than the trader at rank 12, and explain why the ranking is still correct. Then show me a referral attempt from a second wallet funded by the first, and show it being caught.
