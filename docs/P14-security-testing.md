# P14 — security testing

P14 is the phase that decides whether this product may hold real money. The kit states the consequence itself and
it is repeated here without softening: **if break-glass takes longer than an hour, or if any authorisation test
fails, the product does not launch** — and that is said in writing before any result is reported, so that a passing
run cannot be read as permission and a failing one cannot be argued away.

Two other constraints from the kit run through everything below:

* **No finding is closed without a re-test.** Every defect found in this phase has a test that fails on the old
  behaviour; the test is named next to the finding.
* **Nothing on keys or authorisation may be deferred to after launch.** "Fix after launch" is not an option for
  this class, at all.

The targets are the deployed surfaces (`polygm-api`, the Mini App, the bot), and the scope is ordered by *expected
loss*: what a user loses if it goes wrong, times how likely it is to go wrong. The order the kit gives — authorisation
first — is the order the work follows, because a cross-account read is the one defect that cannot be explained to a
customer.

---

## D1 — the penetration-test plan, scoped by expected loss

### The authorisation matrix, run against the running app

`tools/p14-authz-matrix.py` is the phase's first deliverable and its most important one. It is not a reading of the
P07 registry — that registry exists and its own gate audits it. This is the other half: **behaviour, per route, with
two real accounts**, booted in-process against a throwaway migrated-and-seeded database and in the **production
identity shape** (`PGM_REQUIRE_SECURITY_ENV=1`, which is what the deployed box runs: `X-User-Id` is a development
convenience and a bearer session is the only way in).

Recorded run: `docs/verification/P14-authz-matrix.txt` / `.json` → **`37 checks passed, 0 failed`**.

| Section | What it establishes | Result |
|---|---|---|
| `registry` | every served operation has an authorisation decision; every level is a known level; declared-but-unserved rows equal the registry's own written promise | 95 served, 109 declared, 0 undeclared, 0 drift |
| `docs` | the interactive API surface is **absent** in the production shape — and the check asks a real path, so a 404-everything box cannot pass it | 4 of 4 paths 404 |
| `anonymous` | no non-public operation answers an anonymous caller; the public half is reachable (33 of 33) | 62 refused, 0 leaks |
| `escalation` | a user session reaches none of the 14 admin operations; a wrong admin token is never accepted; no admin token configured answers 503, not 403 | 14 of 14 refused |
| `needles` | A carrying B's identifiers gets no needle back — and only where B's own call *did* surface one (19 conclusive probes, the rest named as inconclusive) | 0 leaks |
| `own` | every user operation accepts its rightful owner (48 answered, none 401/403, none 5xx) | green |
| `foreign` | A acting on B's objects with **valid** payloads: cancel, withdraw, allowlist, key export, session revoke, automation, alert, copy caps, plus IDOR reads | 20 attacks, 0 accepted, 0 rows changed |
| `auth` | initData (forged, truncated, expired, wrong token, replayed), sessions, the second factor, the destination allowlist, and an IDOR fuzz | 16 probes, 0 wrong; 39 hostile ids, all answered |

Four controls in the tool exist because the tool got them wrong first, and they are the reason a green run here
means something:

1. **A probe counts only once B's own call surfaced a private identifier.** Without that sensitivity control, a
   scan where nothing is ever surfaced reads as a clean bill of health. The report splits the two populations —
   19 conclusive, 29 named as inconclusive — rather than averaging them.
2. **Every cross-account attack carries a valid payload and is followed by a re-read of the victim's row.** A 422
   on a malformed body proves nothing about authorisation; a refusal that still mutated the row is the worst
   outcome and a status-code-only suite misses it. The post-state read has its own canary (a row changed by the
   harness must be visible to it).
3. **The run asserts both sessions are still alive at the end.** The needle scan fires every user operation,
   including `logout` and session revoke; the first version logged A out halfway through and every later 401 was
   counted as a refusal — a green report produced by the harness breaking itself.
4. **Refusals are read by their code, not by their status.** An over-cap order answered `503 STALE_QUOTE` (a
   stale book) and the risk-gate check *passed on it*: a refusal for an unrelated reason is not evidence about the
   control under test.

### Findings, and their re-tests

Each of these is a defect found by D1, fixed in the same chunk, with the test that fails on the old behaviour.

**F1 — a stored referral link token that is not a link token answered `500 INTERNAL retryable:true`, for ever.**
`GET /v1/referrals/me` and `POST /v1/referrals/code` both build a URL from the account's stored token, and
`link_for` raises on a token without the `ref_` prefix: the whole referrer dashboard was unreachable, and
`retryable: true` told the client to retry a request that can never succeed. The row is ours and the replacement is
unguessable either way, so it is **repaired** — a fresh token is minted, the broken row is retired, and the event is
written to the audit log as `referral.link_repaired` — rather than answered with an internal error.
*Retest:* `tests/test_referrals_api.py::test_a_stored_link_token_that_is_not_one_is_repaired_rather_than_a_500`.

**F2 — claiming the referral code you already hold was a `500`, permanently.** The path retired every one of the
caller's short codes and then inserted the same primary key: a UNIQUE violation, found by sending one body twice —
which is what a flaky connection does. It is now the state the caller already has, and rotating still works.
*Retest:* `tests/test_referrals_api.py::test_claiming_the_code_you_already_hold_is_not_an_error`.

**F3 — the interactive API surface was served in the production shape.** `/docs`, `/redoc` and — the largest of the
three at **137 KB** — `/openapi.json` (87 paths, every parameter name, every enum, every error code) answered
anyone who asked on the API's own origin. The production identity shape now turns the whole surface off, and
`PGM_DOCS=1` turns it back on for local work and for the tools that read the schema.
*Retest:* the matrix's `docs` section (4 of 4 paths 404), with a canary that a *served* schema is caught.
*Note:* this finding was invisible to the first scan because FastAPI adds `/openapi.json` to the router lazily —
a route that exists but is not in `app.routes` is a route a scanner walks past, and the check now asks for the
schema explicitly.

**F4 — the registry's "stale" rows were a promise nobody could check.** Thirteen rows were declared and unserved;
a test asserted they matched a list written *inside the test file*, so both sides of the comparison were the
test's own copy and a route renamed out from under the table could not be noticed. The promise now lives in the
product (`authz.PLANNED`), and the matrix cross-checks it against the running router **in both directions**: a row
that is neither served nor planned is drift, and a planned row the API now serves is a promise nobody collected.
*Retest:* the matrix's two registry checks and their drift canary;
`tests/test_security_plane.py::TestAuthzTable::test_every_served_route_declares_a_level_and_the_mirror_agrees`.

**F5 — the order path had no admin door, and that is now asserted rather than assumed.** The kit asks that admin
paths must not bypass the risk gate. There is no admin operation on the order path at all (14 admin operations, none
of them an order route), an admin credential does not substitute for a user session, and an over-cap order is
refused **for the limit reason** (`403 OVER_ORDER_CAP`) while an order inside the limits is still accepted.
*Retest:* the four risk-gate checks in the matrix's `foreign` section.

### What the matrix did *not* find, and how it knows

Findings F1–F5 are all the "wrong answer" class: a 500 where a 200 belongs, a page that should not exist, a table
that lied about itself. **No authorisation defect was found**: no cross-user read, no cross-user write, no
anonymous 2xx on a non-public operation, no escalation, no IDOR that answered 5xx or leaked. The matrix says so in
the only way that is worth anything — by naming the controls that would have caught one (above), each with a
canary, and by refusing to count inconclusive probes as passes.

Two surfaces the kit names are **not testable over HTTP today**, and that is recorded rather than papered over:

* **"A cancels B's order."** There is no HTTP route that cancels an order: the registry declares
  `POST /v1/orders/{intentId}/cancel` and the app serves no such path. The cancellation paths that exist are the
  bot's `/stop` command and the automation `cancel_open` action. The matrix keeps the probe (route-driven, so the
  day the route ships it is covered) and the finding is the honest one: the kit's scenario has no surface yet.
* **"Service-to-service order intents."** No HTTP route accepts a service credential — three probes with
  `X-Service-Token` are ignored — and the shared secret guards the *executor*, which is a worker rather than a
  service. The matrix therefore asserts the two things that are true and checkable here: the credential is not an
  API identity, and the comparator that guards the executor's door answers correctly in both directions (it
  refuses an empty or missing secret, and accepts the real one). The executor's own refusal of a foreign intent is
  `tools/p08-gate-check.py`'s drill.

### The trading, injection and business-logic probes

`tools/p14-attack-surface.py` is the second half of D1, and it asks a different question from the matrix. The matrix
answers *who may do what*; this tool answers **what happens when an authorised user sends the wrong thing on
purpose**. Every probe in it is made by the rightful owner of a qualified account, because that is the threat model
the kit names: a user attacking the venue's rules, the risk gate, or another user through the product's own
surfaces.

**77 checks, 0 failures, 1 OPEN** (`docs/verification/P14-attack-surface.{txt,json}`). The one remaining OPEN is a
*latent surface* rather than a soft pass — a payment-fulfilment path that does not exist yet — and it is listed at the
end of the record with the exact work it needs on the day it ships. Every finding this phase made *about the
product's own detectors* has since been closed, the last two as changes to the product rather than to the probe: the
close ceiling (a risk-review decision, in the gate) and the tax export's writer (below).

#### Trading: what a bad order does

| Probe | Result |
| --- | --- |
| A legal order is accepted first (the must-accept) | `202`, so the refusals below mean something |
| Off-grid price (`0.5555` on a `0.01` tick) | `422 OFF_TICK`, **not rounded** — and the refusal left a `rejected` intent row and no live order |
| Size below the minimum / zero / negative / enormous / not-a-number / `1e30` | `422 BELOW_MIN_SIZE`, `422 ZERO_SIZE`, `422 BAD_AMOUNT`, `403 OVER_ORDER_CAP`, `422`, `422` |
| TOCTOU: quote, age the book past the gate's 5 s window, then submit | `503 STALE_QUOTE` — the gate re-reads freshness at submit rather than trusting the price the user saw |
| The same idempotency key with the same body, then with a different body | one order, then `409 IDEM_CONFLICT` |
| Eight concurrent over-cap submits (the race the gate runs on SQLite) | all `403 OVER_ORDER_CAP`, no 5xx, nothing half-placed |
| An automation action above the per-order cap | refused at **save** time with a sentence naming the cap (F19) |
| 500 mirror copiers of one whale fill | each capped at its own $25 order ceiling, 500 distinct keys, a replayed fill producing exactly one key — and, since P16, **run end to end**: 500 real intents, a filling venue, fan-out 86–92 ms, $6,375.00 aggregate (largest $12.75 of $25.00), 500/500 fills booked |

#### Injection

| Probe | Result |
| --- | --- |
| SQLi (`' OR 1=1 --`, `'; DROP TABLE markets; --`, …) across four routes | no result-set change, no 5xx, no SQL error text, table intact |
| Prototype pollution (`__proto__`, `constructor` in an order body) | `422 VALIDATION`, refused as unknown properties |
| Stored XSS through the chat renderer (market question, side, refusal card, limbo card) | escaped at assembly; the renderer's own scanner reports no tag-level failure — **and a deliberately unescaped canary card is caught**, so the green is falsifiable |
| The same hostile text stored in an alert rule's `params` | round-tripped byte-for-byte, escaped at render rather than at save |
| SSRF: every declared route (87 paths) enumerated for a caller-supplied fetch target; every endpoint constant walked with an AST | **0 URL-ish parameters**, 5 endpoint constants all pointing at known vendors, 3 socket-opening call sites |
| SSRF through the one caller-supplied address in the product (a Pro webhook's `params.url`): 20 hostile URLs through the transport, a split-horizon DNS answer, a redirect into `169.254.169.254`, and the same hostile URL saved through the served API | **all 20 refused with zero sockets opened**, the private DNS answer refused, the redirect refused **at the hop** (1 connection, not 2), the signed delivery verified against a signature recomputed from first principles, save refused `422 VALIDATION` while a public URL saves `200`, and a refused target dead-lettered rather than retried |
| ReDoS: 10,000-character pathological parameters | worst 6 ms, all refused or answered |
| CSV injection in a tax export | **closed after P16**: the export is built in the browser (no route exists to probe), so the guard lives in the writer every cell passes through — `csvCell` in `web/src/terminal/portfolio.ts`, with 12 assertions in the module's own tests (see below) |

#### Business logic: getting value you did not earn

| Probe | Result |
| --- | --- |
| An account applying its own referral link | `409 SELF_REFERRAL`, no attribution row, audit says `builder_code_revoked: true` **and** `builder_code_state: "disabled"`, and the programme's builder code is `disabled`/`manual` with the account named |
| The same account applying it **again** | still `409`, and the audit row reads `builder_code_state: "already-disabled"` — the state is what tells an operator the programme is still off; the boolean alone said "nothing happened" (closed after the phase, see below) |
| An operator clearing that review | `POST /v1/referrals/review` with `decision=clear` puts the code back (`active`/`manual`, note naming the review, the actor and the reason) and writes a `referral.builder_code.cleared` audit event |
| The same clear against a code the **venue** disabled | untouched (`disabled`/`venue_rejection`), and no restore event is written — our review cannot overrule the programme's owner |
| Two accounts claiming one referrer from the **same funding source** | the second is refused and recorded as `refused`/`duplicate_funding`, earning nothing |
| Six fresh accounts claiming one referrer | 3 attributed, 3 held for `velocity`; every held claim carries its reason; **none has earned anything**, because a referral is worth $0 until a matched order clears the threshold |
| Wash trading: a same-wallet round trip 30 s apart at one price, plus one genuine trade 3 h later | $500 of round-tripped volume subtracted exactly once, the genuine $620 trade kept (verified $1,120 of $1,620) |
| The washer's controls | another wallet's opposite fill washes $0; a pair outside the window washes $0 |
| A copy farm (12/12 fills mirroring a wallet that traded 5 s earlier) | named, with the leader, the count and the rule |
| Free tier: a `webhook` (Pro) alert channel | `402 PLAN_REQUIRED` with an actionable sentence |
| Declaring `plan: pro` in the request body | `422` as an unknown field, and the account still reads `free` |
| The concurrent automation-rule cap through the API | 10 created, the 11th refused `409 RULE_CAP` |
| A forged Telegram update (wrong secret header) | refused, **zero claim rows**: nothing was processed |

#### Findings F18 and F19

**F18 — a zero-size order was an unbreakable 500.** `parse_usdc("0")` is a valid zero, so `"size": "0"` walked past
the parser, reached the risk gate, was denied `ZERO_SIZE` — and then the refusal could not be *recorded*:
`order_intents` carries `CHECK (size_micro > 0)` and the schema raised on the deny path's own INSERT. The user saw
`500 INTERNAL`, whose message is "retry with the same Idempotency-Key", and every retry re-derived the same denial
and 500'd again. A client-side typo became an infinite loop with no way out.

The fix keeps the schema invariant (a zero-size row must never be storable) and refuses the input *before* anything
is recorded, exactly as a non-numeric or negative size already was: `422 ZERO_SIZE`, key released, no row. The gate
keeps its own `ZERO_SIZE` check for every other caller. *Retests:*
`tests/test_api.py::TestRefusalsThatCouldNotBeRecorded` (4 tests) — the authored refusal, no row written, the key
still usable for the corrected request, the schema invariant unmoved, and a matrix of thirteen malformed money
inputs proving none of them can produce a 5xx or an `INTERNAL`.

**F19 — an automation rule could be armed that the gate would refuse on every fire.** The engine validated shape but
had no per-order ceiling, and the builder's own error message ("a rule needs at least one market to watch") had made
an earlier version of this probe report a pass. With targets present, a rule whose action was 20,000 shares at $0.55
— **$11,000 on one order, against a $2,500 per-order cap** — compiled cleanly, saved, and would have dry-run clean
too (a dry run places nothing, so the gate never sees it). Every live fire would have been refused `OVER_ORDER_CAP`:
an armed rule that can never trade, with the product silent at the one moment the user could have acted.

The fix is the check the console already claimed its compiler performed: `engine.validate_rule` now refuses an action
above the per-order ceiling, with a sentence naming the cap, and the ceiling is read from `config.flags` at both save
and fire time (an incident response that lowers `max_order_notional_micro` must bind every door into the venue, not
just the hand-placed one). A `market` action is measured at $1 a share — the most a share can cost — so it is
refused conservatively rather than skipped. *Retests:* `tests/test_automation_api.py` (4 tests: above the cap refused
with the cap named, just under the cap still saves, a `market` action measured at the worst case, and the live flag
moving the ceiling without a restart).

#### What the probes found about the product's own detectors

The probe's second job is to attack the product's own detectors, and every finding in this section has since been
closed — the last one as a *risk-review decision*, which is what it needed to be:

* ~~**A `close_position` action larger than the per-order cap is refused at fire time only.**~~ **CLOSED after
  P16**, as the decision the probe asked for rather than as a workaround. F19's fix covers *sized* actions; a close
  is sized by the position itself, so a $25,000 position could not be closed by a rule while the entry cap was
  $2,500 — the failure mode being an armed stop-loss that can never fire, which is the opposite of risk control.
  The decision, in the gate rather than in a document nobody reads at 3am: **a reduce-only sell answers to its own
  ceiling** (`max_close_notional_micro`, one position's worth) **instead of the entry cap**, because the entry cap
  bounds *new* exposure and refusing an exit is not risk reduction, it *is* risk. It is not an exemption, and the
  three edges are enforced rather than asserted: the size must not exceed what is actually held — read from
  `position_lots` by the caller (the API's order path and the executor each read their own copy), **never** from
  the request body, because a client-supplied "I hold this much" is a cap bypass with extra steps; selling *more*
  than is held is not a close (the excess opens a short, which creates exposure) and keeps the entry cap; and an
  absurd close is refused by the close ceiling itself, with `OVER_CLOSE_CAP` existing precisely so the refusal
  says *which* ceiling stopped it. `Decision.reduce_only` is on the success *and* the refusal for the same reason:
  "was it a close, and which ceiling refused it" is the question asked after an incident. Re-measured in the probe
  against the real gate with a real $25,000 position (a close above the entry cap now allowed, the same sell with
  nothing held still `OVER_ORDER_CAP`, an excessive close `OVER_CLOSE_CAP`), at the door by three API tests
  (`tests/test_api.py::TestTheCloseCeilingOverTheApi`), and at the gate by two unit tests including the hoisted
  `reduce_only` a first draft of this fix only reported on the success path — the refusal test caught it.
* ~~**No CSV export route exists, so formula injection could not be probed end to end.**~~ **CLOSED after P16**,
  and the item's framing was half wrong in a way worth recording: there is no *route* because there is no
  server-side export at all — the tax CSV is built in the browser from the payload on screen, with its columns
  chosen by the wire (`portfolio.csv.columns`). "So it cannot be probed" did not follow: the writer is a module,
  and the guard belongs in the one function every cell passes through. `csvCell` neutralises a value a spreadsheet
  would execute — a leading `=`, `+`, `-`, `@`, tab or CR gets an apostrophe — while leaving a well-formed signed
  integer alone, because this export exists to be *added up* and prefixing `-5` to defend against a formula that
  `-5` cannot be would trade a real number for a hypothetical attack. The header row stays verbatim, which is what
  the requirement asked for and what keeps the file the same document as the table. The guard is written as an
  explicit character Set with TAB and CR from their code points rather than as a regex class: the first version
  depended on how many backslashes survived being written, and a security control whose meaning cannot be read off
  the source is one nobody can review. The probe's re-test is a source-level check with that stated plainly (an
  artifact that only exists in a browser blob cannot be fetched by a Python probe), and the arithmetic is executed
  where it can be — `web/src/terminal/portfolio.test.ts` drives `csvCell` with the strings this item names, both
  directions, and asserts the header is untouched. A market title arriving through ingest is venue text: tomorrow
  that is what a column will carry, which is why the guard is in the writer and not in today's column list.
* ~~**The copy-farm rule cannot tell a follower from two active traders on the same cadence.**~~ **CLOSED in
  P16**, and by the tape the finding itself used. `copy_farm()` now pairs fills **one-to-one** (within a market and
  side, a candidate fill explains at most one of ours, nearest first) and requires **coverage**: if the candidate
  still has fills left over in the markets where we paired, our fills were not following its fills, they were
  merely near some of them. Pairing alone does not fix the reported tape (10 of 12 still pair); coverage does
  (2 of its fills left over, 20% against a 10% tolerance — deliberately tight, because a missed farm costs
  nothing while a false one accuses somebody). The same tape is now the test in both directions, and two more
  isolate the mechanisms: one leader fill cannot explain twelve of ours, and a market maker's dense tape is not a
  leader's tape.
* ~~**The cascade was bounded arithmetically, not end to end.**~~ **CLOSED in P16** as drill 11 of the P13 chaos
  suite, which the probe re-runs rather than citing: 500 funded copiers, one source fill through the product's own
  `CopyEngine`, 500 intents through `enqueue_intent`, the executor claiming them `batch_size` at a time, the venue
  filling all 500, and the fills booked through the same `book_fill` the venue's trade stream uses. The two
  numbers the finding asked for: **fan-out 86–92 ms** (0.17–0.18 ms per copier) and **$6,375.00 aggregate**, with
  the largest order **$12.75 against the $25.00 per-trade ceiling**, zero new intents from replaying the same
  fill, and no fill above the decisions' own sum. Building it also found that the drill's first version had 400 of
  500 orders refused `STALE_QUOTE` — the pre-flight was right and the harness was wrong, which is worth recording
  because it is the check doing its job.

**Closed after the phase, with the re-test the rule demands (P16).** *The revocation ground is the programme-wide
builder code, and nothing re-enables it in the product* named two gaps. Both are closed, and both by the probe
rather than by a note here: `_ref_invalidate_builder_code` now returns the code's **state** (`disabled` /
`already-disabled` / `no-code`) instead of a bare flip, so the audit line answers the question an operator actually
has; and `_ref_restore_builder_code` — called by `POST /v1/referrals/review` when a `self_referral` review is
*cleared* — puts a **manual** disable back (`active`/`manual`, reason in the note) and writes a
`referral.builder_code.cleared` event naming the actor. It refuses to touch a `venue_rejection` disable, which is
the guard that keeps a review from overruling the party whose programme this is. The probe now drives all four
behaviours: repeated apply, clear, the restore event, and the venue guard — `docs/verification/P14-attack-surface.json`
reads **60 passed, 0 failed, 6 OPEN** where it read 8 open, and the schema's own CHECK constraints (`source IN
('venue_rejection','manual','api')`) are why the restore writes `active`/`manual` rather than inventing a new value:
the first attempt did exactly that and the insert was refused, which is the schema doing its job. Three tests in
`tests/test_referrals_api.py::TestTheThreeCollisionsOverTheApi` hold the same ground hermetically.

**Also closed after the phase (P16).** *A legitimate question containing `_`, `*`, `[`,
`]` or a backtick trips the broadcast "looks like Markdown" warning* was recorded as cosmetic. Re-reading the probe
showed it was not only questions: **every refusal card tripped it**, because refusal codes are `UPPER_SNAKE`
(`OFF_TICK`, `STALE_QUOTE`, `IDEM_CONFLICT`) — the warning fired on the most-opened card in the product, which is how
a warning becomes noise. `MARKDOWN_CONFETTI_RE` now matches the paired forms Markdown actually uses, with
CommonMark's own two rules (no whitespace inside a delimiter pair, no intraword `_` emphasis), and the re-test is
the probe itself: the same three cards are re-rendered through the real renderer and `cosmetic` is now empty —
`docs/verification/P14-attack-surface.json` reads **56 passed, 0 failed, 7 OPEN** where it read 8. Four tests in
`tests/test_telegrambot.py::TestTheMarkdownScanner` hold both directions: real Markdown still fires, ordinary
questions, brackets and refusal codes do not.

**Also closed after the phase (P16).** *No outbound webhook transport exists yet, so the stored-URL SSRF path is
latent rather than absent.* The right way to close this one was to build the transport **with** the guard in it
rather than to write a promise next to the absence, because the promise is what a hurried week deletes.

`packages/polygm_core/signals/webhook.py` is now the only place in the product where a *user-chosen* address
becomes a socket — every other outbound call site takes a constant, which is why P14 could enumerate them. The
guard is the first thing that runs, and it is split deliberately in two:

* **At save time** (`signals.console.validate_alert_payload`) the URL's *shape*: https only, no userinfo, a real
  host, no percent-escapes or integer notations in the host, no privileged port, and — for a literal address —
  nothing internal. This half is **DNS-free on purpose**: saving a rule must not fail because our resolver is
  having a bad minute, and a hostname's current resolution is not a property of the rule. What it guarantees is
  that nothing *already* internal can be stored at all.
* **At send time** (`webhook.deliver`) everything above **plus** resolution: every address the host resolves to
  must be globally routable, and *no resolution at all is a refusal* (fail closed — an unresolvable host is not a
  safe host, it is a host we can make no claim about). A hostname that resolved publicly when the rule was saved
  and privately when it fired is exactly the attack, so this check is repeated on **every send and every redirect
  hop**.

Three details are the whole of the closure, and each was found by writing it rather than by reasoning about it:

* **`is_global`, not a hand-written list of ranges.** `100.64.0.0/10` (carrier-grade NAT) is `is_private=False`
  on this interpreter while also not being routable — the kind of gap a hand-written filter has and does not
  know it has. The named ranges come first so the refusal can explain itself; `not is_global` is the last word.
* **A redirect hop is a new decision, and both shapes of it are guarded.** A client that *raises* on a 3xx and a
  client that *returns* one must take the same path, or the guard is only as good as the client's exception
  style: the returned-`Location` branch was the gap, and it is now the same code as the raised one. Redirects are
  followed (refusing them outright breaks the ordinary `example.com → www.example.com` case) but capped at three,
  re-guarded each hop, and a downgrade to `http` is refused by the scheme rule.
* **A refused target is dead, not retried.** `fanout.fail` schedules a backoff for failures that might not
  repeat; a URL that resolves into `10.0.0.0/8` is not going to stop being private on the fourth attempt, and
  retrying it keeps a hostile rule alive in the queue. The row lands in `dead` with `dead_reason` naming the code.

Evidence: `docs/verification/P14-attack-surface.txt` / `.json` → **70 passed, 0 failed, 4 OPEN** where it read
56/0/7 before the phase's post-fixes and 64/0/5 after the copy-farm closure. Six of the new checks are this
section, including the one that reads through the *product* rather than the library: a Pro account saving
`https://169.254.169.254/latest/meta-data/` through the served API gets `422 VALIDATION`, saves an ordinary public
URL beside it in the same breath, and the number of sockets opened by the whole battery is asserted to be **zero**
— the opener the probe installs fails the run if it is ever called, so "refused" is a claim about the socket and
not a status code. `tests/test_signals_webhook.py` (17 tests) holds the same line, plus the receiver's side of the
contract: the `X-PolyGM-Signature` a delivery carries is recomputed in the test from the timestamp and body, and
`X-PolyGM-Delivery` is asserted to be the queue's own idempotency key, which is what makes a redelivery after a
lost lease droppable by the receiver.

---

## D2 — the six key-compromise drills, with a stopwatch on each

`tools/p14-key-drills.py` runs six scenarios end to end and records a wall-clock time for every one. Recorded run:
`docs/verification/P14-key-drills.txt` / `.json` → **`15 checks passed, 0 failed, 1 OPEN`**, and the one open item
is a launch condition, not a formality (below).

Two rules shape the tool, and both of them bit during its own construction:

* **Every step calls the product's own function** — `SEC.revoke_all_sessions`, `SEC.revoke_key`,
  `keys.rotation_plan`, `keys.can_retire_kek`, the kill switch's own table, the executor's own preflight. A drill
  that re-implements the procedure measures the drill; the first version of the provider-outage scenario drove an
  HTTP route that returns *wrapped* material and never touches a signer, so it was measuring a refusal by a
  different control entirely.
* **The clock starts when the response starts.** The break-glass population (500 wrapped keys by default) is built
  before the clock starts, and the population's Argon2id hashing is deliberately outside the measurement. Inflating
  a security measurement is a lie in the safe direction, and it is still a lie.

| Drill | What it does | Measured | Budget |
|---|---|---|---|
| `user_key_leaked` | revoke the account's sessions, revoke the key generation, confirm no live wrap remains | **0.2 s** | 15 min |
| `signing_service_compromised` | kill switch → two-approver session revocation → re-wrap every DEK under a new KEK → read every one back → revoke the old generations → retire the old KEK → release the halt | **0.06 s** for 500 keys (local half) | 60 min |
| `key_in_a_log` | a private key, a mnemonic and an initData hash through the **real log-line builder**, then the key treated as compromised | **0.1 s** | 30 min |
| `contractor_leaves` | rotate the operator token, confirm the old one is refused and the new one works, record the rotation in the secret inventory | **0.2 s** | 60 min |
| `support_impersonation` | an operator token against every money path a support agent could be asked to use | **0.2 s** | 5 min |
| `wallet_provider_outage` | the executor's signer seam answering nothing, then recovery | **0.03 s** | 15 min |

What makes these more than "it worked":

* **Must-accept guards.** The stolen session is used *before* the drill (a drill measuring an already-dead token
  proves nothing), the operator credential is used before rotation, and the revenue-side refusal is paired with a
  legal order that still gets through.
* **Must-refuse on the dangerous path.** A **single** approver is refused (an attacker holding the admin token must
  not be able to run the break-glass alone), and the kill switch is checked on the *record*, not in the response.
* **Read-before-retire.** Every re-wrapped DEK is opened under the new KEK before any old generation is revoked,
  and the old KEK is retired only when `can_retire_kek` says nothing live references it. The rotation follows P07's
  order — mint new → serve both → revoke old → retire — because `key_wraps` is unique on `(user_id, dek_version)`
  and the schema is what refuses a rotation that would leave an in-flight signature unresolvable.
* **Recovery is asserted, not assumed.** The provider drill ends with the halt released, the intent executed, and a
  third pass that must not re-POST.

### Finding F6 — a provider outage killed the executor pass (fixed)

The drill found it on its first run: **`signer.sign()` was the one external call in `handle_intent` that was not
guarded** — the venue calls beneath it already catch `TimeoutError`/`ConnectionError` — so a signing provider that
raised (a 500, a timeout, a dead session key) propagated out of `handle_intent`, out of `tick`, and killed the
whole pass: every other intent in the batch unprocessed, the intent left in `signing`, and a worker that
crash-loops for as long as the provider is down. The attempts-permanent class of defect that the kit says may never
be deferred: the component that signs for everybody was one provider hiccup away from a halt.

The fix is in `services/executor/main.py` and the decision inside it is the load-bearing part: **the intent goes
back to `queued`, not to `uncertain`.** The attempt row is written *after* signing, so a signer that answered
nothing proves nothing was sent; `uncertain` is never auto-requeued, so using it here would have turned a provider
blip into a queue of orders a human has to clear one at a time. The failure is named (`SIGNER_UNAVAILABLE`), the
lifecycle state stays inside the state machine's own vocabulary (`draft`, from the vocabulary the DB checks), and
the requeued intent re-runs the risk gate on its next attempt — so a retry can never ride an old book snapshot into
the market. The drill observed exactly that on its own first green run: with a stale book the retry came back
`rejected / STALE_QUOTE`, which is the gate doing its job on the second pass.

*Retest:* `tests/test_executor_service.py::TestSignerOutage` — the tick must not raise, the intent must be `queued`
with `SIGNER_UNAVAILABLE`, there must be no attempt row / venue call / order / fill / cash, the trail must name the
reason, the user must be notified, and the retry after the provider returns must produce **exactly one** order
(and a third pass must not re-POST it).

### OPEN — the one-hour break-glass is unproven, and that gates the launch

The kit's constraint is that a full break-glass takes under an hour and that a breach is said in writing. Here is
the writing:

* **The half we control is measured**: kill switch, two-approver revocation of 500 sessions, re-wrap of 500 DEKs
  under a new KEK, read-back verification of all 500, revocation of the superseded generations and retirement of
  the old KEK took **0.06 s**, and at the measured 0.06 ms/key a 10,000-key rotation projects to well under a
  minute.
* **The half we do not control is unmeasured.** Re-wrapping 10,000 DEKs at a custodian is 10,000 provider calls,
  and `keys.revocation_throughput` is explicit that the provider's rate limit — not our batching — is the binding
  constraint: at **1 call/s per key, 10,000 keys is 2.8 hours**, over the limit. Turnkey's limit has not been
  measured on real keys.

So the recorded artifact says `CONDITIONAL`, not `PASS`, and the condition is repeated in the report:
**the product does not launch on a key-compromise promise nobody has measured.** The measurement is one re-run —
`python3 tools/p14-key-drills.py --provider-rate <calls per second>` — and until it exists the claim is open. This
is the honest shape of the kit's rule: a drill that reported `PASS` here would be claiming an hour we cannot
demonstrate.

---

## D3 — SAST, DAST, secret scanning, dependencies, IaC, containers

`tools/p14-appsec-scan.py` — seven sections, one artifact. Recorded run:
`docs/verification/P14-appsec-scan.txt` / `.json` → **`36 checks passed, 0 failed, 1 OPEN`**.

| Section | What it checks | Result |
|---|---|---|
| `sast` | ruff's `S` (bandit) rule set over `services/` + `packages/` — 80 findings across 8 rules, every one either fixed or triaged in `tools/sast-triage.json` | green, with a planted-violation canary |
| `secrets-history` | **all history, all branches**: 85 commits, 166 300 added lines, added lines only, scanned with shape rules plus a ±2-line context window | 0 findings |
| `log-redaction` | the CI log scanner (self-test + `--sources`), plus a live probe: a request carrying a token, then the line the API actually printed | green |
| `dependencies` | P07's scanner (pins, advisory feed, digest record) plus `docs/dependency-review.md` — a row per pinned requirement, with a review date and an owner | green |
| `iac` | compose read as security config: the one literal password is the dev DB with the exception written on the line, no port published beyond loopback, nothing privileged, api drops all capabilities, no-new-privileges, read-only root | green |
| `containers` | every Dockerfile: non-root, no credential-shaped `COPY`, no `curl \| sh` | green, contents **OPEN** |
| `dast` | the running API in its production identity shape: headers, framing, CORS, error verbosity, the interactive surface, unknown route, unexpected methods, hostile query | green |

### The triage rule, and why the file cannot rot

The scan fails on a finding that is **not** in `tools/sast-triage.json` **and** on an entry whose finding has gone.
That second half is the one that matters: a suppression list with no expiry becomes a list of things somebody once
worried about, and the entry that should have been deleted three phases ago is the one that hides a regression. The
80 findings are 20 distinct (rule, file) pairs with written reasons — most of them callers of the same two helpers,
which is why the finding count and the number of decisions differ.

### Findings F7–F8 (fixed, with re-tests)

**F7 — `assert` used as control flow on the money path.** Three sites: the deny-code severity table
(`risk/limits.py`), `deny()` inside `evaluate_extra`, and the copy engine's skip-reason check. Under `python -O`
asserts vanish, so a typo'd severity or an undocumented skip reason would ship silently to the surface that decides
how a refusal is displayed — and a skip reason nobody documented is a copy that stopped without saying so. All three
raise now, and the deny table's check is a named function (`validate_deny_table`) so it can be re-run with a
known-bad table rather than greped for.
*Retest:* `tests/test_security_plane.py::TestOptimisedBuildIsNotADifferentProduct` — it runs a subprocess under
`python -O` and asserts the refusal survives.

**F8 — the SAST stage could have had no rules in play.** The first version of the canary assertion read `group(1)`
from ruff's output, which is the *file path*: it asserted that ruff mentioned a file, not that a rule fired, and it
would have printed "canary caught" for a run whose rule set had failed to load. The canary now reads the rule code
and requires **`{S608, S105, S311, S602, S310}` ⊆ caught**.

### The scanner corrections that made "0 findings in history" mean something

The first honest run reported **5 508** findings. That number is worth recording, because every one of them was a
mistake in the scanner and none was a secret in the product:

1. **A 64-hex string is not a private key without something calling it one.** Polymarket condition ids and
   transaction hashes are 64 hex characters; 2 700 of the findings were captured venue fixtures. Key rules now
   require a context word (`private_key`, `mnemonic`, `wrapped_dek`, `keks`, `signing_key`, …) — with a ±2-line
   window, because a config file names the key on one line and holds the value on the next.
2. **The log redactor's patterns do not belong on source code.** `redact.scan()` is a high-recall matcher built for
   log lines; pointed at TypeScript and Python it flagged `password: string`, `BOT_TOKEN = "[bot-token]"`, an
   already-redacted value, and `secret = (body.get("secret") or "").strip()`. 100 findings, every one of them the
   redactor being right about its own job and the scanner using it for the wrong one. `scan_text(..., log_line=True)`
   now decides that, and history scanning is source.
3. **A `$` or `{` in the matched text means it is not a literal.** `https://x-access-token:$TOKEN@github.com/…` is
   the *safe* way to write that line, and it is the line somebody writes during an incident — flagging it teaches
   the wrong lesson at the worst possible moment. Both scanners skip interpolations.
4. **One escape hatch, not two.** The history scanner honours the repo's existing `# lint-allow: <reason>` marker
   (which `tools/lint-rules.py` already refuses to accept without a reason), and every honoured reason is printed
   in the artifact — five lines of "shape only, never a real token", two "fixture". `tools/ci-log-scan.py` now
   loads the *shared* allowlist file, so an exemption is written once and is in force in both scanners instead of
   two lists drifting apart. The marker has to be a comment with a real reason: matching the bare string also
   matched the docstrings that *explain* the marker, which is how the artifact first came to report an exemption
   reading "` sprayed over the codebase until the".
5. **Path-level exemptions with reasons** for the three classes that cannot hold a production secret — captured
   fixtures (121 402 lines skipped), recorded evidence artifacts, and the 155 vendored third-party skill docs.
6. **The "did the scan read everything" guard compares against git's own count**, not a hardcoded threshold: the
   first version demanded 100 commits and this repository had 85, so the guard failed on a scan that had read the
   entire history.

### OPEN — container image contents are unscanned

`docker` is not installed in this environment, so the Dockerfiles are read **statically** and no built image has
been inspected for OS packages, layer contents or CVEs. That is stated in the artifact rather than implied away:
the static checks do not cover base-image vulnerabilities. It closes in CI with `trivy`/`grype` against the built
digests, and `deploy/image-digests.txt` exists to be the record of which digests that ran against.

---

## D4 — infra verification, and the defect it found on the live deployment

`tools/p14-infra-verify.py` — six sections, every network check made by the tool rather than typed into this
document. Recorded: `docs/verification/P14-infra-verify.txt` / `.json` → **`20 checks passed, 2 failed, 7 OPEN`**.

### F9 — the deployed API accepted a spoofed identity header (found, fixed, re-tested live)

This is the worst defect of the phase and it was live on a public URL.

`https://polygm-api.vercel.app` was running with **no `PGM_REQUIRE_SECURITY_ENV`**, which put it in the
*development identity shape*: `X-User-Id` was trusted as the caller's identity. Probed from this machine, before
the fix:

```
GET /v1/referrals/me   -H "X-User-Id: u-demo"   -> 200  {"link":{"token":"ref_n3p3obixucx4nvhb7k5l57", …}}
GET /v1/auth/sessions  -H "X-User-Id: u-demo"   -> 200  {"items":[] …}
GET /docs                                        -> 200  (the interactive surface, 80 paths)
GET /openapi.json                                -> 200  (123 KB of schema)
```

Anyone on the internet could read any account by setting one header. The kit's rule for this class is absolute —
**nothing on authorisation may be deferred to after launch** — so the finding gated the launch *and* was fixed in
the same session, in this order:

1. **Said in writing**, in the artifact and here, before touching anything: the authorisation test failed, so the
   product does not launch. That is the kit's own sentence, applied.
2. **The security plane's required environment** was generated and set on the Vercel project
   (`PGM_KEK_v1`, `PGM_IP_PEPPER`, `PGM_SERVICE_TOKEN`, `PGM_IMAGE_PROXY_SECRET`, `PGM_REQUIRE_SECURITY_ENV=1`) —
   values written to `~/.secrets/polygm-prod.env`, never the repository, because the production shape refuses to
   boot without them and a boot-time refusal is the correct failure.
3. **The deployment was rebuilt from the current source.** The build that was live predated the docs gate and the
   session-checking work; the identity shape alone would not have fixed `/docs`, and the older build is why the
   first redeploy still served the schema.
4. **Re-tested live, and the re-test is now a permanent check in the tool** (`section_headers`), because this is
   the one defect that must never come back quietly:

```
spoofed /v1/referrals/me  -> 401 UNAUTHENTICATED      /docs        -> 404
spoofed /v1/auth/sessions -> 401 UNAUTHENTICATED      /redoc       -> 404
spoofed /v1/wallet/*      -> 401 UNAUTHENTICATED      /openapi.json-> 404
```

### F10 — an anonymous writer got a retryable *signing* error instead of an identity answer

Found by the D4 header probe on the same live box: `POST /v1/orders` with no session answered
`503 SIGNER_UNAVAILABLE retryable:true`. Nothing was placed — the D1 matrix's "anonymous is refused" check passed
on it, which is exactly how a wrong refusal hides inside a green run — but the answer was wrong twice: it named a
*signing* problem for an *identity* problem, and it told the client to retry a call that can never succeed without
a session. The source carried the comment `# in prod: 401 from auth middleware`, and **there is no auth
middleware** — the assumption is precisely what a penetration test exists to find. It answers `401 UNAUTHENTICATED`
in the production shape now, and keeps the dev-shape behaviour the repo's harnesses depend on.
*Retest:* `tests/test_security_plane.py::TestAnonymousWriterGetsAnIdentityAnswer` (both shapes, in one test).

### F11 — the deployed API served its whole schema (fixed with F9, re-tested)

`/openapi.json` was 123 KB: every path, parameter, enum and error code, to any caller. The docs gate existed in
the source; the **deployed build predated it**, which is the lesson worth keeping — a control that is in `main` is
not a control that is on the box. The tool now checks all three paths on every run.

### F12 — the container users had login shells

`useradd -r` gives `/bin/sh` by default, and neither image needs a shell: the CMD is `uvicorn`/`python3` and the
healthcheck is a `python3 -c`. Both images now create their user with `-s /usr/sbin/nologin`, so code execution in
the container does not come with an interpreter to type into.
The image-level proof (`docker run … id`, `touch /`, `getent passwd`) is **OPEN** — there is no docker here.

### What else D4 found, and what it could not measure

* **F13 — the GitHub account that owns this repository has MFA disabled** (`two_factor_authentication: false`),
  and its token holds `admin:org`, `admin:public_key` and `delete_repo`. Enabling MFA needs a phone, so this is an
  owner action, and it is a launch blocker: the account that can rewrite every commit is protected by a password.
* **F14 — a database on the linked Supabase account accepts connections from `0.0.0.0/0` and `::/0`**
  (`hashcats-mining`). It is not this product's database, and it is still a finding because it is the same account
  and the same token: the default is what a new project inherits. The honest fix has a trade-off — restricting the
  CIDRs to the app's egress is only possible once that egress is static (the Supavisor pooler or an IPv4 add-on),
  so it is stated as a decision to make rather than a setting to flip. When the PolyGM Postgres lands it must be
  created with restrictions set, never with the default.
* **A restore that actually ran**: a consistent logical backup (`VACUUM INTO`), restored to a second file, checking
  that 123 tables and 3 273 rows match, that the money-path queries (balances, ledger sum, open orders, intents,
  key wraps) return identical answers, and that the API's own boot schema guard accepts the copy — 17 ms to back
  up, 8 ms to restore. The managed-Postgres PITR drill is **OPEN** with the exact procedure written down.
* **Egress**: no outbound call site builds its URL from a request value (the SSRF surface is absent by
  construction), and every configured vendor endpoint is `https`/`wss`. The deployed-subnet proof is **OPEN**: the
  executor is not deployed, so there is no subnet to try from, and a pass here means "one allowlist to write", not
  "the firewall was tested".
* **Headers, live**: API and Mini App both send HSTS with `preload`, `frame-ancestors` scoped
  (`'none'` for the API, Telegram-only for the Mini App), and **no wildcard CORS** anywhere.
* **MFA on the other three providers** (Vercel, Supabase, Railway) is **OPEN**: none exposes an MFA field on the
  endpoints these tokens can reach. It is written down as open rather than assumed, because an MFA status nobody
  verified is not an MFA control.

---

## D5 — rate-limit abuse, and the segfault the 100-user probe found

`tools/p14-abuse-probe.py` — recorded: `docs/verification/P14-abuse-probe.txt` / `.json` →
**`18 checks passed, 0 failed, 1 OPEN`**.

| Probe | What it establishes | Result |
|---|---|---|
| per-IP | the anonymous budget refuses a sitemap walk at the 10th request, with `Retry-After: 599` and `X-RateLimit-*`; the refused caller can still read a *different* surface (the budget is per kind, not global) | green |
| per-user | 21 uncached scans against a 20/day quota → the 21st is `429 QUOTA_EXCEEDED`; a *different* account is untouched | green |
| own-budget DoS | 100 aggressive users, 600 requests, each inside their own budget | **no 5xx, 93 req/s, p50 311 ms (34.9× the 8.9 ms unloaded baseline), p95 608 ms, max 961 ms** |
| victim lockout | four separate claims: the victim's session survives, the lock expires, the victim is told, recovery is not blocked | green |
| fanout amplification | 50 fires across 3 channels → at most one send per channel, the other 47 held with a sentence saying why | green |
| cost amplification | a 10,000-row page request is bounded by the stated `pageSizeHardCap: 100` | green |

### Findings F15–F17

**F15 — the account login budget never fired for the ordinary case.** The lock was *checked* against the typed
identifier (`trader@example.test`) and the failure was *recorded* against the resolved user id (`u_…`), so the two
keys never met: an attacker could guess at a real account's password with only the address budget — which is
deliberately four times looser, because carrier NAT exits share addresses — bounding them. Both keys are counted
now (and the failure writes a row under each), so eleven wrong passwords reach the account budget.
*Retest:* `TestLoginThrottleCountsTheRightKey` — three tests: a real account, an unknown identifier, and a spray
across accounts from one address.

**F16 — the login response time told you whether an account existed.** The code's own comment claimed "a missing
user and a wrong password run the *same* code path, and the same body", and the second half was false: the
missing-account branch returned **before** `_hasher().verify()`, so an unknown identifier answered in ~1 ms while a
real one paid ~100 ms of Argon2id. That is a user-enumeration oracle on a product whose identifiers are email
addresses. The unknown path now spends the same hash against a fixed dummy envelope (built once, never derived
per request), so the two are indistinguishable by the clock.
*Retest:* `TestLoginTimingIsEqual` — the ratio between the two paths must stay under 2×, **and** both must be doing
real work, so the equality cannot be satisfied by making both fast.

**F17 — a segfault under concurrent load, from the connection proxy keying on thread ident.** This is the one the
kit's "own-rate-budget DoS" question exists to find. The proxy that hands each thread its own SQLite connection
kept its bookkeeping in `dict[int, Connection]` keyed by `threading.get_ident()`, and reaped the connections of
not-alive threads by that same key. **Idents are reused as soon as a thread exits**, so under thread churn a
freshly started thread could be handed the connection of a thread that had already died, or lose its own to a reap
triggered from a third thread. On ordinary reads that surfaced as `Cannot operate on a closed database`; with
concurrent use and close, **sqlite3 — a C library — segfaulted**, which in a server is a worker dying rather than
a 500. It reproduced on the first run of the 100-user probe (exit 139) and never again after the fix.

The fix has two load-bearing properties: the connection lives in `threading.local()` (keyed per *thread*, never
per ident, so the wrong connection cannot be handed out even if idents collide), and the registry holds a strong
reference to the thread object so `is_alive()` is asked about the thread that actually owns the connection. Reaping
is an explicit method, and its safety property is tested directly: a reap never closes a connection whose owner is
still running.
*Retest:* `TestConnectionProxySurvivesThreadChurn` — two waves of 16 short-lived threads (the overlap is where an
ident gets reused) asserting no errors, one connection per thread and no sharing; plus the reaping property.

### The victim-lockout question, answered in four pieces

The kit's requirement is that throttling must not let an attacker lock a victim out. The login budget is the one
place a throttle keys on the victim's own account, so each claim is measured rather than argued:

1. **The victim's existing session keeps working while the attacker is refused** — a login throttle that logs the
   victim out would be a denial of service with extra steps.
2. **The lock expires by itself** — the same arithmetic the route uses, evaluated at the far end of the window
   (`locked now=True`, `locked after the window=False`). A lock with no expiry is a permanent lockout.
3. **The victim is told** — the failed attempts appear in the account's own security log, so "somebody is guessing
   at your password" is visible rather than silent.
4. **The recovery path is not behind the same bucket** — locking the login door must not lock the way back in,
   or an attacker holds the account closed for as long as they keep guessing.

The residual is stated rather than hidden: an attacker *can* make password login fail for up to fifteen minutes.
That is the deliberate trade (the account budget is what stops guessing), and the four properties above are what
keep it from being a lockout: the victim keeps their session, is notified, and can regain access through recovery.

### What D5 could not measure

**One process is not a fleet.** The 100 aggressive users are threads against an in-process ASGI app on one
machine, which measures the product's own budgets and failure modes — and found a crash — but not the deployed
fleet behind a CDN. The OPEN item records that P13's load harness owns the fleet question and must be re-run
against the deployed API before launch, with the p95 and error-rate curves attached to the security gate document.

---

## D6–D8 — the decisions, the gate document, and what keeps it true

### D6 — decisions, in `docs/P14-audit-bounty-legal.md`

* **An external penetration test, before real funds**, scoped to the money and identity paths in five priorities
  (authorisation, custody, the order path against the venue, the Mini App, infrastructure), with our own evidence
  pack handed to the tester so their hours go on what we missed rather than on what we documented. The document
  also states what our own testing *cannot* cover — the registry cannot be both the map and the territory, and the
  executor has never talked to the real venue — and that a skipped audit is recorded as a sentence in writing
  rather than as a gap nobody mentions.
* **A private, invited bug bounty at launch**, with published bands set against expected loss (critical
  2 000–5 000, high 750–2 000, medium 150–750), safe harbour written first, a 72-hour triage commitment, a budget
  line at 5 % of the first year's security spend, and the same rule this phase has run on: **no finding closed
  without a re-test**, visible to the reporter.
* **Twelve questions for counsel**, written with the product facts attached so they are answerable (delegated
  custody, builder-code revenue, Telegram distribution, pseudonymised leaderboards, copy-trading, the logging
  design), grouped by audience, with the two that gate the launch named as such.

### D7 — the pre-launch security gate, `docs/P14-security-gate.md`

Written by `tools/p14-security-gate.py` **from the recorded artifacts**, so the verdict cannot drift from the
evidence: `--check` fails if the document on disk disagrees with what the six harnesses recorded. It carries the
kit's launch conditions as a table, the blocking findings, the open items, a sign-off block that is deliberately
empty until a human puts a name and a date against it, and the standing rule it inherits — no real funds until
P13/P14 are green.

Its verdict today is **NO-GO**, and the reason is not the code: the two failing checks are owner actions (MFA on
the GitHub account that owns this repository; the inherited `0.0.0.0/0` default on a database on the linked
Supabase account). Everything the build itself can close is closed.

### D8 — continuous assurance

The controls are only worth what their cadence is worth, so the cadence is executable rather than aspirational:

| Cadence | What runs | What it catches |
|---|---|---|
| every PR touching `services/`, `packages/`, `tools/p14-*`, `db/` | `tools/p14-appsec-scan.py`, `tools/p14-authz-matrix.py`, `tools/p14-attack-surface.py`, the triage-file diff, and `p14-security-gate.py --check` | a new SAST finding, a route that stops refusing the wrong principal, an order path that accepts what the gate forbids, a triage entry that has gone stale, and a gate document that no longer matches reality |
| nightly | `make security` — all six harnesses, including the six key drills and the 100-aggressive-users probe | a control that stopped working while nobody was looking |
| quarterly (and on any advisory touching `cryptography`, `argon2-cffi`, `asyncpg`, `py-clob-client-v2`) | the dependency review in `docs/dependency-review.md`, with a date and an owner per row | the advisory that lands on an unowned package |
| before any deployment that touches money or keys | `make security-record && make security-gate`, then the sign-off block | shipping on evidence that has expired |

`.github/workflows/security.yml` implements the first two; `make security` and `make security-record` are the
by-hand equivalents, which matters because a control that can *only* run in CI is a control nobody can reproduce
during an incident.

**The drill calendar** (from D2, with the same names the `drill_records` table uses): `key_compromise` quarterly —
the six scenarios in `tools/p14-key-drills.py`; `phishing_support` twice a year; `pg_failover` after any database
change; `channel_poison` (the Telegram channel) quarterly. Each run writes its own row with a stopwatch time, and
a failure has to name the step that failed or the record is refused — a failure with no named step is a failure
that gets "fixed" by re-running it until it says pass.

**What is deliberately not automated**: the sign-off, the external audit, and the legal questions. Those are the
three places where a human signature is the control, and automating them would replace the control with a
document.
