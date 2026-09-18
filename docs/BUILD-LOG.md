# BUILD-LOG — one entry per phase

Format per phase: **built / verified / `[UNVERIFIED]`**. Newest first.

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
