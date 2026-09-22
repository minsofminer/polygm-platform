# P14 D6 — the audit decision, the bug bounty, and the questions for counsel

Three decisions, each recorded with the reasoning and the cost, because the kit asks for a *decision* rather than
an intention. Two of them commit money and one commits legal exposure, so they are written to be overruled by the
owner rather than to be silently obeyed: the recommendation, the alternatives, and the consequence of each option
are all here.

---

## 1. The audit decision

**Decision: commission an external penetration test before real funds, scoped to the money and identity paths,
with our own evidence pack handed to the tester rather than withheld.**

### Why an audit when this phase already ran five harnesses

The honest answer is what our own testing *cannot* cover, because "we tested it ourselves" is exactly the argument
that fails in an incident review:

* **A fresh set of eyes on the authorisation model.** D1's matrix proves that 95 served operations refuse the
  wrong principal *for the requests it knows how to build*. It cannot prove no door was forgotten, because it
  enumerates from the same registry the product does — the same source of truth cannot be both the map and the
  territory.
* **The venue and custody boundaries.** The executor has never talked to the real CLOB (`P06`'s seam is a CI
  double) and the signer has never wrapped a real key under a real custodian, so nobody — us included — has
  reviewed those paths under production conditions.
* **Independence as evidence.** A report signed by somebody who does this for a living is what a payment
  processor, an exchange partner or an insurer asks for. Our artifacts are excellent for *behaviour* and are not
  a substitute for that.

### Scope, in priority order (expected loss first)

| Priority | Scope | Why it is here |
|---|---|---|
| 1 | Authorisation, every user-scope route, two live accounts, plus the Telegram `initData` path | the one defect class that cannot be explained to a customer |
| 2 | The custody plane: key export ceremony, KEK rotation, break-glass, destination allowlist | keys and money; the kit's own rule ("no fix after launch") |
| 3 | The order path against the venue: idempotency, replay, TOCTOU, reconciler adoption | duplicate orders are unrecoverable |
| 4 | The Mini App surface: session handling in a webview, the proxy, framing | the surface with the largest audience |
| 5 | Infrastructure: the deployed API, its identity shape, and the executor's subnet when it exists | D4 found a live authorisation hole here, so it earns its place |

**Deliverable**: a report with a reproduction per finding, a severity, and a re-test result for every finding we
close. **Evidence pack we hand over**: `docs/verification/P14-*.{txt,json}` (all five), the triage file, and
`docs/P14-security-gate.md` — handing over our own findings makes the tester spend their hours on what we missed
rather than on what we documented, which is the difference between a cheap test and an expensive one.

**Budget band**: a 5–8 day engagement at market rates is the realistic shape for this scope with a solo builder
supporting it; anything materially cheaper is a scan with a report template.

**Timing**: after P15 puts the executor on a real host (so there is a subnet and a deployed signer to test), and
before the first real deposit. If the owner wants an earlier signal, a 2-day review of the authorisation and
custody paths alone is the highest-value slice and can run now.

**Consequence if it is skipped**: the product may still launch by our own evidence, and this document will say in
writing that no independent test was performed — which is the sentence that costs the most in a post-incident
review.

---

## 2. The bug bounty

**Decision: a private, invited bounty at launch, with published bands; a public program only once the audit's
findings are closed and the product has held real funds for a full month.**

| | Private (recommended now) | Public (later) | No program |
|---|---|---|---|
| Duplicates | manageable | a constant tax at this size | n/a |
| Cost | paid per accepted report | paid per accepted report + triage time | nothing |
| Signal | a handful of researchers we choose | everyone | none |
| Risk | small pool | noise, and a queue nobody staffs | researchers sell to the other side |

**Structure**:

* **A security page that states safe harbour in plain words**: we will not pursue anyone who reports in good faith,
  who does not access data that is not theirs, who does not degrade the service, and who gives us a reasonable
  window before disclosure. Written first, because a bounty without safe harbour is a trap.
* **Bands**, set against expected loss rather than against what a report "feels" worth: critical (funds or keys
  reachable, cross-account access) — **USD 2 000–5 000**; high (authenticated-only compromise, authorisation
  bypass on a read path) — **USD 750–2 000**; medium — **USD 150–750**; low — thanks plus swag. An out-of-scope
  list is part of the page (missing headers on a static asset, self-XSS, rate limiting that is documented).
* **The triage commitment**: a human answer within 72 hours, a status page for open reports, and **no finding
  closed without a re-test** — the same rule this phase has been built on, so a reporter can see their report end
  in a test that fails on the old behaviour.
* **A budget line**: 5 % of the first year's security spend, reviewed after the first ten reports. A bounty with no
  budget is a promise that turns into an argument at the worst moment.

**Why not public immediately**: because a public program's first week is a duplicate storm, and duplicate triage
competes with the same hours the launch needs. The audit plus this phase's harnesses cover the ground a public
program would cover in month one.

---

## 3. Questions for counsel

Each of these is written as a question with the facts a lawyer needs, because "is this legal?" cannot be answered
and "we run a Telegram Mini App that lets Indian residents place orders on Polymarket through a delegated wallet
where we never take custody or hold funds, and we take a builder-code share of the venue's fee" can.

**A. Custody and regulatory characterisation**

1. We do not hold funds or keys in a way we can move: the wallet is delegated (the user's key material is wrapped
   under our KEK but an export ceremony returns the wrapped material to the user), and every deposit and trade goes
   directly to the venue. Does that keep us outside India's VDA custody definitions, and what changes if the export
   ceremony is temporarily unavailable while a user wants out?
2. We route orders to Polymarket, a foreign venue, and earn a builder-code share of its fee. Are we (a) a
   technology provider, (b) an intermediary of a foreign exchange, or (c) an unregistered gaming/betting operator —
   and which of the three characterisations does each Indian regulator (RBI, SEBI, MeitY, the GST council) apply?
3. We advertise "prediction markets". What are the disclosure obligations before a user's first deposit, and is a
   prominent risk acknowledgement (a typed confirmation, like the ones we already require for export) enough?

**B. KYC, AML and sanctions**

4. With crypto-only funding, no fiat on-ramp of our own, and per-transaction caps, what are our obligations under
   the PMLA and the VDA travel-rule guidance — and specifically, is a self-declared identity plus device binding
   defensible at our size, or is document verification required from the first user?
5. We screen wallets? We currently do not. What is the minimum viable sanctions screening (on the wallet address
   at deposit, on the withdrawal destination, or both) that a regulator would consider reasonable, and what do we
   do when a match is partial?

**C. Product specifics that are not generic**

6. Our referral program pays nothing in cash — it grants fee discounts — and we revoke a builder code when
   self-referral is detected. Is a revoked revenue share a contractual risk (the user's expectation of a payout) or
   is the discount structure itself a promotional scheme that needs registration?
7. We show a leaderboard with pseudonymised wallets, and P14 verified we never publish a wallet address against a
   pseudonym. Are the pseudonyms personal data under the DPDP Act once a user can link them to themselves, and does
   our "no wallet address published" rule satisfy the de-anonymisation duty?
8. Our copy-trading feature mirrors one user's trades into another's account with a delay of at least one block's
   worth of venue latency. Do we take on any fiduciary or advisory duty by ranking sources on risk-adjusted return
   — i.e. are we "advising" even though we publish no recommendation?
9. Telegram is our primary distribution channel and the Mini App frames inside it. What do Telegram's terms
   require of us when we accept orders there, and does the Telegram Stars path for in-app purchases change the
   characterisation of our subscriptions?
10. We log no IP address in plaintext (a pepper-hashed value only) and hold security events for 90 days. Is that
    enough for the record-keeping a financial intermediary is expected to keep, and what retention is *required*
    rather than merely allowed?

**D. The questions that gate the launch**

11. If the answer to A2 is "gaming/betting", does that put the product out of scope entirely in India, or require
    geo-restriction rather than closure?
12. If the answer to B4 is "document verification from the first user", what is the lightest compliant path that
    does not change the onboarding we have built?

**Deliverable expected from counsel**: written answers to A, B, D before the first real deposit, and to C within
the first quarter. The answers go into `docs/AGENTS-BUILD.md` as constraints — the same way "no real funds until
P13/P14 green" got there — so they are enforced by the build rather than remembered.
