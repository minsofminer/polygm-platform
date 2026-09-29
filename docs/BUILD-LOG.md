# BUILD-LOG — one entry per phase

Format per phase: **built / verified / `[UNVERIFIED]`**. Newest first.

---

## P15 — deployment, observability and operations (2am, one page, a phone, five minutes)

**What the phase gates on.** Not a checklist: a scenario. It is 2am, you get one page, and from a phone in under
five minutes you can say whether the system is healthy, whether any user's money is inconsistent, and whether you
should stop trading. Everything below is arranged around making those three answers cheap.

**Three of this phase's gates failed because the gates themselves had rotted, and each fix was a real improvement:**

| failure | the real cause | the fix |
| --- | --- | --- |
| `make check` → `gate-mutate` baseline | the harness copied the tree with `git init`, so the copy had an *unborn HEAD*; three checks ask the copy about its history (exemption liveness, `git grep`, the new deploy-ledger test) | clone with history (`--local`, falling back to `--shared` across filesystems) and lay the working tree over it |
| the secret-scan's exemption liveness | `git log -S` matched the token inside the allowlist's own *description* of the entry, so every entry was permanently "live" and a dead one could hide the next real key | exclude the allowlist from the search; the entry now legitimately matches the commit it was written for |
| two wallet tests, one full-suite run in three | the TOTP arming code came from `setUp`'s frozen clock while the server windows the code at request time; a 30 s boundary turned a correct code into `cur−2` | compute the code from the live clock, prev→current window, re-enrol up to three times; verified 3× wallet-only, 3× full suite |

**Two fail-open paths in the deploy audit** were found while writing its tests, and both are now refusals with
tests: `begin` in a repository with an unborn HEAD would have written an entry naming no revision (a ledger that
looks complete and names nothing to roll back to), and `verify` could return PASS while checking nothing, because
with no resolvable commits every per-entry check passes vacuously. `PGM_DEPLOY_REPO` points the tool at a scratch
checkout, which is how both refusals became provable rather than asserted.

**The alarm engine was lying in two ways, and the drill is what found them.** Notifications rendered
`<object object at 0x…>` for every value (the placeholders resolved against the rule's detail rather than the
payload, and `json.dumps` of the missing-value sentinel produced a Python repr), and a list rule that filters on
one field and tests another could never fire (the filter needs the items, not the projected booleans). Both fixed;
`{path|age}` and `{path|usd}` render durations and micro-dollars; an unresolved placeholder is `?`.

**Every page-class alarm now fires on purpose, and the recorded evidence is the notification text.**
`tools/p15-alert-drill.py` seeds each rule's condition into a copy of the seeded database, reads
`/v1/admin/metrics` through the app, evaluates the registry and **fails if its rule does not fire**. Two more bugs
came out of it: one clock for the whole run made healthy feeds look lagging by the tenth scenario, and the seeded
sample's stuck deposit fired in every scenario until the drill learned to reset its own baseline. Ten firings are
recorded in `docs/verification/p15-alerts-fired.jsonl`, each with the rendered notification and the environment
("seeded locally") in the evidence — delivery to a phone needs the bot token and stays an owner step.

**Observability gained the blocks the kit names and the endpoint did not have:** an executor heartbeat (one row,
written at the *top* of each tick so a hung tick goes stale instead of reporting alive), three named
compromise indicators, the builder code's state as the venue told us, in-flight money (stuck deposits, withdrawals),
delivery counts, and per-feed `transport`/`lagging`/`thresholdMs`/`resyncs` where `null` means "not reported" and
never "zero".

**DR has a tested restore with a measured result.** 123 tables, 3,261 rows, money checksums matched on all seven
money columns, 58 triggers present after the restore, the append-only guard confirmed live, 128 ms. The production
transport stays `[UNVERIFIED]` — and the drill prints that sentence in its own transcript, so the gap travels with
the evidence.

**Cost is arithmetic in one file** ($119.38/month against a $300 envelope, 39.8% used) checked against the D2 doc
and the Terraform output, which is how a stale `~$72` was found. **Runbooks are checked as procedures:** 18 pages,
every tool invoked with its own `--help` to verify flags — which caught three wrong commands and one module path
that does not work from the repo root. **Dashboards are three self-contained ~9 KB pages** whose alarm blocks are
rendered by *importing* the alarm engine, so a screen and a page cannot disagree.

**D9's checklist is 0/8 by artefact today, and it says so.** `tools/p15-readiness.py` names the missing artefact
for every red line and refuses to record a signature while one is red.

**Late fixes, all of them found by running the whole gate rather than by reading it.** A gate that has not been
run end to end in a while rots exactly where nobody is looking, and these four were all stale or wrong in ways the
phase's own prose was happy with:

| what failed | why | fix |
| --- | --- | --- |
| `p05-gate-check` | the check asserted `len(kinds) == 8`, true when written and false since P10 D9 added three | a floor plus the extras named; the count is never re-hardcoded |
| `ci-log-scan.py --sources` | crashed (`KeyError: 'string'`) on the allowlist's third entry shape, and the planted-key probe passed *because* the unscoped `AKIA…` exemption covered the plant | an entry may name a `path` and then only exempts that file; probe rc=1, `--sources` rc=0, `--self-test` rc=0 |
| `web-build` (Turbopack) | OOM-killed on a 2 GB box, which hid a real type error: `metadataFor` exported from three `app/leaderboard/**/page.tsx` | extracted to `web/src/public/metadataFor.ts`, a `page-exports` guard added, and `next build --webpack` is `WEB_BUILD_FLAGS` — it skips `npm run measure`, so the committed Turbopack bundle record cannot be overwritten by a different build's chunking |
| the P14 **F17** churn test | it keyed its observation on `threading.get_ident()`, which the kernel recycles: under CPU contention the second wave inherited the first wave's numbers and the test reported "a thread was handed more than one connection" when nothing of the sort had happened | the observation is per worker; the mechanism (per-thread storage, a registry of thread *objects*) is asserted directly, and a self-test plants the pre-P14 `(ident, connection)` shape and requires that assertion to reject it |
| `deploy/*.sh` in git | both were tracked `100644`, while the pipeline SSHes `deploy/deploy.sh …` and the runbooks say `deploy/rollback.sh --env prod …` — a fresh checkout, which is what a deploy host *is*, would answer "Permission denied" | the two scripts are committed `100755`; a rollback that fails on a permission bit at 2am is a rollback that does not exist |
| the P08 payload budget | the committed measurement said 186–189 KB with 13 KB of room; the tree measured **208.4 / 200.1 / 226.7 KB** on `/`, `/markets`, `/tma`. The record was from the P08 era, and `c8` only compared its mtime against the newest source file — a check that can only see whether a number was written *recently*, never whether it is *true* | the budget is enforced again on a measurement that names its own scope; see below |

**The payload budget was the finding of the session, and it arrived as a side effect.** Regenerating the web's API
schema made a source file newer than `docs/verification/P08-bundle.txt`, which is what `c8` checks — and when the
number was finally re-measured against a build instead of against a timestamp, three routes were over the 200 KB
budget the shell had claimed to be under for five phases. The measurement that produced the old record was real; it
had simply never been repeated. Two fixes, and the second is the one that matters:

- **the payload.** `app/page.tsx` imported the Mini App screen *statically* for a branch that only the Mini App's own
  deployment takes, so the marketing page shipped the trading terminal to every visitor; and the Mini App itself
  shipped the trade sheet and the wallet views before either was asked for. Both are chunk boundaries now.
  Measured: `/` 208.4 → **184.2 KB**, `/markets` 200.1 → **192.6**, `/tma` 226.7 → **196.1** (first-party), with a
  full web suite of 64 files / 569 tests green and the P08 gate back to 16/16.
- **the measurement.** The budget now says what it budgets — first-party JS — and the one script that is outside it,
  Telegram's platform bridge, is *measured and printed per route* (18.0 KB on `/tma`) rather than counted or hidden.
  `c8` requires the artefact to state its scope and to name every excluded script with its size, so a future
  third-party script cannot cross that line without somebody writing a sentence about it.

The context that made the decision legible, and worth keeping: a **minimal** Next 16.3.5 + React 19 App Router app,
built with this toolchain and containing no application code, measures **169.0 KB** of first-party JS. 84% of the
budget is the framework, which is why 17 KB of `@tanstack/react-query` — configured `refetchOnWindowFocus: false`,
`retry: false`, per-call-site `staleTime`, i.e. told not to do the things a query library is for — stopped earning
its place; it is `src/api/data.ts` now, with the semantics it relied on asserted in `src/api/data.test.ts`.

The last one is the uncomfortable kind: a **red gate that was not a product bug**, and the honest reading is that
the *test* was wrong, so the fix had to keep the regression value rather than merely stop the flake.
`tools/p07-mutation-test.py` therefore gained two mutants for F17 — a reap that ignores `t.is_alive()`, and a
connection registered against a live thread that does not own it — and the second one **survived the first
attempt**: the assertion that was meant to catch it ran on the main thread, where `main_thread()` and
`current_thread()` are the same object, so a misattributed registry looked correct. The check now runs inside the
threads that own the connections, and the mutant is killed by the test that names the property. Both mutants are
in the recorded run: **30 planted weaknesses, 30 KILLED, 0 survived** (`docs/verification/P07-mutation.txt`).

---

## P14 — Security Testing · 2026-09-22 → 09-23

**Built.** `docs/P14-security-testing.md` (D1–D8) and `docs/P14-audit-bounty-legal.md`, plus six harnesses:
`tools/p14-authz-matrix.py` (every served operation, against two real accounts, in the production identity shape),
`tools/p14-attack-surface.py` (trading, injection, business logic — 56 checks made by *empowered* accounts),
`tools/p14-key-drills.py` (the six compromise drills), `tools/p14-appsec-scan.py` (+`tools/sast-triage.json`,
20 entries over 80 findings, and `docs/dependency-review.md`), `tools/p14-infra-verify.py`, and
`tools/p14-abuse-probe.py`. `tools/p14-security-gate.py` generates `docs/P14-security-gate.md` from the recorded
artifacts and `--check` fails when the document no longer matches them; `make security` / `security-record` /
`security-gate` and `.github/workflows/security.yml` (PR-fast job, nightly full) wire it into CI.

**Verified.** Authorisation matrix **37/37 PASS** (95 operations served of 109 declared, drift 0; no cross-user
read, write or escalation; 39-request IDOR fuzz → 404/422 only). Attack surface **56 passed / 0 failed / 8 OPEN**.
Key drills **15/0/1** (six drills, 0.03–0.2 s each; local break-glass 0.06 s for 500 keys). AppSec **36/0/1**
(ruff SAST 80 findings → 20 triaged entries; 85 commits and 166,300 added lines of history → 0 secret findings;
CI log redaction). Infra **20/2/7** — the two failures are owner actions. Abuse **18/0/1** (100 aggressive users,
600 requests, 0 5xx; the login budget and the timing oracle both found and fixed here). `pytest` **1355 passed**,
`vitest` 62 files / 560 tests.

**Nineteen findings, F1–F19, every one with a re-test.** The ones that mattered: **F9** the *live* deployment
accepted a spoofed `X-User-Id` and served its whole schema (fixed by generating the security-plane secrets, setting
them on the Vercel project and redeploying; re-verified live: 401/404); **F17** a segfault under thread churn from a
connection proxy keyed on `threading.get_ident()`, where idents are reused (fixed with `threading.local()` + strong
thread refs); **F18** a zero-size order was an unbreakable 500 — the gate denied it `ZERO_SIZE` and then the schema's
own `CHECK (size_micro > 0)` killed the refusal path, so the client got "retry with the same key" forever (fixed:
refused as malformed input before anything is recorded, schema invariant untouched); **F19** an automation rule
whose action was $11,000 against a $2,500 per-order cap compiled, saved and dry-ran clean, and would have been
refused by the gate on every fire — an armed rule that could never trade (fixed: the engine's validator enforces the
ceiling, read from `config.flags` at both save and fire time).

**Verdict, in writing.** `docs/P14-security-gate.md` is **NO-GO**: two recorded failures, both owner actions —
GitHub 2FA is off on the account whose token holds `admin:org` and `delete_repo` (F13), and the Supabase project
allows `0.0.0.0/0` (F14). Eight OPEN items are measurements rather than decisions: the provider-bound half of
break-glass (Turnkey's revoke/rewrap rate), container image contents, managed-Postgres restore, deployed-subnet
egress, the three provider MFA statuses, the 500-copier cascade end to end, the copy-farm cadence limitation, and
the missing payment-webhook surfaces. All are carried in the gate document with the exact work each needs.

**`[UNVERIFIED]`.** No real funds: per `docs/AGENTS-BUILD.md`, money waits for P13 **and** P14, and P14's gate is
NO-GO until the owner acts. `PGM_TELEGRAM_BOT_TOKEN` is still unset on `polygm-api`, the BotFather Mini App URL is
still a manual step, and no real-phone acceptance run has happened.

---

## P13 — Test Strategy & Implementation · 2026-09-21

**Built.** `docs/P13-testing.md` (D1–D8, ~330 lines) plus the machinery it documents:
`tools/p13-gate-check.py` (the phase gate, 8 sections, 12 canaries), `tools/p13-money-matrix.py` (D2: **43 rows**
— OL-1…13, AMB-1…8, RG-1…5, FEE-1…5, NEG-1…3, WAL-1…5, AUTH-1…4 — each mapped to the test that proves it, with
`--check` refusing a row whose test does not exist and `--run` refusing to call a run green unless every mapped
test actually executed), `tools/p13-chaos-suite.py` (D7: the ten drills, each now printing its **expected
outcome above its observations** from an `EXPECT` table), `tools/p13-load.py` (D5: soak, books, api, storm,
fanout, budget — `--quick` for CI, the kit's sizes for nightly), `tools/p13-recovery-loop.py` (the headline loop).
Frontend: `web/src/num/p13-property.test.tsx` (number-layer properties over seeded inputs, 2,000 rendered money
values), `web/src/screens/p13-a11y.test.tsx` (the axe rules written out as a function over the rendered tree),
`web/playwright.config.ts` with a **telegram-webview** project and `web/e2e/{buy-flow,wallet-ceremony,telegram-webview}.spec.ts`.
CI: `.github/workflows/nightly.yml` (drills + full-size load + Playwright + the gate over the night's records),
`.github/workflows/release.yml` (a tag cannot be cut without them), and the PR path gained the money matrix and
the phase gate plus a `web` job. `Makefile` gained `p13`, `p13-read`, `p13-matrix`, `p13-chaos`,
`p13-chaos-live`, `p13-recovery`, `p13-load-quick`, `p13-load`, `p13-soak`; `make check` now runs `p13-read`.

**Verified.** `python3 tools/p13-gate-check.py` → **25 passed, 0 failed** (every section canaried, all ten
recorded chaos drills counted); `pytest` **1332 passed in 120.4 s**; `vitest` **62 files / 560 passed**;
`tsc --noEmit` clean; `p12-gate-check` **41/0** (with `node_modules` restored); `check-openapi` 659/0; chaos suite
**9 of 9 PASS** here plus the live drill-2 artifact; money matrix **43 of 43 rows green, 62 of 62 mapped tests ran
in 15 s**. D5 at the kit's sizes — including the clause that cannot be argued with: **360,000 fills at 200/s in
1,800.0 s of wall clock, zero duplicate deliveries, consumer skew flat (−204 → −198 ms), 2.1 MB of second-half RSS
growth** (`P13-soak-1800s.{txt,json}`). The rest:
2,000 books — **11,955,200 deltas at 99,625/s**, RSS +7.8 MB; **500 concurrent users** p95 1,997 / p99 2,138 ms
with 0 errors; **10,000 subscribers** drained in 54.7 s (SLO 300 s); **1,000 clients** through a SIGKILL, back in
6.09 s with 0 slow failures while the server was down; **100 aggressive users** demanding 30,000 calls,
184 served inside `{data.trades 60, clob.book 62, gamma.markets 62}` per 10 s. The headline loop:
**100/100 kills, zero duplicate orders, zero lost positions** (`ccfa0e1`).

**Three bugs, one of them money.** `Reconciler.sync_fills` booked a venue fill priced **worse than the order's
own limit** — a BUY above our limit, silently, at the venue's price. Cost basis moved, the notification quoted a
number the user never agreed to, and nothing said so. Now refused with the ledger untouched and an
`ambiguous_settlement` case naming both prices, while price *improvement* still books at the venue's better
price (asserted separately). Proven by stashing the fix and watching the new test fail. The other two were in
P13's own tooling and both were found by noticing a runtime instead of a verdict: the money matrix passed pytest
node ids without the `tests/` prefix — pytest collected **nothing**, the failure-set stayed empty, and the matrix
reported "43 of 43 rows green in 0.3 s" — and the first fix then read any node-id-shaped line as a failure, so the
warnings summary turned a passing test red. `summarise()` now demands returncode 0, a parseable summary, counts
that add up to exactly the mapped tests, and zero skips, and the gate **replays both false greens as canaries**.

**Harness honesty.** Two of the three load findings were the harness's own: the soak keyed its delivery queue on
`(rule_id, dedupe_key)` while the product keys a signal on `UNIQUE (rule_id, dedupe_key, fired_bucket)`, so a
legitimate re-fire in a later window reused an idempotency key and the harness counted 6,000 duplicates of its own
making; and the storm's 1-second health probe timed out against a load that the API was serving at p95 2.0 s, so
it reported a restart that had already happened as "the API did not come back". Both are written into
`docs/P13-testing.md` with the product's version of events, along with the pacing rule that an unpaced 30-minute
soak finishes in 45 s and must not report 1,800.

**`[UNVERIFIED]`.** Playwright's chromium downloads here and cannot launch (host libraries, uid 1000, no root),
so the three browser specs run in the nightly `e2e` job and the gate reports "deferred" rather than a pass. There
is no Redis and no Postgres in this deployment: drills 4 and 5 test the equivalent failure against SQLite's lock
and the in-process cache, stated in the drills themselves. The kit's "nightly green for 3 consecutive days"
release clause is a property of a CI account with history, so the release workflow gates on one full run of the
same evidence instead. And no real funds: the $50 canary and the user phase still wait for P14 and owner action.

---

## P03 — Design system · 2026-09-17

**Built.** `docs/P03-design-system.md` (D0–D8, 1,283 lines): D0 records where the prompt's premises were
stale against measurement; D1 foundations (4px spacing 12 steps + 3 named off-grid, 4 elevation levels where
level 1 is a hairline not a shadow, density 22/28/36 with `min_touch_target` 44 never scaled, 7 breakpoints
with xl=1280 as the full terminal, motion quoted from the vendored skill, layers 1000–1500); D2 31 primitives
+ 13 domain components over an 11-state contract, each with density, do/don't and an accessibility clause;
D3 22 screens with route + endpoint per field; D4a four order-book states; D4b the colour-collision finding
that overturns part of P02; D5 performance at 14.7–33.3 fills/sec; D6 a11y/i18n with WCAG 2.2 AA as the
stated normative target; D7 deliverables; **D8 records what the verification found in itself**. New tooling:
`tools/build-foundations.py` (owns D1, `--check` refuses hand-edited output), `tools/build-tokens.mjs`,
`tools/build-tailwind-preset.mjs` (64 colours/15 spacing/7 screens/7 zIndex, refuses on JSON↔CSS
disagreement), `tools/build-storybook-list.py` (**1,062** derived stories/66 roots), `tools/build-contrast-table.py
--foundations` (generates and verifies D1's numbers), `tools/component-colour-audit.py` (per-row pairwise
ΔE/ΔL with `CONTROL@` canaries), `tools/p03-gate-check.py` (**62 checks/8 groups**),
`tools/p03-mutation-test.py`, `tools/repin-p02-digest.py` (classifies drift as added vs modified and refuses
to re-pin audited rule changes without the gate re-run), `brand/specimen.html`, `web/DESIGN.md`.

**Verified.** `python3 tools/p03-gate-check.py` → **62 passed, 0 failed, 0 skipped**;
`python3 tools/p03-mutation-test.py` → **20/20 mutations caught** (19→20 took four rounds, each round's
failure documented in D8). Regression gates still hold on the changed tree: `p01-gate-check.py` 33/33,
`p02-gate-check.py` 69/69 (G10 asserts the strengthened `never-same-row` rule), `colour-audit.py` 0
failures, `component-colour-audit.py --gate` exit 0. Every generator is idempotent and every cross-check
agrees: `build-foundations --check`, `build-tokens --check`, `build-tailwind-preset --check`,
`build-storybook-list --check`, `build-contrast-table --foundations --check`, `rename-wordmark --check`.
Delegated checks (G1.6/G1.7/G3.8b) run the owning tool rather than re-deriving its arithmetic, which is the
fix for P02's two-conflicting-numbers bug. Marker exemptions (`P03: anti-example`, `P03: nocode`) were tested
in five states each so they cannot widen — a `nocode` marker in front of a code block is itself a failure.

**`[UNVERIFIED]`.** (1) **Nothing was rendered.** No rasteriser or headless browser exists here, so no claim
about how any of this *looks* — row heights, chip legibility at 11px, and the specimen's font metrics are
computed from font binaries, not pixels. Closing step: P08's browser pass plus `review-animations`.
(2) **Brand rasters still show the previous wordmark** (`brandboard.png` 1536×1024, `app-icon-512.png` and
`avatar-512.png` actually 1254×1254, `og-1200x630.png` actually 1731×909) and cannot be regenerated here;
D3.1 blocks shipping the landing page on the OG image until a true 1200×630 derivative exists.
(3) **The alert-hue proposal is undecided.** `#CC79A7` is the only tested hue clearing ≥3:1 in both themes
(worst ΔE 31.2); applying it changes a `fixed` Brand Lock field, so it waits for the brand owner — before
P08 builds any coloured number. (4) **P02's buy/sell hue pair is unusable in light theme** (ΔL 0.008): the
fallback in D4b is adopted for the terminal, but the palette itself is locked, so this is a documented
consequence rather than a fix. (5) No deployment surface is claimed: `vercel`/`supabase` CLIs are absent from
this workspace.

### P03 addendum — the "non-text boundary" I recommended was already being violated (same day)

Acting on the hue proposal, I checked the claim it rested on ("colour is only used non-text today") against
the audit rows instead of trusting the spec prose. It was false, and so was my **B-now-A-later**
recommendation as written: the tape's side pill and whale badge were specified with a money hue as their
**11px foreground** (4.38–4.43:1), and the YES/NO chip word plus the book ladder's ask price used
`#D55E00` as text (3.55–4.26:1). None of it rendered wrong yet, because none of it is built — but the
*specification* was inaccessible, which is exactly the thing P03 exists to prevent.

Why nothing caught it: `component-colour-audit.py` printed "AA-large" for the 3.0–4.5 band and only failed
below 3.0, while its own error string cited the 4.5:1 body-text floor it was not applying — and nothing in
this UI can claim AA-large (largest text token 13px; WCAG's exemption needs 18.66px bold / 24px).

Fixed at the spec and tool level, with no palette change (so no `fixed` Brand Lock field was touched):
`tokens.rules["hue-never-small-text"]`; D2.2 rows (Yes/No pair, OrderBook ladder) and the chip/badge
compositions now put the word in `text.primary` and the hue on the outline/tint/depth bar; the audit grades
by a per-cell contrast class (text 4.5, nontext and *declared* large 3.0) with the class defaulting to text,
so a cell must opt **out** of the bar rather than opt in; `web/DESIGN.md` §2 carries the rule so P08's
engineer cannot reintroduce it. A sixth canary (`CONTROL@hue as small text on an elevated panel`) was added
and **verified non-vacuous**: restoring the old permissive grader turns it to `PASSED ✗ / canary BROKEN` and
fails the gate, while the current audit reports 0 findings with 6/6 canaries caught.

Two of my own mechanisms were wrong on the way to that fix, both recorded rather than edited out: I tried to
make `build-foundations.py` own the `rules` merge and it **refused** (correctly — `rules` is inside the
P02-certified section set, so the generator must not write there; additions go through
`repin-p02-digest.py`, which classified this as additive and re-pinned to `5f231683ce481b0b`); and before
that, adding `rules` to the builder's emit path without adding it to its *compared* sections list made the
run print "nothing to do" while the rule it supposedly added was absent. A generator's emit list and
comparison list must be one list.

The recolour I had left "to the brand owner" is now **searched and declined on measurement**, not deferred:
`tools/p04-hue-search.py` swept the red family in both lightness directions against contrast (4.5:1 on
`bg.base` AND `bg.elevated`), buy separation, both outcome hues, and `alert.high`. Dark: **zero candidates**.
Light: the first fully clean hex is `#7e1616` — 10.43:1 but a maroon, not the brand red — while the
attractive `#b91c1c` fails separation (8.4 ΔE from `alert.high`, 9.9 from `outcome.no`). A recolour could
only buy small coloured text by replacing the red with a different colour, so B is final.

Adding that missing audit coverage first reported **7 findings, and all 7 were my audit model's fault**: I had
written the forbidden compositions into the rows (a bare warning edge; a gold rule inside a compared pair),
which `rules.no-alert-hue-inside-a-compared-pair` and `whale-flag-is-a-badge-not-a-dot` already ban. Corrected
the model to the legal composition (warning = word + badge; ladder = words only) → 0 findings, 6/6 canaries,
and the ladder's `outcome.no ↔ alert.high` ΔE 2.7 collision is now prevented by rule scope rather than row
scope. Lesson kept: a gap in *coverage* looks identical to a clean result, which is why the canaries are
asserted in both directions.

## P02 — Brand & identity · 2026-09-16

**Built.** `docs/P02-brand.md` (D1–D8): 12 name candidates each carrying a *measured* collision result,
with **Openout** chosen and `oddspace`/`openbet` as fallbacks; positioning sentence + three landing
messages; the mark's construction documented and its size survival measured; a full semantic colour
system in `brand/tokens.json` (+ generated `brand/tokens.css`); a 10-step type scale with numeric
display rules and a fetched webfont budget; an 18-icon set spec with the metaphor named per icon;
13 states of verbatim microcopy; the asset checklist. New tooling: `tools/colour_audit_lib.py` (one
WCAG/ΔE implementation, now shared), `tools/build-contrast-table.py`, `tools/build-tokens.mjs`,
`tools/palette-search.py`, `tools/mark-legibility.py`, `tools/name-collision-probe.py`,
`tools/p02-gate-check.py`; `brand/SIZE-RULES.md`, `brand/palette-search.json`. Vendored `brandkit.py`
state ledger used for real: `approve_palette`, `approve_typography`, `set_visual_axes` → rev 3, with
the state file kept out of the repo per `SKILLS.md`.

**Verified.** `tools/p02-gate-check.py` **69/69 exit 0**; `tools/colour-audit.py` **0 contrast
failures** across 24 dark + 24 light roles; `tools/build-contrast-table.py --check` (doc table ≡
generated); `node tools/build-tokens.mjs --check` (CSS ≡ tokens). Colour claims are computed, not
asserted: best 8-hue dark chart palette reaches worst-case ΔE*ab **11.3**; light tops out at **6**
(13.4) and fails at 7 (9.1), so the themes ship *different-length* arrays. `outcome.no` vs
`alert.critical` measures **ΔE 4.1 (dark) / 3.2 (light)** — hence the hard layout rule, recorded in
`tokens.json.rules`, that an outcome chip and a severity badge never share a row. The mark was
measured analytically (not screenshotted) at 512/128/64/40/24/**16**px: it survives to 24px and the
**gold dot is sub-pixel (Ø 1.44px) at 16px**, which is exactly why the kit's `favicon.svg` exists
(stroke 6u vs 3.5u, dot r 6u vs 4.5u, 19.4px² per chevron at 16px) — so `--pgm-mark-min-size-px: 24`
is now a rule, not folklore. `mark.svg` is byte-identical to the kit's approved asset and no logo
candidates were generated (Brand Lock `fixed`; `logo.md` forbids redesign-by-adjacent-request).
The gate was mutation-tested: 4 of 5 corruptions caught (non-hex token, sub-floor contrast, near-
duplicate chart hue, gutted bullet copy, deleted bullet) — and **one is admitted undetectable**: a
mid-sentence shortening that leaves a valid 120-char quote passes any length floor. `tools/
name-collision-probe.py` re-fetches page titles for every candidate, and the gate re-probes the
font claims live so they cannot rot.

**`[UNVERIFIED]`.** (1) **Trademark clearance for all 12 names** — no USPTO/EUIPO access from this
sandbox; `openout` DNS/CT/GitHub are clean and `openout.app` is an unrelated link-in-bio product, but
registration status is a paid search. (2) **WHOIS registrant** for `openout.*` — "no A record" is not
"unregistered"; the 195-byte `oddspace.com` response shows how thin that signal is. (3)
**`rsvg-convert` is absent**, so the skill's `logo-export` / `logo-inspect` / brandbook-PDF stages
cannot run and the logo slot was deliberately **not** approved rather than faked; real raster previews
of 16/40/512px are unverified (the numbers in this phase are computed geometry, which is stronger for
the question asked and weaker for "what it looks like"). (4) **Telegram Mini App dark-mode chrome
behaviour** at 40px in an actual chat list — needs a device. (5) `Instrument Sans Condensed` may exist
as an unhosted release somewhere; I proved it is *not on Google Fonts* and has no obvious repo, not
that it never existed.

**Corrections made to my own draft during this phase** (recorded, not hidden): `no: #D55E00` was
adopted before I checked it against `alert.critical` (ΔE 4.1 — it collides); the "8 chart colours
per theme, any canvas" premise is false and became 8/6 with per-theme arrays; the light
`brand.primary` `#2e5cff` failed AA for body text (4.05:1) and became `#1c3fe2` with
`brand.primary-text` added; `Instrument Sans Condensed` is not a real Google Fonts release, so
`Archivo Narrow` (11,776 B, fetched) replaced it and the webfont total is now measured (88.4 KB) not
estimated (118.4 KB); "tapemark" was dropped as a name because its racing etymology is false;
and `tools/colour-audit.py` had been auditing its own stale copy of the palette — it now loads
`brand/tokens.json`, which is why it could report "0 failures" while the gate disagreed.


## P01 — Research & product specification · 2026-09-16

**Built.** `docs/P01-product-spec.md` (≈34 KB): wedge selection with a weighted matrix and a
recommended 5th wedge; 6-competitor teardown with provenance discipline; 20-row feature spec where
every P0 row names a live-verified data source; data model (19 required tables + `order_events`,
`usdc_flows`, ClickHouse/Postgres split, fail-closed reconciliation, negRisk PnL algorithm);
10 metrics with formulas and week-4/week-12 gates; risk-adjusted revenue model, 3 scenarios × 3
horizons, every figure recomputable. Supporting: `tools/datasource-probe.py` (26 live endpoint
assertions), `tools/p01-gate-check.py` (33 gate assertions incl. D6 arithmetic and spec↔probe
consistency), `docs/verification/P01-verification-log.md` + `P01-probe.json` / `-output.txt`.
Also corrected upstream facts into `docs/00-SHARED-CONTEXT.md` (appended, dated, nothing deleted)
and added `docs/AGENTS-BUILD.md` (protocol in force) + repo `README.md`.

**Verified.** `python3 tools/datasource-probe.py` → **26/26 exit 0**; `python3 tools/p01-gate-check.py`
→ **33/33 exit 0**. Gate mutation-tested with 9 deliberate spec corruptions (bad ARR, deleted ⚠
flags, removed table, contradicting the probe, unobserved fee type, stripped unknown marker): **9/9
caught, control passes**. Live API evidence backs every claim: Gamma caps at **100 rows** (so
`limit=` is not a page budget — pagination required; my 300-row sweep returned $59.19M, reproducing
the kit's $59.1M); `data-api /trades` is served from **Cloudflare cache** (`cf-cache-status: HIT`,
byte-identical 8 s apart) ⇒ the tape must come from the WebSocket; up/down 5-min markets are
**≤0.14% of top-100 market volume** and **0 of 300** volume-ordered events ⇒ wedge 1 rejected on
measurement, not taste; `lb-api /pnl` **404**, `/rank` **400** even with params supplied; trade rows
already carry `name/pseudonym/bio/profileImage` (no profile endpoint exists — `/profile` 404s);
`REDEEM` rows have `price==0` on **366/366** with payout in `usdcSize` on **280/366** at 1:1
$-per-share, which fixes the PnL algorithm; `feeType` is readable per market (6 values observed) so
fees need no category guesswork; CLOB unauthenticated L2 → **401**.

**`[UNVERIFIED]`.** 10 items, each with a closing step, in the log's last section. The load-bearing
three: (1) the **WebSocket message shapes were never observed** — host resolves but I did not
subscribe, and the whole wedge's latency claim rests on it; (2) **negRisk redemption exactness**
needs the adapter contract plus a test against one real resolved event; (3) **all six competitors'
onboarding step counts** — 4/6 sites return empty bodies to curl and one is behind a Vercel
checkpoint, so the D2 "steps to first trade" column is honestly empty and needs a human with a
wallet. Also open: Polygon RPC/pUSD-CTF addresses, deep `/activity` history, Kalshi API, builder
profile queries, Telegram Stars net take, infra prices, and the $2k-vs-$10k whale threshold
(0.2–1.2% of fills are ≥$1k on a 500-row sample, which is too small to set it).

## P04 — backend architecture and scaffold (this session)

**Built.** `packages/polygm_core` (dependency-free: `money/cents`, `risk/gate`, `risk/idempotency`,
`ledger/ledger`, `config/flags`, `executor/executor`), `services/api/app.py`, `services/executor-mock/`
(`mock_clob.py` + `transport.py`), five Postgres migrations + the generated SQLite subset + `DROPPED.json`,
`db/seed.sql` (generated, time-relative), `contracts/openapi.yaml` (9 paths), `tools/` (`check-openapi.py`,
`envelope-demo.py`, `p04-gate-check.py`, `p04-mutation-test.py`, `run-sql.py`, `build-sqlite-migrations.py`,
`lint-rules.py`, `doctor.py`), `tests/` (156 tests, stdlib unittest), `docker-compose.yml` (6 services + a
profile-guarded `seed`), `Makefile` (30 targets), `.env.example` (27 documented vars), two non-root
Dockerfiles, and `.github/workflows/ci.yml`. `docs/P04-backend-architecture.md` carries D1–D9.

**Verified, in this environment only:** `make test` 156 OK · `check-openapi` 82/82 with 13/13 canaries ·
`p04-gate-check` 55/55 · `envelope-demo` 12 steps over real TCP sockets (the money path: 10 × $0.50 =
`notionalMicro: 5000000`) · `p04-mutation-test` **24/24 mutants caught, 0 uncaught** · `lint-rules` clean with
8/8 rules firing · P01/P02/P03 gates and the colour gate still green · `docker`/`psql`/`sqlite3 CLI` absent, so
compose, the Dockerfiles, CI and the Postgres dialect are `[UNVERIFIED]` and labelled as such in the doc.

**Three findings worth the ink**, because each one was a *checker* being wrong rather than the product:
1. `db/seed.sql` froze wall-clock at generation time, so a freshly seeded dev DB read 18 min stale and the gate
   answered `STALE_QUOTE` to every order — a correct program shipping an unusable demo. Now `{{NOW_MS}}` is
   expanded per engine by `tools/run-sql.py`, and the gate greps the seed for absolute 13-digit stamps.
2. The secret scan used `git grep`, i.e. it only ever looked at *committed* files — which meant it was scanning
   P01–P03 and reporting "clean" about an entire uncommitted phase. It now walks
   `git ls-files --cached --others --exclude-standard`, verification logs included, with the lint canary
   exempted by exact line through `tools/secret-scan-allowlist.json` and a companion check that fails if an
   exemption stops matching a real line (so the allowlist cannot become a hiding place).
3. `yaml.safe_load` forgives a duplicate mapping key. The compose file had one (`restart:` twice under `seed`)
   and every check that parsed it was satisfied while `docker compose up` would have aborted. `yaml_strict`
   now rejects duplicates in the gate, with a mutant (`compose-duplicate-key`) that plants one.

The mutation harness is the part of this phase I would defend hardest: it found **three** rules the gate only
asserted in prose (`tick-raw-string`, `price-is-a-number`, `no-append-only-trigger`), and each one became a
test or an audit check rather than a paragraph. Two of the three were genuine coverage gaps in code I had
already called finished.

## P05 — data ingestion, signals, alerts · 2026-09-18 (outage evidence recorded 2026-09-17T22:49Z)

**Built.** `services/ingest/`: `net.py` (named token buckets, one per source; a request for an unregistered
source is refused rather than unbounded), `wsclient.py` (stdlib socket: TLS, frames, fragmentation, ping/pong),
`books.py` (snapshot + delta, one-sided books have no mid, gap detection from `price_change`'s declared
best bid/ask, 200-subscription sharding, 2,000 books ≈ 16.5 MB), `tape.py`, `freshness.py`
(`down > silent > stale > lagging > ok`, heartbeats never advance the event clock, only `down`/`silent` page),
`universe.py` (keyset backfill with a resumable cursor, discovery with an overlap counter, metadata versioning,
prune/wake with 10× hysteresis), `normalise.py` (per-source units: seconds on `/trades`, milliseconds on the
socket, `outcomeIndex: 999` is "not applicable" and never an index), `main.py` (the loop — the first process this
phase ever drove end to end). `packages/polygm_core/signals/`: `engine.py` (8 kinds, user-composable validated
rules, cooldown + dedupe + suppression counts), `fanout.py` (delivery plan: age before priority, per-user
fairness cap, visibility timeout that costs an attempt, dead-letter, deterministic jitter);
`classify/labels.py` (6 labels, each with a named false-positive control). `db/migrations/0006_ingest.sql` +
`0007_ingest_market_stats.sql`, the regenerated portable subset, and `db/clickhouse/tape.sql` — the alternative
that was measured, priced and **not** adopted. `tools/`: `p05-chaos-test.py`, `p05-capture-fixtures.py` (with
`--check` drift mode), `p05-seed-rules.py`, `p05-gate-check.py` (14 executed checks), `p05-mutation-test.py`.

**Verified.** 293 tests OK offline (47 ingest-module, 45 `main.py` against recorded payloads and a fake
transport, 22 signals, 16 fanout, plus P01–P04 suites still green). `p05-gate-check.py --fast` 14/14,
`tools/lint-rules.py` clean, `check-openapi` 88/0 after the new read was documented,
`build-sqlite-migrations.py --check` in sync. The gate's headline is live and was executed: 8 subjects, **300 s**
socket outage, `ALL 7 CHECKS PASSED` (`docs/verification/P05-chaos-output.txt`) — stale indicator flipped in
1.6 s and held 261/262 samples; **17 alerts, zero duplicates**, with the two clocks genuinely colliding (12 live
WS fills against 8,129 durable merges); 2 of 2 venue fills ≥ $1,000 in the window; book matched the venue within
one tick after reconnect; tape +4,348 rows from REST while the socket was dead. `make check` now includes `p05`;
`make gate-p05` re-runs the outage and then the gate that reads its artifact.

**Corrections this phase forced** (all recorded in the spec repo's §15, nothing deleted there): the P01
cache-busting conclusion is void — `/trades` staleness is origin-side, and the bounded `start`/`end` range is the
only fresh path (0–1 s measured against 236–277 s); `markets` has no `closed` column, resolution is
`tokens.is_winner IS NOT NULL`; `data-api` returns some fill prices as IEEE float noise (`0.1699999983` for 0.17
in **49 of 200** recorded rows) which the strict parser refused — a quarter of the tape was silently missing, and
volume/whale/alert numbers are computed from what we keep; `POST /books` is 400 for every payload shape tried; a
WS trade frame carries no transaction hash, so REST is the record and the socket is the latency layer;
`feeType` is an open taxonomy, so unknown means *charged and flagged*; and `build-sqlite-migrations.py` dropped
every `ALTER … ADD COLUMN`, which is how dev/CI would have run a schema missing columns production has — it
refuses now, and the mutation harness proves the refusal is load-bearing.

**[UNVERIFIED] / owed by later phases.** No provider transport: `alert_deliveries` rows are scheduled by
`fanout.plan()` and nothing hands them to APNs/Telegram/Firebase (P10). `gap detections = 0` in the live run —
the `price_change` top-of-book detector has fired in a synthetic test and not yet on real traffic, so "we detect
lost deltas" is asserted by construction and not yet by observation. `market_stats` activity numbers are stored
and used for policy but not reconciled against the venue's own displayed figures. Nothing was pushed to
`polygm-platform` (it has no remote); the spec repo push carries §15.

## P06 — the trading plane · 2026-09-18

**Built.** `db/migrations/0008_trading_plane.sql` (+ generated sqlite twin): 29 tables, including
`builder_attribution_terms`. In `polygm_core`: `venue/clob_v2.py` (pinned `py-clob-client-v2==1.1.0` shapes,
15-order batch cap refused rather than truncated, `int` fees rounded up, cancel budget 250/10 s, the 10-step
preflight), `wallets/lifecycle.py` (6 states, 13 legal transitions of 36, policy-drift refusal, typed-amount
guard, deposit floor = 10× the measured on-chain bill, export refused while the wallet owes anyone anything),
`risk/{limits,gate}.py` (78 deny codes with status/retry/severity, breaker that fails closed and half-opens,
loss halt that needs a named actor), `copy/engine.py`, `automation/engine.py`, `reconcile/reconciler.py`
(eight cases, each with a handler and an executed test), `revenue/attribution.py`. `services/executor` talks
HTTP to `services/executor-mock` as a **separate process**, so `SIGKILL` is a real event rather than a mock
flag. `docs/P06-trading-plane.md` is the phase's decisions with their numbers.

**Fixed in the product, not in the harness.** Five, each found by a probe that tried to make the answer wrong:
the wallet state machine was a diagram — `Store.set_wallet_state` never called `assert_transition`, so
`suspended → provisioned` was accepted; `_norm_decimal` referenced a name it never imported and a broad
`except` turned every typed amount into the same sentinel, so the withdrawal guard passed by agreeing with
itself; the deposit bill divided wei→µUSD twice, making the minimum economic deposit $0.0000004; the
unlimited-allowance sentinel was `2**256-1`, which does not fit a `BIGINT` column in either dialect, so the
"unlimited" row threw at INSERT and a caller that swallowed it would have stored *zero* approval in the one
column where the direction of the mistake is the whole point; and the copy and automation engines had no view
of the kill switch at all — a source fill during a halt queued an intent whose Activity line read "copied" for
two seconds before the executor rejected it, and a live automation tick placed orders with `enabled=1`. Both
now refuse at the moment of decision (`disabled:…`, `RISK_HALT`), and a dry run still evaluates so an operator
can read what the rules would have done.

**Measured (all re-runnable).** `make p06` → 31/31. `python3 -m unittest discover -s tests` → 500 OK, 22.1 s.
`make chaos-p06` → 7/7 against a real `SIGKILL` after signing, inside the POST, and after a fill was booked;
the verdict is read from the **venue's** counters (`orders created: 1`, POST count unchanged when the order was
adopted) and one `cash_ledger` row per venue trade. `make drill-p06` → 5/5 components refuse the switch within
**460 ms** against `lm.KILL_BUDGET_MS = 1000` (api 3 ms over HTTP, executor and worker 460 ms, copy and
automation ~1 ms), with **0** venue POSTs after the propagation window and two in-flight orders legitimately
finished inside it. `make gate-p06-mutate` → **26/26 mutants killed**.

**What the mutation harness taught, in order.** It caught its own first draft: 10 of 29 anchors did not exist,
and the pre-flight that requires each anchor to be present turned a silent skip into a hard error. Then three
mutants survived, and all three were the harness's fault rather than the product's — the fee check asserted
`floor` on an example with no remainder (so "rounds up" was prose), the breaker check tested only the
consecutive-failure trip and not the 50 % error-rate trip, and the recovery check ran `tick(reconcile=False)`,
which cannot see the reconcile-before-requeue ordering the phase depends on. Each got a real assertion; two
survivors were *equivalent mutants* (a `min(share, observed)` line upstream made the pay-the-estimate bug
inert; the `enable()` guard, not the tick guard, is the enforcement the gate owns) and were retargeted rather
than deleted.

**Gate weakness recorded for P07.** `c_schema_parity_and_append_only` is a *subset* test: it asks that every
source CHECK appear in the dev twin, and so it happily passed while the twin lagged my own migration edits and
was missing the P06 triggers and the widened `automation_rules.kind` CHECK. `build-sqlite-migrations.py
--write` has been re-run and the twin now matches; the check should compare generator output byte-for-byte, the
way `sql-sqlite-check` does for drops.

**[UNVERIFIED] / owed by later phases.** Provider pricing (`turnkey`, `privy`, `dynamic`, `self_hosted`) is
carried as `[UNVERIFIED]` in the doc and in `PROVIDERS[i].verified = False`; the venue's fee and `builder`
semantics are pinned against the mock and the pinned client, and a real venue change is a P13 finding; the
on-chain `OrderFilled` measurement has a table, a job and an idempotent write but no live RPC in CI, so the
first real day may legitimately read `unreconciled`. Nothing in this phase has touched real money: the signer is
an HMAC and the venue is ours — per `docs/AGENTS-BUILD.md`, funds wait for P13 and P14 to be green.

## P09 — the markets surfaces · 2026-09-19

**Built.** The four screens a prediction-market terminal is for, plus the arithmetic under them:
`web/src/lib/{depth,ladders,upstream,anchor}.ts` (integer micro-units; directional aggregation, bids floor /
asks ceil, so an aggregated level is never better than what was there; one depth scale across both sides; the
event probability-sum invariant against its tick-implied tolerance; strip→decode→strip sanitising of
attacker-written resolution text with an http(s)-only link check; the ladder's scroll anchor across a
re-ladder), and `MarketCard` · `MarketsClient` · `OrderBook` · `PriceChart` · `EventTable` · `MarketRail` ·
`MarketView` · `EventView` with the two SSR detail routes. Backend half in `d54d5e7` (migration 0010, discovery
facets/hidden tail/event summaries, the book's `aggregate` + `oneSided`, `/history`, `/holders`,
`/v1/events/{id}`); web + doc + gate in `0ad8830`. `docs/P09-frontend-markets.md` carries the D1–D7 controls,
the per-screen state table and ten `[UNVERIFIED]` launch items; `tools/p09-gate-check.py` is 7 checks whose c3/c4
*call* the P08 gate's scanners rather than writing a second opinion.

**Verified.** `make test` 674 OK; `check-openapi` 200/200; `make lint` 92 files 0 findings; web `npm run test`
**145 passed / 20 files**; `tsc --noEmit` clean; i18n 345 keys, 0 missing; `make p08` **15/15** with P09 in the
tree (worst route `/markets` **198.0 KB** of a 200 KB budget, route-level splitting proven);
`make p09` **7/7** with **7/7 canaries** (`docs/verification/P09-gate.txt`). Ladder fixture from P01's own
shape: 94 asks 0.001→0.094, no bids, notional $21,899,999.999976, banner states the count and the no-bid
sentence. Five defects the phase's own tools caught, all recorded in §2.8: `kind="price"` takes **tick units**
and was fed micro (`0.001` rendered as `1.000` — a plausible price, ten times the size); sizes out by 10^6;
an already-cents spread multiplied by 100; `SuccessBody` unioning every documented response, so `book.bids` was
a type error on a 200; and a contract that never declared `cumShares` or six market-detail fields the API
already served. The P09 gate itself shipped four bugs its canaries caught (a ledger reader truncating at the
`}` inside `{market_id}`, a `[UNVERIFIED]` pairing check that compared a slug against the document containing
it, comment-blind scans, and a probe shelling out to a runner the repo does not have).

**`[UNVERIFIED]`.** No browser here, so Lighthouse/LCP on the three routes, a 10 Hz frame trace of a 200-level
ladder, virtualisation's necessity, touch behaviour, screen-reader output, `prefers-reduced-motion` and the
Storybook states are launch items 1–9 in `docs/P09-frontend-markets.md` §4, each with a role owner. The recorded
build is `next build --webpack`: Turbopack's builder is OOM-killed in this sandbox (~700 MB free), so the
bundler is part of the measurement. The `.git` rewind recurred a fourth time (tree current, `.git` at a P05-era
tip, `origin` missing); recovery was `git remote add` + `fetch` + `git reset --mixed`, never `--hard`.

## P08 — the frontend shell · 2026-09-19

**Built.** The whole page layer: Next 16 App Router, 19 routes (`/`, `/markets`, five `(auth)` screens, seven
`(app)` screens, `/tma`, and `app/api/[...path]` as the authenticated proxy), the money path
`src/money/cents.ts`, the number layer `src/num/{Number,StaleIndicator,flash}`, the session machine with its
refresh single-flight, the Telegram bridge, `src/ui` primitives and `WidgetBoundary` per widget,
`src/api/routes.ts` as the route ledger, and `tools/p08-gate-check.py` — 15 checks that read the served response
rather than the source.

**Verified.** `docs/P08-frontend-shell.md` §5 records it: `make p08` 15/15 (`--self-test` 11/11, recorded in
`docs/verification/P08-gate.txt` and `P08-bundle.txt`), 95 web tests in 15 files, first-load 187.6 KB on `/`
against a 200 KB budget (worst route 190.3 KB), 223 dictionary keys with 181 used, `check-openapi` 177/177,
`ci-log-scan --built web/.next` 403 built files 0 findings. The bug that mattered was a two-ended contract:
`pgm_at` written as a bare expiry and read as `"<expiry>:<token>"`, so every request after a rotation presented
a *spent* single-use token and upstream revoked the whole family — writer and reader now live in one file with a
test that reads both ends, and the property is exercised against `next start` + uvicorn, not a `TestClient`.

**`[UNVERIFIED]`.** No browser: `tma-real-device`, Lighthouse, the 60 fps rail-drag trace and Storybook are
launch items with numbered owners in the P08 doc's §4, and `measure-first-load.mjs` reports *bytes fetched per
document* — a payload claim, not a rendering one.

## P10 — the terminal · 2026-09-19

**Built.** `docs/P10-frontend-terminal.md` (the phase, decision by decision), the terminal's data layer and its
three-column frame with the live tape (D1, D2) in `web/src/terminal/`, and D5's Wallet Radar end to end —
`packages/polygm_core/radar/rankings.py`, `POST /v1/radar/runs`, `GET /v1/radar/runs/{job_id}`, its contract,
and `tools/p10-gate-check.py`'s new `c10`.

**Verified.** `make p10` **10/10 in 2.5 s** with `--self-test` **8/8 canaries fired**
(`docs/verification/P10-gate.txt`); `python3 -m unittest discover -s tests` **771 tests OK** (25 radar, 42
terminal API); `cd web && npx vitest run` **166 tests** (21 for the tape) and `tsc` clean; `check-openapi`
**287 passed, 0 failed** over a 36-path contract. Commits `4a7b299` (D1/D2) and `bd3169e` (D5) on
`origin/main`, on top of `25122ae`.

**Two findings that were bugs and not documentation.** (1) The radar iterated `_market_rows()` as a list — it
returns a dict keyed by **condition** id — so every scan was a 500; the radar's own tests found it before any
screen did. (2) The P10 POSTs accepted a mutation with **no `Idempotency-Key`** while the contract has required
one since P04 and documented a 400 without it: `_idem_shape` treated `None` as "nothing to check", so the header
was decorative on exactly the routes where a duplicate costs money. It now answers `IDEM_KEY_REQUIRED`, the four
tables and four contract operations document the 400, and the read halves of the two mixed paths have their own
tables — an invariant `check-openapi` now checks per verb.

**Then D3 and D4.** The trader dossier (`/trader/[anon]`) and the whale tracker (`/whales`), with their logic in
`src/terminal/{dossier,whales}.ts` and 70 terminal tests in total. Two decisions worth recording: a classification
label's rule and disclaimer are rendered as **text** rather than only as a tooltip (a disclosure you have to hover
for is not a disclosure), and the dossier's header states that a pseudonym is deliberately not resolved to an
on-chain address instead of offering an explorer link the API's own gate check forbids. The i18n check also caught
that the tape had been rendering its own keys — computed `t()` keys are invisible to the build check, so the panel
now holds a literal copy table and the dictionary gained the 129 entries the terminal asks for.

**Then the rest of the screens.** D6's portfolio (with a curve drawn by the same builder the dossier uses, after
the gate refused a `.toFixed` in the new path code), D7's copy trading — which needed a new endpoint,
`GET /v1/copy/sources`, because the phase's own acceptance sentence asks the discovery list to default to a
risk-adjusted sort that nothing could produce — and D5's Wallet Radar, whose hook existed with no screen calling
it. D8 and D9 stay in P11: there is no automation or alert endpoint in the P10 contract, and a rule builder over
an API that does not exist is a mockup, not a feature.

**Then the idempotency record half, and the way it broke.** The four P10 mutations validated the
`Idempotency-Key` and then ignored it, so a client whose request timed out and retried created a second copy
config, spent a second radar scan, or saved a second view — the failure the header exists to prevent, and the
one a user cannot see, because both answers look successful. They now run under `_idem_run`: a conflicting body
is `409 IDEM_CONFLICT`, a key still running is `409 IDEM_IN_PROGRESS` (through `Record.busy`, not
`state == 'in_progress'`), and a replay returns the **stored** body verbatim. A refusal abandons the key instead
of storing it — a user who fixes the typo must be able to retry — and an exception abandons it too, because an
`in_progress` row left behind answers every later attempt `IDEM_IN_PROGRESS` forever.

Splitting the four handlers to make room for the wrapper is where it went wrong, and the shape of the failure is
the lesson: each `_*_work` function was inserted **above** its thin handler, so `@app.post` decorated the work
function and FastAPI read `(rid, uid, body)` as required **query** parameters. Every call 422'd naming fields no
client had ever heard of, four routes silently disappeared, and a decorator strayed onto a helper. The check that
would have caught it in one second — ask the app which function each route points at, before running any test —
is now the first thing done after touching a route. A second finding came out of the same change: the radar's
budget test creates its own account, and the new `idempotency_keys` row hits an FK to `users`, so a made-up uid
that spent budget fine now 500'd. The test creates the account; a real user always has a row.

**Then the phase's loose ends, which were not loose ends.**
Closing P10 meant re-running the gates that P09 and P10 built on top of, and both of them had been describing a
tree that had moved.

*Re-running P08 failed it* — 12/15, four regressions from two phases of work on top of it. `schema.gen.ts` was
stale against the contract (regenerated); the terminal's stylesheet and layout shipped a `44px`, a `4.5rem` and
two `6px` literals past the token scan (now `--pgm-min-touch-target`, `--pgm-space-16` and `var(--pgm-space-2)`,
because the design system owns dimensions and 6px was on nobody's grid); the live tape rendered a `kind="price"`
with a freshness and **no age**, so a price could say "stale" without saying how old (the row now carries
`ageMs`, the same quantity the header shows); and the P08 bundle artefact described a build that no longer
existed. Its own parser also crashed before running a single check: `brace_at` read a `//` comment containing an
apostrophe as an unterminated string, which is exactly the class of bug the comment in that function already
documents once. All four fixed, P08 re-recorded at **15/15**, P09 **7/7**, P10 **11/11**.

*And the P08 first-load number had genuinely gone over budget*: `/markets` measured **204.4 KB against a 200 KB
budget**, because `en.ts` is one object that every route that calls `t()` imports — the terminal's 319 keys were
riding along in a page that renders no terminal surface. The dictionary is now split by route family
(`en.ts` + `en.terminal.ts`, registered by `src/i18n/terminal.ts`), and `scripts/i18n-check.mjs` fails the build
when a `terminal.*` key is asked for by a file that does not import the family — a missing key's failure mode
reached from the other direction. Measured after: `/markets` 199.1 KB, everything else 188.5–191.6 KB, splitting
still proven.

*The measurement tool was measuring a stranger.* The first re-record said "money layer absent" on every route,
which the build contradicted: a `next start` from an earlier run still held port 3111, `waitReady()` asked the
port and believed the answer, and the run described the *previous* build. `measure-first-load.mjs` now probes the
port before spawning and refuses to measure a server it did not start. The gate's c15 also learned to name the
test file that failed instead of only counting it — which is how a genuinely flaky `CopyView.test.tsx` (the copy
screen's monitor assertion raced the monitor fetch; ~1 run in 5) was found and fixed rather than dismissed as
load.

**Two claims changed status in this pass.** The 60fps line is no longer only prose: `npm run measure:tape` drives
the tape's real functions at 200 fills/second and records **2.134 ms of JavaScript per second of load** against a
16.7 ms frame (worst release 0.884 ms), `c11` fails an artefact that lacks the numbers *or* the caveat that
paint/layout/compositing are unmeasured, and `src/terminal/perf.test.ts` asserts the same budgets in the suite.
And the pixel half is still `[UNVERIFIED]`: a browser cannot run here — Playwright's Chromium download fails its
host-requirements check — so the honest statement is "the JS half is measured, the pixels are not", in the
artefact, in the gate, and in `docs/P10-frontend-terminal.md` §2.12.

**Verified at the idempotency commit.** `python3 -m unittest discover -s tests` **783 tests OK**; `make p10`
**10/10** re-recorded to `docs/verification/P10-gate.txt`; `check-openapi` **293 passed, 0 failed** over a
37-path contract.

**`[UNVERIFIED]`.** No browser has been in the loop, so the 60 fps-under-live-load requirement, the resize
persistence and mobile tab parity are claims about code, not measurements; D8/D9 wait on P11's rule engine. No
real funds: per `docs/AGENTS-BUILD.md`, money waits for P13 and P14.

## P07 — the security plane · 2026-09-18

**Built.** `docs/P07-security.md` (D1–D10: STRIDE ranked by cost, the two-address key envelope with a revocation
that has been run, the factors and windows of authentication, one authorisation table with five levels, input
integrity, secrets, network posture, abuse, incident response and the compliance sentences), the verification
plane in `packages/polygm_core/security/*`, and `tools/p07-gate-check.py` plus its mutation suite.

**Verified.** Recorded in the doc's head: `make p07` **32/32 in 45 s** (`docs/verification/P07-gate.txt`);
`make drill-p07` pass — 10,000 wrapped keys revoked in 42 ms across 20 batch statements, 0 of 800 sessions
surviving the global revocation (`P07-key-drill.txt`); `make gate-p07-mutate` **28 planted weaknesses, 28
killed, 0 survived** (`P07-mutation.txt`); `python3 -m unittest discover -s tests` **641 tests OK**, 141 of them
P07; `make lint` 8/8 rules clean with 8/8 canaries firing; `check-openapi` 176/176; `ci-log-scan --sources` 133
files 0 findings. The bug that mattered: `_principal` derived the authorisation operation from the request
*URL*, so every served route with an identifier in its path answered 500 `AUTHZ_UNDECLARED` to an entitled
caller — invisible to the unit suite and found by a served response.

**`[UNVERIFIED]`.** Provider custody pricing is carried as unverified in the doc and in
`PROVIDERS[i].verified = False`; the geofence/age-gate sentences are counsel items; the first real-venue day is
a P13 finding. No real funds: per `docs/AGENTS-BUILD.md`, money waits for P13 and P14.

---

## P12 · Telegram: the bot, the channel, and the Mini App on its own URL

The phase where the product gained a second front door — a chat that can place an order, and a public channel that
anyone can watch — and where the expensive bug was found in the seam *between* phases rather than inside one.

**Shipped.** D1 (webhooks, `update_id` as the process boundary, per-chat and global rate buckets, priority so a paying
user's fill never queues behind the channel's broadcast), D2 (the Mini App: server-side `initData` HMAC from P07, the
trade sheet and its integer money path, its own surface and its own deployment), D3 (20 commands, every one tappable,
with a natural-language fallback that answers with a confirmation card), D4 (the order card and the fill/refusal
notifications, on `_order_core` — the same risk gate as the web), D5 (`channel.py` and the broadcast routes, reading
`tape_fills`), D7 (onboarding instrumentation) and D8 (kill switch, staged broadcast, ops page). D6's Mini App screens
are the one deferred piece: its command surface and `/verify` shipped, and the wallet screens belong with P13's custody
work, which is also the rule that keeps real funds out until P13/P14 are green.

**The bug worth the whole phase.** Every trade button on every channel alert was dead, from P08 to P12. The bot mints
`?startapp=<payload>` in Python; the Mini App parses it in TypeScript; the two implementations used different grammars
and each was internally consistent, so both suites stayed green. Fixed structurally: the grammar lives once in
`contracts/startapp.json`, both sides read it, and `tools/p12-gate-check.py` mints links with the real Python function
and runs the real TypeScript parser over them in node. Writing the contract immediately caught a second bug of the same
family — a charset written in regex notation, read as a class by one language and as a literal list by the other, which
turned `fed-cut-sept` into `--`.

**Also fixed in passing.** `POST /v1/orders/amount` and the web ticket's 422 (the ticket had posted the wrong shape for
four phases and had no test of its own); a route defined twice in `app.py`; the refusal vocabulary keyed on registered
codes on both sides, with the code moved out of the user's line and into the toast.

**Deployments.** `polygm-mini-app` (the trade screen, off-surface paths 404 with a sentence, Telegram-only framing, two
noindex signals) and `polygm-api` (`api/index.py`: the repo's own migration ledger and seed into `/tmp`, then the same
ASGI app everything else runs). The two are separate projects so the front end can be rolled back without the backend.
Honest limit, written in the file: `/tmp` is per-instance, so fixtures are stable and records are disposable — Postgres
stays the production path.

**Verified.** `pytest` **1219 passed**; `vitest` **492 passed / 56 files**; `tsc --noEmit` clean; `next build` clean on
both surfaces; `check-openapi` **617/0**; `tools/p12-gate-check.py` **31/0**, and **38/0** with `--live` (the deployed
Mini App's 404s, its noindex header, the API's health, and market data over the wire). Tampered `initData` → 401,
replayed payload → 409, duplicate `update_id` → no second execution. The Mini App → API hop is proven on the origin: a
tampered payload returns the *API's* refusal, generated on the other side of the network.

**`[UNVERIFIED]`.** `PGM_TELEGRAM_BOT_TOKEN` is deliberately unset on the API deployment, so no session can be minted
and no real trade can be placed from the Mini App; the BotFather Mini App URL is a manual step with no API. Both are
recorded in `docs/P12-telegram-bot.md`, not carried as a silent gap. No real funds: money still waits for P13 and P14.

## P15 — deployment, observability and operations · 2026-09-26

**What the phase is for.** One test, from the kit: at 2am, from a phone, in under five minutes, can the person on
call answer three questions — is the system healthy, is any user's money in an inconsistent state, should I stop
trading? D1/D2 (the environment matrix and the costed infrastructure) landed earlier; this closes D3–D9: the
pipeline and its manual gate on anything touching the executor, the four observability pillars with
`unreconciled_orders` at the centre, 25 alert rules each carrying a runbook link and an owner, eighteen runbooks
with drill records, DR with a rehearsed restore (123 tables, ~3,261 rows, 128 ms), per-service cost attribution at
$119.38 of a $300 ceiling with the cut order written down in advance, and a readiness checklist that refuses to be
signed while any line is red.

**The finding of the session was not in P15.** Regenerating the web's API client made one source file newer than
`docs/verification/P08-bundle.txt`, which was enough to prove that the frontend's 200 KB budget had been **asserted
for five phases and measured once**: `/` 208.4 KB, `/markets` 200.1, `/tma` 226.7 against a record claiming 13 KB of
headroom. `c8` compared the record's *mtime* with the newest source file — a proxy that reports an accurate record as
stale after a checkout and a stale record as current, which is exactly how the breach hid. The payload was fixed
(statically-imported Mini App screen behind a chunk boundary; the trade sheet and wallet behind `next/dynamic`; the
17 KB `@tanstack/react-query` dependency, configured to decline the things a query library is for, replaced by
`web/src/api/data.ts` with its semantics pinned by tests) and so was the measurement (the budget now names its
scope, prints the Telegram bridge it excludes with the reason, and both records stamp a `sources-sha256` over
exactly the files they describe, recomputed by the checks). `/` 184.2 · `/markets` 192.6 · `/tma` 196.1. A minimal
Next 16.3.5 + React 19 app on the same toolchain measures 169.0 KB: 84% of the budget is the framework.

**Two more things a fresh checkout would have broken, found the same way.** `deploy/deploy.sh` and
`deploy/rollback.sh` were tracked `100644` while the pipeline runs them over SSH and the runbooks tell a human to
run them: on a host that checks the repo out, the one command that has to work at 2am answers "Permission denied".
Both are committed `100755`. And a full-suite gate run failed with `1272 tests, exit 1, tail 'FAILED (errors=33)'` —
none of the 33 named in the last line — because the 1 GB `/tmp` tmpfs was full of dead test databases from a killed
run; `tests/conftest.py` now refuses to start with the free megabyte count, the reason, and `PGM_TEST_TMPDIR`, and
`tests/test_suite_guard.py` tests that refusal, including a rehearsal through the same entry point a real run takes.

**Also re-run and refreshed:** `docs/verification/P04-gate-output.txt` (55/55 and 156 tests from P04's own session →
**56/56** with the suite at **1489 tests**), because an un-re-run record is a claim, not evidence.

**The quality gate, demonstrated rather than described.** The kit's gate for P15 is a sentence — one page, from a
phone, five minutes, three answers — so `make p15-2am` rehearses it with the real components and records the result
in `docs/verification/P15-2am-drill.txt`. It writes a single `orphan` (an order at the venue we cannot map to a
user, ten minutes old) into a scratch copy of the seeded database and leaves the rest of the world healthy — one
page, one problem — then reads `/v1/admin/metrics` through the app, evaluates the alarm registry with the engine the
dashboards import, opens the runbook the notification links to and reads it, renders the on-call page from the same
payload, and times itself: **0.9 s of the 300 s budget**, page **8.2 KB**, 1 alarm firing of 25. Eight checks, none
of which passes by construction, and ten canaries in `--self-test` that prove it. Two of the checks were wrong when
first written and the fixes are the interesting part: c3 searched the dashboard's raw HTML for `>1 <` and failed
against a page that does carry the number (the count follows a status chip inside its own span), and the c3 canary
therefore "passed" against a baseline where c3 was *already* failing — a canary that goes red for the wrong reason
proves nothing, so the self-test now refuses to plant anything until the unmutated drill is green.

**Verified at close.** Batch A `p01–p07` + `seed-sql-check` → **EXIT_A=0**; batch B `p08 p09 p10 p12 p12-selftest
p13-read infra-check p15-migrations p15-pipeline p15-alerts p15-runbooks p15-cost p15-dashboards` → **EXIT_B=0**.
P08 **16/16**, P09 **7/7**, P10 **15/15**, P15 environments **18/18**, and no `FAIL` line anywhere in either log
except the string of the canary that asserts a recorded failure is reported rather than counted.

**`[UNVERIFIED]`, carried forward.** Everything the readiness checklist names: dashboards reviewed by the on-call
person, every alarm fired *in staging*, every runbook walked by a non-author, a timed rollback against real
infrastructure, the synthetic probe running outside the fleet, a 30-day rotation staffed, the kill switch thrown
from a real phone, and a cost dashboard showing actuals rather than a projection. Plus the pre-existing owner
steps: GitHub 2FA, the Supabase CIDR, the Turnkey provider rate, real image digests, provider credentials for
`terraform apply`, the domain and Cloudflare delegation, `PGM_TELEGRAM_BOT_TOKEN`, the BotFather Mini App URL,
real-phone acceptance — and the $50/72 h canary, which stays blocked while the P14 gate reads NO-GO.

## P16 — launch, distribution and growth · 2026-09-26

The last phase, and the only one whose artefact is not code. Its deliverable is a plan, and a plan cannot be tested
the way a money path can — so the phase is built the way the rest of this repository is: the plan lives in
`config/gtm.json`, everything it claims about the product is recomputed by a checker, and the documents are held to
the config rather than the other way round.

**D1, the beachhead.** One audience, chosen and defended: **the 5-minute crypto Up/Down trader who already lives in
Telegram**. Reachable ≈ **18,000** people, from 300,000 monthly venue wallets × 0.20 hourly crypto × 0.60 in
Telegram × 0.50 reachable organically, every input a range with a provenance tag — two of them `[ASSUMPTION]`,
written as ranges so the estimate moves with the reasoning instead of being asserted as fact. The whale-follower,
the sports trader (kept as the named fallback) and the analytics power user are rejected with reasons, and the
mechanism is stated so it cannot drift: this market's shape — $125M/week across ~114 builders, Gini 0.83, six
builders holding 81% of lifetime volume — says a new entrant wins with a distribution surface the incumbents are
not using, which is a Telegram alert channel, not founder visibility.

**D2–D3, two channels.** Ranked by cost per activated user, **exactly two chosen** (the free alert channel at $0
and the public pages' organic search at $0) and six refused with named failure modes — including paid acquisition,
refused on arithmetic ($120 per activated user against a $12/month subscription is a twelve-month payback before
churn) rather than on taste. The free channel carries a **12-message daily budget** that is the channel engine's own
cadence (4/hour, 2 of a kind/hour, 6-minute gap, read out of `telegrambot/channel.py` by the checker), and the free
tier never withholds the exit, the withdrawal path, the kill switch or the loss numbers.

**D4–D5, activation and retention.** Activation is a funded wallet plus one matched order inside 7 days; the funnel
multiplies out to **1.25%** of channel members (0.06 × 0.95 × 0.97 × 0.35 × 0.70 × 0.92), with an intervention
named at every step the user can leave from, and the day-1/3/7 plan deliberately excludes the one message every
consumer product sends (`never: a "we miss you" message, a streak, or anything that implies a comeback is likely`). Retention is alerts, watchlists, self-ranking
and automation, plus the losing-streak rules: the halt fires with the P&L, rules pause with the reason stated, the
exit and kill switch are on the same screen — and churn is defined as 30 days with no position, no live alert, no
live rule and no session, because that is the definition we cannot accidentally satisfy.

**D6, the ramp in code.** `revenue/schedule.py` loads the plan and refuses an illegal one: **0 bps at launch**, 10
bps at day 90 only if D7 retention ≥ 0.15 and ≥30 users traded twice, 25 bps at day 180 only if the month-6 volume
gate is met — inside the venue's own mechanics (7 days between changes, 3 days notice, one pending change), with
increases gated on retention and cuts deliberately exempt. Never a token; builder fees never above 60% of revenue,
because the venue can revoke the privilege at its discretion.

**D7–D9, the gates and the words.** Five gates, each carrying the kit's numbers and each stating what happens when
it is missed; the month-12 miss is written out as a procedure (what users are told, what keeps running read-only,
how money gets out) rather than left as a sentence. The words are three documents: the plan, the launch assets
(landing hero, `/start`, the pinned post, X, Telegram, Product Hunt, Hacker News with an honest audience assessment
that predicts 0–2 activated users, the cross-promo DM, ten support macros) and the community document (useful before
promotional, transparent loss handling, a status page that reads the same sources the alert engine pages on, and
impersonation defence as a five-point, checkable differentiator).

**The new thing in the codebase: the disclosure, on every surface.** `web/src/legal/disclaimer.tsx` holds the three
sentences once — not affiliated with Polymarket; odds are a market, not a forecast; this is a market that resolves to
zero and you can lose everything you deposit — and the landing page, the Mini App and the public page frame each
render it. `web/src/legal/disclaimer.test.tsx` asserts the phrases against `config/gtm.json` rather than restating
them, renders the component, proves it is server-safe, and checks that the three surfaces actually render it rather
than merely import it. The P08 gate's c5 then caught the first version of its stylesheet, which used `px` fallbacks
inside `var()` — five design-token violations — and the fix removed them rather than the check.

**Verified at close.** `make p16` → **75 passed, 0 failed** (`docs/verification/P16-gtm.txt`); `--self-test` →
**18 caught, 0 missed** (`docs/verification/P16-gtm-selftest.txt`), including two canaries aimed at the checker
itself: the first draft of c5 read the disclaimer *with its comments*, so a deleted sentence could still pass on a
docstring, and c4 validated `config/gtm.json` from disk instead of the config it was handed, so a mutation was
invisible to it. Both were found by the canaries and fixed in the checker — the plan was right and the test lied,
which is the failure mode this phase's self-test exists for. Web: `npm test` **65 files / 580 tests**, `npm run
build` clean, `npm run measure` inside budget at `/` 184.2 KB and `/tma` **196.5 KB** of 200 KB, the disclosure
costing 0.4 KB of JS on the Mini App and 0.1 KB of CSS everywhere. Python: the full suite at **1529 passed**
(180.2 s), and the P08 gate back to **16/16** on the re-measured bundle record (`sources-sha256`
`487239bd499fde28`), because adding a footer to three surfaces invalidates a budget record and a record that is not
re-measured is a number nobody should read.

**The neighbouring gates, re-run because a web change is never only a web change.** After the commit, the gates
whose subject the disclosure touched were re-run rather than assumed: **P03 62/62** (the token layer, with `web/`
present), **P09 7/7** (the markets surfaces, 73.1 s), **P10 15/15** (the terminal, 5.2 s), **p12-selftest 4/4**,
**p16 75/0** and **p16-selftest 18/0** — one batch, `EXIT=0`. P08 was already back to 16/16 on the re-measured
record, and the P04 gate to 56/56 on the suite. Nothing about adding a footer is innocent: it changes the CSS
token audit, the bundle record, and every gate that reads a page.

**`[UNVERIFIED]`, carried forward for the last time.** Everything in P15's list, plus the four things this phase
cannot run from a workspace: the channel has no members yet, no alert has reached a real phone, the launch posts
have not been published anywhere, and the five gates are dated in the future. P16 ships the plan and the machinery
that keeps it honest; the numbers in it are the owner's to earn.

---

## Post-P16 — closing P14's open items, one measurement at a time · 2026-09-26

P16 closed and the frontier became the only question left: **what stands between this and the owner pushing the
button?** `docs/P14-security-gate.md` answers it, and the answer is now a short list. Each of the four measurable
items the gate carried was closed the way the rest of this repository closes things — by re-running the probe that
found it, not by writing a paragraph saying it was fixed.

**The copy farm stops pairing by coincidence.** P14 measured that the rule could not tell a follower from two
traders sharing a cadence: with the candidate's fills moved 200 s *earlier* — so it is never the one being followed
in any pairing sense — 10 of 12 of our fills still matched, because *any* candidate fill inside 120 s counted and a
60 s cadence always has one. The row that produces is a public "derived from 0x…", which is a claim about a person.
`copy_farm()` now pairs **one-to-one** (same market and side, the candidate first, nearest first) and requires
**coverage**: if the candidate still has fills left over in the markets where we paired, our fills were not
following its fills, they were merely near them. Pairing alone does not fix the reported tape — 10 of 12 still pair
— coverage does, at 2 left over against a 10% tolerance. The tolerance is deliberately tight, and the reason is the
asymmetry: a missed farm costs a wallet a place it would have ranked into anyway, a false one accuses somebody.

**The webhook transport lands with its guard already inside it.** The finding was that a stored-URL SSRF path
existed in the product's channel list with no transport behind it — latent rather than absent. The right way to
close that is not to wait for the transport: `packages/polygm_core/signals/webhook.py` (333 lines) parses the URL on
landing, refuses any scheme but https, refuses loopback, link-local `169.254.0.0/16`, RFC1918 and IPv6 ULA
targets, and re-checks every redirect hop — and refuses *before* the send rather than after. 17 tests in
`tests/test_signals_webhook.py` hold it, including the DNS-rebinding shape.

**The 500-copier cascade is measured, not argued.** The arithmetic bound was done first (the engine sizes 500
copiers through the same code as one); the finding was that no run had ever placed 500 orders against a *filling*
venue. Chaos drill 11 does exactly that: 500 funded copiers, one source fill through the product's own
`CopyEngine`, 500 intents queued, the executor claiming them `batch_size` at a time, the venue filling all 500, the
fills booked through the same `book_fill` the trade stream uses. Measured: **fan-out 124 ms (0.25 ms per copier)**,
**$6,375.00 aggregate across 500 orders, largest $12.75 against the $25.00 per-trade ceiling**, zero new intents
from replaying the same fill, and no fill above the decisions' own sum. Building it found the first version of the
harness had 400 of 500 orders refused `STALE_QUOTE` — the pre-flight was right and the harness was wrong, which is
worth writing down because it is the check doing its job.

**Three harness bugs and one schema bug, found by running the gates rather than by reading them.** A dict-shaped
allowlist entry (P15's RFC 4226 vector) crashed the AppSec scanner with `TypeError: unhashable type` and took the
nightly down instead of reporting a finding; git's `b/` diff prefix made every line-scoped exemption read as stale;
and the DSN rule matched our *own source* through a URL-shaped regex plus an `@import`. The AppSec scanner now
parses both allowlist shapes, honours scoped entries only on the exact added line, and **fails on malformed or
stale exemptions** rather than staying silent. The schema bug is the one P11's floor check (c27) named on the first
full run after P16: **`telegram_kill_state` was declared append-only, granted the `polygm_app` half, and never
given the Postgres trigger** — so our own code could rewrite the ledger of a switch thrown during an incident. The
hermetic suite could not see it: the transpiler drops `CREATE TRIGGER` and generates the SQLite triggers from the
declared list, so the test database had both halves all along. Fixed in 0019, next to the table it describes, and
it is the second time the same shape has been found in the same place — which is the argument for the check reading
the *declaration* rather than a scan of `CREATE TRIGGER` statements.

**The last item was a decision, and it landed in the gate.** F19's fix covers *sized* actions; a close is sized by
the position itself, so a $25,000 position could not be closed by a rule while the entry cap was $2,500 — an armed
stop-loss that can never fire, which is the opposite of risk control. The decision: **a reduce-only sell answers to
its own ceiling** (`max_close_notional_micro`, one position's worth) **instead of the entry cap**, because the entry
cap bounds *new* exposure and refusing an exit is not risk reduction, it *is* risk. It is not an exemption. The
size must not exceed what is actually held, read from `position_lots` by the caller — the API's order path and the
executor each read their own copy, never the request body, because a client-supplied "I hold this much" is a cap
bypass with extra steps; selling more than is held is not a close (the excess opens a short, which creates
exposure) and keeps the entry cap; and an excessive close is refused by the close ceiling, with `OVER_CLOSE_CAP`
existing so the refusal says *which* ceiling stopped it. `Decision.reduce_only` is set on refusals too, for the
question actually asked after an incident. The first draft reported it only on the success path, and the refusal
test caught that. Verified three ways: the probe re-measures the decision against the real gate with a real
$25,000 position, three API tests drive it through `POST /v1/orders` (`TestTheCloseCeilingOverTheApi`), and two
unit tests pin the edges.

**A lint finding, and the one web-side open item.** Re-running the gates after the close ceiling found two more
things, which is the whole reason a change gets re-run rather than reasoned about. `tools/lint-rules.py` reported
`core-dep-free: imports socket` in the webhook transport — so `make p04` had been failing (55/56) on the tree the
transport landed on, and a red gate is not a gate anybody reads. `socket` is stdlib and the SSRF guard genuinely
needs it: refusing a URL that points at loopback is only a guard if the *hostname* is resolved before the send, and
moving that call out of the module would move a security decision into whichever caller remembered it. The core's
stdlib allowlist is finished the same way P07 finished it for `base64`/`struct`/`urllib` — with the reason written
next to the name. **The tax export's formula-injection item turned out to be a real gap in the product, not just a
missing route**: there is no server-side export because the CSV is built in the browser (`portfolio.ts`), whose
columns come from the wire — so the guard belongs in the writer every cell passes through, and `quoteCsv` escaped
quotes and newlines but nothing else. `csvCell` now neutralises a leading `=`, `+`, `-`, `@`, tab or CR with an
apostrophe, leaves a well-formed signed integer alone (this export exists to be added up), and the header row stays
verbatim. It is written as an explicit character Set with TAB and CR from their code points rather than a regex
class, because the first version's meaning depended on how many backslashes survived being written — a guard nobody
can read off the source is not a guard. 12 assertions in the module's tests drive both directions; a source-level
check in the probe (stated as such: a browser blob cannot be fetched by a Python probe) holds the shape.

**Verified after the batch.** The P14 chain, re-recorded: authz **37/0/0**, attack surface **77/0/1** (was 8 open),
key drills **15/0/1**, AppSec **38/0/1**, infra **19/2/6 FAIL**, abuse **18/0/1**; `docs/P14-security-gate.md`
regenerated and `--check` clean. The gate is **NO-GO on two recorded failures, and both are the owner's** — the
Supabase token in `~/.secrets/tokens.env` now returns 401 (the check says "rotate the token" out loud rather than
printing a bare status) and GitHub 2FA is off on the account whose token holds `admin:org`. The two remaining
probe OPENs are latent surfaces with no code behind them yet: a tax-export route that does not exist, and payment
fulfilment that does not exist (both carry the exact work they need on the day they ship).

Suite counts on the final tree, both runners: **unittest 1543 OK** (189.0 s), **pytest 1563 passed**; web **65
files / 580 tests**, `npm run build` (Turbopack) clean, `npm run measure` re-recorded — `/tma` 196.5 KB of the
200 KB budget, route-level splitting proven. Neighbouring gates on the same tree: **P04 56/56, P06 31/31, P08 16/16,
P10 15/15, P11 30/30, P12 41/0**, `tools/check-openapi.py` 685/0, `build-sqlite-migrations --check` clean.

**`[UNVERIFIED]`, and it is the same list as P16's.** No real funds: money waits for P13 **and** P14, and P14's gate
is NO-GO until the Supabase token is rotated and 2FA is on. `PGM_TELEGRAM_BOT_TOKEN` is unset on `polygm-api`, the
BotFather Mini App URL is still a manual step, the real-phone acceptance run has not happened, and the $50/72 h
canary stays blocked while the gate says no.

---

## Post-P16 — the skills are installed, and the motion pass they implied · 2026-09-26

**Installed, and made reproducible.** The named repos are vendored in `skills/` with provenance in
`skills/VENDOR.json` (44 skills from six sources: the higgsfield brand/image kit, emilkowalski's animation set,
`vercel-labs/agent-skills` including `web-design-guidelines` and `react-best-practices`, Leonxlnx's taste-skill and
its image-to-code skill, VoltAgent's awesome-design-md, and microsoft's playwright-cli). The vendored copy is what
survives this workspace's resets — and the mirrors under `~/skills`, `~/.claude/skills` and `~/.agents/skills` are
not part of the snapshot, so `tools/install-skills.sh --apply` rebuilds all three from the repo in one command
instead of leaving the installation in a shell history nobody kept.

**Then the work the skills are for.** `plans/animation-audit.md` is the audit that
`improve-animations` prescribes — recon, findings, vetting — and it found something worth writing down: **the
design system already permitted this motion and the shell had never shipped it.** Every motion consumer in the
product lived in the Mini App block; the desktop animated a number flash and a button press and nothing else.
`brand/tokens.json` even allowed "toast" and "tab indicator slide" by name, while `--pgm-dur-micro` had zero
consumers and `--pgm-dur-medium`'s comment promised a slide no rule implemented.

**Six fixes, all values read from the ladder rather than typed.** The dialog enters and exits at
`--pgm-dur-large` with `--pgm-rise-sm` and a fade for the layer behind it; the toast stack rises at
`--pgm-dur-small` and its rows leave through a `dismissing` state, so `dismiss` stays the only remover and a row
is never removed between two frames; the press is the design system's own `scale(0.97)` (it was a `translateY`,
a drift from `P03 §D3`) with the transition moved to the base rule, because a transition declared inside `:active`
animates the press and not the *release*; `--pgm-dur-micro` gets its first desktop consumer on the rail handle;
and the tab indicator grows from the leading edge at `--pgm-dur-medium` instead of popping.

**Two things the pass is careful about, and one it deliberately is not.** The exit paths short-circuit under
`prefers-reduced-motion`, because the reduced-motion block removes animations and an exit that waits for an
`animationend` that cannot fire is a **dialog nobody can close** — that case has a test, and a second test exists
because the first draft of the rule was wrong: a browser too old to have `matchMedia` still runs CSS animations, so
waiting there is correct. A true cross-tab slide is **deferred with its reason** (the marker lives on each tab in a
`grid-auto-flow: column` bar, so travelling needs each tab's offset measured at runtime) and the token's own text now
describes what exists. And auto-dismiss is **not** in scope: `ttlMs` is carried by every row and no timer reads it —
that is a product behaviour change, so the leaving state is built and documented for the day somebody adds it.

**The contract comments were updated rather than quietly broken.** `--pgm-rise-sm`'s text ended *"no other class
animates"*; it now lists its consumers, because "no other" is unfalsifiable while a list can be checked. That edit
belongs in the *generator*, not the generated file — `web/styles/tokens.css` is built from `brand/tokens.css`, which
is built from `brand/tokens.json` — and P08's c6 said so out loud by failing on the first hand-edit.

**Verified.** Web **67 files / 601 tests** (was 65/580: `src/ui/motion.test.ts` asserts the stylesheet's values, the
`Dialog` and `Toast` tests assert the lifecycle), Python **1543 OK** unittest and pytest as recorded, and the gates
a stylesheet change touches, re-run rather than assumed: **P03 62/62** with all 20 mutations caught, **P08 16/16**
(bundle re-measured from the product's own build), **P10 15/15**, **P12 41/0**, **P09 7/7**. `build-foundations
--check` and `build-web-tokens --check` both clean, so the generated layers are reproducible from their sources.

**`[UNVERIFIED]`.** The values are asserted mechanically; **how 8px of rise and a 125ms toast actually feel has not
been eyeballed on a device** — this box has no browser session, and the honest statement is that the mechanics are
proven and the feel is a judgement the owner can now make against a real screen. That is the one line in this
change a test cannot carry.

---

## The design review, and the three things the guidelines found in the shell · 2026-09-27

**Method.** `plans/design-review.md` is the `web-design-guidelines` pass: Vercel's Web Interface Guidelines fetched
from source, the whole app reviewed against every rule, one finding per line. The file records what was fixed,
what was **already correct** (so nobody re-litigates it), what was **refused with a reason**, and what stays
`[UNVERIFIED]` — the same shape as the animation audit, because a review that only lists fixes gets re-run forever.

**The headline finding was not a design opinion.** Both rail handles render `role="separator"` with `tabIndex={0}`
— which makes them *window splitters*, and a focusable separator owes `aria-valuenow`/`min`/`max` and the arrow
keys. They had a label, a drag handler, and nothing else: a keyboard user could Tab to a control that did nothing
and could not resize a rail at all, on a product whose entire layout is three resizable columns. Meanwhile the
**terminal's** handles have answered arrows, Home and Shift-coarse steps since P10. The same product disagreed with
itself about what a control is, and only the newer of the two had forgotten.

That is now one implementation: `RAIL_STEP`/`nudgeFraction` beside `clampFraction` (the terminal's own numbers,
exported so the two layouts cannot drift again), `applyRail` behind the same `railsFit` gate the pointer uses, the
four keys, Enter/Space for collapse, and the values a splitter owes — including an `aria-valuetext` that names the
keys, because a percentage announced alone is a number with no affordance attached. The test that guards it also
asserts the ARIA values, because the first draft of this fix added the handler and forgot the values.

**Five more, each one line of cause.** `color-scheme` was never declared, so a dark-first product painted light
scrollbars and a light `<select>` popup inside dark cards — now per theme, on the attribute the server already
writes. The handle had no `touch-action`, and on a touch device a finger drag is a *scroll*: the browser claims the
gesture, sends `pointercancel`, and the rail is unresizable by touch — `none`, because the handle has exactly one
gesture. `.overlay` did not contain scroll chaining, so a reader closing a long dialog found the page underneath had
moved; the ladder's own container already knew this rule and the newest layer did not. There was **no skip link**,
so every navigation tabbed an entire rail before the content. And four loading strings ended in `...` while three
in the same file ended in `…`.

**What was refused, with the reason.** Rail widths stay in `localStorage`, not the URL (a device property, not an
account one). The ladder is not virtualized (bounded scroll, and `content-visibility` would change layout timing on
rows that re-ladder 20×/sec). The palette keeps `autoFocus` (it opens *because* the user asked for it, by
keyboard). Sentence case stays (one style guide owns the copy — `web/DESIGN.md §8`).

**Also repaired this block, and worth recording because it nearly cost the repository.** A workspace reset restored
a *stale* `.git` while the files on disk had advanced, so a commit built in good faith landed on `3921df6` — six
commits behind `origin/main` — and would have **reverted the close-ceiling, CSV, skills, brand and motion work** in
one push. GitHub refused it as a non-fast-forward; the diff stat is what confirmed why. The tree was rebuilt on
`origin/main` and the pass re-applied there. Two lessons, both now in the tooling: `tools/git-push.sh` re-derives
the `origin` remote and its credential from `.secrets/tokens.env` on every push (both live outside the snapshot),
and **a push that is rejected is information, not an obstacle** — `git log --oneline` before believing a commit is
where you think it is.

**Verified.** Web **68 files / 613 tests** (601 + `rails.test.ts` ×12), `tsc --noEmit` clean, `i18n-check` ok (998
keys), and the gates a shell/CSS/i18n change touches, re-run rather than assumed: **P03 62/62** with all mutations
caught, **P08 16/16** on a rebuilt bundle (`sources-sha256 d1d6df72a9507672`, /tma 196.7 KB of 200 KB), **P09 7/7**,
**P10 15/15**, **P12 41/0**.

**`[UNVERIFIED]`.** The touch fix is correct by spec and by the Mini App's precedent, but no finger has dragged the
rail on a real device from here; and the hover/active contrast holds by token construction (P03's gate) rather than
by rendered measurement. Both are recorded in `plans/design-review.md` as the owner's to earn.

---

## The React pass: two reads where one would do, and the poll that outran its own answer · 2026-09-27

**Method.** `plans/react-review.md` is Vercel's `react-best-practices` (70 rules, eight categories, ordered by
impact) applied to this app. The dependency list is four packages, so there was no library to swap for a better
one and every finding is about this code's behaviour. The highest-impact category — waterfalls — is where the cost
actually was, in three places, and all three are invisible to a test suite because they are about *what the page
does over the network* rather than what it renders.

**The same read, twice per request.** `app/market/[market]` and `app/trader/[who]` call `publicRead` once in
`generateMetadata` and again in the page body: two identical URLs, two HTTP round trips, and Next cannot collapse
them because this transport carries `cache: "no-store"` deliberately. `React.cache` now wraps both server readers
behind a generic façade, scope = one request, nothing retained afterwards — so it cannot serve one render another
one's data. It is worth stating what stands in for a test here: `React.cache` is a **pass-through outside a React
request** (measured in this very environment), so the suite asserts the mechanism at source level, asserts that
the *reason* still holds — those pages still read twice, and if that stops being true the memo stops paying for
itself — and asserts the pass-through property that makes putting a cache in that module safe.

**Independent reads awaited in turn.** `alerts` awaited rules then history; `automation` awaited rules then
catalog. Neither pair had a dependency, so both pages paid the sum of two round trips where they owed the slower
one. Both now go through a loader that uses `Promise.all`, and the loaders take the reader **as a parameter** —
not ceremony, but the thing that makes the fix testable at all, since `serverRead` imports `server-only` and a
page that imported it could only be verified by reading its source. With the reader injected, the test records
start and finish order and asserts the second read begins while the first is still open. The client's own
`AlertsView.refresh` had the same waterfall one layer down and got the same fix.

**A poll that fires over its own request.** `setInterval(() => void load(), ms)` is not "every 2 seconds"; it is
"start a request every 2 seconds whether or not the last one answered". The client retries with backoff, so a
slow API turns the book poll into a stack of in-flight reads, and unordered responses mean an older book can land
after a newer one — the screen freezes on stale data exactly when the API is unwell. `src/live/usePoll.ts`
schedules the next run after the previous one settles (the interval becomes a floor, at most one request in
flight), keeps the task in a ref so a market-id-dependent loader does not re-arm the timer each render, survives a
failing task without an unhandled rejection, and stops on unmount. Tested on the timeline, because a call-count
test would pass against the old code too.

**Also verified.** Twelve rules checked and already right, listed in the review so nobody re-litigates them (no
barrel files, `next/dynamic` already at the TMA boundary, drag writes CSS variables instead of re-rendering,
`TerminalScreen` already parallel, no `&&`-with-a-number anywhere). Two deferrals with reasons — hidden-tab
pausing, and `useTransition` on the tape — and one refusal: SWR. This client carries idempotency keys, per-attempt
timeouts, a retry policy that will not retry a timed-out mutation, and envelope stamping; SWR would replace those
semantics with a cache and leave them to be rebuilt around it. The deduplication half is taken where it pays (R1);
the cache half is a redesign, not a fix.

**Gates.** Web **70 files / 627 tests**, `tsc` clean, `i18n-check` ok (998 keys), **P03 62/62** (all mutations
caught), **P08 16/16** on a rebuilt bundle (`sources-sha256 d9e6bbab958015d6`), **P09 7/7**, **P10 15/15**,
**P12 41/0**.

**`[UNVERIFIED]`.** The dedupe has no unit-observable test (stated in the review, with what stands in for it); and
the 2s book poll has not been watched in a browser against a genuinely slow API from here. Both are the owner's to
take with the pair running.

---

## The Vercel audit, and the credential that stopped it · 2026-09-27

**The skill ran and stopped at step one, and that is the finding.** `vercel-optimize` is metrics-first by doctrine —
"recommendations start from Vercel production signals, not repo-wide grep" — so the audit's first act is to collect
production signals. It could not: the Vercel credential authenticates as the user `minsofminer` and no longer has
access to the team its own default points at (`miners4` / `team_CXIJ9RpnYma4N3nDVMzE8DiV`). `GET /v9/projects/<id>
?teamId=…` answers **403**, and `GET /v2/teams` returns an empty list. The blocker is `forbidden`, which the skill
says to fix rather than work around.

**Why that is a launch item and not just an audit delay.** This is the token the Mini App deploys with.
`polygm-mini-app` is live and serving — but **a redeploy from this workspace cannot authenticate right now**. It is
the second credential in this environment to lose access without anyone rotating it, the Supabase token's 401 being
the first, and both now sit in the owner's list together.

**What the run produced anyway.** The skill's scanner needs no credentials: 192 files, 36 routes, 15 scanners, 18
findings, preserved verbatim with a provenance header at `docs/verification/vercel-scan.json`. Seventeen are
`force-dynamic` — five of them on the only routes whose audience is strangers (`/market/[market]`, `/trader/[who]`,
`/markets`, `/whales`, the leaderboard) — and one is `app/layout.tsx`'s cookie read, which is what makes the tree
dynamic. Every finding carries `trafficIndependent: false`, and the skill's rule for that is to drop route-local
patterns without route-level traffic evidence. So **none of them was promoted to a recommendation**, and
`plans/vercel-review.md` records them as a watch-list instead: the five public routes *may* deserve a named cache
policy once there is traffic data to justify one, and the root-layout cookie is a **trade** P08 already made on
purpose (dark-first with no first-paint flash) rather than a defect to remove.

**No code changed.** The deliverable of a blocked audit is the blocker, its evidence, the artifact that did come
out of it, and the owner action — the same treatment the Supabase 401 has had since it appeared. When the token is
re-authorized, `collect-signals.mjs` → `gate-investigations.mjs` → `deep-dive.mjs` → verify → render runs end to end
from the vendored skill.

---

## The browser ran, and it disagreed with three passes at once · 2026-09-27

**Finding zero: the note saying this could not be done.** `playwright.config.ts` carried an honest-status paragraph
claiming the browser cannot run on this box — chromium needs system libraries "and this sandbox runs as a non-root
user with no way to install them". The libraries were installed, `sudo` works, and `npx playwright install chromium`
plus `install-deps` produced a working browser. The paragraph is corrected in place, because a stale "we cannot
check this" is the most expensive kind of wrong note. `plans/browser-verification.md` has the reproduction.

**Then it found four defects, three in product code, none reachable from a unit test.**

1. **An unreachable API 500'd every authenticated page.** `callUpstream` awaited `fetch` with no `try`/`catch`, so a
   rejected connection escaped `proxy()` → `serverRead()` → the render and every signed-in page answered Next's
   error document — no shell, no explanation. The client half has always handled this carefully; the server hop was
   the half that did not. Now wrapped, with the client's own 8s timeout, answering **503 + a NETWORK envelope** —
   503 rather than 0, because `serverRead` reads `status >= 400` as the error path and a zero would have been
   treated as *success with an empty body*.
2. **A failed sign-in left the button spinning for ever.** Same shape in `SignInForm`: the rejection took
   `setBusy(false)` with it, so the button stayed disabled with no message and the retry the user wanted was
   impossible. Fixed at all three call sites, with a new `auth.signin.unreachable` key — deliberately not the
   wrong-password sentence.
3. **Escape raced the dialog it had just opened.** The motion pass's exit animation was being killed on the same
   tick: the Shell's global `keydown` (window) and the Dialog's (document) both saw the keypress, so the parent
   unmounted the panel the instant the exit began — the panel vanished between two frames, exactly the bug the
   exit was added to remove. Fixed by ownership: while a dialog is open, the shell's close action is a no-op.
4. **Two specs had never reached their screens.** `buy-flow` and `wallet-ceremony` stub the wire in the browser, but
   their pages are behind `(app)`'s server-side session decision — so every assertion had been running against the
   sign-in page since the day they were written. `e2e/session.ts` supplies the cookie; that fixed one whole class
   of failure. They still fail on route/selector drift from P12/P16 and are marked `test.fixme` with the reason in
   the file, not deleted and not silently red.

**What the suite says now:** `18 passed, 8 skipped`, including the new `e2e/shell-fixes.spec.ts` — 13 green cases
that turn the design and motion passes' claims into browser facts: keyboard resize against the drag's own store,
the inverted right rail, Enter-to-collapse, `touch-action: none` computed on the live element, the skip link's
focus path, `color-scheme` in both themes, the dialog's enter→exit→removal ordering, the reduced-motion
short-circuit, and "the API is down" rendering the shell instead of a 500.

**Verified:** web **70 files / 627 tests**, `tsc` clean, `i18n-check` ok (999 keys), e2e **13/13** for the new spec
and 18/26 overall with the remainder marked.

---

## The last P14 OPEN closes, and it closes by being an attack that fails · 2026-09-28

**The item.** For the whole of P14–P16 the attack-surface record carried one OPEN: *"neither Stripe nor
Telegram-Stars fulfilment exists yet, so payment-webhook forgery has no target"* — with the requirements for the day
either ships attached to it (raw-bytes signature, constant-time compare, a five-minute window, every event id
stored against replays, Stars only ever inside a verified update, and never reading the amount, the user or the plan
from the body).

**Why waiting was the wrong call.** The OPEN named its own target: the day a payment route ships, the way in is
whatever the first version of that handler does. Building the gate while there is no product means there is no
temptation to bend it, no deadline to ship under, and no cost — and the CSV writer's closure had already taught the
lesson this one follows: **a control is closed when something can exercise it, not when a document promises it.**
So the close is the opposite of a promise: nine checks in the probe that run the control and require each attack to
fail.

**What now exists.** `packages/polygm_core/payments/` carries the two verifiers, written against the requirements
rather than from memory:

* **`verify_stripe`** hashes `"<timestamp>.<raw body>"` — the bytes that arrived, with no JSON parse anywhere on the
  verification path, because the tempting refactor (parse first so the handler can read `type`) is exactly what
  breaks the MAC for every honest request and invites "fixing" it by verifying a reconstruction of the request.
  It compares **every** `v1=` candidate with `hmac.compare_digest` and accumulates instead of returning early, so
  the response time says nothing about *which* signature matched during a secret rotation. The five-minute window
  is enforced in both directions, and a `str` body is refused outright rather than re-encoded and hoped over.
* **The replay ledger** is `db/migrations/0022_payment_events.sql`, keyed on the provider's own event id, claimed by
  **INSERT** rather than by a prior SELECT — a read-then-write lets two concurrent deliveries both see "not seen" and
  both fulfil. It is append-only, registered in the builder's `APPEND_ONLY` list, so a replayed event cannot be
  un-claimed by deleting its row; the P11 gate's c27 checks both halves.
* **`stars_payment`** reads `successful_payment` only from inside a message of a verified update, and refuses the
  shape somebody invents when they build a payment endpoint and want the same field to arrive as its own POST.
* **`fulfilment`** is the only function that can say yes, and it says yes by cross-checking the provider's amount
  against the record we already hold, returning the **user and plan from that record**. The event's payload is not
  an input to the decision.

**The probe result.** `P14 ATTACK SURFACE: PASS — 85 checks passed, 0 failed, 0 OPEN` (was 77/0/1), recorded in
`docs/verification/P14-attack-surface.{txt,json}`. The gate's D1 row is now **PASS — 85 passed, 0 failed, 0 open**.
Thirty-one unit cases live in `tests/test_payment_webhooks.py`, including two that read the implementation: that the
candidate loop does not return early, and that the raw bytes are hashed before anything parses them — because those
are the two places a later "simplification" would quietly remove the control.

**One honest note about the surface.** There is still no payment product: no route, no checkout, no Stars price. A
test asserts the *absence* of a provider route in `services/api/app.py`, so the day one is added it has to come
through this package. The gate is real; the thing it guards does not exist yet, and the record says both.

**Also this block.** `p11` 30/30 and `p08` 16/16 needed `.next` and `node_modules` reinstalled first — the workspace
reset strips both, and the resulting "server exited during boot" reads like a code regression until you check. P04's
secret scan caught my own test constant (`SECRET = "whsec_test_do_not_use"`) because `whsec_` is Stripe's real
endpoint-secret prefix and it looked like a key; the placeholder is now short and obviously not one, rather than an
allowlist entry — an exemption that exists because a test constant resembles a credential is how a scanner stops
being a scanner.

**Verified.** unittest **1574 OK**, pytest **1594 passed** (+31 each), lint-rules 192 files / 0 findings, P04 56/56,
P05 14/14, P11 30/30, `build-sqlite-migrations --check` clean, gate document regenerated and `--check` matching.

---

## The container half of P14, and the five defects it found · 2026-09-29

**Two OPENs had stood since D4 was written**, both for one reason: `docs/verification/P14-infra-verify.txt` said it
out loud — *"no docker here, so the Dockerfiles and compose are read as text; nobody has yet run `id` inside the
built image or tried to write to `/`"* — and D3 carried the same shape for image contents. A check that reports an
OPEN because of the machine it runs on is not a check; it is a note. This sandbox turned out to have passwordless
`sudo`, so the machine was no longer the excuse: docker and trivy were installed, the images were built, and the
stack was brought up for the first time in this repository's life.

**What running it found — none of which any test could see, because every test ran in the working tree:**

1. **The API image could not boot.** `services/api/Dockerfile` never copied `contracts/` or `config/`, and
   `app.py`'s import graph reads `contracts/startapp.json` (the Telegram channel) and `config/gtm.json` (the
   revenue schedule). `uvicorn` died with `FileNotFoundError` before binding a port. Every test passed, the whole
   time, because both files exist in the tree.
2. **`edoburu/pgbouncer:1.23.1` does not exist.** The publisher's tags carry a `v` and a patch suffix
   (`v1.23.1-p0`…`-p3`), so `docker compose up` failed at *pull* — before a single container started. The stack
   had never been started, so nothing had ever asked a registry for the image.
3. **The Postgres migrations had never been applied to Postgres.** Four SQL defects, each invisible to the sqlite
   twin and each fatal to the source of truth: `date_trunc('day', to_timestamp(...))` is not IMMUTABLE
   (`0002`), `unique (user_id, lower(name))` is not legal as a table constraint and `substr(trim(jsonb))` resolves
   to `btrim(jsonb)`, which does not exist (`0004`), and `INTEGER NOT NULL DEFAULT FALSE` mixes types twice over
   (`0009`). Also `(sent_ms - queued_ms DESC)` — an unparenthesised expression in an index, invalid in *both*
   engines, invisible because the transpiler had been dropping every partial index from the subset.
4. **The append-only trigger raised `TG_TABLENAME`**, a variable that does not exist, so the control P11's c27
   counts across 30 tables produced an error *about the error* on every attempt. The sqlite twin has its own
   triggers, so the pgSQL function had never executed anywhere.
5. **`btrim`/`FALSE` the other way round**: `audit_log`'s object CHECK was written in the *sqlite* spelling inside
   the Postgres file, because the transpiler could not translate the Postgres one. The generator now translates
   `jsonb_typeof(x) = 'object'` outward — Postgres in, sqlite out, the direction it exists to work in.

**The tools, so this is repeatable rather than a story.** `tools/p14-container-verify.py` builds both images, runs
the four probes the OPEN text named (`id`, a write outside the working directory, network clients, `getent passwd`)
plus a read-only-rootfs probe, boots the API image, then brings the compose stack up and proves the *database*
enforces append-only: a real `INSERT`, then an `UPDATE` and a `DELETE` refused by the trigger, then the REVOKE
half refused for the application role. `tools/p14-image-scan.py` builds and scans both images with trivy and
records every finding one of two ways — fixed, or recorded with a reason. Both write the house artifact shape
(`verdict`/`checks`/`open_conditions`), because a tool that invents its own shape is invisible to the gate: the
first container record rendered as MISSING while the run behind it had passed 18 checks.

**The results.** `P14 CONTAINER VERIFY — PASS, 18 passed / 0 failed / 0 OPEN`. `P14 IMAGE SCAN — PASS, 4 passed, 1
OPEN`: **88 CRITICAL/HIGH across 8 distinct CVEs in the distro layer of `python:3.12-slim`, none with a published
fix** — real, written down with its mitigations (non-root, no login shell, no network client, `cap_drop: [ALL]`,
`no-new-privileges`, read-only rootfs) and left as an owner decision between accepting a rebuild cadence and moving
to a distroless base, because that is not a decision a build agent should take silently. And `0005`'s refusal now
says what it means: `append-only table: audit_log is not updatable or deletable`.

**Verified this block.** pytest **1594 passed**; P04 **56/56**; P05 **14/14**; P07 **32/32**; P08 **16/16**;
**P11 30/30**; sqlite `--check` clean (with the twin now carrying the partial-unique indexes it had been silently
dropping — `telegram_outbox`'s dedupe guarantee among them); lint-rules clean; P14 gate document regenerated with
two new rows (D3 containers, D4 containers) and `--check` matching.

**One housekeeping note.** The workspace snapshot this block started from had `HEAD` at an ancestor of
`origin/main` — the `.git` directory was a stale copy while the remote was two commits ahead. The recovery was
`git fetch` + `git reset --mixed origin/main`: the working tree is preserved, the history moves forward, and the
only things left dirty are this block's own changes. A `--hard` there would have thrown away the work; the check
that makes it safe is `git merge-base --is-ancestor <local> <remote>`, which said "no divergence" first.

---

## A Postgres restore, drilled on Postgres · 2026-09-29

The fourth OPEN to close today. `P14-infra-verify`'s restore section had said the whole time that it knew the
difference — *"VACUUM INTO is a real logical backup… (For Postgres this is pg_dump/PITR; the drill shape is the
same, the tool differs)"* — and its verdict row carried the matching admission: *no restore has been performed
against the managed Postgres that production will use.* The tool that differs now exists and has run:
`tools/p14-postgres-restore.py` starts the compose Postgres, applies `db/migrations/` (21 files, for real), writes a
money-path fixture, takes a `pg_dump -Fc` logical backup, creates a database **that does not exist yet**, restores
into it, and then asks both databases the same eight questions — table count, balances, ledger sum, open orders,
markets, tokens, users, and the `watchlists_user_name_uq` index.

**`P14 POSTGRES RESTORE — PASS, 9 passed / 0 failed / 0 OPEN.`** 291 KB dump, 338 ms to take, 1.7 s to restore,
identical answers on both sides. Two of the nine checks are the ones worth keeping: **all 30 append-only triggers
survived the round trip**, and the restored database *still refuses* an UPDATE against `audit_log`. A restore that
silently loses a trigger is a restored box that will happily delete a ledger row, and nothing but a drilled restore
would have said so.

**The finding it did *not* fix, and said so instead.** The repo's own `db/seed.sql` cannot be applied to Postgres:
it emits `1` where the schema declares `neg_risk BOOLEAN`, and epoch-ms integers where three columns are
`TIMESTAMPTZ`. The generated SQL is shared between engines, so the seed — like the API — assumes the *sqlite*
representation of columns the Postgres schema declares differently. The drill writes its own fixture, records the
defect as a fact, and hands it to the Postgres-portability workstream, because the right fix is a decision about
which representation is canonical rather than a cast bolted onto a generator. This is now the third face of one
root: **the API is sqlite-only** (`services/api/app.py` imports `sqlite3`; there is no `asyncpg` in `services/` or
`packages/`), while compose, `docker-compose.prod.yml` and P15 all specify Postgres for preview, canary and prod.
That is launch blocker **F14**, and it is the largest thing left between this repository and a deploy.

**Where P14 stands after today.** Four OPENs closed with executed evidence rather than prose — the payment-webhook
control, image contents, image runtime hardening, and now a real Postgres restore. What remains open is what a
machine cannot measure: the break-glass provider rate limit (needs real Turnkey keys), egress from a deployed
subnet (needs a deployment), the managed instance's own PITR restore (needs the production account), MFA on the
three accounts (needs their owners), and F14 itself.


---

## 2026-09-29 — the web screen that was not there, and the grid defect it was hiding behind

**The ask was "start building the website".** The website exists (Next.js App Router, 14+ routes), so the work
started where the product actually was: one screen returning HTTP 500, and a verification pass that had never run
against a rebuilt server. Both of those turned out to be smaller than what was underneath them.

**`/terminal` was a 500, and the reason is a rule, not a typo.** The console showed a Trusted-Types
`TrustedScriptURL` error, which was a cascade: the server log named the real cause —
*`Attempted to call marketTitle() from the server but marketTitle is on the client`*. `TerminalScreen.tsx` is a
`"use client"` module, and `app/(app)/terminal/page.tsx` is a server component importing a plain function from it.
Anything exported from a client module becomes a **client reference** for a server importer: callable from the
browser, fatal at render on the server. The helper itself was pure — a title lookup over a market object — so it
moved to a plain module (`web/src/terminal/market-view.ts`) that both sides may import.

**A rule that one bug found is worth a test that finds the next one.** `web/src/client-boundary.test.ts` walks every
`"use client"` module under `src/` and fails if it exports anything callable that is not a component (`PascalCase`)
or a hook (`use*`). Written against the tree as it stood, it found **18 offenders in 10 files**, all of them latent
copies of the same bug — a server component that imported one of them would have 500'd exactly like `/terminal` did.
All 18 moved to plain modules beside their screens (`terminal-logic.ts`, `automation-logic.ts`, `portfolio-logic.ts`,
`palette-logic.ts`, `dossier-logic.ts`, `whales-logic.ts`, `layout-logic.ts`, `data-cache.ts`, `announce.ts`,
`toast-store.ts`), and every importer was repointed. The guard's own first version was wrong in a way worth
recording: it sliced the first 200 characters looking for the directive and therefore *skipped* six client modules
whose file headers are longer than that (`useLive`, `Number`, `Button`, `Dialog`, `Field`, `Toast`). It now skips
comments before looking.

**The real user-visible defect was one CSS track.** With `/terminal` finally returning 200, the screenshot showed a
34-pixel-wide screen and two enormous empty rails. The frame's tracks were
`var(--pgm-rail-left) minmax(0, 1fr) var(--pgm-rail-right)`, and `rails.ts` emitted `18fr` and `22fr` for the rails:
`fr` divides *free* space, so the centre got **one part in forty-one**. Every authenticated screen was like that —
portfolio, wallet, alerts, leaderboard — and it had survived the design review, the animation pass and the browser
pass, because each of those looked at the part of the page it was about. The fix is one scale for all three tracks:
the rails carry their fraction, the centre carries the complement, and the sum is asserted in `rails.test.ts`
(`18 + 60 + 22 = 100`), including the collapsed cases. Found by looking at a screenshot, fixed in arithmetic, and
pinned by a test that would have caught it.

**Verified after the fix.** `npx tsc --noEmit` clean; `vitest run` **632 passed / 71 files** (up from 629: the
boundary guard's 2 and the rails track tests' 3); `make web-build` passes with `sources-sha256: f5ea6524c7d2da1d`
and no route over budget; the browser suite is **18 passed / 8 skipped / 4 failed**, where the 4 failures are
exactly the two parked specs (`buy-flow`, `wallet-ceremony`) and are the next work item. Screenshots in
`/home/user/shots/` now show a real terminal — market titles, watchlist, trending, a trade ticket — instead of a
blank page, and `/portfolio` renders its honest refusal panel, which is the correct product behaviour when the
session cookie is synthetic.

---

## 2026-09-29 (second block) — the two parked specs, and the three defects that were holding them

The web's remaining work was two specs that had been skipped since P13: `buy-flow` (the ticket posts the route's
own shape; the ladder does not move when a price ticks) and `wallet-ceremony` (the withdrawal ladder cannot be
skipped). Both were skipped because the browser "could not run here"; when it could, both failed, and the reasons
turned out to be three real defects rather than three stale tests.

**Defect 1 — the order-book ladder never rendered on any market page.** `depth: 400` was passed as a *path*
parameter to a route whose path is `/v1/markets/{market_id}/book`, and `urlFor` throws on a param the path has no
`{token}` for. The throw was swallowed by the poll that called it, so the screen kept its initial `null` and drew
"no order book" — while the API was answering 200 to the same route with a 12-level ladder. The same mistake was
in three more places (history on the market page, the event page's book, the terminal's history read). Fixed by
sending query parameters as `query`, and pinned by `src/api/params.test.ts`, which walks the source for
`key: …, params: {…}` pairs and fails on any key that is not a `{token}` of that route — with a planted misuse in
the file so a regex that stopped matching fails there rather than passing everywhere.

**Defect 2 — every id this database has 404'd on the app's own detail route.** `/market/<segment>` dispatches to
two pages: the app's detail page for a `0x…` condition id, the public odds page for a slug. The predicate was
`^0x[0-9a-fA-F]+$`, and the seed's markets are `0xM1`…`0xM159` — `M` is not hexadecimal, so **all 159** took the
slug branch and 404'd. Every deep link the product makes (the dossier's positions table, the terminal, the
buy-flow spec) pointed at a 404. The predicate now lives in `src/public/market-dispatch.ts` with its own test:
the property that distinguishes the two pages is the `0x` prefix, not the hex-ness of what follows.

**Defect 3 — the withdrawal ceremony did not exist.** `Withdraw.tsx` was a disabled form with a note saying the
route was missing (launch item P08-L6), while the contract has described the whole route for four phases:
`POST /v1/wallet/withdraw` takes `{amountUsdc, addressId, typedAmount, typedAddress, password, code}` and answers
202 with a recorded request whose *signing* is the custody plane's job. The screen is now the ladder the contract
describes and the spec asserts: an amount bounded by the ledger's own available figure, a destination that must
come from the allowlist and must have finished its hold, the amount typed back, the address typed back
character-exact, then the two locks — password and authenticator — in order, with the primary control unreachable
until the step above it is satisfied. The arithmetic is in `src/screens/withdraw-logic.ts` (13 unit tests): micro
to cents, `12.5` and `12.50` as the same amount, `12.51` as a different one, a near-miss address as a different
destination, and the request body built from the integer the parser accepted rather than from the field's text.
The wallet page now puts deposit/withdraw/addresses/keys behind a tablist, with the balance card above it — which
is also what the spec's `getByRole("tab")` was asking for. And the success line says what is true: *recorded, not
sent — the signing belongs to the custody plane and is not live yet. No money has moved.*

**A fourth thing, found by the mobile project.** The phone layout has a fixed tab bar over the bottom edge, and a
control scrolled into view lands under it. `scroll-padding-block-end` on the document now reserves that strip for
every scroll, focus and anchor — the half that `padding-block-end` on the frame was not covering.

**Where the two specs stand.** `wallet-ceremony` passes in both projects. `buy-flow` passes in both projects on
two claims — the ticket refuses in words with **nothing on the wire** while the feed is down (the send control is
`aria-disabled` with the reason as its title), and a real ask tick moves the row's digits without moving the row's
box — and its third claim, the wire shape `{slug, side, amountUsdc}` to `/v1/orders/amount`, is **parked with its
blocker named**: the gate is opened by the live feed, the feed needs `NEXT_PUBLIC_WS_ORIGIN` and there is no
`/v1/live/tape` server, so no environment in this repository can reach a posting ticket. `test.fixme`, not a
deleted test: the moment a transport exists it is the assertion that proves the client half of the order path.
The spec's fixture is also now the payload's real shape — a truncated book is not a smaller book, it is a payload
the client refuses to parse, and the first version of this fix rendered *no ladder at all* because of it.

**Verified.** `npx tsc --noEmit` clean. `vitest run` **651 passed / 74 files** (from 629/71). `make web-build`
passes with `sources-sha256: d81f2c7813d55e33`, no route over budget. The browser suite is **24 passed /
10 skipped / 0 failed** — the four red tests that opened this block are gone, and the suite runs in 17 seconds
instead of 2.6 minutes now that nothing is retrying against a timeout.

---

## The block that makes a deployment worth looking at — signed-out reads, one site origin, and two crawl artefacts

Written for a deployment request ("Deploy it on Vercel, I want to see it carefully") that is still blocked on an
owner re-auth, this block is what a careful look would have found first. Three defects, all of them invisible to
every test in the repository, all of them visible to a stranger with no cookies — which is exactly the visitor a
deployment creates.

**1. The site's own market list failed for every stranger.** `/markets` and `/market/*` rendered an error surface
("Something failed", "we cannot load this page") for anybody without a `pgm_at` cookie, on both the local and the
production-API configuration. The cause was one word missing from thirteen rows of the route ledger: the contract
serves 23 operations with `x-auth: none`/`public`, the ledger had declared `anonymous: true` for six of them, and
the web's own session hop answers **401 `UNAUTHENTICATED` ("this device has no session to refresh")** for any read
it does not consider public. `pgm_at` was the workaround, never the fix. The thirteen are now flagged, derived
directly from the contract rather than from memory, and `src/api/public-reads.test.ts` cross-checks the ledger
against `contracts/openapi.yaml` **in both directions** so the next public route cannot be added without it.
`src/auth/anonymous-read.test.ts` — which used to pin a hand-written list of six, the artefact that let this
happen — now derives its expectation from the same reader (`src/api/contract-public.ts`).

**2. Every public URL named the API.** `page.url`, the OG url, the share link and the four URLs inside the JSON-LD
graph are built by the API from `PGM_PUBLIC_BASE`, which is unset on the deployment: so the canonical of
`/market/mayor-2027` was `https://polygm-api.vercel.app/market/mayor-2027`, a JSON endpoint. A crawler would have
been told the site's pages are duplicates of the API, and nothing would have errored. `src/public/site-origin.ts`
re-bases all of them onto `NEXT_PUBLIC_SITE_ORIGIN` (the same variable that feeds `metadataBase`, so the two
cannot drift), including a deep rebase for the JSON-LD graph that rewrites only URLs belonging to the payload's
own origin — a partner link is copied through untouched.

**3. The site published no sitemap and no robots.txt.** The API has served a 143-URL sitemap since P11 and nothing
read it; `/sitemap.xml` and `/robots.txt` both 404'd on the site. Both now exist, built by
`app/sitemap.xml/route.ts` and `app/robots.txt/route.ts` over the pure serialisers in `src/public/crawl.ts`. The
first version of the sitemap shipped `http://0.0.0.0:3200` as the origin of all 143 URLs — Next fills
`request.url` from the address the server bound — so `src/public/request-origin.ts` reads `x-forwarded-host` first
and refuses a wildcard bind, which is the one origin that is never a site.

**4. A defect found by reading the sitemap the site now serves: the category board's own canonical URL could not be
fetched.** `page_url` lower-cases a path segment and the site reads that segment back as `category=`, but
`rank_board` demanded the seeded spelling (`Politics`): `/leaderboard/category/c/politics` was published to
crawlers and answered **422** to whoever fetched it, so four sitemap URLs were dead and the category pages 404'd
for everybody. `rank.py` now matches a category case-insensitively and stores the vocabulary's own spelling —
which it must, because that spelling is what `_category_rows` matches rows on. Unknown categories still refuse.

**Verified.** `npx tsc --noEmit` clean. `vitest run` **682 passed / 78 files** (from 651/74: 31 new tests across
four new files). `make web-build` passes with `sources-sha256: 82d24f82af463af9`. Playwright **24 passed /
10 skipped / 0 failed**. The Python suite: **1575 tests, 0 failing** (run one process per file — the whole-suite
single process is OOM-killed at ~681 tests in this 1.9 GB sandbox, which is a limit of the sandbox and not of the
suite). Two suites that had appeared red were a **restored-sandbox** artifact, not a regression:
`argon2-cffi`/`cryptography` were missing, so every security-plane path answered the correct 503
`SECURITY_ENV_MISSING`; reinstalling them turned `test_security_plane.py` 59/59 and `test_wallet_api.py` 51/51
green. Live checks: on a build started with `PGM_API_ORIGIN=https://polygm-api.vercel.app`, all ten public
addresses (`/`, `/markets`, `/market/0xM1`, `/market/mayor-2027`, `/whales`, `/leaderboard/risk_adjusted`,
`/sign-in`, `/sitemap.xml`, `/robots.txt`) answer 200 and render for a cookie-less visitor, and the sitemap's 143
`<loc>`s name the site that served it.
