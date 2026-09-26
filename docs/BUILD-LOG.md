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

**`[UNVERIFIED]`, carried forward for the last time.** Everything in P15's list, plus the four things this phase
cannot run from a workspace: the channel has no members yet, no alert has reached a real phone, the launch posts
have not been published anywhere, and the five gates are dated in the future. P16 ships the plan and the machinery
that keeps it honest; the numbers in it are the owner's to earn.
