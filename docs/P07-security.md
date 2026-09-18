# P07 · The security plane

Threat model, keys, authentication, authorisation, input integrity, secrets, network posture, abuse, incident
response and the compliance sentences a human will have to say out loud. This is the phase whose deliverable is
mostly *restraint*: the prompt's rule is that no control ships without an owner and a test, and that a control
whose cost exceeds the loss it prevents is not a control, it is a costume.

Measured in this tree on 2026-09-18: `make p07` → **see `docs/verification/P07-gate.txt`**;
`python3 -m unittest discover -s tests` → **631 tests OK** (of which 131 are P07: 89 in
`tests/test_security_core.py`, 42 in `tests/test_security_plane.py`); `make lint` → **8/8 rules clean, 8/8
canaries fire**; `python3 tools/check-openapi.py` → **176/176**; `tools/ci-log-scan.py --sources` → **132 files,
0 findings** with a self-test that proves it can fail; `make drill-p07` → **`docs/verification/P07-key-drill.txt`**.

Every control marker below (an owner handle, then a resolvable test reference) is machine-checked: the gate resolves the owner against the owner
table and the test against a real `unittest` method in this repository, and fails on a control whose test was
deleted or renamed. A control with no test is not a control; it is a paragraph.

## Owners

| handle | who | holds |
|---|---|---|
| `security-owner` | the person who wrote this file | policy, key envelope, this document's honesty |
| `founder-on-call` | whoever is paged | kill switch, break-glass, customer notice |
| `api-owner` | the API service | routes, authz table, schema strictness |
| `ingest-owner` | the market feed | market metadata, resolution sources, broadcast gate inputs |
| `executor owner` | the signer's process | unwrap path, policy-hash comparison, egress |
| `ops-ani`, `ops-ben` | the two named approvers | break-glass, KEK retirement |

[owner: security-owner · test: tests/test_security_core.py::TestIncidentResponse::test_no_alarm_is_orphaned_and_no_step_is_unwired]
Every runbook step in `incident.first_60_minutes()` carries an owner, a verification and a test reference, and
`runbook_is_wired()` fails if any of the three is an empty string. That is the same rule this document is under.

---

## D1 · STRIDE, ranked by what it would actually cost

The ranking is planning arithmetic, not a measurement: `[UNVERIFIED — confirm before launch: threat-model-numbers]`
assumes the first 12 months at 10 000 accounts, 1 000 funded wallets, $2 000 average balance, and one of each
event happening at the stated rate. The numbers are here so the *order* can be argued with; a threat model
without a number next to each row produces a to-do list sorted by how alarming the words sound.

| # | threat | S/T/R/I/D/E | USD per event | events/yr | expected USD/yr | control that answers it |
|---|---|---|---|---|---|---|
| 1 | The imported-key tier ships while the provider cannot scope calls, so one leaked key signs an `approve` and drains every wallet | T,E | 150 000 | 1 | **150 000** | `keys.policy_is_sufficient` + `provider_can_enforce` → a provider that cannot name two targets is **disqualified**, and the tier is off until then |
| 2 | An insider (admin tooling, or an account with an admin token) exports a key or skips a cooldown | I,R | 200 000 | 0.5 | **100 000** | `authz.ADMIN_FORBIDDEN`, `keys.export_gate`, two-approver break-glass, append-only `auth_events` |
| 3 | Clipboard substitution on the deposit address | S,T | 600 | 100 | **60 000** | The deposit address is shown once, per user, never reused, with a typed confirmation and the on-chain rule that funds arriving anywhere else are not credited |
| 4 | A poisoned alert channel — one market our megaphone pointed 40 000 people at | S,E | 15 000 | 4 | **60 000** | `abuse.broadcast_gate` runs *before* broadcast; refusals are stored, not swallowed |
| 5 | One user's 200 alert rules exhaust the upstream request budget and take the product down for everyone | D | 3 000 | 12 | **36 000** | `abuse.upstream_budget_guard`: a per-user share, scheduled round-robin, an alarm at 70 % |
| 6 | Database leak correlates who someone is from IPs, emails and wallet lists | I | 30 000 | 1 | **30 000** | `authz.ip_hash` is keyed and truncated, addresses are projected to a short form, `redact` runs before the line is written |
| 7 | A backup is restored and quietly incomplete, so the ledger is wrong and we do not know | D,I | 50 000 | 0.5 | **25 000** | `incident.backup_is_current` refuses a restore that was not read back; `money_checks_ok` is a column, not an adjective |
| 8 | Wash trading through our own builder code to farm the commission | S,R | 8 000 | 3 | **24 000** | `abuse.wash_score` on fees and counterparty shape; `payout_gate` holds *our* money, never their order |
| 9 | Referral / Sybil payout farming across shared fingerprints | S | 6 000 | 2 | **12 000** | `abuse.referral_flags` + `cluster_size_for`; turnover thresholds before any payout |
| 10 | Support impersonation — "your wallet needs re-verification, send here" | S | 200 | 60 | **12 000** | We never write first; `sanitise` flags `fake_support` in market copy; no admin can un-enrol a factor |
| 11 | Order repudiation: a user denies an order we signed, and we cannot show the session | R | 12 000 | 1 | **12 000** | append-only `auth_events` + intent → session → TOTP step → policy hash chain, all in `0009_security.sql` |
| 12 | Malicious market metadata (markup, invisible characters, a lookalike title) | S,T | 10 000 | 1 | **10 000** | `sanitise.market_metadata` cleans, flags and refuses; the API renders no raw field |
| 13 | Telegram Mini App clickjacking or a fake host embedding our view | S | 5 000 | 2 | **10 000** | CSP `frame-ancestors 'none'` by default; the allowlist exists only for the `x-openout-frame: miniapp` frame |
| 14 | The image proxy becomes an open redirect or a SSRF pivot | S,T | 3 000 | 2 | **6 000** | `sanitise.proxy_image_url` signs the target, https-only, and the browser never contacts the third party |
| 15 | Refresh-token theft from a proxy or a log, then a silent session takeover | S,I | 800 | 5 | **4 000** | rotation with reuse detection revokes the family; `no-store` on identity responses; 15-minute access tokens |
| 16 | The executor is reachable from the network because a compose file grew a `ports:` | D,E | 200 000 | 0.015 | **3 000** | `internal: true` network, no `ports:`/`expose:`, and the gate greps the compose file for both |
| 17 | A dependency is poisoned between our lockfile and the signing host | D,E | 200 000 | 0.01 | **2 000** | `tools/dependency-scan.py`: exact pins, digest-pinned images for the critical set, advisory feed in CI |
| 18 | A secret reaches a log line and therefore a third-party retention window | I | 20 000 | 0.05 | **1 000** | `redact` on every line, `tools/ci-log-scan.py` on every CI log, Sentry `before_send` wired at boot |

What the ranking changed, in words: **key management and admin power outrank every input-validation concern**,
which is why D2 and D4 are where the code volume went, and why the phishing-shaped threats (3, 4, 10, 12, 14)
share one control surface — we render nothing we did not clean, and we broadcast nothing we did not gate.

[owner: security-owner · test: tools/p07-gate-check.py] The gate re-parses this table and fails if it has fewer
than 15 rows or if the expected-loss column stops being non-increasing, so the ranking cannot drift into a list
someone shuffled.

---

## D2 · Keys: two addresses, one envelope, and a revocation that has been run

**The policy.** A platform key may call exactly two addresses on one chain id: the CLOB exchange contract and
the pUSD token. No `approve`, no `transferFrom`, no arbitrary call, no message signing, ≤600 signatures/min,
exportable only by its owner (`keys.DEFAULT_POLICY`). The word in the requirement is *only*, so
`keys.policy_is_sufficient` checks the subset against `keys.MAX_CALL_TARGETS` and not merely that the targets
look like addresses: an earlier version accepted a policy that named a third contract, which is the difference
between a trading key and a drain key that nobody notices.
[owner: security-owner · test: tests/test_security_core.py::TestKeyPolicyAndEnvelope::test_a_policy_wider_than_two_addresses_is_not_a_trading_key]

**The addresses themselves are placeholders in this repository.** `deploy/venue-addresses.json` carries them
with `"status": "UNVERIFIED-placeholder"` and the gate fails if `keys.py` and that file disagree, so drift
between the policy the executor signs under and the policy we describe to users becomes a red build instead of
an incident. [UNVERIFIED — confirm before launch: venue-addresses]

**The envelope.** AES-256-GCM, one DEK per wallet, wrapped by a KEK that never enters the database or the
compose environment. `keys.WRAPPED_LEN == 48` is asserted rather than hoped for (32-byte DEK + 16-byte tag), the
nonce is 96 bits, and the per-DEK budget is `2**32 − 1` messages, after which `nonce_budget_ok` refuses to wrap:
a reused (key, nonce) pair in GCM is not weaker encryption, it is no encryption. The AAD binds every blob to its
row (user id, KEK version, DEK version, policy hash), so a wrapped DEK copied between rows does not decrypt —
that single property is what makes "we served the wrong user's key" impossible rather than unlikely.
[owner: security-owner · test: tests/test_security_core.py::TestKeyPolicyAndEnvelope::test_the_envelope_arithmetic_is_checked_not_hoped_for]

**Self-hosted is not the plan; it is the fallback we have to be able to run.** If the provider cannot express
"these two targets, this hash readable per signature, revoke immediately, export requires the end user",
`provider_can_enforce` returns `DISQUALIFYING` — not a lower score, not a comparison-table footnote.
[owner: security-owner · test: tests/test_security_core.py::TestKeyPolicyAndEnvelope::test_a_provider_that_cannot_scope_the_key_is_disqualified_not_scored_lower]
[UNVERIFIED — confirm before launch: provider-scoping] No vendor has promised us any of these capabilities;
the note the function returns says so, and P13 either gets it in writing or the imported-key tier stays off.

**Rotation with zero downtime.** `keys.rotation_plan` re-wraps every DEK under the new KEK in batches, *re-reads
and unwraps every new wrap*, and only then allows `can_retire_kek`; one straggler row blocks the retirement,
while a revoked row does not (an old wrap of a dead key must not hold the ceremony hostage). The journal makes a
re-run resume rather than re-wrap. [owner: ops-ani · test: tests/test_security_core.py::TestKeyPolicyAndEnvelope::test_rotation_rewraps_verifies_and_only_then_retires]

**The 10 000-key revocation drill.** `keys.revocation_throughput()` prints the arithmetic before anyone needs it:
10 000 keys at batch 500 is 20 calls, 600 ms of wall clock at 120 ms/call across four workers — and the same
sentence states the number that matters, which is the provider's: at one call per second and one call per key,
10 000 keys is 2.8 hours. `tools/p07-drill.py` runs the revocation against real processes and writes
`docs/verification/P07-key-drill.txt`, including the part where our own bookkeeping (sessions, refresh tokens,
withdrawal freeze, executor stop) is what actually bounds the loss.
[owner: security-owner · test: tools/p07-drill.py]

**Break-glass.** Two distinct named humans, a reason of at least 20 characters, a 30-minute window, no cooldown
on opening one (an emergency path with a cooldown is not an emergency path), and every session it minted is
revoked when the window closes. An approver list containing the same person twice is refused: the duplicate
check is case-insensitive and whitespace-trimmed, because "me and the runbook" must not work.
[owner: founder-on-call · test: tests/test_security_core.py::TestKeyPolicyAndEnvelope::test_break_glass_needs_two_distinct_humans_a_real_reason_and_a_window]

**Export.** `keys.export_gate` refuses while the wallet has open orders, unfinished intents, an in-flight
withdrawal or an unreconciled deposit, requires a verified TOTP step, and checks the *requester* against the
policy — an admin asking is not the owner asking. [owner: security-owner · test: tests/test_security_core.py::TestKeyPolicyAndEnvelope::test_the_export_gate_refuses_the_four_situations_that_make_a_user_poorer]

**The imported-key tier, said plainly.** A key the user pastes to us cannot be scoped by any provider, because
the provider is not holding it. So the tier is not "the same product with a weaker guarantee": it stores the
secret in the same envelope, it is *never* used to sign without a TOTP step in the same request, the user can
delete it instantly, and it is excluded from the automation and copy-trading paths where a key signs without a
human present. A user who imports a key and enables automation is asking us to hold something we cannot bound,
and the UI says so instead of quietly allowing it.
[owner: security-owner · test: tests/test_security_plane.py::TestAdmin::test_a_scoped_revocation_names_its_target]
[UNVERIFIED — confirm before launch: imported-key-ui] The sentence the UI shows for this tier is drafted, not
approved, and P09 owns the screen.

---

## D3 · Authentication: the factors, the windows, and what gets refused

**Passwords.** Argon2id with `m=65 536 KiB, t=3, p=4, hash_len=32, salt_len=16`; the *floor* a stored hash must
clear to be accepted at all is the published 2024 minimum (19 456 KiB, t=2, p=1), and it lives in
`passwords.PARAMS`/`MIN_*` rather than in a comment. No custom hash function exists anywhere in this repository —
that is a decision, not an oversight, and `tools/lint-rules.py` would fail a PR that tried. A hash that parses
but sits at the floor still logs in and is **rehashed on the way through** (`needs_update`), because the only
moment we can upgrade a password hash is while we hold the password.
[owner: api-owner · test: tests/test_security_plane.py::TestLogin::test_the_hash_is_upgraded_while_we_still_hold_the_password] · [owner: api-owner · test: tests/test_security_core.py::TestPasswordRules::test_a_hash_below_the_floor_is_refused_rather_than_stored_anyway]

Wrong password and unknown account return the same body, and both are counted. Ten failures on one account
inside 15 minutes locks that account for 15 minutes; 40 from one IP trips a separate per-IP bucket, because a
shared office NAT exit must not lock an office out of the product. The window is fixed, not sliding — a sliding
window never stops a determined attacker and makes "am I locked?" depend on event order at 3am.
[owner: api-owner · test: tests/test_security_plane.py::TestLogin::test_ten_bad_passwords_lock_the_attacked_account_and_nobody_else]

**Telegram Mini App.** `initData` is verified against Telegram's documented construction, not from memory:
`secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)`, the check string being the URL-decoded
`key=value` lines sorted alphabetically, joined with `\n`, excluding `hash` (and `signature`). A malformed token
raises instead of producing an HMAC over an empty key — with an empty key every empty signature would match.
Freshness is mandatory even though the spec calls it optional: `auth_date` must be within 300 s for a login,
60 s of future skew tolerated, 24 h for a `sendData` refresh. A payload that has already bought a session is
refused forever (`telegram_nonces`), which is the branch that turns a captured query string into nothing; the
row survives a restart, and the test proves it.
[owner: api-owner · test: tests/test_security_core.py::TestTelegramInitData::test_the_secret_key_is_the_published_derivation] · [owner: api-owner · test: tests/test_security_plane.py::TestTelegram::test_the_replay_row_survives_and_names_the_session_it_minted]

Telegram's third-party mode (Ed25519 `signature` over `"{bot_id}:WebAppData\n" + check_string`, verified against
Telegram's public key) is supported by `telegram.third_party_check_string` and the verification is deliberately
left to the backend, because the key is rotated by Telegram and fetching it is a network call.
[UNVERIFIED — confirm before launch: telegram-third-party]

**TOTP is mandatory, not recommended.** `withdraw`, `key_export`, `address_add`, `address_remove`, `break_glass`,
`revoke_keys`, `un-halt` — the list is `totp.REQUIRED_FOR` and `is_mandatory()` is the only way routes ask.
RFC 4226/6238 vectors are asserted in the suite so a "refactor" of the truncation or the dynamic-offset
arithmetic is a red test rather than a security incident: the standard's own secret
(`base32("12345678901234567890")`) produces `755224` at counter 0 and `287082` at counter 1, and 8-digit
`94287082`. Window is ±1 step of 30 s — five minutes of acceptance is already a replay window, so we take the
support cost of clock drift instead. A code at or before the last accepted step is refused as `reused`; five
wrong codes lock the factor for 15 minutes (not the account, so the user can still read their positions); an
enrolment is not usable until a code is *verified* from it.
[owner: security-owner · test: tests/test_security_core.py::TestTotp::test_rfc_4226_vectors_as_relocated_by_rfc_6238] · [owner: api-owner · test: tests/test_security_plane.py::TestTotpAndAddresses::test_an_unverified_authenticator_authorises_nothing]

**Sessions.** Access tokens are short-lived; refresh tokens rotate on use, and presenting a rotated token again
revokes the whole family and writes `refresh_reuse` — theft, not clumsiness, is the default assumption.
`auth_events` records which session spent which token. A password or recovery change bumps the credential
generation and every session minted before it stops resolving, immediately, without a sweep.
[owner: api-owner · test: tests/test_security_plane.py::TestSessions::test_a_refresh_token_can_be_spent_once] · [owner: api-owner · test: tests/test_security_plane.py::TestSessions::test_a_password_change_ends_every_existing_session]

**SIWE (Ethereum signature login) is deliberately not built.** A signature that costs nothing to obtain cannot
gate a withdrawal; the honest use of a wallet signature is *binding an address to an account*, which is what
`user_identities` and the Telegram link already do, and it is the same primitive an attacker can replay from a
duplicated address. It returns in P10 with a nonce-bound typed payload if the product ever needs address-login.
[owner: security-owner · test: tools/p07-gate-check.py]

**Withdrawal-address cooldown: 24 hours, enforced in the schema.** `withdrawal_addresses.usable_ms` is
`created_ms + 86 400 000`, and a CHECK constraint makes `skip_cooldown` unrepresentable — there is no code path
to forget, and no admin action that can take it (it is in `ADMIN_FORBIDDEN`). An address in cooldown cannot be
deleted, or "wait a day" becomes "delete it and add the attacker's". Two accounts naming the same destination are
linked in the record, which is the cheapest detection we have for a takeover in progress. Ten destinations per
user, because a list of fifty is a dragnet and a list of ten is a person with a hardware wallet.
[owner: api-owner · test: tests/test_security_plane.py::TestTotpAndAddresses::test_a_new_destination_is_held_for_24_hours_and_cannot_be_deleted_while_held] · [owner: api-owner · test: tests/test_security_plane.py::TestTotpAndAddresses::test_the_destination_cap_holds_and_no_row_can_skip_the_cooldown]

**Recovery.** A token with a one-hour life, single-use, at most one request per 15 minutes, voided by a password
change (the generation bump is the mechanism) and by any of the two factors being *changed*, not just used.
Recovery never bypasses TOTP for a withdrawal in the same session it was used in.
[owner: api-owner · test: tests/test_security_core.py::TestPasswordRules::test_a_recovery_token_is_time_boxed_single_use_and_voided_by_a_password_change]

---

## D4 · Authorisation: one table, five levels, and a route that forgot is a build failure

`authz.LEVELS_TABLE` maps 39 served operations to `public | user | user-owns-resource | admin | service`, and
`authz.require()` is the single entry point every route calls. Three properties are worth their space:

* **Undeclared fails closed at 500, not open and not 401.** `AUTHZ_UNDECLARED` on a route that grew without a
  row is a deployment bug, and mapping it to a 401 (which an earlier draft did) turned a bug into thousands of
  confusing "log out again" reports and hid the real fault. The mirror test walks the served OpenAPI paths and
  the Starlette routes and asserts `coverage()["undeclared"] == []`.
  [owner: api-owner · test: tests/test_security_plane.py::TestAuthzTable::test_every_served_route_declares_a_level_and_the_mirror_agrees]
* **Object-level misses answer 404.** `GET /v1/orders/{intent_id}` for somebody else's intent is
  indistinguishable from a nonexistent one; a 403 is an oracle that tells an attacker which ids are real.
  [owner: api-owner · test: tests/test_security_plane.py::TestAuthzTable::test_object_level_checks_are_404s_not_403s]
* **Placeholders may be renamed on one side.** `{intentId}` in the registry and `{intent_id}` in the router are
  the same route; the matcher is pattern-based on purpose, because the first version of this table keyed on the
  literal string, every route 500'd, and the code was turned into a 401 that looked like an auth problem for a
  day. [owner: api-owner · test: tests/test_security_core.py::TestAuthzRegistry::test_a_placeholder_rename_does_not_unclassify_a_route]

`service` is declared and enforced (`authz.check_service_token`: constant-time, an unset or short expected value
matches *nothing*) but **no HTTP route uses it yet** — the executor is a queue consumer from P06, not a
listener. The branch is exercised against a probe row the test installs and removes, so it is not a vacuous
pass, and the first P08/P12 webhook inherits it. [owner: executor owner · test: tests/test_security_core.py::TestAuthzRegistry::test_each_level_refuses_the_wrong_kind_of_caller]

`X-User-Id` is a development affordance and nothing more: it is refused outright when
`PGM_REQUIRE_SECURITY_ENV=1`, a bearer token always wins over it, and a mismatch between the two is a refusal
rather than a preference. [owner: api-owner · test: tests/test_security_plane.py::TestSessions::test_the_dev_identity_header_is_off_when_the_security_env_is_required] · [owner: api-owner · test: tests/test_security_plane.py::TestSessions::test_the_bearer_identity_beats_the_header_on_the_order_path]

**Admins may not**: `place_order`, `bypass_risk_gate`, `move_user_funds`, `delete_ledger_row`,
`skip_withdrawal_cooldown`, `un-enroll_totp`, `export_key_without_user`. The two that look like customer support
quality-of-life are the two that would let a single compromised admin login beat the whole plane; they route
through the user's own authenticated flow with a second approver instead.
[owner: founder-on-call · test: tests/test_security_plane.py::TestAuthzTable::test_admins_are_denied_the_two_operations_that_matter]

---

## D5 · Input integrity: we render nothing we did not clean

**Schemas are strict in both directions.** Every POST body is validated against a declared field set; unknown
keys are rejected rather than ignored, because an ignored key is how a `skip_cooldown` that no test asserts on
becomes a real parameter. `Error` has `additionalProperties: false` and no `detail` field *ever* — that is where
a Python exception message would have gone, and the contract check fails if it returns.

**Market metadata is untrusted input authored by whoever created the market, which in the worst case is the
attacker.** `sanitise.clean_text` runs on every externally-authored string: entity-decode repeatedly (so
`&lt;script&gt;` cannot survive one pass), strip invisible and directional characters, tag-strip to text, name
the phishing shapes we can recognise (`claim_airdrop`, `connect_wallet`, `send_funds_to`, `fake_support`,
`urgency`, `account_verification`, `yield_promise`, `free_tld_link`), flag a domain whose confusable-folded form
is a brand we or Polymarket own (`impersonates:polymarket.com`), flag mixed-script domains, and cap length
*after* cleaning with the truncation reported rather than hidden. `market_metadata` refuses outright where there
is nothing to render (`no_title`, `no_outcomes`) and flags rather than truncates where truncation would
misrepresent (33+ outcomes): the reader should see "too many outcomes to list" instead of a market that appears
to be binary.
[owner: ingest-owner · test: tests/test_security_core.py::TestSanitise::test_a_lookalike_domain_is_flagged_rather_than_folded_into_the_real_one] · [owner: ingest-owner · test: tests/test_security_core.py::TestSanitise::test_the_whole_market_object_is_cleaned_bounded_and_badged]

**Images are proxied or not shown.** https only, no raw third-party URL in the page (that URL is a
`Referer`-bearing leak and an analytics vector), and the proxy path is signed with
`PGM_IMAGE_PROXY_SECRET` so it cannot be pointed at an arbitrary host. The proxy *fetches* on our side, which
is the SSRF exposure; the egress allowlist in D7 is what bounds it.
[owner: ingest-owner · test: tests/test_security_core.py::TestSanitise::test_third_party_images_are_proxied_and_the_proxy_url_is_signed]
[UNVERIFIED — confirm before launch: image-proxy-fetch] The fetch side (timeouts, byte caps, redirect policy,
no internal ranges) is specified in `docs/AGENTS-BUILD.md` but the proxy worker is P08's; the gate checks the
spec exists and says which half is unbuilt.

**The resolution source is the field an attacker controls least — that is the test for what we display as
fact.** `sanitise.resolution_source` trusts only allowlisted hosts; anything else becomes a link with a badge,
never a verified line, and `broadcast_gate` refuses a market whose resolution source is not trusted.
[owner: ingest-owner · test: tests/test_security_core.py::TestSanitise::test_the_resolution_source_is_the_field_the_attacker_controls_least]

**No floats in the money path**, enforced by a lint rule rather than by care: `money-no-float` and
`money-as-string` are two of the eight, both with a planted canary that the lint self-test requires to fire.

---

## D6 · Secrets: never in the repo, never in a log, never in an error

`.env.example` carries every variable with an **empty value**; the file is the list of what exists, not a place
to put things. Real values live in the environment, the KMS, or a developer's shell.

**CI greps every log line before it lands.** `tools/ci-log-scan.py` reads the CI job's own output and every
tracked source file against the same shape rules the redactor uses, plus the provider-token shapes this product
does not use but a contributor will paste anyway. Three properties make it trustworthy: the excerpt it prints is
a *fingerprint* rather than the line (a scanner that prints the secret it found puts the secret somewhere new —
usually the CI log, which is public on a public repo); `--self-test` proves each rule can fire and that clean
lines stay quiet; and the allowlist is matched against `path + ":" + line`, which was fixed this phase after the
scanner reported four findings in its own fixture table and revealed that the path-scoped exemptions nobody had
ever actually honoured.
[owner: security-owner · test: tests/test_security_core.py::TestRedact::test_every_shape_we_could_ever_log_is_gone_and_an_independent_matcher_agrees]

**The redactor runs before the line, not instead of it.** `redact.line()` is the one log builder; PATTERNS cover a connection URI's password, PEM blocks, 32-byte hex keys, bot tokens, JWTs, bearer/basic headers, labelled secrets, query parameters,
`initData`, emails, addresses, phone numbers, seed phrases and a 2 000-character cap per field. `scan()` is a
deliberately *independent* matcher so the redaction is checked by something other than the regex that removed it
— that is how the `***` placeholder bug was found: `scan` was reporting our own redaction as a secret, which
was wrong, but it proved the two lists are genuinely separate.
[owner: security-owner · test: tests/test_security_core.py::TestRedact::test_scan_reports_the_rule_name_so_ci_can_fail_with_a_reason]

**Sentry gets nothing it can leak with.** `redact.before_send` is installed at boot
(`app.py::_install_sentry_scrubbing`) with `send_default_pii=False`, `request_bodies="never"`, breadcrumbs
capped, and the scrubber applied to the message, the transaction, the request URL and query string, the headers,
the user context, the breadcrumb messages *we* wrote, and `contexts`. The request body is dropped rather than
scrubbed: a body is where a pasted private key lives. If the SDK is missing while `PGM_REQUIRE_SECURITY_ENV=1`,
boot **refuses** and `/readyz` names the problem — a security control that silently stops installing is worse
than an absent one, because the absent one is not in the runbook.
[owner: security-owner · test: tests/test_security_core.py::TestRedact::test_before_send_scrubs_the_event_end_to_end_and_never_raises]
[UNVERIFIED — confirm before launch: sentry-retention] What Sentry *keeps* server-side after scrubbing (and for
how long) is their contract, not ours, and no scrubber recovers an event that left the process.

**An untested backup is not a backup.** `incident.backup_is_current` requires a restore *read back*:
`restore_done_ms` within 7 days, `verified_rows > 0`, `money_checks_ok`, `encrypted`. A backup that was written
and never restored is a rumour, and the row shape exists so the argument is about a query instead of a vibe.
The staging keystore gets a 30-day ceiling because its restore is a different ceremony.
[owner: ops-ani · test: tests/test_security_core.py::TestIncidentResponse::test_an_untested_backup_is_not_a_backup]
[UNVERIFIED — confirm before launch: backup-restore-staging] No restore has been performed against the real
staging bucket yet; the drill in this repository is against a local database, and the gap is the bucket.

---

## D7 · Network and supply chain: the shape of the box, not the firewall rules inside it

**No ingress to the executor, stated in the file a reviewer reads.** `docker-compose.yml` puts the signer on an
`internal: true` network with no `ports:` and no `expose:` — `expose` is documentation, and documentation that
says "reachable" is what a reviewer trusts. Egress goes through the proxy, which is the only place an allowlist
exists; `PGM_SERVICE_TOKEN` is the executor's whole environment along with the DB and venue URLs, and the KEK is
not in it. [owner: executor owner · test: tools/p07-gate-check.py]

**Digest-pinned images for the critical set.** `deploy/image-digests.txt` is deliberately empty in this tree and
says why (no container daemon in the build environment, so any digest written here would be invented); the file
lives under `deploy/` because `var/` is gitignored, and a pin CI never clones is not a pin.
[owner: security-owner · test: tools/dependency-scan.py] [UNVERIFIED — confirm before launch: image-digests]

**Dependency scanning is product work.** `tools/dependency-scan.py` requires an exact pin on every line of every
`requirements*.txt`, reports `web/package.json` as *not applicable until P08* rather than passing vacuously, and
runs `pip-audit` when it is importable — printing "the advisory feed was not consulted" when it is not, which is
the sentence an auditor should see more often. A lockfile is not a security control; a lockfile whose hash was
never checked against an advisory feed is a *reproducibility* control, and the difference is what this tool's
output says out loud. [owner: security-owner · test: tools/dependency-scan.py]
[UNVERIFIED — confirm before launch: dependency-feed]

**CSP and `frame-ancestors`, including for the Mini App.** The API default is
`frame-ancestors 'none'` with no `X-Frame-Options` at all (the latter cannot express an allowlist and would
override the former on older clients). Telegram's webview sends `x-openout-frame: miniapp`; only that request
gets `frame-ancestors https://web.telegram.org https://*.telegram.org`, overridable by
`PGM_MINI_APP_FRAME_ANCESTORS` because Telegram's hostnames are theirs to change.
[owner: api-owner · test: tests/test_security_plane.py::TestHeadersAndRedaction::test_the_mini_app_allowlist_is_not_the_api_policy]
[UNVERIFIED — confirm before launch: miniapp-frame-ancestors] The final allowlist needs the OpenRouter host
that shells the app, which P08 picks.

**Responses that carry identity say `no-store`; market data does not**, because caching public data is a
feature and caching who-is-logged-in is a incident. [owner: api-owner · test: tests/test_security_plane.py::TestHeadersAndRedaction::test_an_identity_response_is_never_cacheable_and_a_market_list_still_is]

---

## D8 · Abuse: the money we hold, the megaphone we own, and the one we refuse

**Wash trading is scored on fees, because P&L is trivially faked.** `abuse.wash_score` adds four facts a human
can re-check with a `SELECT`: self-cross (maker and taker are the same address, or the counterparty is you) 4
500 bps; a round trip inside 10 minutes 2 500; counterparty concentration ≥80 % across ≥5 fills 1 800; three
identical sizes 1 200. At 4 000 bps the payout is held; at 7 000 it is frozen and a person is paged. Two design
choices matter more than the weights:

* the gate is `payout_gate`, on **our** commission, never on their order — a false positive that stops a trade
  costs a user a fill they needed, a false positive that holds a payout costs a creator a week and a phone call;
* the "identical size" factor needs three, not two, because a human who trades round numbers twice is a person.

[owner: security-owner · test: tests/test_security_core.py::TestAbuse::test_wash_trading_is_scored_on_the_shape_of_the_fills_not_on_a_pnl_claim] · [owner: security-owner · test: tests/test_security_core.py::TestAbuse::test_the_gate_holds_our_money_and_never_their_order]

**Referral and Sybil.** `referral_flags` refuses a payout to a self-referral outright (no hold date — a hold
implies a date, and this does not), and requires three trades and $500 of turnover before a referee earns
anything. `cluster_size_for` groups by (IP hash, user-agent hash) with the *empty* fingerprint excluded: an
unknown IP must never make somebody a Sybil. [owner: security-owner · test: tests/test_security_core.py::TestAbuse::test_a_referral_payout_needs_real_turnover_and_a_self_referral_never_gets_one]

**The builder code is the venue's resource, not ours, and when it stops working the user hears it from us.**
`builder_code_event` moves `active → throttled → disabled` on rejections *inside a 60-second window* and back to
active when the venue confirms; rejections older than the window are history, not a pattern, and that was a bug
we fixed this phase (the counter ratcheted, so a venue hiccup in March could keep a code stripped in June). When
a code is disabled, the user's own copy says: *"Your order went through. The commission code we attach to
orders was turned off by the market's operator, so orders are running without it while we sort that out."* —
the money moved, so the message must not imply it did not, and "failed" appears nowhere in it. Continuity plan:
trading proceeds without the code, the commission is simply not claimed, and if the venue's own state says
active again the code returns without the user doing anything.
[owner: security-owner · test: tests/test_security_core.py::TestAbuse::test_a_disabled_builder_code_tells_the_user_it_helped]

**The alert channel's gate runs before the broadcast, not instead of it.** `broadcast_gate` needs ≥$500 on the
book, a market older than 30 minutes, a *trusted* resolution source, no markup or lookalike-domain flags, an
audience, a creator wallet older than 72 hours, no duplicate inside 24 hours, and ≤5 broadcasts an hour from us.
`hold` (time-based, will resolve) and `refused` (the content is the problem) are different verdicts, and both are
written to `broadcast_gates` — a broadcast refused silently is indistinguishable from a broken pipeline, which
is how a team ends up turning the gate off.
[owner: ingest-owner · test: tests/test_security_core.py::TestAbuse::test_the_megaphone_has_a_gate_that_distinguishes_wait_from_never]

**Rate-limit exhaustion is a product-quality problem wearing a security costume.** 200 alert rules per user
against a 300-request budget is the same outage as an attack, and the answer is not a queue that drops: one
user's share is 25 % of the budget (75 polls/min), the excess is *scheduled* round-robin, the rule's own UI says
what rate it will run at, and the alarm goes at 70 % headroom. A rule the user can see and we never run is a lie
about the product, so the policy string in `upstream_budget_guard` is "schedule and say so; never silently drop".
[owner: ingest-owner · test: tests/test_security_core.py::TestAbuse::test_rate_limit_exhaustion_is_scheduled_with_a_number_not_absorbed_by_the_loudest_user]

**Geofencing: the decision, written down.** We do not geo-block at the application layer at launch. The honest
reasons, in order of weight: (1) an IP check we control is trivially bypassed by the audience that would use
it, and the *cost* of bypass is roughly zero for a VPN-literate user and total for everyone else, so it filters
honest users and not others; (2) our exposure is not "an American saw a UI" but "we facilitated a trade for a
sanctioned address", which is screening, not geography, and is a P13 legal task with a different tool;
(3) the loss we would prevent by geofencing (per-row above: not in the top 18) is smaller than the loss from
shipping a broken onboarding flow in the jurisdictions we have most users in. What we ship instead: no
promotional targeting into US app stores, the venue's own restrictions respected for anything *we* sign, and
`user_identities` + `auth_events` retained so a real legal request can be answered accurately.
[owner: security-owner · test: tools/p07-gate-check.py] [UNVERIFIED — confirm before launch: geofence-counsel]
This ruling is a product decision by an engineer and needs a lawyer's sign-off before launch, including for
the sanctions-screening question above.

---

## D9 · Incident response: the drill comes before the launch

**First 60 minutes** (`incident.first_60_minutes()`), eight steps, each with an owner, a verification and a test
reference: declare the severity in the channel and page the named humans (2 min — the page *is* the clock);
engage the kill switch with a reason (5); revoke sessions and refresh tokens (10) — the attacker's foothold,
not the key; freeze withdrawals and exports at the *policy* level rather than by asking a provider politely
(15); revoke the affected keys, or all of them if the blast radius is unknown (25); preserve — snapshot the DB,
the `auth_events` window, the executor's logs and the venue's order list, and write each snapshot's hash into the
incident record (40); notify users with the template at the 60-minute mark, not after the forensics (60); and
only then start the post-mortem clock. A channel-scoped variant exists for `channel_poison`.
[owner: founder-on-call · test: tests/test_security_core.py::TestIncidentResponse::test_the_first_sixty_minutes_has_an_owner_and_a_proof_per_minute_budget]

**Severities are computed, not declared.** `severity_for(money_moving, keys_exposed, data_left, degraded)` →
S1 pages in 30 s with a 5-minute ack, S2 in 5 minutes, S3 is a status-page line with a 30-minute page, S4 pages
nobody (and says so — a band that pretends to page is how a team stops reading pages). Every band names humans
and states what the customer is told.
[owner: founder-on-call · test: tests/test_security_core.py::TestIncidentResponse::test_severity_is_a_rule_two_people_at_4am_apply_the_same_way]

**Eleven breach alarms** (`policy_drift`, `keystore_unwrap_burst`, `session_mint_burst`, `refresh_reuse`,
`totp_lockout_burst`, `withdrawal_address_churn`, `export_after_hours`, `unattributed_order_surge`,
`telegram_replay_refused`, `broadcast_gate_refused_spike`, `admin_path_touched`) and nine operational ones, kept
in separate lists on purpose: a breach alarm buried among latency pages gets ignored, and `admin_path_touched`
fires on a legitimate break-glass too — an emergency path that only logs when it is abused is a path with no
audit. [owner: security-owner · test: tests/test_security_core.py::TestIncidentResponse::test_no_alarm_is_orphaned_and_no_step_is_unwired]

**The key-compromise drill runs before launch and quarterly after** (`make drill-p07` →
`docs/verification/P07-key-drill.txt`), and `incident.drill_is_current` (90 days) is what the gate checks — not
"we have a runbook". The artifact ends with a paragraph titled *what this does NOT prove*, because a drill
artifact that reads like a certification is how a team talks itself into skipping the next one.
[owner: security-owner · test: tools/p07-drill.py]

**The customer notice has a style rule that is actually a test.** `incident.template_ok` fails on
`"no evidence of"`, `"we believe there is no"`, `"at this time"`, `"isolated incident"`, `"out of an abundance
of caution"`, `"no indication that"`, and on a template that omits either a next-update time or a sentence about
the user's money. Two of our three templates failed that rule when it was written, which is the whole argument
for testing prose: every crypto product that lost trust lost it in the *notification*, not in the breach.
[owner: security-owner · test: tests/test_security_core.py::TestIncidentResponse::test_the_customer_templates_pass_our_own_style_rule]

---

## D10 · Compliance, in plain language

What we are, said the way a user would read it: **Openout is software in front of someone else's market.** We
do not hold custody of a balance the way an exchange does — the money sits in a wallet whose key we may use only
to place an order and move the settlement token, and the user can delete that key or take it away at any moment.
That is a real distinction and it is also the *weakest* sentence in this document, because "we cannot do
anything else with it" is a promise about our key management, not a property of the blockchain; which is why D2
is the largest section here.

What we will tell a customer, in order: what we store (identity links, balances, orders, the last 90 days of
audit events, IP *hashes* not addresses); what we never store (seed phrases for external wallets, passwords in
any reversible form, card data — we take none); what we will do if we are breached (the D9 clock, the D6
scrubbing meaning the leak does not include our credentials either); and what we cannot promise (that a market
resolves fairly, that the venue stays up, that a key provider's own compromise is invisible to us).

Tax reporting: we do not issue documents at launch and we do not claim to be exempt; we export a flat CSV of
fills, fees and transfers per wallet because that is what any obligation eventually needs, and the answer to
"do we owe anything" is a lawyer's, not an engineer's. [UNVERIFIED — confirm before launch: tax-1099]
Sanctions screening of counterparties and venue addresses is a P13 item with a vendor, and until it exists the
product must not be marketed where it matters. [UNVERIFIED — confirm before launch: sanctions-screening]

---

## Controls deliberately not built, with the arithmetic

| not built | why | what we do instead |
|---|---|---|
| HSM for the KEK | at $0 AUM a rented HSM is a fixed cost against an expected loss that is mostly *our* engineering time; the envelope + KMS + no-KEK-in-compose already bounds the blast radius | KMS-held KEK, 48-byte wrapped DEKs, drain-and-rotate drill |
| SOC 2 / pen test before launch | weeks of lead time and a five-figure bill for a product with no funds; the finding a pen test would produce first (authz, secrets, key handling) is covered by the 131 tests here | re-evaluate the moment AUM > $250 k; the gate prints that threshold as a reminder |
| Full Unicode confusables table | ~2 500 lines of generated data on the hot path of every market update, to catch a domain nobody has registered yet | 40-entry lookalike map + mixed-script detection + `impersonates:` flag; revisit when a real one is used against us |
| Device binding / IP allowlist for login | pushes users onto mobile networks and VPNs into lockout; expected loss prevented is a fraction of the support cost | per-IP failure bucket (40), keyed IP hash for clustering, TOTP on money paths |
| 24/7 human on-call | paying two people to watch an empty queue is theatre until there is a queue to watch | S1/S2 page the founder + the owning engineer, 30 s/5 min targets, `SEVERITIES[].who` is the page list |
| Geo-blocking in the app | see D8: filters honest users, not others | no US promotional targeting, screening as a P13 legal task |

---

## Launch checklist — every `[UNVERIFIED]` in this document

Each is a gate failure until it is closed; the gate asserts each slug appears here and nowhere is a slug
mentioned in the prose without a line in this list.

- [ ] **threat-model-numbers** — replace the planning assumptions above with the real cohort once there is
      volume; re-sort the table.
- [ ] **venue-addresses** — take `CLOB_EXCHANGE` / `PUSD_TOKEN` from the venue's documentation, write them into
      `deploy/venue-addresses.json` with `status: confirmed`, and keep `keys.py` in step.
- [ ] **provider-scoping** — get it in writing that the provider can restrict call targets, expose the policy
      hash per signature, revoke immediately, and require the end user for export. If any answer is "no": the
      provider is disqualified (D2) and the self-hosted envelope becomes the primary plan, not the fallback.
- [ ] **imported-key-ui** — the sentence users see on the imported-key tier, approved by whoever owns support.
- [ ] **telegram-third-party** — decide whether to verify Ed25519 `signature` ourselves or to trust the
      bot-token HMAC; fetch and pin Telegram's public key set if the former.
- [ ] **image-proxy-fetch** — timeouts, byte cap, redirect policy, no RFC1918/loopback/metadata ranges, egress
      through the proxy only.
- [ ] **sentry-retention** — read the contract: data groups, retention window, and whether `before_send`
      applies to session replays (it does not by default, so replay stays off).
- [ ] **backup-restore-staging** — perform a real restore from the staging bucket and record
      `verified_rows`/`money_checks_ok` in `security_backups`; until then this row is empty and the gate says so.
- [ ] **image-digests** — populate `deploy/image-digests.txt` from the build that signs, on a machine with a
      container daemon.
- [ ] **dependency-feed** — run `pip-audit` (and `npm audit` once P08 exists) in CI with network access, and
      record the last run's timestamp in `docs/verification/`.
- [ ] **miniapp-frame-ancestors** — the sheller's real hostname from P08 goes into
      `PGM_MINI_APP_FRAME_ANCESTORS`.
- [ ] **geofence-counsel** — legal sign-off on the D8 ruling, including sanctions screening and where the
      product may be marketed.
- [ ] **sanctions-screening** — vendor chosen, screening on the counterparties and venue addresses we touch.
- [ ] **tax-1099** — accountant's answer on our obligations in each launch jurisdiction, and the CSV's field
      list agreed with them.
- [ ] **incident-paging-phone** — the page targets (30 s / 5 min / 30 min) are only real if a human with a phone
      is named in `SEVERITIES[].who`; today they are role names.

---

## What this phase does **not** prove

* It does not prove the controls work against a real adversary: the attacks here are the ones we could
  reproduce in-process, and a hostile market, a hostile provider and a hostile Telegram user are all *simulated
  by fixtures that agree to be caught*.
* It does not prove the key provider can do what D2 requires. It proves what we *demand*, which is the part this
  repository can own; the demand being satisfiable is a P13 conversation with a vendor and a contract.
* It does not prove the app is safe. The web front end is P08; anything that runs in a webview is untested here,
  including the CSP's interaction with the sheller and the Mini App's storage of tokens.
* It does not measure the numbers in D1; it ranks them. The ranking is the deliverable, the magnitudes are a
  proposal.
* It does not prove the log scanner is complete. `scan()` and `PATTERNS` are two independent lists on purpose —
  which means a shape in neither is invisible to both, and the only defence against that is the self-test and a
  reviewer who asks what a rule was written for.
* The drill measures a local, in-process keystore with a mock provider: the wall-clock it reports is *ours*, not
  a provider's, and the difference is the whole lesson of D2.
