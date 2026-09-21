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
