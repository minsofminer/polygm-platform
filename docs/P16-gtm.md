# P16 — launch, distribution and growth

*The phase's gate, verbatim from the kit: **"who exactly is the first 1,000 users, where do we find them, what do
we say to them, what do we measure on day 7, and what do we do if the number is half of what we hoped?"***

Everything in this document is held by `config/gtm.json`, and `tools/p16-gtm-check.py` fails the build when the
prose and the config disagree — the reachable population must multiply out, the funnel must multiply out to the
activation rate, the message budget must be the channel engine's own constants, the fee ramp must pass the venue
mechanics in `packages/polygm_core/revenue/schedule.py`, the required phrases must exist in the copy that ships, and
each of the five gates must state what happens when it is missed. A growth plan is the easiest document in a
repository to write dishonestly, because nothing runs it. This one is run.

Provenance on every external number, per `config/gtm.json`'s `_provenance`: **[CTX]** comes from the shared context
and the kit and is not ours to re-derive, **[PROBE]** was measured by this repository (see `docs/verification/`),
**[ASSUMPTION]** is ours — written as a range with the reasoning next to it, so it moves when the reasoning does.
The checker refuses a number with no source at all.

---

## 1. Who the first 1,000 users are (D1)

**The beachhead is the 5-minute crypto Up/Down trader who already lives in Telegram.** Not "crypto traders", not
"prediction market users" — the person who watches the hourly BTC and ETH Up/Down markets, has an opinion about the
next fifteen minutes, and currently gets that opinion expressed by opening a browser tab on a desktop site.

### 1.1 Why this one

| Reason | What it buys us |
| --- | --- |
| Highest order frequency of the four candidate audiences | Frequency is what makes a $12/month subscription a rounding error rather than a decision. A holder of a long-dated political position opens the product once a month; this person opens it several times a day. |
| The venue's fee schedule charges the most on hourly crypto (`crypto_fees_v2`, 0.07 on the winning side) [CTX] | The platform's incentives and this trader's behaviour point the same way. We are not fighting the venue's economics; we are routing through the part of it that is priced highest. |
| Worst served by existing tooling | The current state of the art is a button-menu bot that posts a link, and the trade happens on a website in a browser. A Mini App that fills the ticket from the alert is a different product, not a slightly better one. |
| Already inside Telegram | The free channel, the personal alerts and the Mini App all live where this person already is. There is no new surface to earn — which matters because §4 of the shared context says the incumbents' advantage is distribution, not product. |
| The alert is the product | A fill worth watching is news to this audience in a way it is not to somebody holding a position for six weeks. The channel is not a marketing channel with a product behind it; for this person the channel is the everyday experience of the market. |

### 1.2 The reachable population, with the arithmetic

`reachable = monthly traders × share_updown × share_in_telegram × share_reachable_organically`

| Input | Value | Range | Source | Note |
| --- | --- | --- | --- | --- |
| Monthly venue traders (active wallets) | 300,000 | 200,000–450,000 | [ASSUMPTION] | Order of magnitude. We cannot measure it and do not pretend to; the estimate moves with the input instead of being a fact. |
| Share trading hourly crypto Up/Down | 0.20 | 0.10–0.30 | [CTX] | The highest-frequency segment; the venue's own fee table names it separately. |
| Share who live in Telegram | 0.60 | 0.45–0.75 | [ASSUMPTION] | This segment skews crypto-native and mobile-first. |
| Share reachable organically (channel + search, no paid) | 0.50 | 0.30–0.70 | [ASSUMPTION] | A channel at week 4 is a few thousand people at most; "reachable" here means reachable by the two channels we chose, not by all means. |

**Reachable ≈ 18,000 people** (range 2,700–70,875 across the corners of the inputs). The week-4 gate asks for
**2,000 members = 11% of that pool**, and that is the number the whole plan has to be honest about: a channel that
wins 11% of a segment in four weeks is a good channel, not an average one. If we are at 4% we are at the miss case in
§8, not "slightly behind plan".

The corridor matters more than the point estimate. Every input is a range, and the plan is written so that the
*decisions* do not change inside the range — the channels, the budget and the gates are the same at 2,700 reachable
people or 70,875. What changes is the calendar, and the gates are what tell us which corridor we are in.

### 1.3 The audiences we said no to

| Rejected | Why, in one line |
| --- | --- |
| **Whale-follower** | Real, reachable, and the behaviour *is* copy-the-whale — the one thing P14's anti-gaming work and P11's leaderboard spend their time refusing to look like a promise. It is also the audience every incumbent already targets, which is the opposite of the instruction to take a channel nobody is using. |
| **Sports trader** | The volume is real and seasonal; it is also the most crowded Telegram niche in this market, and we would be the ninth-fastest-moving bot in a race that rewards being first. Kept as the **named fallback** if the week-4 gate fails. |
| **Analytics power user** | Highest revenue per user, smallest population, eleven existing competitors (the kit's own count). Judged on data depth we do not yet have, and it is where the Pro tier already has substitutes. |

### 1.4 The mechanism, stated so we cannot drift into it

The shared context is blunt about the shape of this market: **$125M/week attributed across ~114 builders, Gini 0.83,
six builders holding 81% of lifetime volume, a median top-50 builder at $4.7M ever, and 80% of Q1-2026 builders
under $1M/quarter** [CTX]. A new entrant does not win by being seen. The one incumbent with scale in exactly this
behaviour — Betmoar, $101M/month routed, no named founder — won by owning a distribution surface, not a personality.

So the growth mechanism here is deliberate and narrow: **a Telegram distribution channel that the incumbents are
not using, fed by alerts that are worth reading on their own.** Not founder visibility, not a launch-day spike, not
paid spend. Everything below is in service of that one mechanism, and the constraint "two channels maximum until one
of them works" exists because a mechanism diluted across six channels is a wish.

---

## 2. The free channel is the acquisition engine (D2)

The plan's central bet: **the free alert channel reaches 2,000 members in 30 days with zero ad spend**, and the
reason it can is that the product it posts — one market per message, the whole thing legible in the preview line,
with a one-tap trade that works for a stranger — is better than what the channel replaces.

### 2.1 What it posts, and what it never posts

| Posted (free) | Not posted |
| --- | --- |
| Large fills above the venue-visible floor | Anything that reads as advice, a call, or a forecast |
| Volume spikes, with the market and the window | Unlabelled numbers — every figure carries its age |
| New markets worth knowing about | A market we have a position in without saying so |
| Markets resolving within six hours | More than one market per message |

Message kinds map one-to-one onto the engine's four (`large_fill`, `volume_spike`, `new_market`, `resolution_soon` in
`packages/polygm_core/telegrambot/channel.py`), so "the plan posts four kinds" is not a promise about behaviour we
would have to build — it is the set the engine already emits, gated by P07's `abuse.broadcast_gate` before
composition.

### 2.2 The message budget, and why it is the strategy rather than a constraint

| Limit | Value | Enforced by |
| --- | --- | --- |
| Channel messages per rolling 24h | 12 | plan (`channel_per_day_max`) |
| Per hour, any kind | 4 | engine `CADENCE.max_per_hour` |
| Per kind, per hour | 2 | engine `CADENCE.max_per_kind_per_hour` |
| Minimum gap between messages | 6 minutes | engine `CADENCE.min_gap_ms` |
| Quiet hours (local) | 00:00–06:00 | plan |

**A channel people mute acquires nobody.** Twelve a day is the point where the preview line is still worth reading:
four kinds at up to three each, sitting inside the engine's own caps. The checker reads those caps out of
`channel.py` and fails if this table and the code disagree, which is what stops the plan from slowly describing a
cadence nobody enforces.

Personal alerts are **not** capped here. A user's own follows are theirs; the per-user throttle lives in
notification settings where the user set it. The free channel is a broadcast, and a broadcast takes a budget.

### 2.3 The quality gate before anything is broadcast

Nothing reaches the channel without passing all of: P07's `abuse.broadcast_gate`; a named market with a resolvable
link; a price with its age attached; the deep link resolving to a Mini App screen that renders without a session
(the read-only path — a stranger has no session, and a broken first tap is the whole funnel gone); and a human
reading of the preview line as a stranger would. A channel that posts a wrong number once keeps posting to a
smaller audience forever.

### 2.4 Free has to be genuinely good

The policy in one sentence: **the upsell is depth and automation, never withholding something a trader needs to
act.** Concretely — the free channel carries the four event kinds above; personal alerts add filters, watched
wallets, moves on your own positions, and **the losing case** (your position moving against you, with the maximum
loss attached); Pro adds automation rules, unlimited watchlists and follows, API access, order-book history and
wallet dossiers, and priority delivery.

Four things are never paywalled, and they are named in the config so this cannot quietly change: **exiting a
position**, **the withdrawal path**, **the kill switch**, and **the loss numbers on your own positions**. A product
that charges for the exit deserves the churn it gets, and in this market it would deserve worse.

---

## 3. Two channels, ranked by what an activated user costs (D3)

Ranked by cost per activated user. "Activated" is §4's definition, not a signup.

| # | Channel | Chosen | Cost per activated user | Time to effect | Failure mode |
| --- | --- | --- | --- | --- | --- |
| 1 | Free alert channel | ✅ | $0 (own labour) | 2–6 weeks | Compound: no members by week 2 means the alerts are not worth forwarding, and no amount of posting fixes that |
| 2 | Public pages' organic search | ✅ | $0 | 6–16 weeks | Slow and non-linear; a page set that does not rank by week 8 is a page set nobody links to |
| 3 | Referrals | ❌ | $8 | 3–8 weeks | Needs an activated base to refer from — it amplifies a working funnel and cannot start one |
| 4 | X/Twitter | ❌ | $25 | weeks, with luck | Account-age cold start; a new account's replies are throttled, so the cost is paid in weeks of posting before the first activation |
| 5 | Telegram cross-promotion | ❌ | $0 (swap) | 1–3 weeks | Free, effective, and it puts someone else's audience in our metric — the swap only works with a channel of comparable quality, and there is no way to hold the audience once it arrives |
| 6 | Short-form video | ❌ | $40 | months | Production cost per activated user is high and the audience is not traders |
| 7 | Community participation | ❌ | $15 | 4–12 weeks | Real but slow, and it fails the "one working channel" test: it is a place to be seen, not a machine that produces members |
| 8 | Paid ads | ❌ | $120 | immediate | **The arithmetic**: $120 per activated user against a $12/month subscription is a twelve-month payback before churn, on a platform whose fees on our best case are 25 bps. Also forbidden by this phase's constraints until activation clears its own gate — argued against, not merely deprioritised. |

Two chosen, six refused, and the two chosen cost nothing but attention. That is the whole D3 budget: **no paid
acquisition before activation clears its gate, and no third channel until one of these two works.**

---

## 4. Activation: /start to a filled order in under 90 seconds (D4)

**Activation is defined as a funded wallet plus one matched order within 7 days of signup.** Not a signup, not a
deposit, not a session. The 7-day window matters because the alert channel delivers people at the moment a market
moves, and the product has to survive the difference between that moment and the one where they have money in.

The funnel, with the intervention at each step (this is what the plan does about drop-off, not a chart of it):

| Step | Share | Source | Intervention |
| --- | --- | --- | --- |
| Member sees an alert | 1.00 | [ASSUMPTION] | The channel is the intervention — one market per message, legible in the preview line, deep link that works for a stranger |
| Taps the deep link | 0.06 | [ASSUMPTION] (0.03–0.10) | One market per message; the button is a Mini App deep link, not a callback, so a non-user's tap lands somewhere that renders |
| Reads `/start` and understands the product | 0.95 | [ASSUMPTION] | Two lines: what this is, what it costs, what a loss looks like |
| Wallet exists, address shown | 0.97 | [ASSUMPTION] | No signup, no password prompt before value; the wallet is there before the user decides anything |
| Funds the wallet | 0.35 | [ASSUMPTION] (0.20–0.50) | "$10 is enough to trade", multi-chain detection, and a live "waiting for your deposit" state that shows the chain scan rather than a spinner |
| Places the first order | 0.70 | [ASSUMPTION] (0.50–0.85) | The market arrives pre-filled from the alert that was tapped; the ticket shows the maximum loss before the confirm |
| The order matches | 0.92 | [ASSUMPTION] | Fill notification with the position view, and `/stop` pointed out in the same message |

**0.06 × 0.95 × 0.97 × 0.35 × 0.70 × 0.92 = 1.25%** of channel members become activated users — about 25 people
from a 2,000-member channel in the first month. That is a deliberately pessimistic number sitting where the pressure
to be optimistic is loudest, and it is the reason the week-4 gate is *members or MAU* rather than activated users:
at this rate, activation is not measurable in month 1 in any way that supports a decision.

The two steps doing the damage are the deep-link tap (6%) and the deposit (35%). Both are product problems, not
copy problems, and both are already built: the market must arrive pre-filled, and the wallet must exist before the
user is asked for anything.

### 4.1 Second session, at day 1, 3 and 7

| When | What is sent | Why |
| --- | --- | --- |
| Day 1 | An alert about a market they already touched | It is about the market, not about them — "not a welcome-back message" is the rule |
| Day 3 | The first signal on a market they viewed but did not trade, with the reason it fired | Shows the product noticing something they missed, which is the only honest reason to come back |
| Day 7 | Their own week in numbers — trades, volume, P&L **including the losses** — plus one suggested alert to create | The P&L line is the trust play; a product that only reports the good week is a product nobody believes in the bad one |
| Never | A "we miss you" message, a streak, or anything that implies a comeback is likely | A losing user told to come back is being churned on purpose |

---

## 5. Retention (D5)

Four mechanics, in the order they matter:

1. **Alerts.** The default experience after signup is one a user configured. A user who has created an alert has
   given us the reason to send the next message, and it is their reason, not ours.
2. **Watchlists.** Cheap to build, cheap to keep, and they convert a browsing session into a returning one.
3. **Self-ranking.** The leaderboard, ranked the way the user chooses (P11), with the risk-adjusted board as the
   default because it is the one that does not reward a single lucky day.
4. **Automation.** A live rule is checked daily, which makes it the strongest retention mechanic in the product —
   and the one place where a user has delegated a decision, so it is also the one place where the loss handling has
   to be loudest.

### 5.1 The losing-streak problem

Losing streaks are the norm here, not the exception: these are markets that resolve to zero all the time. What we
do when a user is losing, in order:

* Keep reporting **both** directions. The P&L in the day-7 message includes the losses, and a user's own page never
  hides them behind a "this week" window.
* At a 5-loss streak, the loss-halt machinery from P13 fires — the drawdown banner appears **with** the P&L, and a
  rule that is firing through a losing streak is paused with the reason stated.
* Surface `/stop`, the kill switch and the withdrawal path in the same place as the loss numbers, on every surface
  where a position is displayed.
* **No comeback framing, ever.** No chasing, no streak to protect, no message that treats a fresh deposit as the
  answer to a loss. The config's banned list carries the exact phrasing this is aimed at, which is why it does not
  appear anywhere in these three documents: the checker scans the launch copy for every banned string, and a plan
  that quotes the phrase while forbidding it is a plan whose next campaign will paste it.

### 5.2 Churn, defined before we argue about it

A user has **churned** when they have no open position, no live alert, no live automation rule and no session for
**30 days**. This definition is chosen because it is the one we cannot accidentally satisfy: somebody with a live
rule is still a user even if they never open the app, and somebody who logs in weekly to stare at a position is not
one we can claim. Alert delivery is the leading indicator (a user who stops receiving alerts churns first), which is
why the scorecard reads delivery before it reads activity.

---

## 6. The ramp: free at launch, paid when retention holds (D6)

The ramp is not a price list — it is a **decision sequence with preconditions**, loaded from `config/gtm.json` into
`packages/polygm_core/revenue/schedule.py` and validated by the gate check.

| Step | Day | Builder fee | Requires (enforced in code) |
| --- | --- | --- | --- |
| `launch` | 0 | **0 bps** | nothing |
| `step_10` | 90 | **1,000 bps = 10 bps** on routed volume | D7 retention ≥ 0.15 **and** ≥30 users who traded twice |
| `step_25` | 180 | **2,500 bps = 25 bps** (the ceiling) | month-6 volume gate met ($500k/month) **and** Pro conversion ≥ 3% |

At launch we buy **volume share, not margin**. Betmoar and Stand.trade both launched at zero and both sit top-5; at
this stage leaderboard position is the moat, and the kit's own data says the incumbents' advantage is distribution
rather than price. 10 bps is invisible next to a 2–3 cent spread on an hourly crypto market, and 25 bps is where the
fee starts being a reason to route around us — which is why the ramp stops there.

**Venue mechanics, and why the ramp is shaped the way it is.** A fee change is a venue-level act: minimum **7 days**
between changes, **3 days** advance notice to users, and **one pending change at a time**. Increases require the
stated retention preconditions; **decreases are exempt, by design** — the asymmetry is deliberate and it only runs
one way: it is always legal to make the product cheaper for users, and never legal to raise the price without the
numbers to justify it. Both still obey the venue's spacing rules. `validate_steps` runs inside the gate check, so a
future edit that shortens the spacing or raises the ceiling fails the build rather than the users.

The rest of the revenue stack, in the order it is allowed to exist:

* **Pro at $12/month** — automation, unlimited watchlists and follows, API access, depth, priority delivery. Never
  the exit, the withdrawal path, the kill switch, or the loss numbers.
* **Builder fees** — 0 → 10 → 25 bps as above, paid by the venue's builder programme rather than by users.
* **The builders programme's grant pool** ($2.5M, $100–$75k per builder) [CTX] — applied for after week 8, with the
  scorecard's real numbers attached rather than a pitch.
* **API and data licensing** — the normalised, low-latency version of what the channel already publishes: $500–$5,000
  per month per licensee [ASSUMPTION], not before month 6, and only if the API's own logs show third parties pulling
  it. The underlying data is public on the venue's API, so the price is for labelling, normalisation and latency; if
  we cannot be measurably faster or cleaner than a competent scraper, the honest move is to say so rather than sell
  it to somebody who will find out.
* **Never a token.** Stated in the config so it cannot be quietly reintroduced, and stated here because the shortest
  path to a large number in this market is the one that ends with users holding nothing.

**Builder fees never exceed 60% of revenue**, because the privilege is revocable at the venue's sole discretion and
a business that dies when somebody else's dashboard changes is not a business. The target mix at month 6 is ~85%
subscriptions, ~10% builder fees, ~5% other; at month 12, ~80/12/8.

---

## 7. What we measure, and the five gates (D7)

Nine metric definitions live in `config/gtm.json`, each with a formula and the tables it reads — verified against
`db/migrations` by the checker, so a metric that reads a table we never created fails the build. The three that
matter most:

| Metric | Formula | Source |
| --- | --- | --- |
| `activation_rate` | users with a deposit **and** ≥1 matched order within 7d of signup ÷ signups | `users`, `deposits`, `orders`, `fills` |
| `attributed_volume_share` | our routed notional ÷ venue notional | `builder_attribution` (via the attribution ledger) |
| `alert_to_trade` | trades within 15 minutes of a channel alert ÷ alert impressions | `telegram_*` + `orders` |

### 7.1 The gates

| Gate | Must be true | If it is missed |
| --- | --- | --- |
| **Week 4** | **2,000 channel members OR 500 Mini App MAU** | The wedge is wrong, not the execution. Change the audience (sports trader is the named fallback), keep the channel and the public pages running since they cost nothing, and **do not build trading features for an audience that did not arrive**. |
| **Week 8** | **30 users who traded twice** and **$50k/week** attributed volume | The product is not sticky. Fix retention before scaling — automation rules and the losing-streak handling are the two levers, and both are already built. |
| **Week 12** | **$150k/month** attributed volume and **150 paying users** | Is this a business or a hobby? Run the ramp's second step only if retention held; otherwise hold the fee and cut the cost base to the $109/month floor (P15's cost model). |
| **Month 6** | **$500k/month** and **$15k MRR** | Raise fees, or raise money, or stop. The decision comes from the scorecard, not from mood. |
| **Month 12** | **$1.5M/month** and **$40k MRR** | §8. |

The week-4 gate is an **OR** on purpose: in month 1 the channel's member count is a distribution signal and the Mini
App MAU is a product signal, and either clearing means the mechanism is working. Requiring both would fail a channel
audience that has not deposited yet, which is exactly the audience the free tier is for.

---

## 8. The month-12 miss, and the clean shutdown

A plan that only describes success is a plan with an unexamined failure mode, and in this market the failure mode is
specific: the venue changes its builder programme, the fee gets revoked, and a business that was 60% dependent on it
has a month of runway and no product.

**The honest case, written now so that it is a decision and not a scramble:**

1. **The gate is read from the scorecard.** `$1.5M/month` attributed volume and `$40k MRR` at month 12. If we are at
   half — $750k/month and $20k MRR — we are not "nearly there"; we are at the point where the plan's own arithmetic
   says the two-channel mechanism produced a product with a few hundred paying users and no path to the fee ramp's
   second step. The plan does not survive on hope from there.
2. **What users are told.** The same week, in the channel and in-app: what is happening, what stops working, what
   keeps working, and the date. Not a slow decay, and not silence followed by a shutdown notice. The channel that
   acquired them is the channel that tells them.
3. **What keeps running.** Read-only mode as the default, not a shutdown: positions are on the venue, not with us,
   and a user's ability to see and close them is not ours to withdraw. Alerts and watchlists keep running for as
   long as the read path costs less than the floor — P15's cost model puts the full stack at $119.38/month (39.8% of
   a $300 budget), and the floor configuration at $109/month.
4. **How money gets out.** The withdrawal path stays free and open, documented in `docs/P16-community.md`'s support
   macros, until the last user who has a balance has withdrawn — support answers those tickets first, and indefinitely.
   Pro subscriptions stop billing on the announcement date; the remaining period is refunded pro-rata or honoured,
   the user's choice.
5. **What is published.** The scorecard, including the numbers that missed. The repository is the record; the
   shutdown note is the last page of it.

The same procedure runs for the smaller version of the same event — a revoked builder fee mid-ramp drops revenue to
the subscription line, and the response is §7's week-12 branch: hold the fee, cut to the floor, keep the read path.

---

## 9. The gate, answered in one screen

| The kit asks | The answer |
| --- | --- |
| **Who exactly is the first 1,000?** | The subset of ~18,000 reachable 5-minute crypto Up/Down traders who already live in Telegram, follow at least one crypto alert channel, and placed a trade on the venue in the last 30 days. Not "crypto traders" — the person who has an opinion about the next fifteen minutes. |
| **Where do we find them?** | Two channels: the free alert channel, with members recruited from groups that quote the venue's odds; and the public odds pages' organic search — cross-promotions with two similarly-sized non-competing channels once one of those two works. |
| **What do we say to them?** | `docs/P16-launch-assets.md`, written to be copy-pasted: the `/start` message, the pinned channel post, the X and Telegram launch posts, the Product Hunt and Hacker News post, the cross-promo DM, and ten support macros. |
| **What do we measure on day 7?** | The cohort of the first 100 signups: the activated rate (funded + one matched order), which channel brought them, D1 and D7 return, and how many created an alert. One cohort, read on one page. |
| **What if it is half?** | If 1,000 members arrive by week 4 instead of 2,000, the week-4 gate's own answer applies: the **wedge is wrong, not the execution** — switch to the sports trader (the named fallback), keep the channel and the public pages running because they cost nothing, and do not build trading features for an audience that did not arrive. Half of the *week-8* numbers is a retention problem with two named levers (automation, losing-streak handling) rather than an audience problem. |

## 10. What would make this plan wrong

* **The 6% deep-link tap.** If it is 2%, the channel is a media property, not an acquisition engine, and the week-4
  gate fails on arithmetic rather than on effort.
* **The 35% deposit step.** If strangers will not fund a wallet from a Telegram link, the Mini App is the wrong
  surface for this audience and the fallback is a desktop-first funnel — a different plan, written then.
* **Reachable ≈ 18,000.** If the venue's Telegram-native share is 0.2 rather than 0.6, the pool is 6,000 people and
  2,000 members is a third of the market, which is not a plan.
* **The venue's own cadence.** If Polymarket ships the same alert channel itself — which it can, and has the data to
  do well — our channel becomes a slower copy of a free product, and the response is depth and personalisation, not
  volume.
* **The ramp's preconditions.** If retention never reaches 0.15, the ramp's second step never runs, and the business
  is a $12 subscription product whose free channel is bigger than its paid base. That is survivable and it is not
  what this plan is for — which is why the preconditions are in code and not in an email thread.
