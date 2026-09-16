# P16 — Launch, Distribution & Growth

> Paste `00-SHARED-CONTEXT.md` and the P1/P12 outputs first, then this.

## Role
You are a growth lead who has launched crypto products with no ad budget. You know that in this category distribution is the entire game — Betmoar reached **$101M of monthly routed volume** with no named founder and no press coverage, while tools with 25k Twitter followers and better UI sit under $1M. You have opinions about why, and they are not about product quality.

## Objective
Produce the go-to-market plan: the wedge audience, the acquisition loops, the retention mechanics, the monetisation ramp, and the metrics with gates. Then produce the actual launch assets.

## The hard truth to design around
From the verified data: the whole Polymarket builder ecosystem did **$125M of attributed volume in a week** across ~114 builders, with a **Gini of 0.83** — six builders hold 81% of lifetime volume and the median top-50 builder has done **$4.7M ever**. In Q1 2026, **80% of builders did under $1M for the quarter** and a third never crossed $10k.

So the plan is not "build a good product and grow." It is: **find a distribution channel the incumbents are not using, and take one narrow wedge completely.** Everything below follows from that.

---

## Deliverables

### D1. Audience definition — one beachhead, not a segment
Pick ONE and defend it:
1. **The 5-minute crypto Up/Down trader** — highest frequency, highest platform fee rate (0.07), worst served by button-menu bots, lives in Telegram already
2. **The whale-follower** — wants alerts and one-tap follow, low sophistication, high volume
3. **The sports trader** — high volume, event-driven, seasonal spikes, huge Telegram presence
4. **The analytics power user** — pays for depth, low volume, high LTV per user but a tiny market and 11 existing competitors

For the chosen beachhead: where they already gather (specific Telegram groups, subreddits, X accounts, Discords — name them), what they currently use, what they complain about, what would make them switch in one session, and how many of them exist. **Estimate the reachable population and show the arithmetic.**

### D2. The free product as the acquisition engine
The free alert channel is the wedge. Specify it as a product:
- Content mix and cadence. **A channel people mute acquires nobody** — define the daily message budget and defend it.
- The message format: one market, the signal, the number that matters, a deep link into the Mini App, and **a one-tap trade button on the alert itself**. Alert → trade in one tap is the gap nobody in this market has closed.
- The quality gate before broadcasting (P7 D1): an attacker can create a market and trade it. If our channel amplifies junk, we lose it permanently.
- Which signals go to the free channel vs personal alerts vs Pro. **The free channel must be genuinely good** — the upsell is depth and automation, not withholding.
- Channel growth tactics that work in Telegram: cross-promotion with non-competing channels, the public shareable pages from P11 D6, and the fact that prediction-market odds get quoted in news coverage constantly (a fast, crawlable odds page is durable SEO)
- Target: **2,000 members in 30 days with zero ad spend.** If that fails, the wedge is wrong — say what changes.

### D3. Acquisition loops — rank by cost per activated user
Evaluate and rank: the free channel, SEO on public market/trader pages, referral (P11 D5), X/Twitter presence, Telegram cross-promo, YouTube/short-form, community participation, and paid ads (probably wrong at this budget — argue it).

For each: mechanism, expected cost per activated user, time to effect, and the failure mode. Then pick **two** and ignore the rest until those work. Spreading a $10k budget across six channels is how you get zero of anything.

Be explicit about the thing the data says: only 25 of 114 builders scored above 1 on founder visibility, and the volume leader has none. **Visibility is not the mechanism. Distribution inside Telegram is.** Design accordingly.

### D4. Activation — /start to first trade in under 90 seconds
Map the funnel with expected drop-off at each step (from P12 D7) and the specific intervention for each:
1. `/start` → value in two lines
2. Wallet auto-created, deposit address shown
3. **Deposit** — the biggest drop-off. Interventions: minimum deposit framing, multi-chain detection, a "waiting for your deposit" live state, and a nudge sequence
4. First market card, pre-filled
5. **First trade** — the activation event
6. First fill → position view + `/stop` pointed out

Then: the second-session hook. A user who trades once and never returns is the most expensive user we will ever acquire. Specify the day-1, day-3, and day-7 re-engagement (an alert that is genuinely relevant, not a "we miss you" message).

**Define activation precisely and measure it.** Recommend: a funded wallet plus one matched order within 7 days of signup.

### D5. Retention mechanics
- **Alerts are the retention engine.** A user with one active alert comes back. Specify the nudge that gets a new user to their first alert, and the target alerts-per-active-user.
- Watchlists and follows — same logic
- The self-ranking hook (P11 D4)
- Automation rules: a user with a live rule checks the app daily. This is the strongest retention mechanic we have and it is also the Pro upsell.
- **The losing-streak problem.** Prediction markets resolve to zero. A user who loses their first deposit churns and tells people. Specify the honest onboarding about risk, the max-loss display in the trade ticket, and what we send after a total loss. **Do not send a "win it back" message.** That is how a product becomes a scam.
- Churn definition, measurement, and the win-back sequence (or the deliberate decision not to have one)

### D6. Monetisation ramp — sequenced, not simultaneous
1. **Launch at 0 bps builder fee.** Betmoar and Stand.trade both charge zero and are top-5. We are buying volume share, not margin. The leaderboard position is the moat.
2. **Pro subscription** ($12/mo web, Stars-equivalent in Telegram) — depth, automation, API, unlimited watchlists. **Never paywall the ability to exit a position.**
3. **Raise builder fees to 10–25 bps only after retention holds.** Remember the mechanics: one change per 7 days, 3 days advance notice, one pending change at a time — we cannot react fast, so we cannot experiment aggressively. And rates are publicly queryable, so pricing is a public product decision.
4. **Grants:** apply to the Polymarket builders program ($2.5M pool, $100–$75k each) with real traction numbers. This is non-dilutive money and it is available to us specifically.
5. **API/data licensing** — we index everything anyway. Competitors charge $19.99/mo for API access.
6. **Never:** a token. Telegram requires TON for Mini App token distribution, it is a regulatory target, and it will consume the entire year.

State the revenue mix target at month 6 and month 12, with the constraint from P1: **builder fees never exceed ~60% of revenue**, because Polymarket can revoke the privilege at its sole discretion.

### D7. Metrics, gates, and the decision rules
Define exactly, with the formula and the source:
- Activation rate (signup → funded + first matched order, 7d)
- Routed volume per active user per month
- Alert → trade conversion
- D1 / D7 / D30 retention
- Free → Pro conversion, and revenue per active user
- Attributed volume as a share of platform builder volume
- **Revenue per 1,000 routed dollars** — the number that tells us whether the builder economics actually work
- CAC per channel vs LTV per channel

Then the **gates**, with the decision attached to each:

| When | Gate | If missed |
|---|---|---|
| Week 4 | 2,000 channel members OR 500 Mini App MAU | Change the wedge. Do not build trading. |
| Week 8 | 30 users who traded twice; $50k attributed volume in a week | The product isn't sticky. Fix before scaling. |
| Week 12 | $150k/mo attributed volume, 150 paying users | Reassess whether this is a business or a hobby. |
| Month 6 | $500k/mo attributed volume, $15k MRR | Raise fees, or raise money, or stop. |
| Month 12 | $1.5M/mo volume, $40k MRR | On the path to $1M ARR in year 2–3. |

**Write the honest version of the month-12 miss case: what we do, what we tell users, and how we shut down cleanly if it fails.** A plan with no exit is not a plan.

### D8. Launch assets — write them
- Landing page copy: headline, subhead, the three messages, the risk disclosure, the CTA. No "revolutionising", no "seamless", no "empowering".
- The `/start` message
- The pinned channel message
- The launch post for X and for Telegram
- The Product Hunt / Hacker News post (and the honest assessment of whether either audience is our beachhead — probably not, argue it)
- The DM template for channel cross-promotion
- The support macros for the 10 questions we will get most
- The "we are not affiliated with Polymarket" disclaimer, worded correctly, in the footer of every page

### D9. Community & trust
- Where we are present, and the rule that we are useful before we are promotional
- How we handle a public complaint about a loss (spoiler: transparently, and without arguing)
- The public status page and the public changelog
- **The support-impersonation defence** (P12 D6) as a marketing asset — competitors' users are actively warned about fake support; making our defence visible is a differentiator
- What we say about returns: never expected returns, never "guaranteed", losing wallets shown next to winning ones

---

## Constraints
- Two channels maximum until the first one works.
- No paid acquisition before activation is above the gate.
- No feature built for growth that is not instrumented.
- No message that implies a profit is likely.

## Quality gate
Read this plan and answer: who exactly is the first 1,000 users, where do we find them, what do we say to them, what do we measure on day 7, and what do we do if the number is half of what we hoped? If any answer is vague, the plan is not done.
