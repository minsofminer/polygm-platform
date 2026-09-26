# P16 — the launch assets

*The words, written to be copy-pasted. Every piece below is checked by `tools/p16-gtm-check.py`: the banned list in
`config/gtm.json` may appear in none of it, and the three required phrases must appear in the `/start` message, in
the pinned channel post, and in the footer of every surface that renders a number.*

**The three required phrases**, which are the whole tone of this launch in one place:

* **Affiliation** — "not affiliated with Polymarket".
* **What a number means** — "odds are a market, not a forecast".
* **What we cannot do** — "no one can tell you what a market will do".

Two of these are handled by code rather than by copy discipline: the disclosure is a component
(`web/src/legal/disclaimer.tsx`) rendered by the landing page, the Mini App and every public page's footer, and its
text is asserted in `web/src/legal/disclaimer.test.tsx`. The `/start` and pinned messages are written by hand, which
is exactly why they are in this file where the checker can read them.

**Voice rules for everything below:** short sentences. Numbers with their age. No state of mind we cannot verify
("excited", "thrilled"). No urgency. We are the tool that tells you what happened; we are not the friend who knows
what happens next.

---

## 1. The landing page

The landing page is `web/app/page.tsx` — a server component with no client JavaScript beyond the shell, because a
page that ships 200 KB to say "come sign up" is a page nobody on a phone reads. The hero it does not have yet is
below; when it lands, the paragraph goes above the two buttons and the `<h1>` stays the shell tagline.

**Hero**

> **Openout** — the tape, the books and the wallets behind them, on one keyboard.
>
> Watch a market move and see who moved it. Fill sizes with the age of every price next to the number, order books
> you can read, and wallets you can follow without a spreadsheet. Openout reads Polymarket's public data and shows
> you what a whale is doing while it is doing it.

**Three-line product statement (under the hero)**

> Everything you can see is computed from the public tape, including the P&L, the rank and the win rate — ours, not
> the venue's, and labelled as ours. Where we can be wrong, we say which number is a guess.
>
> Free: alerts on large fills, volume spikes, new markets and markets about to resolve. Personal alerts on the
> markets and wallets you choose. Pro, $12/month: automation rules, unlimited watchlists, the API and the depth.
>
> **Odds are a market, not a forecast.** No one can tell you what a market will do. Prediction markets resolve to
> zero all the time, and nothing here is financial advice.

**Footer (rendered by `DisclaimerFooter`, same on the Mini App and every public page)**

> Openout is an independent product and is not affiliated with Polymarket. We read Polymarket's public data; we do
> not speak for them, and they have not endorsed anything here.
>
> Odds are a market, not a forecast. No one can tell you what a market will do, and nothing on this site is
> financial advice.
>
> Prediction markets resolve to zero all the time. You can lose everything you deposit, and the number in the trade
> ticket is the most you can lose on that trade.

---

## 2. The `/start` message

The first thing a stranger reads, and the message the funnel's third step is measured on (95% of people who tap the
deep link must understand the product from **two lines**). The structure is: what this is, what it costs, what a
loss looks like — then one button.

> **Openout** — alerts and a trading terminal for Polymarket, inside Telegram.
>
> I post large fills, volume spikes, new markets and markets about to resolve, up to twelve times a day and never
> between midnight and 6am local. Every alert links to the market it is about, and you can trade it from the alert
> in a couple of taps.
>
> **It is free.** A wallet is created for you when you open the app; $10 is enough to place a trade. Pro is
> $12/month for automation rules, unlimited watchlists, the API and order-book history — and no feature you need in
> order to *exit* a position is ever paid.
>
> **Odds are a market, not a forecast.** No one can tell you what a market will do. These markets resolve to zero
> all the time and you can lose everything you deposit; every ticket shows you the most you can lose before you
> confirm.
>
> Openout is not affiliated with Polymarket.
>
> [ **Open the terminal** ] · [ **See what the channel posts** ]

*Notes for whoever pastes this:* the message must fit one screen without scrolling (Telegram's preview truncates),
the button is the Mini App deep link with a `start` parameter so the first screen is the market from the alert, and
`/stop` is documented in the same message when it is sent as a reply to a fill — never as a surprise later.

---

## 3. The pinned channel post

Pinned before the first alert goes out. It sets the deal: what gets posted, how often, what never gets posted, and
what happens when a member loses money. It is also the only place where the channel's own probability of being
boring is admitted, which is the point.

> **What this channel is**
>
> Large fills, volume spikes, new markets and markets resolving soon, from Polymarket's public data. Up to twelve
> posts a day, never between midnight and 6am local, one market per message. Every price carries its age.
>
> **What this channel is not**
>
> Not a signal group. There are no calls here, no targets, and no plan for your money. If you see a post that reads
> like advice, it is a mistake and I want to hear about it.
>
> **What it costs**
>
> Nothing. The alerts are free and stay free. The terminal is free, and Pro ($12/month) adds automation rules,
> unlimited watchlists, the API and order-book depth. Nothing you need in order to close a position or withdraw is
> ever behind a paywall.
>
> **What a loss looks like**
>
> These are markets that resolve to zero all the time. Some of the fills posted here are somebody else's very good
> day and your very bad one. **Odds are a market, not a forecast** — no one can tell you what a market will do,
> including me, and I will never tell you to add to a losing position.
>
> Openout is an independent product and is not affiliated with Polymarket. We read the venue's public data and we do
> not speak for them.
>
> [ **Open the terminal** ]

---

## 4. X / Twitter launch post

Not a chosen channel (§3 of the plan ranks it fourth and refuses it for now), but written, because the day the
channel works is the day this is needed and the copy written that morning is the copy that over-claims.

> I built a terminal for Polymarket that lives where I already am.
>
> It posts the tape — large fills, volume spikes, new markets, resolutions — into a Telegram channel, and every
> alert deep-links into a trade ticket that arrives pre-filled. Wallet is created on first open. $10 is enough.
>
> Free, and the alerts stay free. Pro is $12/month for automation and depth.
>
> The honest part: odds are a market, not a forecast, these markets resolve to zero, and half the fills I post are
> somebody losing money. I do not know what happens next and I am not going to pretend to.
>
> Not affiliated with Polymarket — I read their public data.
>
> [t.me/openout …]

**Thread continuation (only if asked; two posts, no more):**

> 2/ What is actually different: the alert is the product. One market, the whole thing legible in the preview line,
> a button that opens the trade ticket on that market. The old flow was "here is a link, go find a browser".
>
> 3/ What is measured: whether people who arrive from an alert place an order, and whether they come back on day 7.
> Both numbers are public in my build log, including the weeks they were bad.

---

## 5. Telegram launch post (in-channel) and the cross-promo DM

**In-channel launch post** — the first thing the channel's members read, sent the same hour the pinned post goes up:

> First post. Here is what happens from now on.
>
> Up to twelve alerts a day, never between midnight and 6am local, one market each: large fills, volume spikes, new
> markets, and markets resolving in the next six hours. Prices carry their age. No calls, no targets, no plans for
> your money.
>
> Two things I will not do. I will not tell you a comeback is likely, and I will not tell you what a market will do.
> Odds are a market, not a forecast.
>
> Everything you can do with these alerts is in the app behind the button, and the parts you need to exit a position
> are free forever. Not affiliated with Polymarket.
>
> If the alerts are not worth your attention, mute me — that is what the mute button is for, and a channel that
> fights it is a channel that deserves it.

**Cross-promotion DM** (to a similarly-sized non-competing channel's operator — one message, no follow-up, sent
only after the week-8 gate clears):

> Hi — I run a Telegram channel that posts large fills, volume spikes, new markets and near resolutions from
> Polymarket's public data, up to twelve a day, no calls of any kind. Around [N] members, mostly hourly crypto
> Up/Down traders.
>
> I think our audiences overlap without competing: you cover [their beat], I post the tape. Would you be open to a
> straight swap — one post each, no money, no affiliate links, and no requirement that either of us claims the other
> endorses anything? Happy to show you last week's posts first so you can judge the quality rather than the pitch.
>
> Either way, thanks — and if the answer is no, that is a completely normal answer.

---

## 6. Product Hunt / Hacker News

### 6.1 The honest audience assessment (read this before posting)

**Product Hunt is probably the wrong audience and we are posting anyway.** PH's readers are product people and
indie hackers; the people this plan needs are 5-minute crypto Up/Down traders who live in Telegram, and those two
populations barely intersect. A PH launch that "wins the day" delivers a few hundred curious visitors, a spike in
signups that never fund a wallet, and — worse — a *metric* that looks like traction and isn't. Expected outcome,
stated in advance so nobody misreads it afterwards: **50–150 visitors, 5–15 signups, 0–2 activated users.** It is
worth doing because the cost is one hour and it produces one durable artefact (a landing page with a real
description on it, which the SEO channel then points at), and it is *not* worth optimising.

**Hacker News is the better fit and the more dangerous one.** HN's audience will read the build log, which is the
most unusual thing about this project — every phase gated, every number with a provenance tag, the failures kept.
It will also, correctly, ask hard questions about a product that makes it easier to trade a leveraged-ish instrument
on a 5-minute horizon, and it will find the weakest number in the plan if there is one. That scrutiny is the point
of posting; the answer to it is the plan, not a defence of it.

### 6.2 The Product Hunt post

> **Openout — a Polymarket terminal in your Telegram**
>
> **Tagline:** The tape, the books and the wallets behind them, on one keyboard.
>
> **Description:** A Telegram channel posts large fills, volume spikes, new markets and near resolutions from
> Polymarket's public data — one market per message, every price with its age. Each alert deep-links into a trade
> ticket that arrives pre-filled, and a wallet is created on first open. Free. Pro is $12/month for automation
> rules, unlimited watchlists, API access and order-book depth; nothing you need to exit a position is ever paid.
>
> What is different: the alert *is* the product. One market, legible in the preview line, one tap to the ticket.
>
> What is honest: odds are a market, not a forecast. No one can tell you what a market will do, these markets
> resolve to zero, and you can lose everything you deposit. We are not affiliated with Polymarket; we read their
> public data and label every number we compute ourselves.
>
> **Maker comment:** Built in the open — 16 phases, each with a quality gate, a build log and the failures kept in
> it. The four questions I could not answer from a spreadsheet were: who the first thousand users are, where they
> are, what to say to them, and what to do if it is half. All four are written down in the repo, including the
> half-answer.

### 6.3 The Hacker News post

> **Show HN: Openout – a Polymarket terminal that lives in Telegram**
>
> I built a tool for a niche I am actually in: the 5-minute crypto Up/Down markets on Polymarket. The workflow
> everyone uses is a bot posts a link and you go find a browser. This posts the tape into a Telegram channel —
> large fills, volume spikes, new markets, near resolutions, up to 12/day, one market per message, price age on
> every number — and the button on the alert opens a trade ticket already filled in for that market. Wallet is
> created on first open, $10 is enough to trade.
>
> Things HN will ask, answered up front:
>
> * **Is this a signal group?** No. It posts what happened, with the market and the size, and no opinion. There is a
>   banned-words list in the repo that the build fails against.
> * **Is this good for people?** Fair question, and I do not have a comfortable answer. It makes a fast, loss-making
>   instrument easier to reach. What I control: the maximum loss is printed on the ticket before you confirm, the
>   losing-streak handling pauses rules rather than encouraging a comeback, the exit and the withdrawal path are
>   never paid, and there is a kill switch in the same screen as the P&L.
> * **Where does the data come from?** Polymarket's public API. We are not affiliated with them and we say so in the
>   footer of every page.
> * **How does it make money?** A $12/month Pro tier, and builder fees on routed volume that start at **zero** and
>   only rise after retention holds (0 → 10 bps at day 90 → 25 bps at day 180, with the preconditions in code).
>   Never a token.
> * **What proves any of that?** The repo's build log: every phase gated, the verification outputs recorded, and the
>   numbers that missed left in.
>
> The unglamorous part is in the plan: 1.25% of channel members become funded traders, 2,000 members is 11% of the
> addressable Telegram segment, and if the number is half I switch audiences rather than build more features.

---

## 7. Ten support macros

Support is a trust surface, not a cost centre: in this market, the support reply *is* the product's relationship
with money. Every macro below obeys three rules — no state of mind we cannot verify, no number without its age, and
**never a sentence that treats a deposit as the answer to a loss**.

**1 — Deposit not showing**
> Thanks for the details. Deposits are credited after the chain confirms, which is usually a few minutes and can
> take longer when the network is busy. I can see the scan for your address: [state + last checked time, and the
> link]. Nothing is lost while it is in flight. If it has been more than [N] minutes I will take it from here and
> update you in this thread, not by asking you to check again.

**2 — Withdrawal pending**
> Withdrawals leave our side immediately and land when the chain includes them. Your withdrawal: [amount, requested
> time, transaction state]. I will post the transaction hash here the moment it exists. If it has not moved in
> [N] hours I will escalate it and tell you what I found, including if the answer is that it is delayed.

**3 — I lost money, what now**
> I am sorry — that is a real loss and it is yours, not a rounding error. Here is exactly where you stand: [position,
> size, entry, exit or current mark, and the amount]. Nothing here is advice, and I am not going to suggest you add
> to it or try to make it back. If you want to stop trading, `/stop` halts alerts and the kill switch is on the same
> screen as your P&L; both are free and always will be. If you want your money out, the withdrawal path is here:
> [link]. I will stay in this thread until you have what you need.

**4 — Is this financial advice / should I buy**
> No, and I will not answer that. Odds are a market, not a forecast, and no one can tell you what a market will do —
> including me. What I can do is show you what happened: [the data, with its timestamp] and the maximum loss on the
> ticket before you confirm.

**5 — Are you Polymarket**
> No. Openout is an independent product and is not affiliated with Polymarket. We read their public data and label
> every number we compute ourselves. Anything that looks like it comes from the venue's own support is not from us.

**6 — Someone DM'd me offering to double my deposit**
> That is not us, and nobody from Openout will ever DM you first about money, ask for a deposit to "unlock" a
> withdrawal, or promise a return. If you have sent funds, tell me the details and I will help you document it, and
> report the account in Telegram. We never ask for a seed phrase or a private key, and we never will.

**7 — Alert too noisy / why so many messages**
> Understood — the channel posts at most twelve times a day and never between midnight and 6am local, and I would
> rather you mute it than resent it. If you want fewer: personal alerts are yours to configure, and you can follow
> only the markets and wallets you care about. Tell me which kind you would drop and I will look at whether it is
> worth posting.

**8 — Automation rule not firing**
> A rule fires when its conditions are met and the gate in front of it is open; the common reason it stays quiet is
> [reason, from the rule's own log]. Your rule's state: [last checked, last fired, why it did not fire at the last
> check]. I will keep this thread open until it has either fired or you have decided to turn it off — and if it
> fired wrongly, I want the details, because that is a bug and it is the kind I care most about.

**9 — Do you do referrals / affiliate**
> Not yet, and not as a growth tactic. When the paid tier is stable the referral programme will pay from observed
> fees only — never from a deposit, and never for a signup that does not trade. I would rather tell you that than
> sign you up for something half-built.

**10 — Is my data private / what do you store**
> We store what the product needs: your account, your positions, your alerts and the events those produce. We do
> not sell data, we do not publish anything that identifies you, and public pages show aggregates computed from the
> public tape rather than a person's private activity. [What we keep, and for how long — with the link to the
> policy.] If you want your data deleted, ask and I will do it and confirm when it is done.

---

## 8. Where each asset is checked

| Asset | Checked by |
| --- | --- |
| Landing footer, Mini App footer, public page footers | `web/src/legal/disclaimer.test.tsx` (11 tests) + `c5` of `tools/p16-gtm-check.py` |
| `/start` and pinned post | `c5` — the required phrases must be present in each of those two sections, and no banned string may appear anywhere in this file |
| This file, the plan and the community document | `c5` — banned list |
| All of the above | A human reading it aloud once before it is posted, which is the only check that catches a sentence that is technically clean and still wrong |
