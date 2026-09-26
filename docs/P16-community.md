# P16 — community, trust and the support surface

*D9 in one line: **be useful before being promotional, be transparent when the numbers are bad, and never let
anybody impersonate us.** This document is the operating manual for that, plus the status page, the changelog and
the impersonation defence the kit asks for.*

The ordering is the argument. Community comes after the product in this plan because a community built before
there is something to be part of is a chat room with a growth strategy; the channel in §2 of the plan is useful
with **zero** members, which is the test that lets it be promoted at all.

---

## 1. What the community is, and what it is not

| It is | It is not |
| --- | --- |
| A channel where a person sees the tape and can trade it in two taps | A signal group, a room where somebody tells you what to buy, or a place with "calls" |
| A place where a bug gets reported and answered in public | A support inbox that answers "we're looking into it" and closes |
| A build log with the failures left in | A marketing surface that only appears on good days |
| Three rooms, each with a job (below) | A Discord with eleven channels and nobody in ten of them |

**Three surfaces, three jobs:**

1. **The alert channel** — broadcast only. Alerts, status changes and the occasional honest note about what changed.
   Members cannot post; this is not a discussion forum that happens to send push notifications.
2. **The support thread** — a topic in the channel's linked group, or the bot's DM. Every macro in
   `docs/P16-launch-assets.md` §7 is answered here, in public where the answer is reusable, in private where it
   involves somebody's money or identity.
3. **A weekly post** — Sundays, the same time, five lines: what shipped, what broke, how many people traded, and
   the two numbers from the scorecard that moved (including when they moved the wrong way). Boring on purpose, and
   the reason a bad week does not read as a crisis.

There is no Discord, no token, no "ambassador" programme and no leaderboard for who posts most. Every one of those
mechanisms rewards the people who talk the loudest rather than the people who trade, and this product's worst
outcome is a room full of people who talk about markets and lose money in them.

---

## 2. Useful before promotional

**The rule:** no promotional message goes into a room we have not been useful in first. In practice, that means:

* **Answer questions we are not paid to answer**, including about the venue's own mechanics and about competing
  tools. If somebody asks which bot has the best fee display and it is not ours, say so. This is not charity; it is
  the only way a channel with no brand gets believed about anything.
* **Post the alert before the announcement.** The channel exists for the tape. A launch post on day one and silence
  after is the signature of a product that is not being used.
* **Answer in public by default.** Half of support's value is the next person who reads the thread instead of
  asking.
* **No recruiting in other people's rooms** beyond the one cross-promo DM in the assets document, which asks before
  it posts and accepts no as a complete answer.
* **Nothing about other people's money.** We do not comment on an individual's positions in public, we do not quote
  a user's P&L, and public pages carry aggregates from the public tape, never a person's private activity.

---

## 3. Transparent loss handling

The product's credibility in this market will be set by what it does on a bad week, not a good one. The rules:

| Situation | What happens |
| --- | --- |
| A user's position is down | The P&L shows it, next to the drawdown, with the maximum loss. No window toggling, no "recent performance" framing, no hiding a red number behind a green one. |
| A user is on a losing streak | At five consecutive losses the P13 halt machinery fires: the banner appears with the P&L, live rules are paused with the reason stated, and `/stop` plus the kill switch are on the same screen. |
| A user has lost a lot | They are **not** messaged more often. Retention messaging that gets more urgent as a user loses is the most profitable and most indefensible pattern in this industry, and the banned list exists to stop it. |
| Our own numbers are bad | The weekly post says so, in the same format as a good week. |
| An alert was wrong | It is corrected in the channel with the correct number, not deleted. A silent correction teaches members that the channel's history is not evidence. |
| A bug lost somebody money | Publicly acknowledged, described in the changelog with the cause, and answered in the support thread with what was done. No hedging language about "edge cases". |

**No numbers about the future, ever.** No performance projections, no "if you had followed the last ten alerts"
arithmetic, no backtest of the channel's messages sold as a preview of the next ten. The config's banned list carries
the exact phrasing this rule is aimed at, which is why it appears nowhere in these documents. The two sentences that are allowed to
describe the future are the ones in the disclosure: *odds are a market, not a forecast*, and *no one can tell you
what a market will do*.

---

## 4. Status page and changelog

**Status page** — `/status`, public, no login, served from the same deployment as the landing page:

| Component | What it reports |
| --- | --- |
| Data feeds (markets, order books, fills) | Freshness lag per feed, from the same `freshness` payload the P15 alert engine reads |
| Alert delivery | Last alert composed, last alert delivered, delivery failures in the last hour |
| Deposits and withdrawals | Chain-scan lag, stuck-deposit count, withdrawals in flight — the P15 metric sample's own business keys (`stuckDeposits`, `withdrawalsInFlight`) |
| API | Success rate and p95 latency over the last hour |
| Trading | Whether order placement is accepting orders at all, and the reason it is paused if it is |

The status page shows the **same numbers the alert engine pages on**, which is what makes it trustworthy: a status
page with its own hand-maintained "all systems operational" banner is worse than no page, because it teaches users
that green means nothing. When the P15 SEV1 rule for unreconciled orders is firing, `/status` says so in the same
minute, because it reads the same source.

**Changelog** — `/changelog`, append-only, one entry per shipped change with the date, the phase tag from the build
log (`P15`, `P16`, ...) and one line of user-facing consequence. Three rules:

* Every entry says what a *user* can now do differently, or it is not an entry.
* Fixes are entries too, including the embarrassing ones, with the cause named. "Fixed an issue where deposits could
  show as pending for up to 40 minutes when the chain was congested" is an entry; "improvements" is not.
* An entry that removes something says so first, in the sentence, before describing what was added.

---

## 5. The support surface, and why it is a differentiator

**Impersonation is the dominant scam in this market**, and it is structurally easy here: a new Telegram account
with our name, a friendly DM, and a person who has just deposited for the first time. The defence is worth building
properly because the incumbents do not, and because a user who is scammed by somebody wearing our name does not
distinguish between us afterwards.

**Five defences, all of which are checkable:**

1. **We never DM first.** Stated in the `/start` message, in the pinned post, in the support macros and on the
   status page. A user who knows this cannot be recruited by a stranger's DM.
2. **No money flows through a person.** There is no admin who takes a deposit, unlocks a withdrawal, or accepts a
   payment outside the product. Every legitimate payment in this product is a card subscription or an on-chain
   transaction to an address the app displays for that user's own wallet.
3. **The support identity is verified and published.** The one account that answers in the support thread is linked
   from the pinned post and from `/status`; anything not linked from those two places is not us, whatever its name
   says.
4. **An in-app reporting path** with a macro (#6 in the assets document) that documents the account and reports it,
   rather than a "be careful out there" note that leaves the user to do it alone.
5. **We say the sentence out loud, everywhere**: nobody from Openout will ever ask for a seed phrase, a private
   key, or a deposit to unlock a withdrawal. Repetition is the whole mechanism — a user who has read it four times
   recognises the fifth message as a lie.

**No messages about returns** sits in the same category. Support answers "should I buy" with the macro in the
assets document: no, and here is what happened, with the age of every number.

---

## 6. Support operating rules

* **Response targets, published:** money in flight (deposits, withdrawals) within 2 hours during waking hours; bugs
  within a day; everything else within two days. Published because an unpublished target is a hope.
* **One thread per issue**, and it stays open until the user says it is resolved or the issue is closed with the
  reason stated. No "we'll take it from here" followed by silence.
* **Escalation path is real:** a money-in-flight issue wakes the owner, which is the same person as the on-call in
  `docs/P15-alerting.md`. There is no larger organisation to escalate to, and pretending otherwise would be the
  first lie in the relationship.
* **Tickets are the product's cheapest research.** Every macro that gets sent twice is a product defect; the second
  time it is sent, it is filed as one.
* **Nothing is closed as "won't fix" without the workaround in the reply.**

---

## 7. What would make this community a liability

* **A signal group by accident.** If the alerts start carrying an interpretation ("this looks bullish"), we are
  delivering advice with no licence and no honesty. The banned list is the guardrail; a human reading the channel
  once a week is the enforcement.
* **A support surface that only answers during good weeks.** The week the numbers miss is the week support has to
  be fastest.
* **Community as a growth loop.** Nobody is rewarded for inviting people, no referral pays for a signup, and
  `revenue/attribution.py` (P16's own code) pays only from **observed fees** — never from a deposit, never for
  volume that did not happen.
* **Impersonation with no answer.** If a fake account gets a single user to send funds and we have not published
  the five defences, the failure is ours, not theirs.
